import Combine
import CryptoKit
import Foundation
import Security
import UIKit

final class UploadManager: NSObject, ObservableObject, URLSessionDataDelegate, URLSessionTaskDelegate {
    static let shared = UploadManager()
    static let sessionID = "com.browseruse.liferecorder.uploads"
    @Published private(set) var pendingCount = 0
    @Published private(set) var status = "Pair with your Mac to upload"
    @Published private(set) var lastUploadedAt: Date?
    var backgroundCompletion: (() -> Void)?
    private var responseData: [Int: Data] = [:]
    private var retries: [String: Date] = [:]
    private var failureCounts: [String: Int] = [:]
    private var immediateRetryTaskIDs = Set<Int>()
    private var pumping = false
    private var authenticationRejected = false
    private var timer: Timer?
    private static let staleTaskInterval: TimeInterval = 60
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.sessionID)
        config.isDiscretionary = false
        config.sessionSendsLaunchEvents = true
        config.allowsCellularAccess = true
        config.waitsForConnectivity = true
        config.timeoutIntervalForRequest = 120
        // A connectivity-waiting task must not occupy one of the two upload
        // slots for days if the LAN route or hostname becomes stale.
        config.timeoutIntervalForResource = 30 * 60
        config.httpMaximumConnectionsPerHost = 2
        config.tlsMinimumSupportedProtocolVersion = .TLSv12
        config.tlsMaximumSupportedProtocolVersion = .TLSv13
        return URLSession(configuration: config, delegate: self, delegateQueue: .main)
    }()

    private override init() {
        super.init()
    }

    func activate() {
        _ = session // Reattach transfers if iOS launched us to deliver an upload result.
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.pump() }
        }
        pump()
    }

    func configurationChanged() {
        authenticationRejected = false
        retries.removeAll()
        failureCounts.removeAll()
        retryNow()
    }

    func retryNow() {
        authenticationRejected = false
        retries.removeAll()
        failureCounts.removeAll()
        status = "Retrying uploads now"
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
        guard let settings = ReceiverSettings.load() else { status = "Pair with your Mac to upload"; return }
        guard !authenticationRejected else { return }
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
                for chunk in pending where !active.contains(chunk.name) {
                    guard available > 0 else { break }
                    if let retry = self.retries[chunk.name], retry > Date() { continue }
                    guard FileManager.default.fileExists(atPath: chunk.audioURL.path) else {
                        self.status = "A pending audio file is missing; its record has been retained"
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
                if pending.isEmpty { self.status = "All completed audio uploaded" }
                else if active.isEmpty && available == 2 { /* Keep the latest actionable error. */ }
                else { self.status = "Uploading when connected" }
            }
        }
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
        struct Receipt: Decodable { let id: String; let sha256: String; let durable: Bool }
        let response = task.response as? HTTPURLResponse
        if immediateRetryTaskIDs.remove(task.taskIdentifier) != nil {
            retries.removeValue(forKey: id)
            failureCounts.removeValue(forKey: id)
            status = "Retrying uploads now"
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.pump() }
            return
        }
        if error == nil, let response, [200, 201].contains(response.statusCode),
           let receipt = try? JSONDecoder().decode(Receipt.self, from: data),
           receipt.durable, receipt.id == id, receipt.sha256 == chunk.sha256 {
            do {
                try QueueStore.removeAcknowledged(chunk)
                retries.removeValue(forKey: id)
                failureCounts.removeValue(forKey: id)
                UserDefaults.standard.removeObject(forKey: "lastUploadError")
                lastUploadedAt = Date()
                status = "Uploaded safely; local copy removed"
            } catch { status = "Uploaded; local cleanup will retry on reopening" }
        } else {
            if let error = error as NSError? {
                UserDefaults.standard.set("\(error.domain):\(error.code)", forKey: "lastUploadError")
            } else if let response {
                UserDefaults.standard.set("HTTP:\(response.statusCode)", forKey: "lastUploadError")
            }
            let failures = (failureCounts[id] ?? 0) + 1
            failureCounts[id] = failures
            retries[id] = Date().addingTimeInterval(min(1800, 15 * pow(2, Double(min(failures, 7)))))
            switch response?.statusCode {
            case 401:
                authenticationRejected = true
                status = "Pairing token rejected. Audio remains on this phone."
            case 409: status = "Receiver reported a chunk conflict. Audio remains on this phone."
            case 507: status = "Mac storage is full. Audio remains on this phone."
            default: status = "Waiting to upload. Audio remains on this phone."
            }
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
