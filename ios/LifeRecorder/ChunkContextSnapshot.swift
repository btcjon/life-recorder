import Foundation

/// Coordinate-free snapshot shared between main-thread context updates and the
/// serial audio writer. A later fix cannot be attached to an earlier clip.
final class ChunkContextSnapshot: @unchecked Sendable {
    private let lock = NSLock()
    private var id: String?
    private var capturedAt: Date?

    func update(id: String?, capturedAt: Date?) {
        lock.lock()
        defer { lock.unlock() }
        self.id = id
        self.capturedAt = capturedAt
    }

    func observationID(forClipStart clipStart: Date, now: Date = Date()) -> String? {
        lock.lock()
        defer { lock.unlock() }
        guard let id, UUID(uuidString: id)?.uuidString.lowercased() == id else { return nil }
        return LocationContextPolicy.associatedObservationID(id: id, capturedAt: capturedAt,
                                                            clipStart: clipStart, now: now)
    }
}
