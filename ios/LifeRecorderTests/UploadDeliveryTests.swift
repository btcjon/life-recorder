import XCTest
@testable import LifeRecorder

final class UploadDeliveryTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    func testHealthTransportAcceptsOlderReceiverAndDoesNotInventFailure() throws {
        let old = try JSONDecoder().decode(MacProcessingResponse.self, from: Data("{\"chunks\":[]}".utf8))
        XCTAssertNil(old.health)
        let idle = MacProcessingHealth(version: 1, checked_at: 1000, processing: .init(
            received: 1, complete: 1, pending: 0, needs_attention: 0, retrying: 0,
            oldest_pending_age_seconds: nil, last_received_at: 900, last_completed_at: 950,
            delayed: false, state: "idle"))
        let summary = MacProcessingSummary(records: [], checkedAt: now, health: idle)
        XCTAssertFalse(summary.displayText.contains("delayed"))
        XCTAssertFalse(summary.displayText.contains("need attention"))
        XCTAssertFalse(summary.displayText.contains("retrying"))
    }

    func testHealthTransportShowsDeviceQueueBeyondRecentReceipts() throws {
        let payload = Data("""
        {"chunks":[],"health":{"version":1,"checked_at":1000,"processing":{
          "received":12,"complete":4,"pending":6,"needs_attention":2,"retrying":3,
          "oldest_pending_age_seconds":600,"last_received_at":900,"last_completed_at":850,
          "delayed":true,"state":"needs_attention"}}}
        """.utf8)
        let response = try JSONDecoder().decode(MacProcessingResponse.self, from: payload)
        let summary = MacProcessingSummary(records: response.chunks, checkedAt: now, health: response.health)
        XCTAssertTrue(summary.displayText.contains("2 uploads from this phone need attention"))
        XCTAssertTrue(summary.displayText.contains("Mac processing is delayed"))
        XCTAssertTrue(summary.displayText.contains("10 minutes ago"))
        XCTAssertTrue(summary.displayText.contains("3 uploads from this phone are retrying"))
    }

    func testMacStatusUsesOnlyVerifiedProcessingRecords() throws {
        let payload = Data("""
        [{"id":"a","status":"complete","attempts":0},
         {"id":"b","status":"pending","attempts":1},
         {"id":"c","status":"needs_attention","attempts":5,"error_code":"decode_empty","retry_eligible":true},
         {"id":"d","status":"unknown"}]
        """.utf8)
        let records = try JSONDecoder().decode([MacProcessingRecord].self, from: payload)
        let summary = MacProcessingSummary(records: records, checkedAt: now)
        XCTAssertTrue(summary.displayText.contains("1 processed"))
        XCTAssertTrue(summary.displayText.contains("1 processing"))
        XCTAssertTrue(summary.displayText.contains("1 need attention on Mac"))
        XCTAssertTrue(summary.displayText.contains("1 not found on Mac"))
        XCTAssertFalse(summary.displayText.contains("decode_empty"))
        XCTAssertFalse(summary.displayText.contains("a receipt"))
    }

    func testOldestQueuedClipAgeUsesCaptureStart() {
        XCTAssertNil(UploadDeliveryCopy.oldestQueuedAgeText(startedAt: nil, now: now))
        XCTAssertEqual(
            UploadDeliveryCopy.oldestQueuedAgeText(startedAt: now.addingTimeInterval(30), now: now),
            "Oldest queued clip started under a minute ago."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.oldestQueuedAgeText(startedAt: now.addingTimeInterval(-59), now: now),
            "Oldest queued clip started under a minute ago."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.oldestQueuedAgeText(startedAt: now.addingTimeInterval(-60), now: now),
            "Oldest queued clip started 1 minute ago."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.oldestQueuedAgeText(startedAt: now.addingTimeInterval(-120), now: now),
            "Oldest queued clip started 2 minutes ago."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.oldestQueuedAgeText(startedAt: now.addingTimeInterval(-3600), now: now),
            "Oldest queued clip started 1 hour ago."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.oldestQueuedAgeText(startedAt: now.addingTimeInterval(-(2 * 3600 + 14 * 60)), now: now),
            "Oldest queued clip started 2h 14m ago."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.oldestQueuedAgeText(startedAt: now.addingTimeInterval(-(49 * 3600)), now: now),
            "Oldest queued clip started 2d 1h ago."
        )
    }

    func testErrorExplanationStaysSafeAndSpecific() {
        XCTAssertEqual(
            UploadDeliveryCopy.errorExplanation(for: "HTTP:401"),
            "Pairing token rejected. Audio remains on this phone."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.errorExplanation(for: "HTTP:409"),
            "Receiver reported a chunk conflict. Audio remains on this phone."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.errorExplanation(for: "HTTP:507"),
            "Mac storage is full. Audio remains on this phone."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.errorExplanation(for: "HTTP:422"),
            "The Mac rejected a clip checksum. Audio remains on this phone."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.errorExplanation(for: "local:missing-audio"),
            "A pending audio file is missing; its record has been retained."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.errorExplanation(for: "NSURLErrorDomain:-1009"),
            "Cannot reach the Mac right now. Audio remains on this phone and will retry."
        )
        XCTAssertEqual(
            UploadDeliveryCopy.errorExplanation(for: "NSURLErrorDomain:-1202"),
            "The Mac certificate did not match the saved pin. Audio remains on this phone."
        )
        XCTAssertNil(UploadDeliveryCopy.errorExplanation(for: nil))
        XCTAssertNil(UploadDeliveryCopy.errorExplanation(for: ""))
        let poisoned = "Bearer secret-token HTTP body /tmp/private"
        let fallback = UploadDeliveryCopy.errorExplanation(for: poisoned)
        XCTAssertEqual(fallback, "The upload did not finish. Audio remains on this phone.")
        XCTAssertFalse(fallback?.contains("secret-token") ?? true)
        XCTAssertFalse(fallback?.contains("Bearer") ?? true)
        XCTAssertFalse(fallback?.contains("/tmp") ?? true)
    }

    func testPhasesDistinguishLocalQueueFromMacAcknowledgement() {
        XCTAssertEqual(phase(paired: false, pendingCount: 2), .needsPairing)
        XCTAssertEqual(phase(pendingCount: 2), .queuedLocally)
        XCTAssertEqual(phase(pendingCount: 2, transferInFlight: true), .uploading)
        XCTAssertEqual(phase(pendingCount: 2, hasRetryBackoff: true, hasLastError: true), .waitingToRetry)
        XCTAssertEqual(phase(pendingCount: 1, authenticationRejected: true, hasLastError: true), .blocked)
        XCTAssertEqual(phase(hasAcknowledgement: true), .acknowledged)
        XCTAssertEqual(phase(transferInFlight: true, hasAcknowledgement: true), .acknowledged)
        XCTAssertEqual(phase(orphanedAcknowledgedCount: 1), .acknowledgedPendingCleanup)
        XCTAssertEqual(phase(), .idle)
        XCTAssertEqual(
            phase(pendingCount: 1, transferInFlight: true, orphanedAcknowledgedCount: 1),
            .uploading
        )
    }

    func testPanelCopyShowsQueuedAgeAndErrorWithoutClaimingTranscription() throws {
        let queued = UploadDeliverySnapshot(
            phase: .waitingToRetry,
            pendingCount: 3,
            oldestQueuedStartedAt: now.addingTimeInterval(-(2 * 3600 + 14 * 60)),
            lastAcknowledgedAt: now.addingTimeInterval(-86_400),
            lastErrorCode: "HTTP:507",
            orphanedAcknowledgedCount: 0
        )
        let queuedLines = UploadPanelCopy.lines(snapshot: queued, now: now, incompleteClips: 0)
        XCTAssertEqual(queuedLines.first(where: { $0.id == "oldestQueuedAge" })?.text,
                       "Oldest queued clip started 2h 14m ago.")
        XCTAssertEqual(queuedLines.first(where: { $0.id == "uploadStatusExplanation" })?.text,
                       "Mac storage is full. Audio remains on this phone.")
        XCTAssertTrue(queuedLines.contains { $0.id == "uploadHeadline" && $0.text == "3 clips waiting on this phone" })
        XCTAssertTrue(queuedLines.contains { $0.id == "uploadStatus" && $0.text.contains("on this phone") })
        XCTAssertTrue(queuedLines.contains { $0.id == MacProcessingStatusSlot.accessibilityID })

        let acked = UploadDeliverySnapshot(
            phase: .acknowledged,
            pendingCount: 0,
            oldestQueuedStartedAt: nil,
            lastAcknowledgedAt: now,
            lastErrorCode: nil,
            orphanedAcknowledgedCount: 0
        )
        let ackedLines = UploadPanelCopy.lines(snapshot: acked, now: now, incompleteClips: 0)
        let ackedStatus = try XCTUnwrap(ackedLines.first { $0.id == "uploadStatus" }?.text)
        XCTAssertTrue(ackedStatus.contains("acknowledged"))
        XCTAssertTrue(ackedStatus.contains("not a transcription"))
        XCTAssertTrue(ackedLines.contains {
            $0.id == "lastAcknowledged" && $0.text.contains("Transcription status is separate")
        })
        XCTAssertEqual(ackedLines.first { $0.id == "uploadHeadline" }?.text, "No clips waiting on this phone")
        XCTAssertFalse(ackedLines.contains { $0.id == "oldestQueuedAge" })
        XCTAssertEqual(
            MacProcessingStatusSlot.accessibilityLabel,
            "Mac processing status unavailable. An upload receipt does not mean transcription is finished."
        )
        assertDoesNotClaimTranscription(queuedLines + ackedLines)

        let blocked = UploadDeliverySnapshot(
            phase: .blocked,
            pendingCount: 1,
            oldestQueuedStartedAt: now.addingTimeInterval(-90),
            lastAcknowledgedAt: nil,
            lastErrorCode: "HTTP:401",
            orphanedAcknowledgedCount: 0
        )
        let blockedLines = UploadPanelCopy.lines(snapshot: blocked, now: now, incompleteClips: 2)
        XCTAssertTrue(blockedLines.contains {
            $0.id == "uploadStatus" && $0.text.contains("Pairing token rejected")
        })
        XCTAssertFalse(blockedLines.contains { $0.id == "uploadStatusExplanation" })
        XCTAssertTrue(blockedLines.contains { $0.id == "incompleteClips" && $0.text.contains("remain on this phone") })
        assertDoesNotClaimTranscription(blockedLines)
    }

    func testUploadTransportKeepsBackgroundCellularRetry() {
        XCTAssertEqual(UploadTransportPolicy.sessionID, UploadManager.sessionID)
        XCTAssertFalse(UploadTransportPolicy.isDiscretionary)
        XCTAssertTrue(UploadTransportPolicy.sessionSendsLaunchEvents)
        XCTAssertTrue(UploadTransportPolicy.allowsCellularAccess)
        XCTAssertTrue(UploadTransportPolicy.waitsForConnectivity)
        XCTAssertEqual(UploadTransportPolicy.resourceTimeout, 30 * 60)
    }

    func testUnverifiedLocalAudioIsReportedWithoutClaimingUpload() {
        let snapshot = UploadDeliverySnapshot(
            phase: .idle,
            pendingCount: 0,
            oldestQueuedStartedAt: nil,
            lastAcknowledgedAt: nil,
            lastErrorCode: nil,
            orphanedAcknowledgedCount: 0,
            unverifiedLocalAudioCount: 1
        )
        let lines = UploadPanelCopy.lines(snapshot: snapshot, now: now, incompleteClips: 0)
        let warning = lines.first { $0.id == "unverifiedLocalAudio" }
        XCTAssertNotNil(warning)
        XCTAssertTrue(warning?.text.contains("no verified upload record") ?? false)
        XCTAssertFalse(warning?.text.contains("uploaded") ?? true)
    }

    private func phase(
        paired: Bool = true,
        pendingCount: Int = 0,
        transferInFlight: Bool = false,
        authenticationRejected: Bool = false,
        hasRetryBackoff: Bool = false,
        hasLastError: Bool = false,
        hasAcknowledgement: Bool = false,
        orphanedAcknowledgedCount: Int = 0
    ) -> UploadDeliverySnapshot.Phase {
        UploadDeliverySnapshot.resolvePhase(
            paired: paired,
            pendingCount: pendingCount,
            transferInFlight: transferInFlight,
            authenticationRejected: authenticationRejected,
            hasRetryBackoff: hasRetryBackoff,
            hasLastError: hasLastError,
            hasAcknowledgement: hasAcknowledgement,
            orphanedAcknowledgedCount: orphanedAcknowledgedCount
        )
    }

    private func assertDoesNotClaimTranscription(_ lines: [UploadPanelLine]) {
        let forbidden = [
            "transcribed",
            "transcription is done",
            "transcription complete",
            "processing complete",
            "processing is done",
            "processed"
        ]
        for line in lines {
            let combined = (line.text + " " + line.accessibilityLabel).lowercased()
            for phrase in forbidden {
                XCTAssertFalse(combined.contains(phrase), line.text)
            }
        }
    }
}
