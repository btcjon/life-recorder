import Combine
import CryptoKit
import Foundation
import Security
import UIKit

enum UploadTransportPolicy {
    static let sessionID = "com.browseruse.liferecorder.uploads"
    static let allowsCellularAccess = true
    static let isDiscretionary = false
    static let sessionSendsLaunchEvents = true
    static let waitsForConnectivity = true
    static let requestTimeout: TimeInterval = 120
    static let resourceTimeout: TimeInterval = 30 * 60
    static let maximumConnectionsPerHost = 2
}

final class UploadManager: NSObject, ObservableObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let shared = UploadManager()
    static let sessionID = UploadTransportPolicy.sessionID
    private static let lastErrorKey = "lastUploadError"
    private static let lastAckKey = "lastAcknowledgedAt"
    private static let recentReceiptsKey = QueueStore.recentReceiptsKey
    private static let retryStatus = "Retrying uploads now. Clips stay on this phone until the Mac acknowledges them."
    @Published private(set) var pendingCount = 0
    @Published private(set) var status = "Pair with your Mac to upload"
    @Published private(set) var lastUploadedAt: Date?
    @Published private(set) var macProcessing: MacProcessingSummary?
    @Published private(set) var delivery = UploadDeliverySnapshot(
        phase: .needsPairing,
        pendingCount: 0,
        oldestQueuedStartedAt: nil,
        lastAcknowledgedAt: nil,
        lastErrorCode: nil,
        orphanedAcknowledgedCount: 0,
        unverifiedLocalAudioCount: 0
    )
    var backgroundCompletion: (() -> Void)?
    private var responseData: [Int: Data] = [:]
    private var retries: [String: Date] = [:]
    private var failureCounts: [String: Int] = [:]
    private var immediateRetryTaskIDs = Set<Int>()
    private var pumping = false
    private var authenticationRejected = false
    private var timer: Timer?
    private let statusClient = MacProcessingStatusClient()
    private var statusInFlight = false
    private var lastStatusCheck: Date?
    private var statusGeneration = 0
    private static let staleTaskInterval: TimeInterval = 60
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        config.isDiscretionary = UploadTransportPolicy.isDiscretionary
        config.sessionSendsLaunchEvents = UploadTransportPolicy.sessionSendsLaunchEvents
        config.allowsCellularAccess = UploadTransportPolicy.allowsCellularAccess
        config.waitsForConnectivity = UploadTransportPolicy.waitsForConnectivity
        config.timeoutIntervalForRequest = UploadTransportPolicy.requestTimeout
        // A connectivity-waiting task must not occupy one of the two upload
        // slots for days if the LAN route or hostname becomes stale.
        config.timeoutIntervalForResource = UploadTransportPolicy.resourceTimeout
        config.httpMaximumConnectionsPerHost = UploadTransportPolicy.maximumConnectionsPerHost
        config.tlsMinimumSupportedProtocolVersion = .TLSv12
        config.tlsMaximumSupportedProtocolVersion = .TLSv13
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()

    private override init() {
        super.init()
        lastUploadedAt = UserDefaults.standard.object(forKey: Self.lastAckKey) as? Date
        refreshDelivery(transferInFlight: false, hasRetryBackoff: false)
    }

    func activate() {
        _ = session // Reattach transfers if iOS launched us to deliver an upload result.
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
                self?.pump()
                self?.refreshMacProcessingIfDue()
            }
        }
        pump()
        refreshMacProcessingIfDue()
    }

    func configurationChanged() {
        authenticationRejected = false
        retries.removeAll()
        failureCounts.removeAll()
        statusGeneration += 1
        macProcessing = nil
        lastStatusCheck = nil
        retryNow()
    }

    func retryNow() {
        authenticationRejected = false
        retries.removeAll()
        failureCounts.removeAll()
        refreshDelivery(transferInFlight: true, hasRetryBackoff: false, statusOverride: Self.retryStatus)
        session.getAllTasks { tasks in
            DispatchQueue.main.async {
                for task in tasks {
                    if Self.taskID(task.taskDescription) != nil {
                        self.immediateRetryTaskIDs.insert(task.taskIdentifier)
                    }
                    task.cancel()
                }
                self.pump()
            }
        }
    }

    func pump() {
        assert(Thread.isMainThread)
        let pending = QueueStore.pending()
        pendingCount = pending.count
        guard let settings = ReceiverSettings.load() else {
            refreshDelivery(transferInFlight: false, hasRetryBackoff: false)
            return
        }
        guard !authenticationRejected else {
            refreshDelivery(transferInFlight: false, hasRetryBackoff: false)
            return
        }
        guard !pumping else { return }
        pumping = true
        session.getAllTasks { [weak self] tasks in
            DispatchQueue.main.async {
                guard let self else { return }
                defer { self.pumping = false }
                let now = Date()
                var active = Set<String>()
                for task in tasks {
                    guard let id = Self.taskID(task.taskDescription) else {
                        task.cancel()
                        continue
                    }
                    if let started = Self.taskStart(task.taskDescription),
                       now.timeIntervalSince(started) > Self.staleTaskInterval {
                        // A background transfer can remain alive indefinitely
                        // after the route changes. Cancel it and let the
                        // delegate immediately recreate it instead of applying
                        // the normal failure backoff.
                        self.immediateRetryTaskIDs.insert(task.taskIdentifier)
                        task.cancel()
                        continue
                    }
                    active.insert(id)
                }
                var available = max(0, 2 - active.count)
                let slotsBefore = available
                for chunk in pending where !active.contains(chunk.name) {
                    guard available > 0 else { break }
                    if let retry = self.retries[chunk.name], retry > Date() { continue }
                    guard FileManager.default.fileExists(atPath: chunk.audioURL.path) else {
                        if UserDefaults.standard.string(forKey: Self.lastErrorKey)?.hasPrefix("HTTP:") != true {
                            UserDefaults.standard.set("local:missing-audio", forKey: Self.lastErrorKey)
                        }
                        continue
                    }
                    let url = settings.baseURL.appendingPathComponent("v1/chunks").appendingPathComponent(chunk.name)
                    var request = URLRequest(url: url)
                    request.httpMethod = "POST"
                    request.setValue("audio/mp4", forHTTPHeaderField: "Content-Type")
                    request.setValue("Bearer " + settings.token, forHTTPHeaderField: "Authorization")
                    request.setValue(chunk.sha256, forHTTPHeaderField: "X-Audio-SHA256")
                    request.setValue(QueueStore.deviceID, forHTTPHeaderField: "X-Device-ID")
                    request.setValue(Self.timestamp(chunk.startedAt), forHTTPHeaderField: "X-Started-At")
                    request.setValue(String(chunk.duration), forHTTPHeaderField: "X-Duration-Seconds")
                    if let observationID = Self.locationObservationHeader(for: chunk) {
                        request.setValue(observationID, forHTTPHeaderField: "X-Location-Observation-ID")
                    }
                    if let activity = chunk.activity {
                        let value = String(activity.headerValue.prefix(256))
                        request.setValue(value, forHTTPHeaderField: "X-Activity-Shadow")
                        request.setValue(String(activity.version), forHTTPHeaderField: "X-Activity-Version")
                    }
                    let task = self.session.uploadTask(with: request, fromFile: chunk.audioURL)
                    task.taskDescription = Self.taskDescription(for: chunk.name)
                    task.countOfBytesClientExpectsToSend = (try? chunk.audioURL.resourceValues(forKeys: [.fileSizeKey]).fileSize)
                        .map(Int64.init) ?? NSURLSessionTransferSizeUnknown
                    task.countOfBytesClientExpectsToReceive = 512
                    task.resume()
                    available -= 1
                }
                let started = slotsBefore - available
                let pendingNames = Set(pending.map(\.name))
                // A task for a clip already removed must not look like an upload still in progress.
                let transferInFlight = started > 0 || !active.intersection(pendingNames).isEmpty
                let hasRetryBackoff = pending.contains { chunk in
                    self.retries[chunk.name].map { $0 > Date() } ?? false
                }
                self.refreshDelivery(transferInFlight: transferInFlight, hasRetryBackoff: hasRetryBackoff)
            }
        }
    }

    private func refreshDelivery(transferInFlight: Bool, hasRetryBackoff: Bool, statusOverride: String? = nil) {
        let pending = QueueStore.pending()
        pendingCount = pending.count
        let oldest = pending.min { $0.startedAt < $1.startedAt }?.startedAt
        let lastErrorCode = UserDefaults.standard.string(forKey: Self.lastErrorKey)
        if lastUploadedAt == nil {
            lastUploadedAt = UserDefaults.standard.object(forKey: Self.lastAckKey) as? Date
        }
        let orphans = QueueStore.orphanedAcknowledgedAudioCount()
        let phase = UploadDeliverySnapshot.resolvePhase(
            paired: ReceiverSettings.load() != nil,
            pendingCount: pending.count,
            transferInFlight: transferInFlight,
            authenticationRejected: authenticationRejected,
            hasRetryBackoff: hasRetryBackoff,
            hasLastError: lastErrorCode != nil,
            hasAcknowledgement: lastUploadedAt != nil,
            orphanedAcknowledgedCount: orphans
        )
        let snapshot = UploadDeliverySnapshot(
            phase: phase,
            pendingCount: pending.count,
            oldestQueuedStartedAt: oldest,
            lastAcknowledgedAt: lastUploadedAt,
            lastErrorCode: lastErrorCode,
            orphanedAcknowledgedCount: orphans,
            unverifiedLocalAudioCount: QueueStore.unverifiedLocalAudioCount()
        )
        delivery = snapshot
        if let statusOverride, phase == .uploading {
            status = statusOverride
        } else {
            status = UploadDeliveryCopy.statusLine(for: snapshot)
        }
    }

    private func rememberReceipt(_ id: String) {
        let prior = UserDefaults.standard.stringArray(forKey: Self.recentReceiptsKey) ?? []
        let recent = [id] + prior.filter { $0 != id }
        UserDefaults.standard.set(Array(recent.prefix(10)), forKey: Self.recentReceiptsKey)
        lastStatusCheck = nil
    }

    private func refreshMacProcessingIfDue() {
        assert(Thread.isMainThread)
        guard UIApplication.shared.applicationState == .active, !statusInFlight,
              let settings = ReceiverSettings.load() else { return }
        let ids = (UserDefaults.standard.stringArray(forKey: Self.recentReceiptsKey) ?? [])
            .filter { UUID(uuidString: $0) != nil }
            .prefix(10)
        guard !ids.isEmpty else { return }
        let now = Date()
        if let lastStatusCheck, now.timeIntervalSince(lastStatusCheck) < 60 { return }
        lastStatusCheck = now
        statusInFlight = true
        let generation = statusGeneration
        statusClient.fetch(ids: Array(ids), settings: settings, deviceID: QueueStore.deviceID) { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                self.statusInFlight = false
                guard generation == self.statusGeneration else { return }
                switch result {
                case .success(let response):
                    self.macProcessing = MacProcessingSummary(records: response.chunks, checkedAt: Date(), health: response.health)
                case .failure:
                    self.macProcessing = nil
                }
            }
        }
    }

    static func locationObservationHeader(for chunk: Chunk) -> String? {
        guard let id = chunk.locationObservationID, UUID(uuidString: id)?.uuidString.lowercased() == id else { return nil }
        return id
    }

    private static func timestamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    private static func taskDescription(for id: String) -> String {
        "\(id)|\(Int(Date().timeIntervalSince1970))"
    }

    private static func taskID(_ description: String?) -> String? {
        guard let description, !description.isEmpty else { return nil }
        return String(description.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: true)[0])
    }

    private static func taskStart(_ description: String?) -> Date? {
        guard let description,
              let stamp = description.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: true).dropFirst().first,
              let seconds = TimeInterval(stamp) else { return nil }
        return Date(timeIntervalSince1970: seconds)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        if (responseData[dataTask.taskIdentifier]?.count ?? 0) + data.count > 16384 {
            dataTask.cancel()
            return
        }
        responseData[dataTask.taskIdentifier, default: Data()].append(data)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let data = responseData.removeValue(forKey: task.taskIdentifier) ?? Data()
        guard let id = Self.taskID(task.taskDescription),
              let chunk = QueueStore.pending().first(where: { $0.name == id }) else { pump(); return }
        // Storage receipt only. Ignore any extra processing fields on this response.
        struct Receipt: Decodable { let id: String; let sha256: String; let durable: Bool }
        let response = task.response as? HTTPURLResponse
        if immediateRetryTaskIDs.remove(task.taskIdentifier) != nil {
            retries.removeValue(forKey: id)
            failureCounts.removeValue(forKey: id)
            refreshDelivery(transferInFlight: true, hasRetryBackoff: false, statusOverride: Self.retryStatus)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.pump() }
            return
        }
        if error == nil, let response, [200, 201].contains(response.statusCode),
           let receipt = try? JSONDecoder().decode(Receipt.self, from: data),
           receipt.durable, receipt.id == id, receipt.sha256 == chunk.sha256 {
            rememberReceipt(id)
            do {
                try QueueStore.removeAcknowledged(chunk)
                retries.removeValue(forKey: id)
                failureCounts.removeValue(forKey: id)
                UserDefaults.standard.removeObject(forKey: Self.lastErrorKey)
                lastUploadedAt = Date()
                UserDefaults.standard.set(lastUploadedAt, forKey: Self.lastAckKey)
                refreshDelivery(transferInFlight: false, hasRetryBackoff: false)
            } catch {
                refreshDelivery(transferInFlight: false, hasRetryBackoff: false)
            }
            refreshMacProcessingIfDue()
        } else {
            if let error = error as NSError? {
                UserDefaults.standard.set("\(error.domain):\(error.code)", forKey: Self.lastErrorKey)
            } else if let response {
                UserDefaults.standard.set("HTTP:\(response.statusCode)", forKey: Self.lastErrorKey)
            }
            let failures = (failureCounts[id] ?? 0) + 1
            failureCounts[id] = failures
            retries[id] = Date().addingTimeInterval(min(1800, 15 * pow(2, Double(min(failures, 7)))))
            if response?.statusCode == 401 {
                authenticationRejected = true
            }
            refreshDelivery(transferInFlight: false, hasRetryBackoff: true)
        }
        pump()
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let settings = ReceiverSettings.load(),
              challenge.protectionSpace.host.lowercased() == settings.baseURL.host?.lowercased() else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        if settings.certificateSHA256.isEmpty {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let cert = chain.first else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let hash = SHA256.hash(data: SecCertificateCopyData(cert) as Data)
            .map { String(format: "%02x", $0) }.joined()
        if hash == settings.certificateSHA256 {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else { completionHandler(.cancelAuthenticationChallenge, nil) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        urlSession(session, didReceive: challenge, completionHandler: completionHandler)
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        let completion = backgroundCompletion
        backgroundCompletion = nil
        completion?()
    }
}
