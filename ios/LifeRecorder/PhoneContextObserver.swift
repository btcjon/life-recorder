import AVFoundation
import CallKit
import Foundation

protocol PhoneAudioSampling {
    func sample() -> (otherAudioPlaying: Bool?, inputRoute: String?, outputRoute: String?)
}

protocol PhoneCallCategorizing: AnyObject {
    func start(_ onChange: @escaping ([PhoneCallCategory]) -> Void)
    func current() -> [PhoneCallCategory]?
}

struct SystemPhoneAudioSampler: PhoneAudioSampling {
    func sample() -> (otherAudioPlaying: Bool?, inputRoute: String?, outputRoute: String?) {
        let session = AVAudioSession.sharedInstance()
        // Port type only. Port names can identify a personal device and are not sent.
        return (session.isOtherAudioPlaying,
                session.currentRoute.inputs.first?.portType.rawValue,
                session.currentRoute.outputs.first?.portType.rawValue)
    }
}

/// Categorical call adapter. The CXCall UUID is never copied into an observation.
final class SystemCallCategorizer: NSObject, CXCallObserverDelegate, PhoneCallCategorizing {
    private let observer = CXCallObserver()
    private var onChange: (([PhoneCallCategory]) -> Void)?
    private var latest: [PhoneCallCategory]?

    func start(_ onChange: @escaping ([PhoneCallCategory]) -> Void) {
        self.onChange = onChange
        observer.setDelegate(self, queue: .main)
        publish(observer.calls)
    }

    func current() -> [PhoneCallCategory]? { latest }

    func callObserver(_ observer: CXCallObserver, callChanged call: CXCall) {
        publish(observer.calls)
    }

    private func publish(_ calls: [CXCall]) {
        let categories = calls.map {
            PhoneCallCategory.category(hasEnded: $0.hasEnded, isOnHold: $0.isOnHold,
                                       hasConnected: $0.hasConnected, isOutgoing: $0.isOutgoing)
        }
        latest = categories
        onChange?(categories)
    }
}

/// Samples phone audio and calls only while this process is already executing.
/// Polling is not a background assertion and does not claim locked-screen continuity.
@MainActor
final class PhoneContextObserver: CaptureContextReporting {
    static let shared = PhoneContextObserver()
    static let gapKey = "lifeRecorder.phoneContext.gap"
    static let pendingKey = "lifeRecorder.phoneContext.pending"
    static let pendingLimit = 64

    var isCapturing: () -> Bool = { false }
    private let defaults: UserDefaults
    private let deviceID: () -> String
    private let now: () -> Date
    private let enqueue: (String, String, Data, Date?) throws -> Void
    private let audio: PhoneAudioSampling
    private let calls: PhoneCallCategorizing
    private var reducer: PhoneContextReducer?
    private var gap: DurableCaptureGap
    private var pending: [Data]
    private var executionAvailable = false
    private var samplingEnabled = false
    private var pollTimer: Timer?

    init(defaults: UserDefaults = .standard, deviceID: @escaping () -> String = { QueueStore.deviceID },
         now: @escaping () -> Date = Date.init,
         enqueue: @escaping (String, String, Data, Date?) throws -> Void = {
             try ContextTransport.shared.enqueue(path: $0, id: $1, payload: $2, expiresAt: $3)
         },
         audio: PhoneAudioSampling = SystemPhoneAudioSampler(),
         calls: PhoneCallCategorizing = SystemCallCategorizer(),
         prior: DurableCaptureGap? = nil) {
        self.defaults = defaults
        self.deviceID = deviceID
        self.now = now
        self.enqueue = enqueue
        self.audio = audio
        self.calls = calls
        gap = prior ?? Self.loadGap(defaults)
        pending = Self.loadPending(defaults)
        guard let id = UUID(uuidString: deviceID()) else { return }
        var reducer = PhoneContextReducer(deviceID: id)
        let produced = reducer.reduce(.relaunch(at: now(), prior: gap))
        self.reducer = reducer
        self.gap = reducer.gap
        persistGap()
        queue(produced)
        flush()
    }

    var durableGap: DurableCaptureGap { gap }
    var pendingCount: Int { pending.count }

    func activate() {
        flush()
        guard !samplingEnabled else {
            if executionAvailable { sample(provenance: "ios-phone-lifecycle") }
            return
        }
        samplingEnabled = true
        calls.start { [weak self] categories in
            Task { @MainActor in self?.ingestCalls(categories) }
        }
        if executionAvailable {
            armTimer()
            sample(provenance: "ios-phone-lifecycle")
        }
    }

