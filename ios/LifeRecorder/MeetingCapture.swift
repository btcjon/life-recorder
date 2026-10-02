import Combine
import Foundation

/// Manual markers are independent of the recorder switch and audio queue.
final class MeetingCapture: ObservableObject {
    static let shared = MeetingCapture()
    static let currentMeetingKey = "lifeRecorderCurrentMeetingID"
    @Published private(set) var currentMeetingID: String?
    var isActive: Bool { currentMeetingID != nil }
    private let defaults: UserDefaults
    private let deviceID: () -> String
    private let now: () -> Date
    private let enqueue: (String, String, Data, Date?) throws -> Void

    init(defaults: UserDefaults = .standard, deviceID: @escaping () -> String = { QueueStore.deviceID },
         now: @escaping () -> Date = Date.init,
         enqueue: @escaping (String, String, Data, Date?) throws -> Void = {
             try ContextTransport.shared.enqueue(path: $0, id: $1, payload: $2, expiresAt: $3)
         }) {
        self.defaults = defaults
        self.deviceID = deviceID
        self.now = now
        self.enqueue = enqueue
        currentMeetingID = defaults.string(forKey: Self.currentMeetingKey).flatMap {
            UUID(uuidString: $0)?.uuidString.lowercased()
        }
    }

    @discardableResult
    func start() throws -> String {
        // A later start is intentional: the existing receiver closes the old meeting.
        let meetingID = UUID().uuidString.lowercased()
        try marker(kind: "start", meetingID: meetingID)
        defaults.set(meetingID, forKey: Self.currentMeetingKey)
        currentMeetingID = meetingID
        return meetingID
    }

    func end() throws {
        guard let meetingID = currentMeetingID else { return }
        try marker(kind: "end", meetingID: meetingID)
        defaults.removeObject(forKey: Self.currentMeetingKey)
        currentMeetingID = nil
    }

    private func marker(kind: String, meetingID: String) throws {
        let eventID = UUID().uuidString.lowercased()
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let payload: [String: Any] = ["version": 1, "event_id": eventID, "meeting_id": meetingID,
            "device_id": deviceID(), "kind": kind, "occurred_at": formatter.string(from: now())]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        try enqueue(ContextTransport.meetingPath, eventID, data, nil)
    }
}
