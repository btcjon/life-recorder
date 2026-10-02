import XCTest
@testable import LifeRecorder

final class LocationContextTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_700_000_000)

    func testFreshnessRejectsFutureAndOlderThanFiveMinutes() {
        XCTAssertTrue(LocationContextPolicy.isFresh(capturedAt: now.addingTimeInterval(-300), now: now))
        XCTAssertFalse(LocationContextPolicy.isFresh(capturedAt: now.addingTimeInterval(-301), now: now))
        XCTAssertFalse(LocationContextPolicy.isFresh(capturedAt: now.addingTimeInterval(1), now: now))
    }

    func testCadenceUsesFifteenMinutesStationaryAndMinuteMoving() {
        XCTAssertFalse(LocationContextPolicy.maySend(lastSentAt: now.addingTimeInterval(-899), now: now, activity: "stationary", foregroundOneShot: false))
        XCTAssertTrue(LocationContextPolicy.maySend(lastSentAt: now.addingTimeInterval(-900), now: now, activity: "stationary", foregroundOneShot: false))
        XCTAssertFalse(LocationContextPolicy.maySend(lastSentAt: now.addingTimeInterval(-59), now: now, activity: "vehicle", foregroundOneShot: false))
        XCTAssertTrue(LocationContextPolicy.maySend(lastSentAt: now.addingTimeInterval(-60), now: now, activity: "walking", foregroundOneShot: false))
        XCTAssertTrue(LocationContextPolicy.maySend(lastSentAt: now, now: now, activity: "stationary", foregroundOneShot: true))
    }

    func testClipAssociationNeverUsesFutureFixButRetainsStalePointer() {
        let captured = now.addingTimeInterval(-600)
        XCTAssertEqual(LocationContextPolicy.associatedObservationID(id: "observation", capturedAt: captured, clipStart: now, now: now), "observation")
        XCTAssertNil(LocationContextPolicy.associatedObservationID(id: "observation", capturedAt: now, clipStart: now.addingTimeInterval(-1), now: now))
        XCTAssertNil(LocationContextPolicy.associatedObservationID(id: "observation", capturedAt: now.addingTimeInterval(-86401), clipStart: now, now: now))
        XCTAssertNil(LocationContextPolicy.associatedObservationID(id: nil, capturedAt: nil, clipStart: now, now: now))
    }

    func testVehicleLabelDoesNotClaimDriving() {
        XCTAssertEqual(LocationContextPolicy.activityLabel("vehicle"), "In a vehicle")
        XCTAssertEqual(LocationContextPolicy.activityLabel("running"), "Activity unknown")
    }

    func testRevocationClearsUnsentAndStopsSensorsWithoutDeletingServerHistory() {
        let policy = LocationContextPolicy.revocation
        XCTAssertTrue(policy.stopLocation)
        XCTAssertTrue(policy.stopMotion)
        XCTAssertTrue(policy.clearUnsentLocations)
        XCTAssertTrue(policy.forgetLastObservation)
        XCTAssertFalse(policy.deleteMacHistory)
    }

    func testViewerAddressRejectsEmbeddedCredentialsAndTokens() {
        XCTAssertNotNil(LocationContextPolicy.viewerURL("https://lr.genr8ive.ai"))
        XCTAssertNil(LocationContextPolicy.viewerURL("http://lr.genr8ive.ai"))
        XCTAssertNil(LocationContextPolicy.viewerURL("https://user:secret@lr.genr8ive.ai"))
        XCTAssertNil(LocationContextPolicy.viewerURL("https://lr.genr8ive.ai/?token=secret"))
        XCTAssertNil(LocationContextPolicy.viewerURL("https://lr.genr8ive.ai/#token"))
    }

    func testObservationPayloadHasExplicitSourceAndOptionalActivity() {
        let observation = LocationContextObservation(id: "test", capturedAt: now, latitude: 1, longitude: 2,
            accuracy: 100, source: "foreground", activity: nil, confidence: nil)
        let payload = observation.payload["observation"] as? [String: Any]
        XCTAssertEqual(payload?["source"] as? String, "foreground")
        XCTAssertEqual(payload?["status"] as? String, "observed")
        XCTAssertNil(payload?["activity"])
        XCTAssertNil(observation.payload["token"])
    }
}
