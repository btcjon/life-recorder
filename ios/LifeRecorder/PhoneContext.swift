import Foundation

/// Categorical phone context. No contact handles, route names, or call identifiers.
enum PhoneContextReason {
    static let quietHours = "quiet_hours"
    static let manual = "manual"
    static let interruption = "interruption"
    static let route = "route"
    static let startupFailure = "startup_failure"
    static let unknownTermination = "unknown_termination"
    static let captureError = "capture_error"
}

enum PhoneContextSignal: String, Equatable, Codable {
    case macOutput = "mac_output"
    case browserMedia = "browser_media"
    case phoneAudio = "phone_audio"
    case phoneCall = "phone_call"
    case capture = "capture"

    static let all: Set<String> = [
        macOutput.rawValue, browserMedia.rawValue, phoneAudio.rawValue, phoneCall.rawValue, capture.rawValue
    ]
}

enum PhoneContextState: String, Equatable, Codable {
    case active, inactive, unknown, paused, ended
    static let all: Set<String> = [active.rawValue, inactive.rawValue, unknown.rawValue, paused.rawValue, ended.rawValue]
}

struct PhoneCallCategory: Equatable {
    enum Phase: String, Equatable { case incoming, outgoing, connected, held, ended }
    var phase: Phase

    /// Booleans only. Call UUID and handles stay at the system boundary.
    static func category(hasEnded: Bool, isOnHold: Bool, hasConnected: Bool, isOutgoing: Bool) -> PhoneCallCategory {
        if hasEnded { return PhoneCallCategory(phase: .ended) }
        if isOnHold { return PhoneCallCategory(phase: .held) }
        if hasConnected { return PhoneCallCategory(phase: .connected) }
        if isOutgoing { return PhoneCallCategory(phase: .outgoing) }
        return PhoneCallCategory(phase: .incoming)
    }
}

struct DurableCaptureGap: Codable, Equatable {
    var open: Bool
    var reason: String?
    var stopBoundaryKnown: Bool
    var captureWasActive: Bool
    var boundary: PhoneContextObservation? = nil

    static let none = DurableCaptureGap(open: false, reason: nil, stopBoundaryKnown: true, captureWasActive: false)
}

struct PhoneContextObservation: Equatable, Codable {
    var id: UUID
    var producerID: UUID
    var sessionID: UUID
    var sequence: Int
    var observedAt: Date
    var signal: PhoneContextSignal
    var state: PhoneContextState
    var provenance: String
    var metadata: [String: String]
}

struct PhoneContextSample: Equatable {
    var observedAt: Date
    var executionAvailable: Bool
    var otherAudioPlaying: Bool?
    var inputRoute: String?
    var outputRoute: String?
    var calls: [PhoneCallCategory]?
    var captureRunning: Bool?
    var provenance: String
}

enum PhoneContextEvent: Equatable {
    case relaunch(at: Date, prior: DurableCaptureGap)
    case sample(PhoneContextSample)
    case captureStarted(at: Date)
    case captureStopped(at: Date, reason: String)
    case captureStartFailed(at: Date)
}

@MainActor
protocol CaptureContextReporting: AnyObject {
    func captureDidStart()
    func captureDidStop(reason: String)
    func captureDidFailToStart()
}

enum PhoneContextValidation {
    static let metadataKeys: Set<String> = ["app", "title", "host", "reason", "output_route", "input_route", "call_state"]
    static let callStates: Set<String> = ["incoming", "outgoing", "connected", "held", "ended"]

    static func canonicalUUID(_ value: String) -> String? {
        guard let id = UUID(uuidString: value) else { return nil }
        let canonical = id.uuidString.lowercased()
        return value == canonical ? canonical : nil
    }

    static func jsonInt(_ value: Any?) -> Int? {
        // NSNumber(0/1) also bridges to Bool. Use the CF type, not Swift `is Bool`.
        guard let value else { return nil }
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let double = number.doubleValue
        guard double.rounded() == double, double <= Double(Int.max), double >= Double(Int.min) else { return nil }
        return number.intValue
    }

