import XCTest
@testable import LifeRecorder

final class MeetingCaptureTests: XCTestCase {
    private let stamp = Date(timeIntervalSince1970: 1_700_000_000)
    private let device = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    func testCaptureSnapshotCannotAttachFutureObservationAndStaysFixedInJournal() throws {
        let snapshot = ChunkContextSnapshot()
        snapshot.update(id: device, capturedAt: stamp.addingTimeInterval(1))
        XCTAssertNil(snapshot.observationID(forClipStart: stamp, now: stamp.addingTimeInterval(2)))
        snapshot.update(id: device, capturedAt: stamp.addingTimeInterval(-1))
        let journal = RecordingJournal(id: UUID(), startedAt: stamp,
            locationObservationID: snapshot.observationID(forClipStart: stamp, now: stamp))
        snapshot.update(id: UUID().uuidString.lowercased(), capturedAt: stamp.addingTimeInterval(30))
        let restored = try JSONDecoder().decode(RecordingJournal.self, from: JSONEncoder().encode(journal))
        XCTAssertEqual(restored.locationObservationID, device)
        snapshot.update(id: nil, capturedAt: nil)
        XCTAssertNil(snapshot.observationID(forClipStart: stamp, now: stamp))
    }

    func testOlderJournalsAndChunksDecodeWithoutLocationAndHeaderContainsOnlySavedID() throws {
        let old = RecordingJournal(id: UUID(), startedAt: stamp)
        var object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(old)) as! [String: Any]
        object.removeValue(forKey: "locationObservationID")
        let decoded = try JSONDecoder().decode(RecordingJournal.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertNil(decoded.locationObservationID)
        let chunk = Chunk(id: old.id, startedAt: old.startedAt, duration: 60, sha256: "hash", locationObservationID: device)
        XCTAssertEqual(UploadManager.locationObservationHeader(for: chunk), device)
        let legacy = Chunk(id: old.id, startedAt: old.startedAt, duration: 60, sha256: "hash")
        let decodedLegacy = try JSONDecoder().decode(Chunk.self, from: JSONEncoder().encode(legacy))
        XCTAssertNil(UploadManager.locationObservationHeader(for: decodedLegacy))
    }

    func testStartEndSurviveRecreationAndEnqueueBeforeStateChange() throws {
        let suite = "MeetingCaptureTests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var events: [[String: Any]] = []
        let enqueue: (String, String, Data, Date?) throws -> Void = { path, id, data, expiry in
            XCTAssertEqual(path, ContextTransport.meetingPath)
            XCTAssertNil(expiry)
            let object = try JSONSerialization.jsonObject(with: data) as! [String: Any]
            XCTAssertEqual(object["event_id"] as? String, id)
            events.append(object)
        }
        let capture = MeetingCapture(defaults: defaults, deviceID: { self.device }, now: { self.stamp }, enqueue: enqueue)
        let first = try capture.start()
        let restored = MeetingCapture(defaults: defaults, deviceID: { self.device }, now: { self.stamp }, enqueue: enqueue)
        XCTAssertEqual(restored.currentMeetingID, first)
        try restored.end()
        try restored.end() // An ended replay does not manufacture another end.
        XCTAssertEqual(events.count, 2)
        XCTAssertEqual(events[0]["kind"] as? String, "start")
        XCTAssertEqual(events[1]["kind"] as? String, "end")
        XCTAssertEqual(events[1]["meeting_id"] as? String, first)
        XCTAssertNil(restored.currentMeetingID)
        XCTAssertNil(defaults.string(forKey: MeetingCapture.currentMeetingKey))
    }

    func testNewStartHasNewIdentityAndFailedEnqueuePreservesCurrentMeeting() throws {
        let suite = "MeetingCaptureTests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var shouldFail = false
        let capture = MeetingCapture(defaults: defaults, deviceID: { self.device }, now: { self.stamp }, enqueue: { _, _, _, _ in
            if shouldFail { throw ContextTransport.OutboxError.invalidMarker }
        })
        let first = try capture.start()
        let second = try capture.start()
        XCTAssertNotEqual(first, second)
        shouldFail = true
        XCTAssertThrowsError(try capture.end())
        XCTAssertEqual(capture.currentMeetingID, second)
        XCTAssertThrowsError(try capture.start())
        XCTAssertEqual(defaults.string(forKey: MeetingCapture.currentMeetingKey), second)
    }

    func testOutboxIsDurableIdempotentAndRejectsConflict() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let transport = ContextTransport(directory: directory, now: { self.stamp })
        let id = UUID().uuidString.lowercased()
        let data = try JSONSerialization.data(withJSONObject: ["event_id": id])
        try transport.enqueue(path: ContextTransport.meetingPath, id: id, payload: data)
        try transport.enqueue(path: ContextTransport.meetingPath, id: id, payload: data)
        XCTAssertEqual(transport.pendingCount, 1)
        XCTAssertEqual(ContextTransport(directory: directory).pendingCount, 1)
        let changed = try JSONSerialization.data(withJSONObject: ["event_id": id, "kind": "end"])
        XCTAssertThrowsError(try transport.enqueue(path: ContextTransport.meetingPath, id: id, payload: changed))
        XCTAssertTrue((try directory.resourceValues(forKeys: [.isExcludedFromBackupKey])).isExcludedFromBackup == true)
    }

    func testReceiptMustMatchMarkerAndBeLiteralDurableBoolean() {
        XCTAssertTrue(ContextTransport.verifiedReceipt(Data("{\"event_id\":\"a\",\"durable\":true}".utf8), id: "a"))
        for raw in ["{\"event_id\":\"b\",\"durable\":true}", "{\"event_id\":\"a\",\"durable\":false}",
                    "{\"event_id\":\"a\",\"durable\":1}", "{\"durable\":true}"] {
            XCTAssertFalse(ContextTransport.verifiedReceipt(Data(raw.utf8), id: "a"))
        }
    }

    func testLocationExpiryIsCappedAtTwentyFourHours() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let transport = ContextTransport(directory: directory, now: { self.stamp })
        let id = UUID().uuidString.lowercased()
        let payload = try JSONSerialization.data(withJSONObject: ["observation": ["id": id]])
        try transport.enqueue(path: ContextTransport.locationPath, id: id, payload: payload, expiresAt: stamp.addingTimeInterval(100_000))
        let raw = try Data(contentsOf: directory.appendingPathComponent(id + ".json"))
        let stored = try JSONDecoder().decode(ContextTransport.Record.self, from: raw)
        XCTAssertEqual(stored.expiresAt, stamp.addingTimeInterval(86_400))
    }
}
