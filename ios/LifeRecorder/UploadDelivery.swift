import CryptoKit
import Security
import SwiftUI

/// Facts the upload panel can show from the local queue and the storage receipt.
///
/// `POST /v1/chunks/{id}` returns `{id, sha256, durable}` and only proves the Mac
/// stored the audio. Processing state is intentionally absent: see `MacProcessingStatusSlot`.
struct UploadDeliverySnapshot: Equatable {
    enum Phase: String, Equatable {
        case needsPairing
        case queuedLocally
        case uploading
        case waitingToRetry
        case blocked
        case acknowledged
        case acknowledgedPendingCleanup
        case idle
    }

    var phase: Phase
    var pendingCount: Int
    var oldestQueuedStartedAt: Date?
    var lastAcknowledgedAt: Date?
    var lastErrorCode: String?
    var orphanedAcknowledgedCount: Int
    var unverifiedLocalAudioCount: Int = 0

    static func resolvePhase(
        paired: Bool,
        pendingCount: Int,
        transferInFlight: Bool,
        authenticationRejected: Bool,
        hasRetryBackoff: Bool,
        hasLastError: Bool,
        hasAcknowledgement: Bool,
        orphanedAcknowledgedCount: Int
    ) -> Phase {
        if !paired { return .needsPairing }
        if authenticationRejected { return .blocked }
        if transferInFlight && pendingCount > 0 { return .uploading }
        if pendingCount > 0 && (hasRetryBackoff || hasLastError) { return .waitingToRetry }
        if pendingCount > 0 { return .queuedLocally }
        if orphanedAcknowledgedCount > 0 { return .acknowledgedPendingCleanup }
        if hasAcknowledgement { return .acknowledged }
        return .idle
    }
}

enum UploadDeliveryCopy {
    static func headline(pendingCount: Int) -> String {
        switch pendingCount {
        case 0: return "No clips waiting on this phone"
        case 1: return "1 clip waiting on this phone"
        default: return "\(pendingCount) clips waiting on this phone"
        }
    }

    /// Age of the oldest queued clip from its capture start. Nil when nothing is queued.
    static func oldestQueuedAgeText(startedAt: Date?, now: Date) -> String? {
        guard let startedAt else { return nil }
        let seconds = max(0, now.timeIntervalSince(startedAt))
        return "Oldest queued clip started \(compactAge(seconds)) ago."
    }

    static func compactAge(_ seconds: TimeInterval) -> String {
        let total = Int(max(0, seconds).rounded(.down))
        if total < 60 { return "under a minute" }
        let minutes = total / 60
        if minutes < 60 { return minutes == 1 ? "1 minute" : "\(minutes) minutes" }
        let hours = minutes / 60
        let remainderMinutes = minutes % 60
        if hours < 48 {
            if remainderMinutes == 0 { return hours == 1 ? "1 hour" : "\(hours) hours" }
            return "\(hours)h \(remainderMinutes)m"
        }
        let days = hours / 24
        let remainderHours = hours % 24
        if remainderHours == 0 { return days == 1 ? "1 day" : "\(days) days" }
        return "\(days)d \(remainderHours)h"
    }

    /// User-facing text for a stored code (`HTTP:<status>`, `local:missing-audio`, or `Domain:<nserror>`).
    /// The stored value is never copied into the sentence, so a bad code cannot surface a token or body.
    static func errorExplanation(for code: String?) -> String? {
        guard let code, !code.isEmpty else { return nil }
        if code == "local:missing-audio" {
            return "A pending audio file is missing; its record has been retained."
        }
        if code.hasPrefix("HTTP:"), let status = Int(code.dropFirst("HTTP:".count)), (100...599).contains(status) {
            switch status {
            case 401: return "Pairing token rejected. Audio remains on this phone."
            case 409: return "Receiver reported a chunk conflict. Audio remains on this phone."
            case 413: return "A clip was too large for the Mac. Audio remains on this phone."
            case 422: return "The Mac rejected a clip checksum. Audio remains on this phone."
            case 503: return "The Mac could not store audio just now. Audio remains on this phone."
            case 507: return "Mac storage is full. Audio remains on this phone."
            default: return "The Mac did not accept the upload (HTTP \(status)). Audio remains on this phone."
            }
        }
        guard let nsCode = networkErrorCode(code) else {
            return "The upload did not finish. Audio remains on this phone."
        }
        switch nsCode {
        case -1009, -1005, -1001, -1004, -1003, -1020:
            return "Cannot reach the Mac right now. Audio remains on this phone and will retry."
        case -1200, -1202, -1203, -1204, -1205, -1206:
            return "The Mac certificate did not match the saved pin. Audio remains on this phone."
        case -999:
            return "The upload was interrupted. Audio remains on this phone and will retry."
        default:
            return "The upload did not finish. Audio remains on this phone and will retry."
        }
    }

