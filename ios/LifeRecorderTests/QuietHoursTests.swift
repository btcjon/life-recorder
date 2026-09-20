import XCTest
@testable import LifeRecorder

final class QuietHoursTests: XCTestCase {
    private let zone = TimeZone(identifier: "America/New_York")!

    private func date(_ iso: String) -> Date {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: iso)!
    }

    func testQuietHoursCoverTenPMToFiveAMEastern() {
        XCTAssertTrue(QuietHours.isActive(at: date("2026-01-15T03:00:00Z"), timeZone: zone)) // 10 PM EST
        XCTAssertTrue(QuietHours.isActive(at: date("2026-01-15T09:59:00Z"), timeZone: zone)) // 4:59 AM EST
        XCTAssertFalse(QuietHours.isActive(at: date("2026-01-15T10:00:00Z"), timeZone: zone)) // 5:00 AM EST
        XCTAssertFalse(QuietHours.isActive(at: date("2026-01-15T21:00:00Z"), timeZone: zone)) // 4:00 PM EST
        XCTAssertTrue(QuietHours.isActive(at: date("2026-07-15T02:00:00Z"), timeZone: zone)) // 10 PM EDT
        XCTAssertFalse(QuietHours.isActive(at: date("2026-07-15T09:00:00Z"), timeZone: zone)) // 5 AM EDT
    }

    func testNextTransitionIsFiveAMDuringQuietHoursAndTenPMOtherwise() {
        let evening = date("2026-01-15T03:30:00Z") // 10:30 PM EST Jan 14
        XCTAssertEqual(QuietHours.nextTransition(from: evening, timeZone: zone), date("2026-01-15T10:00:00Z"))
        let preDawn = date("2026-01-15T08:00:00Z") // 3 AM EST
        XCTAssertEqual(QuietHours.nextTransition(from: preDawn, timeZone: zone), date("2026-01-15T10:00:00Z"))
        let afternoon = date("2026-01-15T18:00:00Z") // 1 PM EST
        XCTAssertEqual(QuietHours.nextTransition(from: afternoon, timeZone: zone), date("2026-01-16T03:00:00Z"))
    }
}