    static func isTimeZoneAware(_ value: String) -> Bool {
        guard value.contains("T"), value.count <= 40 else { return false }
        let zoned = value.hasSuffix("Z") || value.range(of: #"[+-]\d{2}:?\d{2}$"#, options: .regularExpression) != nil
        guard zoned else { return false }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if formatter.date(from: value) != nil { return true }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value) != nil
    }

    static func containsForbidden(_ value: String) -> Bool {
        let lower = value.lowercased()
        if lower.contains("://") || lower.contains("www.") || lower.contains("mailto:") || value.contains("@") { return true }
        let digits = value.filter(\.isNumber).count
        if digits >= 7 {
            let phone = CharacterSet(charactersIn: "0123456789+-.() \t")
            if value.unicodeScalars.allSatisfy({ phone.contains($0) }) { return true }
        }
        return false
    }

    static func safeText(_ value: String?, limit: Int) -> String? {
        guard let value, (1...limit).contains(value.count),
              value == value.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }),
              !containsForbidden(value), UUID(uuidString: value) == nil else { return nil }
        return value
    }

    static func sanitized(_ metadata: [String: String]) -> [String: String] {
        var clean: [String: String] = [:]
        for key in metadata.keys.sorted() where metadataKeys.contains(key) {
            guard clean.count < 7, let value = safeText(metadata[key], limit: 200) else { continue }
            if key == "call_state", !callStates.contains(value) { continue }
            clean[key] = value
        }
        return clean
    }

    static func aggregate(_ calls: [PhoneCallCategory]) -> (PhoneContextState, String?) {
        let phases = Set(calls.map(\.phase))
        if phases.contains(.connected) { return (.active, "connected") }
        if phases.contains(.held) { return (.active, "held") }
        if phases.contains(.outgoing) { return (.active, "outgoing") }
        if phases.contains(.incoming) { return (.active, "incoming") }
        if phases.contains(.ended) { return (.ended, "ended") }
        return (.inactive, nil)
    }

    static func isValidEnvelope(_ object: [String: Any], headerDeviceID: String) -> Bool {
        guard Set(object.keys) == Set(["device_id", "observation"]),
              let device = object["device_id"] as? String, canonicalUUID(device) == device,
              canonicalUUID(headerDeviceID) == device,
              let observation = object["observation"] as? [String: Any] else { return false }
        let fields: Set<String> = ["version", "id", "producer_id", "session_id", "sequence", "observed_at",
                                   "signal", "state", "provenance", "metadata"]
        guard Set(observation.keys) == fields, jsonInt(observation["version"]) == 1 else { return false }
        guard let id = observation["id"] as? String, canonicalUUID(id) == id,
              let producer = observation["producer_id"] as? String, producer == device,
              let session = observation["session_id"] as? String, canonicalUUID(session) == session,
              let sequence = jsonInt(observation["sequence"]), sequence >= 0,
              let observed = observation["observed_at"] as? String, isTimeZoneAware(observed),
              let signal = observation["signal"] as? String, PhoneContextSignal.all.contains(signal),
              let state = observation["state"] as? String, PhoneContextState.all.contains(state),
              let provenance = observation["provenance"] as? String, safeText(provenance, limit: 80) == provenance,
              let metadata = observation["metadata"] as? [String: Any], metadata.count <= 7 else { return false }
        for (key, value) in metadata {
            guard metadataKeys.contains(key), let text = value as? String, safeText(text, limit: 200) == text else { return false }
            if key == "call_state", !callStates.contains(text) { return false }
        }
        return true
    }
}

enum PhoneContextCodec {
    static func timestamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter.string(from: date)
    }

    static func envelope(deviceID: UUID, observation: PhoneContextObservation) -> Data? {
        let payload: [String: Any] = [
            "device_id": deviceID.uuidString.lowercased(),
            "observation": [
                "version": 1,
                "id": observation.id.uuidString.lowercased(),
                "producer_id": observation.producerID.uuidString.lowercased(),
                "session_id": observation.sessionID.uuidString.lowercased(),
                "sequence": observation.sequence,
                "observed_at": timestamp(observation.observedAt),
                "signal": observation.signal.rawValue,
                "state": observation.state.rawValue,
                "provenance": observation.provenance,
                "metadata": observation.metadata
            ] as [String: Any]
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys]),
              data.count <= 4_096,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              PhoneContextValidation.isValidEnvelope(object, headerDeviceID: deviceID.uuidString.lowercased()) else { return nil }
        return data
    }

    static func observationID(_ payload: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: payload) as? [String: Any],
              let observation = object["observation"] as? [String: Any] else { return nil }
        return observation["id"] as? String
    }
}