    private static func networkErrorCode(_ code: String) -> Int? {
        let parts = code.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2, let status = Int(parts[1]) else { return nil }
        let domain = parts[0]
        guard !domain.isEmpty, domain.count <= 80, domain.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "." }) else {
            return nil
        }
        return status
    }

    static func statusLine(for snapshot: UploadDeliverySnapshot) -> String {
        switch snapshot.phase {
        case .needsPairing:
            return snapshot.pendingCount > 0
                ? "Pair with your Mac to upload. Recorded clips stay on this phone."
                : "Pair with your Mac to upload."
        case .queuedLocally:
            return snapshot.pendingCount == 1
                ? "This clip is on this phone until the Mac acknowledges it."
                : "These clips are on this phone until the Mac acknowledges them."
        case .uploading:
            return "Sending to the Mac. Clips stay on this phone until acknowledged."
        case .waitingToRetry:
            return "Waiting to retry. Clips stay on this phone until the Mac acknowledges them."
        case .blocked:
            return errorExplanation(for: snapshot.lastErrorCode)
                ?? "Uploads are paused. Audio remains on this phone."
        case .acknowledged:
            return "The Mac acknowledged the last upload and this phone removed its copy. That receipt is not a transcription."
        case .acknowledgedPendingCleanup:
            return "The Mac acknowledged the last upload. This phone still has a local copy to remove. That receipt is not a transcription."
        case .idle:
            return "Nothing is waiting on this phone."
        }
    }

    static func acknowledgedLine(at date: Date) -> String {
        "Mac receipt \(date.formatted(date: .omitted, time: .shortened)): audio stored. Transcription status is separate."
    }

    static func cleanupLine(count: Int) -> String {
        count == 1
            ? "1 clip the Mac already acknowledged is still on this phone until cleanup finishes."
            : "\(count) clips the Mac already acknowledged are still on this phone until cleanup finishes."
    }

    static func unverifiedAudioLine(count: Int) -> String {
        count == 1
            ? "1 audio file has no verified upload record. It is kept on this phone for manual recovery."
            : "\(count) audio files have no verified upload record. They are kept on this phone for manual recovery."
    }
}

struct UploadPanelLine: Equatable, Identifiable {
    enum Role: Equatable {
        case headline
        case status
        case detail
        case error
        case notice
    }

    var id: String
    var text: String
    var accessibilityLabel: String
    var role: Role
}

