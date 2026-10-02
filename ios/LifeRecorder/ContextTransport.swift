import Combine
import CryptoKit
import Foundation
import Security
import UIKit

/// Independent durable outbox for small context markers. Never uses audio tasks.
final class ContextTransport: NSObject, ObservableObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let shared = ContextTransport()
    static let sessionID = "com.browseruse.liferecorder.context"
    static let meetingPath = "/v1/meeting-events"
    static let locationPath = "/v1/location/observations"
    static let locationDeletePath = "/v1/location/delete-history"
    static let maximumAttempts = 8
    @Published private(set) var pendingCount = 0
    @Published private(set) var status = "No context markers waiting"
    var backgroundCompletion: (() -> Void)?

    struct Record: Codable {
        let id: String
        let path: String
        let payload: Data
        let createdAt: Date
        let expiresAt: Date?
        var attempts: Int = 0
        var retryAt: Date? = nil
        var acknowledged: Bool = false
    }

    enum OutboxError: Error { case invalidMarker, conflictingMarker }
    private let directory: URL
    private let now: () -> Date
    private var responseData: [Int: Data] = [:]
    private var timer: Timer?
    private var pumping = false
    private var placeStatusInFlight = false
    private var lastPlaceStatusCheck: Date?
    private lazy var statusSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 30
        config.tlsMinimumSupportedProtocolVersion = .TLSv12
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        config.allowsCellularAccess = true
        config.waitsForConnectivity = true
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 15 * 60
        config.httpMaximumConnectionsPerHost = 1
        config.tlsMinimumSupportedProtocolVersion = .TLSv12
        config.tlsMaximumSupportedProtocolVersion = .TLSv13
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()

    init(directory: URL? = nil, now: @escaping () -> Date = Date.init) {
        self.directory = directory ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("PendingContext", isDirectory: true)
        self.now = now
        super.init()
        refresh()
    }

    private func prepareDirectory() throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
            attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var url = directory
        try url.setResourceValues(values)
    }

    private func recordURL(_ id: String) -> URL { directory.appendingPathComponent(id + ".json") }
    private func bodyURL(_ id: String) -> URL { directory.appendingPathComponent(id + ".body") }

    private func protect(_ url: URL) throws {
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: url.path)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var target = url
        try target.setResourceValues(values)
    }

    private func save(_ record: Record) throws {
        try prepareDirectory()
        let url = recordURL(record.id)
        try JSONEncoder().encode(record).write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        try protect(url)
    }

    private func read(_ id: String) -> Record? {
        guard UUID(uuidString: id)?.uuidString.lowercased() == id,
              let raw = try? Data(contentsOf: recordURL(id)),
              raw.count <= 32_768,
              let record = try? JSONDecoder().decode(Record.self, from: raw), record.id == id,
              [Self.meetingPath, Self.locationPath, Self.locationDeletePath].contains(record.path) else { return nil }
        return record
    }

    private func records() -> [Record] {
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? []
        return files.filter { $0.pathExtension == "json" }.compactMap { read($0.deletingPathExtension().lastPathComponent) }
            .sorted { $0.createdAt < $1.createdAt }
    }

    func enqueue(path: String, id: String, payload: Data, expiresAt: Date? = nil) throws {
        guard UUID(uuidString: id)?.uuidString.lowercased() == id,
              [Self.meetingPath, Self.locationPath, Self.locationDeletePath].contains(path),
              !payload.isEmpty, payload.count <= 4_096,
              let object = try JSONSerialization.jsonObject(with: payload) as? [String: Any] else {
            throw OutboxError.invalidMarker
        }
        let payloadID: String?
        switch path {
        case Self.meetingPath: payloadID = object["event_id"] as? String
        case Self.locationPath: payloadID = (object["observation"] as? [String: Any])?["id"] as? String
        default: payloadID = object["id"] as? String
        }
        guard payloadID == id else { throw OutboxError.invalidMarker }
        if let existing = read(id) {
            guard existing.path == path, existing.payload == payload else { throw OutboxError.conflictingMarker }
            return
        }
        // Never overwrite an unreadable manifest: it could contain an unsent marker.
        guard !FileManager.default.fileExists(atPath: recordURL(id).path) else { throw OutboxError.conflictingMarker }
        let expiry: Date?
        if path == Self.locationPath {
            let maximum = now().addingTimeInterval(24 * 60 * 60)
            expiry = min(expiresAt ?? maximum, maximum)
            guard expiry! > now() else { throw OutboxError.invalidMarker }
        } else {
            expiry = nil // Meeting markers survive connectivity interruptions.
        }
        try save(Record(id: id, path: path, payload: payload, createdAt: now(), expiresAt: expiry))
        refresh()
        if timer != nil { pump() }
    }

    func activate() {
        _ = session
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
                self?.pump()
                self?.refreshPlaceStatusIfDue()
            }
        }
        pump()
        refreshPlaceStatusIfDue()
    }

    private func refreshPlaceStatusIfDue() {
        guard UIApplication.shared.applicationState == .active, LocationContext.shared.enabled,
              !placeStatusInFlight, let settings = ReceiverSettings.load() else { return }
        if let lastPlaceStatusCheck, now().timeIntervalSince(lastPlaceStatusCheck) < 60 { return }
        lastPlaceStatusCheck = now()
        placeStatusInFlight = true
        var request = URLRequest(url: settings.baseURL.appendingPathComponent("v1/location/status"))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer " + settings.token, forHTTPHeaderField: "Authorization")
        request.setValue(QueueStore.deviceID, forHTTPHeaderField: "X-Device-ID")
        statusSession.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.placeStatusInFlight = false
                guard LocationContext.shared.enabled,
                      ReceiverSettings.load()?.baseURL == settings.baseURL,
                      ReceiverSettings.load()?.token == settings.token else { return }
                guard error == nil, let http = response as? HTTPURLResponse, http.statusCode == 200,
                      let data, data.count <= 16_384,
                      let projection = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    LocationContext.shared.updatePlaceStatus(["known_places": [[String: Any]]()])
                    return
                }
                // Retain only named-place projection; no coordinate response is used.
                var safe: [String: Any] = [:]
                if let known = projection["known_places"] as? [[String: Any]] {
                    safe["known_places"] = Array(known.prefix(100)).compactMap { place -> [String: String]? in
                        guard let name = place["name"] as? String, !name.isEmpty, name.count <= 100 else { return nil }
                        return ["name": name]
                    }
                }
                if let last = projection["last_known"] as? [String: Any] {
                    var item: [String: Any] = [:]
                    if let id = last["observation_id"] as? String { item["observation_id"] = id }
                    if let state = last["status"] as? String { item["status"] = state }
                    if let place = last["place"] as? [String: Any], let name = place["name"] as? String,
                       !name.isEmpty, name.count <= 100 { item["place"] = ["name": name] }
                    safe["last_known"] = item
                }
                LocationContext.shared.updatePlaceStatus(safe)
            }
        }.resume()
    }

    func clearLocationQueue() {
        let ids = Set(records().filter { $0.path == Self.locationPath }.map(\.id))
        for id in ids { remove(id) }
        refresh()
        session.getAllTasks { tasks in
            DispatchQueue.main.async {
                for task in tasks where ids.contains(task.taskDescription ?? "") { task.cancel() }
            }
        }
    }

    private func remove(_ id: String) {
        // An acknowledged manifest is deleted last: a crash can safely finish cleanup.
        try? FileManager.default.removeItem(at: bodyURL(id))
        try? FileManager.default.removeItem(at: recordURL(id))
    }

    private func refresh() {
        let entries = records()
        let manifests = ((try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.pathExtension == "json" }.count
        let unreadable = max(0, manifests - entries.count)
        pendingCount = entries.filter { !$0.acknowledged }.count + unreadable
        if unreadable > 0 {
            status = "A context marker needs local recovery; kept on this phone"
        } else if entries.contains(where: { $0.attempts >= Self.maximumAttempts }) {
            status = "Context markers need attention; retained on this phone"
        } else {
            status = pendingCount == 0 ? "No context markers waiting" : "\(pendingCount) context markers waiting on this phone"
        }
    }

    private func pump() {
        guard !pumping else { return }
        pumping = true
        session.getAllTasks { tasks in
            DispatchQueue.main.async {
                defer { self.pumping = false }
                let current = self.now()
                for record in self.records() where record.acknowledged || (record.expiresAt.map { $0 <= current } ?? false) {
                    for task in tasks where task.taskDescription == record.id { task.cancel() }
                    self.remove(record.id)
                }
                self.refresh()
                guard tasks.isEmpty else { return }
                guard let settings = ReceiverSettings.load() else {
                    if self.pendingCount > 0 { self.status = "Pair with your Mac to send context markers" }
                    return
                }
                guard var record = self.records().first(where: { !$0.acknowledged && $0.attempts < Self.maximumAttempts && ($0.retryAt ?? .distantPast) <= current }) else { return }
                do {
                    let body = self.bodyURL(record.id)
                    try record.payload.write(to: body, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
                    try self.protect(body)
                    record.attempts += 1
                    record.retryAt = current.addingTimeInterval(min(15 * 60, pow(2, Double(record.attempts)) * 15))
                    try self.save(record) // Persist attempt before task creation.
                    var request = URLRequest(url: settings.baseURL.appendingPathComponent(String(record.path.dropFirst())))
                    request.httpMethod = "POST"
                    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                    request.setValue("Bearer " + settings.token, forHTTPHeaderField: "Authorization")
                    request.setValue(QueueStore.deviceID, forHTTPHeaderField: "X-Device-ID")
                    let task = self.session.uploadTask(with: request, fromFile: body)
                    task.taskDescription = record.id
                    task.resume()
                    self.status = "Sending context marker to Mac"
                } catch {
                    self.status = "Context marker kept on this phone; local storage unavailable"
                }
            }
        }
    }

    static func verifiedReceipt(_ data: Data, id: String) -> Bool {
        struct Receipt: Decodable { let durable: Bool; let event_id: String?; let observation_id: String?; let request_id: String?; let id: String? }
        guard data.count <= 16_384, let receipt = try? JSONDecoder().decode(Receipt.self, from: data), receipt.durable else { return false }
        let receiptIDs = [receipt.event_id, receipt.observation_id, receipt.request_id, receipt.id].compactMap { $0 }
        return !receiptIDs.isEmpty && receiptIDs.allSatisfy { $0 == id }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil) // Context bearer credentials stay on the paired origin.
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        var buffer = responseData[dataTask.taskIdentifier] ?? Data()
        guard buffer.count + data.count <= 16_384 else { dataTask.cancel(); return }
        buffer.append(data)
        responseData[dataTask.taskIdentifier] = buffer
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let data = responseData.removeValue(forKey: task.taskIdentifier) ?? Data()
        guard let id = task.taskDescription, var record = read(id) else { pump(); return }
        if error == nil, let http = task.response as? HTTPURLResponse, (200...299).contains(http.statusCode),
           Self.verifiedReceipt(data, id: id) {
            record.acknowledged = true
            do { try save(record); remove(id) }
            catch { status = "Mac received context marker; local cleanup pending" }
        } else {
            status = "Context marker kept on this phone; waiting to retry"
        }
        refresh()
        pump()
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        DispatchQueue.main.async {
            let completion = self.backgroundCompletion
            self.backgroundCompletion = nil
            completion?()
        }
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let settings = ReceiverSettings.load(),
              challenge.protectionSpace.host.lowercased() == settings.baseURL.host?.lowercased() else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        if settings.certificateSHA256.isEmpty { completionHandler(.performDefaultHandling, nil); return }
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let certificate = chain.first else {
            completionHandler(.cancelAuthenticationChallenge, nil); return
        }
        let digest = SHA256.hash(data: SecCertificateCopyData(certificate) as Data).map { String(format: "%02x", $0) }.joined()
        let matches = digest == settings.certificateSHA256
        completionHandler(matches ? .useCredential : .cancelAuthenticationChallenge, matches ? URLCredential(trust: trust) : nil)
    }
}
