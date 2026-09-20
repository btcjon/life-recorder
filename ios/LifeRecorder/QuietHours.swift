import Foundation

/// Nightly pause from 10:00 PM to 5:00 AM America/New_York.
/// This is a capture schedule, not speaker identity.
enum QuietHours {
    static let timeZoneIdentifier = "America/New_York"
    static let startHour = 22
    static let endHour = 5

    static var timeZone: TimeZone {
        TimeZone(identifier: timeZoneIdentifier) ?? TimeZone(secondsFromGMT: -5 * 3600)!
    }

    static func isActive(at date: Date, timeZone: TimeZone = timeZone) -> Bool {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let hour = calendar.component(.hour, from: date)
        return hour >= startHour || hour < endHour
    }

    static func nextTransition(from date: Date, timeZone: TimeZone = timeZone) -> Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let hour = calendar.component(.hour, from: date)
        if isActive(at: date, timeZone: timeZone) {
            if hour >= startHour {
                let tomorrow = calendar.date(byAdding: .day, value: 1, to: date) ?? date
                return calendar.date(bySettingHour: endHour, minute: 0, second: 0, of: tomorrow) ?? date
            }
            return calendar.date(bySettingHour: endHour, minute: 0, second: 0, of: date) ?? date
        }
        return calendar.date(bySettingHour: startHour, minute: 0, second: 0, of: date) ?? date
    }
}