enum UploadPanelCopy {
    static func lines(snapshot: UploadDeliverySnapshot, now: Date, incompleteClips: Int,
                      macStatus: MacProcessingSummary? = nil) -> [UploadPanelLine] {
        var lines: [UploadPanelLine] = []
        let headline = UploadDeliveryCopy.headline(pendingCount: snapshot.pendingCount)
        lines.append(UploadPanelLine(id: "uploadHeadline", text: headline, accessibilityLabel: headline, role: .headline))
        if let age = UploadDeliveryCopy.oldestQueuedAgeText(startedAt: snapshot.oldestQueuedStartedAt, now: now) {
            lines.append(UploadPanelLine(id: "oldestQueuedAge", text: age, accessibilityLabel: age, role: .detail))
        }
        let status = UploadDeliveryCopy.statusLine(for: snapshot)
        lines.append(UploadPanelLine(id: "uploadStatus", text: status, accessibilityLabel: status, role: .status))
        if snapshot.phase != .blocked,
           let explanation = UploadDeliveryCopy.errorExplanation(for: snapshot.lastErrorCode),
           explanation != status {
            lines.append(UploadPanelLine(
                id: "uploadStatusExplanation",
                text: explanation,
                accessibilityLabel: explanation,
                role: .error
            ))
        }
        if let date = snapshot.lastAcknowledgedAt {
            let receipt = UploadDeliveryCopy.acknowledgedLine(at: date)
            lines.append(UploadPanelLine(id: "lastAcknowledged", text: receipt, accessibilityLabel: receipt, role: .detail))
        }
        if snapshot.orphanedAcknowledgedCount > 0 && snapshot.phase != .acknowledgedPendingCleanup {
            let cleanup = UploadDeliveryCopy.cleanupLine(count: snapshot.orphanedAcknowledgedCount)
            lines.append(UploadPanelLine(id: "acknowledgedCleanup", text: cleanup, accessibilityLabel: cleanup, role: .detail))
        }
        if snapshot.unverifiedLocalAudioCount > 0 {
            let warning = UploadDeliveryCopy.unverifiedAudioLine(count: snapshot.unverifiedLocalAudioCount)
            lines.append(UploadPanelLine(id: "unverifiedLocalAudio", text: warning,
                                         accessibilityLabel: warning, role: .error))
        }
        lines.append(UploadPanelLine(
            id: MacProcessingStatusSlot.accessibilityID,
            text: macStatus?.displayText ?? MacProcessingStatusSlot.notice,
            accessibilityLabel: macStatus?.accessibilityLabel ?? MacProcessingStatusSlot.accessibilityLabel,
            role: .notice
        ))
        if incompleteClips > 0 {
            let recovery = incompleteClips == 1
                ? "1 interrupted clip needs recovery. It remains on this phone."
                : "\(incompleteClips) interrupted clips need recovery. They remain on this phone."
            lines.append(UploadPanelLine(id: "incompleteClips", text: recovery, accessibilityLabel: recovery, role: .error))
        }
        return lines
    }
}

struct MacProcessingRecord: Decodable, Equatable {
    let id: String
    let status: String
    let attempts: Int?
    let error_code: String?
    let retry_eligible: Bool?
}

struct MacProcessingSummary: Equatable {
    let records: [MacProcessingRecord]
    let checkedAt: Date
    var health: MacProcessingHealth? = nil

    var displayText: String {
        let complete = records.filter { $0.status == "complete" }.count
        let pending = records.filter { $0.status == "pending" }.count
        let attention = records.filter { $0.status == "needs_attention" }.count
        let unknown = records.count - complete - pending - attention
        var parts: [String] = []
        if complete > 0 { parts.append("\(complete) processed") }
        if pending > 0 { parts.append("\(pending) processing") }
        if attention > 0 { parts.append("\(attention) need attention on Mac") }
        if unknown > 0 { parts.append("\(unknown) not found on Mac") }
        let recent = "Mac processing (recent uploads): " + (parts.isEmpty ? "no recent clips" : parts.joined(separator: " · "))
        guard let health else { return recent }
        let queue = health.processing
        var notices: [String] = []
        if queue.needs_attention > 0 {
            notices.append("\(queue.needs_attention) uploads from this phone need attention on Mac.")
        }
        if queue.delayed, let age = queue.oldest_pending_age_seconds {
            notices.append("Mac processing is delayed; oldest pending upload from this phone was received \(UploadDeliveryCopy.compactAge(age)) ago.")
        }
        if queue.retrying > 0 {
            notices.append("\(queue.retrying) uploads from this phone are retrying processing.")
        }
        return ([recent] + notices).joined(separator: " ")
    }

    var accessibilityLabel: String { displayText }
}

/// Receiver facts are scoped to this phone, independent of the ten receipt IDs.
struct MacProcessingHealth: Decodable, Equatable {
    struct Processing: Decodable, Equatable {
        let received: Int
        let complete: Int
        let pending: Int
        let needs_attention: Int
        let retrying: Int
        let oldest_pending_age_seconds: Double?
        let last_received_at: Double?
        let last_completed_at: Double?
        let delayed: Bool
        let state: String
    }
    let version: Int
    let checked_at: Double
    let processing: Processing
}

struct MacProcessingResponse: Decodable {
    let chunks: [MacProcessingRecord]
    let health: MacProcessingHealth?
}

