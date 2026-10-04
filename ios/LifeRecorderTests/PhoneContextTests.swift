import XCTest
@testable import LifeRecorder

@MainActor
final class PhoneContextTests: XCTestCase {
    private let device = UUID(uuidString: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")!
    private let stamp = Date(timeIntervalSince1970: 1_700_000_000)

    private func reducer(prior: DurableCaptureGap = .none, ids: [UUID] = []) -> PhoneContextReducer {
        var queue = ids
        var reducer = PhoneContextReducer(deviceID: device, sessionID: UUID(uuidString: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")!,
                                          newID: { queue.isEmpty ? UUID() : queue.removeFirst() })
        _ = reducer.reduce(.relaunch(at: stamp, prior: prior))
        return reducer
    }

    private func capture(_ observations: [PhoneContextObservation], signal: PhoneContextSignal) -> [PhoneContextObservation] {
        observations.filter { $0.signal == signal }
    }

    func testDuplicateAndNestedCallbacksDoNotDoubleOpenOrInventCallIDs() {
        var reducer = reducer()
        let first = reducer.reduce(.captureStarted(at: stamp))
        let duplicateStart = reducer.reduce(.captureStarted(at: stamp.addingTimeInterval(1)))
        let stop = reducer.reduce(.captureStopped(at: stamp.addingTimeInterval(2), reason: PhoneContextReason.interruption))
        let nested = reducer.reduce(.captureStopped(at: stamp.addingTimeInterval(3), reason: PhoneContextReason.interruption))
        XCTAssertEqual(first.map(\.state), [.active])
        XCTAssertTrue(duplicateStart.isEmpty)
        XCTAssertEqual(stop.map(\.state), [.inactive])
        XCTAssertEqual(stop.first?.metadata["reason"], PhoneContextReason.interruption)
        XCTAssertTrue(nested.isEmpty)
        XCTAssertTrue(reducer.gap.open)
        XCTAssertTrue(reducer.gap.stopBoundaryKnown)
        XCTAssertFalse(reducer.captureRunning)

        let calls = [
            PhoneCallCategory(phase: .incoming),
            PhoneCallCategory(phase: .connected),
            PhoneCallCategory.category(hasEnded: false, isOnHold: true, hasConnected: true, isOutgoing: false)
        ]
        let sample = PhoneContextSample(observedAt: stamp, executionAvailable: true, otherAudioPlaying: true,
                                        inputRoute: "MicrophoneBuiltIn", outputRoute: "Speaker", calls: calls,
                                        captureRunning: false, provenance: "ios-phone-lifecycle")
        let once = reducer.reduce(.sample(sample))
        let again = reducer.reduce(.sample(sample))
        let call = capture(once, signal: .phoneCall)
        XCTAssertEqual(call.map(\.state), [.active])
        XCTAssertEqual(call.first?.metadata["call_state"], "connected")
        XCTAssertNil(call.first?.metadata["app"])
        XCTAssertFalse(call.first?.metadata.values.contains(where: { UUID(uuidString: $0) != nil }) ?? false)
        XCTAssertTrue(again.filter { $0.signal == .phoneCall }.isEmpty)
        XCTAssertEqual(capture(once, signal: .phoneAudio).first?.state, .active)
        XCTAssertNil(capture(once, signal: .phoneAudio).first?.metadata["app"])
    }

    func testFailedResumeKeepsGapOpenAndDoesNotEmitActive() {
        var reducer = reducer()
        _ = reducer.reduce(.captureStarted(at: stamp))
        _ = reducer.reduce(.captureStopped(at: stamp.addingTimeInterval(1), reason: PhoneContextReason.interruption))
        let failed = reducer.reduce(.captureStartFailed(at: stamp.addingTimeInterval(2)))
        let nestedFail = reducer.reduce(.captureStartFailed(at: stamp.addingTimeInterval(3)))
        XCTAssertEqual(failed.map(\.state), [.inactive])
        XCTAssertEqual(failed.first?.metadata["reason"], PhoneContextReason.startupFailure)
        XCTAssertTrue(nestedFail.isEmpty)
        XCTAssertTrue(reducer.gap.open)
        XCTAssertFalse(reducer.captureRunning)
        XCTAssertFalse(failed.contains { $0.state == .active })
        let lyingSample = PhoneContextSample(observedAt: stamp.addingTimeInterval(4), executionAvailable: true,
                                             otherAudioPlaying: false, inputRoute: nil, outputRoute: nil, calls: [],
                                             captureRunning: true, provenance: "ios-phone-poll")
        XCTAssertTrue(reducer.reduce(.sample(lyingSample)).filter { $0.signal == .capture }.isEmpty)
        XCTAssertTrue(reducer.gap.open)
    }

    func testQuietCrossingRelaunchPreservesOpenGapUntilSuccessfulRestart() {
        var running = reducer()
        _ = running.reduce(.captureStarted(at: stamp))
        let stopped = running.reduce(.captureStopped(at: stamp.addingTimeInterval(60), reason: PhoneContextReason.quietHours))
        XCTAssertEqual(stopped.first?.metadata["reason"], PhoneContextReason.quietHours)
        XCTAssertTrue(running.gap.open)
        XCTAssertTrue(running.gap.stopBoundaryKnown)

        var restored = PhoneContextReducer(deviceID: device, sessionID: UUID())
        let relaunch = restored.reduce(.relaunch(at: stamp.addingTimeInterval(3600), prior: running.gap))
        XCTAssertNotEqual(restored.sessionID, running.sessionID)
        XCTAssertEqual(relaunch.first?.id, stopped.first?.id)
        XCTAssertEqual(relaunch.first?.sequence, stopped.first?.sequence)
        XCTAssertEqual(relaunch.first?.observedAt, stopped.first?.observedAt)
        XCTAssertEqual(relaunch.first?.state, .inactive)
        XCTAssertEqual(relaunch.first?.metadata["reason"], PhoneContextReason.quietHours)
        XCTAssertTrue(restored.gap.open)
        XCTAssertNotEqual(relaunch.first?.state, .active)
        let resumed = restored.reduce(.captureStarted(at: stamp.addingTimeInterval(3601)))
        XCTAssertEqual(resumed.map(\.state), [.active])
        XCTAssertFalse(restored.gap.open)
        XCTAssertTrue(restored.gap.captureWasActive)
    }

    func testRelaunchAfterActiveCaptureDoesNotClaimExactStop() {
        let prior = DurableCaptureGap(open: false, reason: nil, stopBoundaryKnown: true, captureWasActive: true)
        var restored = PhoneContextReducer(deviceID: device)
        let relaunch = restored.reduce(.relaunch(at: stamp, prior: prior))
        XCTAssertEqual(relaunch.first?.state, .unknown)
        XCTAssertEqual(relaunch.first?.metadata["reason"], PhoneContextReason.unknownTermination)
        XCTAssertTrue(restored.gap.open)
        XCTAssertFalse(restored.gap.stopBoundaryKnown)
        var stillOpen = PhoneContextReducer(deviceID: device)
        let second = stillOpen.reduce(.relaunch(at: stamp.addingTimeInterval(30), prior: restored.gap))
        XCTAssertEqual(second.first?.state, .unknown)
        XCTAssertTrue(stillOpen.gap.open)
        XCTAssertFalse(stillOpen.gap.stopBoundaryKnown)
        XCTAssertNotEqual(second.first?.state, .inactive)
    }

    func testLegacyGapAndFailedStartupNeverInventAnExactStopBoundary() {
        let legacy = DurableCaptureGap(open: true, reason: PhoneContextReason.quietHours,
                                       stopBoundaryKnown: true, captureWasActive: false)
        var restored = PhoneContextReducer(deviceID: device)
        let observations = restored.reduce(.relaunch(at: stamp, prior: legacy))
        XCTAssertEqual(observations.first?.state, .unknown)
        XCTAssertFalse(restored.gap.stopBoundaryKnown)
        let failed = restored.reduce(.captureStartFailed(at: stamp.addingTimeInterval(1)))
        XCTAssertEqual(failed.first?.state, .unknown)
        XCTAssertFalse(restored.gap.stopBoundaryKnown)
        var initial = PhoneContextReducer(deviceID: device)
        XCTAssertEqual(initial.reduce(.captureStartFailed(at: stamp)).first?.state, .unknown)
        XCTAssertFalse(initial.gap.stopBoundaryKnown)
    }

    func testFallbackPreservesMoreThan64TransitionsThroughRelaunchAndRecovery() throws {
        let suite = "PhoneContextTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        var fail = true
        var delivered: [String] = []
        var clock = stamp
        let enqueue: (String, String, Data, Date?) throws -> Void = { _, id, _, _ in
            if fail { throw ContextTransport.OutboxError.conflictingMarker }
            delivered.append(id)
        }
        let first = PhoneContextObserver(defaults: defaults, deviceID: { self.device.uuidString }, now: { clock },
            enqueue: enqueue, audio: FakeAudio(), calls: FakeCalls())
        for _ in 0..<70 {
            first.captureDidStart()
            clock.addTimeInterval(1)
            first.captureDidStop(reason: PhoneContextReason.interruption)
            clock.addTimeInterval(1)
        }
        XCTAssertEqual(first.pendingCount, 140)
        let held = Set((defaults.stringArray(forKey: PhoneContextObserver.pendingKey) ?? []).compactMap {
            Data(base64Encoded: $0).flatMap { PhoneContextCodec.observationID($0) }
        })
        let restored = PhoneContextObserver(defaults: defaults, deviceID: { self.device.uuidString }, now: { clock },
            enqueue: enqueue, audio: FakeAudio(), calls: FakeCalls())
        XCTAssertEqual(restored.pendingCount, 140) // Replayed boundary deduplicates its original UUID.
        fail = false
        restored.flushNow()
        XCTAssertEqual(restored.pendingCount, 0)
        XCTAssertEqual(Set(delivered), held)
    }

    func testWireNumbersDoNotRejectZeroAndOneOrAcceptBooleans() throws {
        let object = try JSONSerialization.jsonObject(with: Data("{\"zero\":0,\"one\":1,\"bool\":true}".utf8)) as! [String: Any]
        XCTAssertEqual(PhoneContextValidation.jsonInt(object["zero"]), 0)
        XCTAssertEqual(PhoneContextValidation.jsonInt(object["one"]), 1)
        XCTAssertNil(PhoneContextValidation.jsonInt(object["bool"]))
    }

    func testStopReasonsStayDistinctAndHandleIsNotSent() {
        for reason in [PhoneContextReason.manual, PhoneContextReason.route, PhoneContextReason.quietHours,
                       PhoneContextReason.interruption, PhoneContextReason.captureError] {
            var reducer = reducer()
            _ = reducer.reduce(.captureStarted(at: stamp))
            let stopped = reducer.reduce(.captureStopped(at: stamp.addingTimeInterval(1), reason: reason))
            XCTAssertEqual(stopped.first?.metadata["reason"], reason)
            XCTAssertTrue(reducer.gap.open)
        }
        var reducer = reducer()
        _ = reducer.reduce(.captureStarted(at: stamp))
        let leaked = reducer.reduce(.captureStopped(at: stamp.addingTimeInterval(1), reason: "+1 919-555-0100"))
        XCTAssertNotEqual(leaked.first?.metadata["reason"], "+1 919-555-0100")
        XCTAssertFalse(leaked.first?.metadata.values.contains("+1 919-555-0100") ?? true)
    }

    func testExecutionLossStopsAudioAndCallClaimsWithoutClosingCapture() {
        var reducer = reducer()
        _ = reducer.reduce(.captureStarted(at: stamp))
        let live = PhoneContextSample(observedAt: stamp, executionAvailable: true, otherAudioPlaying: false,
                                      inputRoute: "MicrophoneBuiltIn", outputRoute: "Speaker",
                                      calls: [], captureRunning: true, provenance: "ios-phone-poll")
        _ = reducer.reduce(.sample(live))
        let background = PhoneContextSample(observedAt: stamp.addingTimeInterval(1), executionAvailable: false,
                                            otherAudioPlaying: nil, inputRoute: nil, outputRoute: nil,
                                            calls: nil, captureRunning: true, provenance: "ios-phone-lifecycle")
        let lost = reducer.reduce(.sample(background))
        XCTAssertEqual(capture(lost, signal: .phoneAudio).map(\.state), [.unknown])
        XCTAssertEqual(capture(lost, signal: .phoneCall).map(\.state), [.unknown])
        XCTAssertTrue(capture(lost, signal: .capture).isEmpty)
        XCTAssertFalse(reducer.gap.open)
        XCTAssertTrue(reducer.captureRunning)
        let again = reducer.reduce(.sample(background))
        XCTAssertTrue(again.isEmpty)
    }

    func testActiveRefreshIsBoundedAndDoesNotRunWithoutExecution() {
        var reducer = reducer()
        let first = PhoneContextSample(observedAt: stamp, executionAvailable: true, otherAudioPlaying: true,
                                       inputRoute: nil, outputRoute: nil, calls: [PhoneCallCategory(phase: .outgoing)],
                                       captureRunning: false, provenance: "ios-phone-poll")
        XCTAssertEqual(reducer.reduce(.sample(first)).filter { $0.signal == .phoneAudio }.count, 1)
        let soon = PhoneContextSample(observedAt: stamp.addingTimeInterval(14), executionAvailable: true,
                                      otherAudioPlaying: true, inputRoute: nil, outputRoute: nil,
                                      calls: [PhoneCallCategory(phase: .outgoing)], captureRunning: false,
                                      provenance: "ios-phone-poll")
        XCTAssertTrue(reducer.reduce(.sample(soon)).isEmpty)
        let due = PhoneContextSample(observedAt: stamp.addingTimeInterval(15), executionAvailable: true,
                                     otherAudioPlaying: true, inputRoute: nil, outputRoute: nil,
                                     calls: [PhoneCallCategory(phase: .outgoing)], captureRunning: false,
                                     provenance: "ios-phone-poll")
        let refresh = reducer.reduce(.sample(due))
        XCTAssertEqual(refresh.filter { $0.signal == .phoneAudio }.map(\.state), [.active])
        XCTAssertEqual(refresh.filter { $0.signal == .phoneCall }.first?.metadata["call_state"], "outgoing")
        XCTAssertEqual(PhoneContextReducer.pollInterval, 15)
    }

    func testObserverOutboxFailureDoesNotBlockCaptureOrLoseTheGap() throws {
        let suite = "PhoneContextTests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var clock = stamp
        var fail = true
        var sent: [Data] = []
        let observer = PhoneContextObserver(defaults: defaults, deviceID: { self.device.uuidString }, now: { clock },
                                            enqueue: { path, id, data, expiry in
                                                XCTAssertEqual(path, ContextTransport.contextPath)
                                                XCTAssertNil(expiry)
                                                XCTAssertEqual(PhoneContextCodec.observationID(data), id)
                                                if fail { throw NSError(domain: "outbox", code: 1) }
                                                sent.append(data)
                                            }, audio: FakeAudio(), calls: FakeCalls())
        observer.captureDidStart()
        XCTAssertFalse(observer.durableGap.open)
        XCTAssertGreaterThan(observer.pendingCount, 0)
        clock = stamp.addingTimeInterval(2)
        observer.captureDidStop(reason: PhoneContextReason.manual)
        observer.captureDidStop(reason: PhoneContextReason.manual)
        XCTAssertTrue(observer.durableGap.open)
        XCTAssertEqual(observer.durableGap.reason, PhoneContextReason.manual)
        XCTAssertTrue(observer.durableGap.stopBoundaryKnown)
        fail = false
        observer.flushNow()
        XCTAssertEqual(observer.pendingCount, 0)
        XCTAssertEqual(sent.count, 2)
        let objects = try sent.map { try JSONSerialization.jsonObject(with: $0) as! [String: Any] }
        let states = objects.compactMap { ($0["observation"] as? [String: Any])?["state"] as? String }
        XCTAssertEqual(states, ["active", "inactive"])
        clock = stamp.addingTimeInterval(3)
        observer.captureDidFailToStart()
        XCTAssertTrue(observer.durableGap.open)
        XCTAssertEqual(sent.last.flatMap { PhoneContextCodec.observationID($0) } != nil, true)
        let last = try JSONSerialization.jsonObject(with: sent.last!) as! [String: Any]
        let metadata = (last["observation"] as? [String: Any])?["metadata"] as? [String: String]
        XCTAssertEqual(metadata?["reason"], PhoneContextReason.startupFailure)
        XCTAssertFalse(states.contains("paused"))
    }

    func testObserverRelaunchPreservesQuietGapAndRetriesFailedOutbox() throws {
        let suite = "PhoneContextTests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var fail = false
        var sent: [[String: Any]] = []
        let first = PhoneContextObserver(defaults: defaults, deviceID: { self.device.uuidString }, now: { self.stamp },
                                         enqueue: { _, _, data, _ in
                                             if fail { throw NSError(domain: "outbox", code: 2) }
                                             sent.append(try JSONSerialization.jsonObject(with: data) as! [String: Any])
                                         }, audio: FakeAudio(), calls: FakeCalls())
        first.captureDidStart()
        first.captureDidStop(reason: PhoneContextReason.quietHours)
        fail = true
        let second = PhoneContextObserver(defaults: defaults, deviceID: { self.device.uuidString },
                                          now: { self.stamp.addingTimeInterval(100) },
                                          enqueue: { _, _, data, _ in
                                              if fail { throw NSError(domain: "outbox", code: 2) }
                                              sent.append(try JSONSerialization.jsonObject(with: data) as! [String: Any])
                                          }, audio: FakeAudio(), calls: FakeCalls())
        XCTAssertTrue(second.durableGap.open)
        XCTAssertEqual(second.durableGap.reason, PhoneContextReason.quietHours)
        XCTAssertGreaterThan(second.pendingCount, 0)
        fail = false
        second.flushNow()
        second.captureDidStart()
        XCTAssertFalse(second.durableGap.open)
        let observations = sent.compactMap { $0["observation"] as? [String: Any] }
        let sessions = Set(observations.compactMap { $0["session_id"] as? String })
        XCTAssertGreaterThanOrEqual(sessions.count, 2)
        XCTAssertTrue(observations.contains { ($0["metadata"] as? [String: String])?["reason"] == PhoneContextReason.quietHours
            && $0["state"] as? String == "inactive" })
        XCTAssertTrue(observations.contains { $0["state"] as? String == "active" && $0["signal"] as? String == "capture" })
        for observation in observations {
            XCTAssertEqual(observation["producer_id"] as? String, device.uuidString.lowercased())
            XCTAssertEqual(observation["version"] as? Int, 1)
            XCTAssertGreaterThanOrEqual(observation["sequence"] as? Int ?? -1, 0)
        }
    }

    func testContextOutboxRejectsInvalidPayloadsAndRequiresObservationReceipt() throws {
        let old = UserDefaults.standard.string(forKey: "deviceID")
        defer {
            if let old { UserDefaults.standard.set(old, forKey: "deviceID") }
            else { UserDefaults.standard.removeObject(forKey: "deviceID") }
        }
        UserDefaults.standard.set(device.uuidString.lowercased(), forKey: "deviceID")
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let transport = ContextTransport(directory: directory, now: { self.stamp })
        var reducer = PhoneContextReducer(deviceID: device, newID: { UUID(uuidString: "cccccccc-cccc-4ccc-8ccc-cccccccccccc")! })
        let observation = reducer.reduce(.captureStartFailed(at: stamp))[0]
        let payload = try XCTUnwrap(PhoneContextCodec.envelope(deviceID: device, observation: observation))
        let id = observation.id.uuidString.lowercased()
        try transport.enqueue(path: ContextTransport.contextPath, id: id, payload: payload)
        try transport.enqueue(path: ContextTransport.contextPath, id: id, payload: payload)
        XCTAssertEqual(transport.pendingCount, 1)
        var changed = try JSONSerialization.jsonObject(with: payload) as! [String: Any]
        var observationObject = changed["observation"] as! [String: Any]
        observationObject["state"] = "inactive"
        changed["observation"] = observationObject
        let conflict = try JSONSerialization.data(withJSONObject: changed, options: [.sortedKeys])
        XCTAssertThrowsError(try transport.enqueue(path: ContextTransport.contextPath, id: id, payload: conflict))

        for bad in invalidPayloads(valid: changed, id: id) {
            let data = try JSONSerialization.data(withJSONObject: bad)
            XCTAssertThrowsError(try transport.enqueue(path: ContextTransport.contextPath, id: id, payload: data))
        }
        XCTAssertTrue(ContextTransport.verifiedReceipt(
            Data("{\"durable\":true,\"observation_id\":\"\(id)\"}".utf8), id: id, path: ContextTransport.contextPath))
        XCTAssertFalse(ContextTransport.verifiedReceipt(
            Data("{\"durable\":true,\"event_id\":\"\(id)\"}".utf8), id: id, path: ContextTransport.contextPath))
        XCTAssertFalse(ContextTransport.verifiedReceipt(
            Data("{\"durable\":false,\"observation_id\":\"\(id)\"}".utf8), id: id, path: ContextTransport.contextPath))
        XCTAssertTrue(ContextTransport.verifiedReceipt(Data("{\"event_id\":\"a\",\"durable\":true}".utf8), id: "a"))
    }

    private func invalidPayloads(valid: [String: Any], id: String) -> [[String: Any]] {
        var url = valid
        var observation = url["observation"] as! [String: Any]
        observation["metadata"] = ["reason": "https://example.test/secret"]
        url["observation"] = observation
        var handle = valid
        observation["metadata"] = ["call_state": "+15551212"]
        handle["observation"] = observation
        var extra = valid
        observation = extra["observation"] as! [String: Any]
        observation["call_id"] = id
        extra["observation"] = observation
        var mismatch = valid
        mismatch["device_id"] = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        var naive = valid
        observation = naive["observation"] as! [String: Any]
        observation["observed_at"] = "2024-01-01T00:00:00"
        naive["observation"] = observation
        var negative = valid
        observation = negative["observation"] as! [String: Any]
        observation["sequence"] = -1
        negative["observation"] = observation
        return [url, handle, extra, mismatch, naive, negative]
    }
}

private struct FakeAudio: PhoneAudioSampling {
    func sample() -> (otherAudioPlaying: Bool?, inputRoute: String?, outputRoute: String?) {
        (false, "MicrophoneBuiltIn", "Speaker")
    }
}

private final class FakeCalls: PhoneCallCategorizing {
    func start(_ onChange: @escaping ([PhoneCallCategory]) -> Void) {}
    func current() -> [PhoneCallCategory]? { [] }
}
