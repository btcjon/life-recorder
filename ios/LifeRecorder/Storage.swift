import AVFoundation
import CryptoKit
import Foundation
import Security

struct ChunkActivity: Codable, Equatable {
    var version: Int
    var decision: String
    var coverageComplete: Bool
    var windowCount: Int
    var expectedWindows: Int
    var maxRMSDBFS: Double?
    var peakDBFS: Double?
    var reason: String

    init(_ decision: ActivityDecision) {
        version = decision.version
        self.decision = decision.decision.rawValue
        coverageComplete = decision.coverageComplete
        windowCount = decision.windowCount
        expectedWindows = decision.expectedWindows
        maxRMSDBFS = decision.maxRMSDBFS
        peakDBFS = decision.peakDBFS
        reason = decision.reason
    }

    var headerValue: String {
        ActivityDecision(
            version: version,
            decision: ActivityDecision.Decision(rawValue: decision) ?? .unknown,
            coverageComplete: coverageComplete,
            windowCount: windowCount,
            expectedWindows: expectedWindows,
            maxRMSDBFS: maxRMSDBFS,
            peakDBFS: peakDBFS,
            reason: reason
        ).headerValue
    }
}

struct Chunk: Codable {
    let id: UUID
    let startedAt: Date
    let duration: Double
    let sha256: String
    var activity: ChunkActivity? = nil
    var locationObservationID: String? = nil
    var name: String { id.uuidString.lowercased() }
    var audioURL: URL { QueueStore.directory.appendingPathComponent(name + ".m4a") }
    var manifestURL: URL { QueueStore.directory.appendingPathComponent(name + ".json") }
}

struct RecordingJournal: Codable {
    let id: UUID
    let startedAt: Date
    var locationObservationID: String? = nil
}

enum QueueStore {
    static let recentReceiptsKey = "recentUploadReceiptIDs"
    static let directory: URL = {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let url = support.appendingPathComponent("PendingAudio", isDirectory: true)
        try! FileManager.default.createDirectory(at: url, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var excludedURL = url
        try? excludedURL.setResourceValues(values)
        return url
    }()

    static var deviceID: String {
        if let value = UserDefaults.standard.string(forKey: "deviceID") { return value }
        let value = UUID().uuidString.lowercased()
        UserDefaults.standard.set(value, forKey: "deviceID")
        return value
    }

    static func pending() -> [Chunk] {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "json" && !$0.lastPathComponent.contains(".recording.") }
            .compactMap { try? JSONDecoder().decode(Chunk.self, from: Data(contentsOf: $0)) }
            .sorted { $0.startedAt < $1.startedAt }
    }