/// Foreground-only status reads use the same pinned HTTPS identity as uploads.
final class MacProcessingStatusClient: NSObject, URLSessionDelegate {
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.allowsCellularAccess = true
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 15
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    func fetch(ids: [String], settings: ReceiverSettings, deviceID: String,
               completion: @escaping (Result<MacProcessingResponse, Error>) -> Void) {
        let bounded = Array(ids.prefix(10))
        guard !bounded.isEmpty,
              var components = URLComponents(url: settings.baseURL.appendingPathComponent("v1/chunks/status"),
                                             resolvingAgainstBaseURL: false) else {
            completion(.failure(URLError(.badURL)))
            return
        }
        components.queryItems = [URLQueryItem(name: "ids", value: bounded.joined(separator: ","))]
        guard let url = components.url else {
            completion(.failure(URLError(.badURL)))
            return
        }
        var request = URLRequest(url: url)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("Bearer " + settings.token, forHTTPHeaderField: "Authorization")
        request.setValue(deviceID, forHTTPHeaderField: "X-Device-ID")
        session.dataTask(with: request) { data, response, error in
            if let error { completion(.failure(error)); return }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let data, data.count <= 16_384,
                  let envelope = try? JSONDecoder().decode(MacProcessingResponse.self, from: data),
                  envelope.chunks.count == bounded.count,
                  Set(envelope.chunks.map(\.id)) == Set(bounded) else {
                completion(.failure(URLError(.badServerResponse)))
                return
            }
            completion(.success(envelope))
        }.resume()
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
        completionHandler(hash == settings.certificateSHA256
                          ? .useCredential : .cancelAuthenticationChallenge,
                          hash == settings.certificateSHA256 ? URLCredential(trust: trust) : nil)
    }
}

struct MacProcessingStatusSlot: View {
    static let accessibilityID = "macProcessingStatusSlot"
    static let notice = "Mac processing status unavailable. A receipt only means the audio was stored."
    static let accessibilityLabel = "Mac processing status unavailable. An upload receipt does not mean transcription is finished."
    var summary: MacProcessingSummary? = nil

    var body: some View {
        Text(summary?.displayText ?? Self.notice)
            .font(.footnote)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityIdentifier(Self.accessibilityID)
            .accessibilityLabel(summary?.accessibilityLabel ?? Self.accessibilityLabel)
    }
}

struct UploadPanel: View {
    let snapshot: UploadDeliverySnapshot
    var macStatus: MacProcessingSummary? = nil
    var incompleteClips: Int = 0
    var now: Date? = nil
    let onRetry: () -> Void
    @Environment(\.horizontalSizeClass) private var sizeClass

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            let lines = UploadPanelCopy.lines(
                snapshot: snapshot,
                now: now ?? context.date,
                incompleteClips: incompleteClips,
                macStatus: macStatus
            )
            VStack(alignment: .leading, spacing: 8) {
                ForEach(lines) { line in
                    if line.id == MacProcessingStatusSlot.accessibilityID {
                        MacProcessingStatusSlot(summary: macStatus)
                    } else {
                        row(line)
                    }
                }
                if snapshot.pendingCount > 0 {
                    Button(action: onRetry) {
                        Label("Retry uploads now", systemImage: "arrow.clockwise")
                            .font(.subheadline.weight(.semibold))
                            .frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("retryUploads")
                    .accessibilityHint("Tries the upload again. Clips stay on this phone until the Mac acknowledges them.")
                }
            }
        }
        .padding(sizeClass == .regular ? 16 : 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("uploadPanel")
    }

    @ViewBuilder
    private func row(_ line: UploadPanelLine) -> some View {
        Group {
            if line.role == .headline {
                Label(line.text, systemImage: "tray")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
            } else {
                Text(line.text)
                    .font(.footnote)
                    .foregroundStyle(color(for: line.role))
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .multilineTextAlignment(.leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityIdentifier(line.id)
        .accessibilityLabel(line.accessibilityLabel)
    }

    private func color(for role: UploadPanelLine.Role) -> Color {
        switch role {
        case .error: return .orange
        case .status, .headline: return .primary
        case .detail, .notice: return .secondary
        }
    }
}