    func setExecutionAvailable(_ available: Bool) {
        let changed = executionAvailable != available
        executionAvailable = available
        if available {
            if samplingEnabled { armTimer() }
        } else {
            pollTimer?.invalidate()
            pollTimer = nil
        }
        if changed { sample(provenance: "ios-phone-lifecycle") }
    }

    func captureDidStart() { apply(.captureStarted(at: now())) }
    func captureDidStop(reason: String) { apply(.captureStopped(at: now(), reason: reason)) }
    func captureDidFailToStart() { apply(.captureStartFailed(at: now())) }

    func ingestCalls(_ categories: [PhoneCallCategory]) {
        var sample = makeSample(provenance: "ios-phone-lifecycle")
        sample.calls = categories
        if !executionAvailable {
            sample.otherAudioPlaying = nil
            sample.inputRoute = nil
            sample.outputRoute = nil
            sample.captureRunning = nil
        }
        apply(.sample(sample))
    }

    func sampleNow(provenance: String = "ios-phone-poll") { sample(provenance: provenance) }
    func flushNow() { flush() }

    private func sample(provenance: String) {
        apply(.sample(makeSample(provenance: provenance)))
    }

    private func makeSample(provenance: String) -> PhoneContextSample {
        let reading = executionAvailable ? audio.sample() : (otherAudioPlaying: nil as Bool?, inputRoute: nil as String?, outputRoute: nil as String?)
        return PhoneContextSample(observedAt: now(), executionAvailable: executionAvailable,
                                  otherAudioPlaying: reading.otherAudioPlaying, inputRoute: reading.inputRoute,
                                  outputRoute: reading.outputRoute,
                                  calls: executionAvailable ? calls.current() : nil,
                                  captureRunning: isCapturing(), provenance: provenance)
    }

    private func apply(_ event: PhoneContextEvent) {
        guard var reducer else { return }
        let produced = reducer.reduce(event)
        self.reducer = reducer
        gap = reducer.gap
        persistGap()
        queue(produced)
        flush()
    }

    private func queue(_ observations: [PhoneContextObservation]) {
        guard let device = UUID(uuidString: deviceID()) else { return }
        for observation in observations {
            guard let data = PhoneContextCodec.envelope(deviceID: device, observation: observation) else { continue }
            let id = observation.id.uuidString.lowercased()
            if pending.contains(where: { PhoneContextCodec.observationID($0) == id }) { continue }
            // Only coalesce not-yet-enqueued periodic refreshes. Every transition stays durable.
            if pending.count >= Self.pendingLimit, observation.provenance == "ios-phone-poll", observation.state == .active,
               let previous = pending.reversed().compactMap({ Self.decodeObservation($0) }).first(where: {
                   $0["signal"] as? String == observation.signal.rawValue
               }), previous["state"] as? String == "active",
               previous["session_id"] as? String == observation.sessionID.uuidString.lowercased(),
               previous["metadata"] as? [String: String] == observation.metadata { continue }
            pending.append(data)
        }
        persistPending()
    }

    private func flush() {
        var rest: [Data] = []
        var failed = false
        for payload in pending {
            if failed { rest.append(payload); continue }
            do {
                guard let id = PhoneContextCodec.observationID(payload) else { continue }
                try enqueue(ContextTransport.contextPath, id, payload, nil)
            } catch {
                failed = true
                rest.append(payload)
            }
        }
        pending = rest
        persistPending()
    }

    private func armTimer() {
        guard pollTimer == nil, executionAvailable else { return }
        let timer = Timer(timeInterval: PhoneContextReducer.pollInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.executionAvailable else { return }
                self.sample(provenance: "ios-phone-poll")
            }
        }
        timer.tolerance = 2
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
    }

    private func persistGap() {
        guard let data = try? JSONEncoder().encode(gap) else { return }
        defaults.set(data, forKey: Self.gapKey)
    }

    private func persistPending() {
        defaults.set(pending.map { $0.base64EncodedString() }, forKey: Self.pendingKey)
    }

    private static func loadGap(_ defaults: UserDefaults) -> DurableCaptureGap {
        guard let data = defaults.data(forKey: gapKey),
              let gap = try? JSONDecoder().decode(DurableCaptureGap.self, from: data) else { return .none }
        return gap
    }

    private static func loadPending(_ defaults: UserDefaults) -> [Data] {
        let encoded = defaults.stringArray(forKey: pendingKey) ?? []
        return encoded.compactMap { Data(base64Encoded: $0) }
    }

    private static func decodeObservation(_ data: Data) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["observation"] as? [String: Any]
    }
}