struct PhoneContextReducer {
    static let pollInterval: TimeInterval = 15
    static let activeRefreshInterval: TimeInterval = 15
    static let captureProvenance = "ios-phone-capture"
    static let relaunchProvenance = "ios-phone-relaunch"

    let deviceID: UUID
    let sessionID: UUID
    private(set) var sequence = 0
    private(set) var gap = DurableCaptureGap.none
    private(set) var captureRunning = false
    private var last: [PhoneContextSignal: (state: PhoneContextState, metadata: [String: String], at: Date)] = [:]
    private let newID: () -> UUID

    init(deviceID: UUID, sessionID: UUID = UUID(), newID: @escaping () -> UUID = UUID.init) {
        self.deviceID = deviceID
        self.sessionID = sessionID
        self.newID = newID
    }

    mutating func reduce(_ event: PhoneContextEvent) -> [PhoneContextObservation] {
        switch event {
        case let .relaunch(at, prior):
            return relaunch(at: at, prior: prior)
        case let .sample(sample):
            return reduceAudio(sample) + reduceCalls(sample) + reduceCaptureSample(sample)
        case let .captureStarted(at):
            return startCapture(at: at)
        case let .captureStopped(at, reason):
            return stopCapture(at: at, reason: reason)
        case let .captureStartFailed(at):
            return failStart(at: at)
        }
    }

    private mutating func relaunch(at: Date, prior: DurableCaptureGap) -> [PhoneContextObservation] {
        captureRunning = false
        if prior.captureWasActive && !prior.open {
            gap = DurableCaptureGap(open: true, reason: PhoneContextReason.unknownTermination,
                                    stopBoundaryKnown: false, captureWasActive: false)
            let boundary = emit(.capture, .unknown, at: at, provenance: Self.relaunchProvenance,
                                metadata: ["reason": PhoneContextReason.unknownTermination])
            gap.boundary = boundary
            return [boundary]
        }
        guard prior.open else {
            gap = .none
            return []
        }
        gap = prior
        gap.captureWasActive = false
        if let boundary = prior.boundary, boundary.producerID == deviceID {
            // Retry the original immutable identity/time, not a newly exact relaunch boundary.
            return [boundary]
        }
        gap.stopBoundaryKnown = false
        gap.reason = PhoneContextReason.unknownTermination
        let boundary = emit(.capture, .unknown, at: at, provenance: Self.relaunchProvenance,
                            metadata: ["reason": PhoneContextReason.unknownTermination])
        gap.boundary = boundary
        return [boundary]
    }

    private mutating func startCapture(at: Date) -> [PhoneContextObservation] {
        let duplicate = captureRunning && !gap.open
        captureRunning = true
        gap = DurableCaptureGap(open: false, reason: nil, stopBoundaryKnown: true, captureWasActive: true)
        if duplicate, let previous = last[.capture], previous.state == .active,
           at.timeIntervalSince(previous.at) < Self.activeRefreshInterval { return [] }
        return [emit(.capture, .active, at: at, provenance: Self.captureProvenance, metadata: [:])]
    }

    private mutating func stopCapture(at: Date, reason: String) -> [PhoneContextObservation] {
        guard captureRunning else { return [] }
        let safe = PhoneContextValidation.safeText(reason, limit: 200) ?? PhoneContextReason.interruption
        captureRunning = false
        gap = DurableCaptureGap(open: true, reason: safe, stopBoundaryKnown: true, captureWasActive: false)
        let boundary = emit(.capture, .inactive, at: at, provenance: Self.captureProvenance, metadata: ["reason": safe])
        gap.boundary = boundary
        return [boundary]
    }