    static func checksum(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let data = try handle.read(upToCount: 65536), !data.isEmpty { hash.update(data: data) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func seal(_ journal: RecordingJournal, duration: Double, activity: ActivityDecision? = nil) throws -> Chunk {
        let name = journal.id.uuidString.lowercased()
        let audio = directory.appendingPathComponent(name + ".m4a")
        let chunk = Chunk(id: journal.id, startedAt: journal.startedAt, duration: duration,
                          sha256: try checksum(audio),
                          activity: activity.map(ChunkActivity.init),
                          locationObservationID: journal.locationObservationID)
        try JSONEncoder().encode(chunk).write(to: chunk.manifestURL, options: .atomic)
        try? FileManager.default.removeItem(at: directory.appendingPathComponent(name + ".recording.json"))
        return chunk
    }

    static func recoverInterruptedFiles() async -> Int {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        var unrecoverable = 0
        for file in files where file.lastPathComponent.hasSuffix(".recording.json") {
            do {
                let journal = try JSONDecoder().decode(RecordingJournal.self, from: Data(contentsOf: file))
                let audio = directory.appendingPathComponent(journal.id.uuidString.lowercased() + ".m4a")
                let duration = try await AVURLAsset(url: audio).load(.duration).seconds
                guard duration.isFinite && duration > 0 else { unrecoverable += 1; continue }
                _ = try seal(journal, duration: duration, activity: ActivityProbe.recoveredUnknown())
            } catch {
                // Keep an incomplete file for recovery; never pretend it was uploaded.
                unrecoverable += 1
            }
        }
        // An .acked marker is the local commit point for a verified Mac receipt.
        // Older builds may have removed the manifest before writing such a marker;
        // only a retained verified receipt ID can establish that older case.
        for audio in files where audio.pathExtension == "m4a" {
            let stem = audio.deletingPathExtension().lastPathComponent
            guard !hasValidManifest(stem), !hasJournal(stem) else { continue }
            guard isVerifiedAcknowledged(stem) else { continue }
            try? FileManager.default.removeItem(at: audio)
            if !FileManager.default.fileExists(atPath: audio.path) {
                try? FileManager.default.removeItem(at: acknowledgedMarker(stem))
            }
        }
        for marker in files where marker.pathExtension == "acked" {
            let stem = marker.deletingPathExtension().lastPathComponent
            let audio = directory.appendingPathComponent(stem + ".m4a")
            if !FileManager.default.fileExists(atPath: audio.path) {
                try? FileManager.default.removeItem(at: marker)
            }
        }
        return unrecoverable
    }

    private static func acknowledgedMarker(_ stem: String) -> URL {
        directory.appendingPathComponent(stem + ".acked")
    }

    private static func hasJournal(_ stem: String) -> Bool {
        FileManager.default.fileExists(atPath: directory.appendingPathComponent(stem + ".recording.json").path)
    }

    private static func hasValidManifest(_ stem: String) -> Bool {
        let path = directory.appendingPathComponent(stem + ".json")
        guard let data = try? Data(contentsOf: path),
              let chunk = try? JSONDecoder().decode(Chunk.self, from: data) else { return false }
        return chunk.name == stem
    }

    private static func hasValidMarker(_ stem: String) -> Bool {
        guard let data = try? Data(contentsOf: acknowledgedMarker(stem)),
              let chunk = try? JSONDecoder().decode(Chunk.self, from: data) else { return false }
        return chunk.name == stem
    }

    private static func isVerifiedAcknowledged(_ stem: String) -> Bool {
        hasValidMarker(stem) || (UserDefaults.standard.stringArray(forKey: recentReceiptsKey) ?? []).contains(stem)
    }

    /// Verified receipts with audio remaining after a crash. Read-only.
    static func orphanedAcknowledgedAudioCount() -> Int {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.filter { file in
            guard file.pathExtension == "m4a" else { return false }
            let stem = file.deletingPathExtension().lastPathComponent
            return !hasValidManifest(stem) && !hasJournal(stem) && isVerifiedAcknowledged(stem)
        }.count
    }

    /// Audio without a valid queue record or receipt. Keep it for manual recovery.
    static func unverifiedLocalAudioCount() -> Int {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.filter { file in
            guard file.pathExtension == "m4a" else { return false }
            let stem = file.deletingPathExtension().lastPathComponent
            return !hasValidManifest(stem) && !hasJournal(stem) && !isVerifiedAcknowledged(stem)
        }.count
    }

    static func removeAcknowledged(_ chunk: Chunk) throws {
        // Only called after a verified durable receipt. Same-directory rename
        // commits the acknowledgment before audio can be removed.
        let marker = acknowledgedMarker(chunk.name)
        try FileManager.default.moveItem(at: chunk.manifestURL, to: marker)
        if FileManager.default.fileExists(atPath: chunk.audioURL.path) {
            try FileManager.default.removeItem(at: chunk.audioURL)
        }
        try FileManager.default.removeItem(at: marker)
    }
}

enum Credentials {
    private static let service = "com.browseruse.liferecorder.receiver"
    static func token() -> String {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "token",
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    static func setToken(_ token: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "token"]
        let changes: [String: Any] = [kSecValueData as String: Data(token.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, changes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(changes) { _, new in new } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
}

struct ReceiverSettings {
    let baseURL: URL
    let token: String
    let certificateSHA256: String

    static func load() -> ReceiverSettings? {
        guard let raw = UserDefaults.standard.string(forKey: "receiverURL"),
              let url = URL(string: raw), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { return nil }
        let token = Credentials.token()
        guard !token.isEmpty else { return nil }
        return ReceiverSettings(baseURL: url, token: token,
            certificateSHA256: UserDefaults.standard.string(forKey: "certificateSHA256") ?? "")
    }

    static func save(url: String, token: String, pin: String) throws {
        guard let parsed = URL(string: url.trimmingCharacters(in: .whitespacesAndNewlines)),
              parsed.scheme == "https", parsed.host != nil, parsed.user == nil, parsed.password == nil,
              parsed.query == nil, parsed.fragment == nil,
              parsed.path.isEmpty || parsed.path == "/", !token.isEmpty else {
            throw NSError(domain: "Settings", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Enter an HTTPS receiver address and its pairing token."])
        }
        let cleanPin = pin.lowercased().replacingOccurrences(of: ":", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard cleanPin.isEmpty || (cleanPin.count == 64 && cleanPin.allSatisfy { $0.isHexDigit }) else {
            throw NSError(domain: "Settings", code: 2,
                userInfo: [NSLocalizedDescriptionKey: "The certificate fingerprint must contain 64 hexadecimal characters."])
        }
        try Credentials.setToken(token)
        UserDefaults.standard.set(parsed.absoluteString, forKey: "receiverURL")
        UserDefaults.standard.set(cleanPin, forKey: "certificateSHA256")
    }
}