    private mutating func failStart(at: Date) -> [PhoneContextObservation] {
        let duplicate = gap.open && (last[.capture]?.state == .inactive || last[.capture]?.state == .unknown)
            && last[.capture]?.metadata["reason"] == PhoneContextReason.startupFailure
        captureRunning = false
        if gap.open {
            gap.captureWasActive = false
        } else {
            gap = DurableCaptureGap(open: true, reason: PhoneContextReason.startupFailure,
                                    stopBoundaryKnown: false, captureWasActive: false)
        }
        if duplicate { return [] }
        let observation = emit(.capture, gap.stopBoundaryKnown ? .inactive : .unknown, at: at,
                               provenance: "ios-phone-startup", metadata: ["reason": PhoneContextReason.startupFailure])
        if gap.boundary == nil { gap.boundary = observation }
        return [observation]
    }

    private mutating func reduceAudio(_ sample: PhoneContextSample) -> [PhoneContextObservation] {
        guard sample.executionAvailable, let playing = sample.otherAudioPlaying else {
            return emitIfChanged(.phoneAudio, .unknown, at: sample.observedAt, provenance: sample.provenance,
                                 metadata: [:], allowRefresh: false)
        }
        var metadata: [String: String] = [:]
        if let input = PhoneContextValidation.safeText(sample.inputRoute, limit: 200) { metadata["input_route"] = input }
        if let output = PhoneContextValidation.safeText(sample.outputRoute, limit: 200) { metadata["output_route"] = output }
        let state: PhoneContextState = playing ? .active : .inactive
        return emitIfChanged(.phoneAudio, state, at: sample.observedAt, provenance: sample.provenance,
                             metadata: metadata, allowRefresh: playing)
    }

    private mutating func reduceCalls(_ sample: PhoneContextSample) -> [PhoneContextObservation] {
        guard let calls = sample.calls else {
            return emitIfChanged(.phoneCall, .unknown, at: sample.observedAt, provenance: sample.provenance,
                                 metadata: [:], allowRefresh: false)
        }
        let aggregate = PhoneContextValidation.aggregate(calls)
        var metadata: [String: String] = [:]
        if let callState = aggregate.1 { metadata["call_state"] = callState }
        return emitIfChanged(.phoneCall, aggregate.0, at: sample.observedAt, provenance: sample.provenance,
                             metadata: metadata, allowRefresh: aggregate.0 == .active && sample.executionAvailable)
    }

    private mutating func reduceCaptureSample(_ sample: PhoneContextSample) -> [PhoneContextObservation] {
        guard let running = sample.captureRunning else { return [] }
        if running && captureRunning && !gap.open {
            guard sample.executionAvailable else { return [] }
            return emitIfChanged(.capture, .active, at: sample.observedAt, provenance: sample.provenance,
                                 metadata: [:], allowRefresh: true)
        }
        if !running && captureRunning {
            captureRunning = false
            gap = DurableCaptureGap(open: true, reason: PhoneContextReason.unknownTermination,
                                    stopBoundaryKnown: false, captureWasActive: false)
            let boundary = emit(.capture, .unknown, at: sample.observedAt, provenance: sample.provenance,
                                metadata: ["reason": PhoneContextReason.unknownTermination])
            gap.boundary = boundary
            return [boundary]
        }
        return []
    }

    private mutating func emitIfChanged(_ signal: PhoneContextSignal, _ state: PhoneContextState, at: Date,
                                        provenance: String, metadata: [String: String], allowRefresh: Bool) -> [PhoneContextObservation] {
        let clean = PhoneContextValidation.sanitized(metadata)
        if let previous = last[signal], previous.state == state, previous.metadata == clean {
            let due = allowRefresh && state == .active && at.timeIntervalSince(previous.at) >= Self.activeRefreshInterval
            if !due { return [] }
        }
        return [emit(signal, state, at: at, provenance: provenance, metadata: clean)]
    }

    private mutating func emit(_ signal: PhoneContextSignal, _ state: PhoneContextState, at: Date,
                               provenance: String, metadata: [String: String]) -> PhoneContextObservation {
        let cleanProvenance = PhoneContextValidation.safeText(provenance, limit: 80) ?? "ios-phone-context"
        let observation = PhoneContextObservation(id: newID(), producerID: deviceID, sessionID: sessionID, sequence: sequence,
                                                  observedAt: at, signal: signal, state: state, provenance: cleanProvenance,
                                                  metadata: PhoneContextValidation.sanitized(metadata))
        sequence += 1
        last[signal] = (state, observation.metadata, at)
        return observation
    }
}
