import Foundation
import OmyKit
import SwiftUI

/// Date/duration formatting shared by Today and Ask (after common.dart).
enum TAFormat {
    private static let locale = Locale(identifier: OmyLanguage.english ? "en" : "it")
    private static var utc: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        return c
    }

    static func date(_ iso: String) -> Date? {
        let p = iso.prefix(10).split(separator: "-").compactMap { Int($0) }
        guard p.count == 3 else { return nil }
        return utc.date(from: DateComponents(year: p[0], month: p[1], day: p[2]))
    }

    static func iso(_ d: Date) -> String {
        let c = utc.dateComponents([.year, .month, .day], from: d)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// The day before and after `day`.
    static func neighbours(_ day: String) -> [String] {
        guard let d = date(day) else { return [] }
        return [utc.date(byAdding: .day, value: -1, to: d), utc.date(byAdding: .day, value: 1, to: d)].compactMap { $0.map(iso) }
    }

    /// Today on the phone, as yyyy-MM-dd.
    static func localToday() -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }

    /// "29 ott", "Oct 29".
    static func shortDay(_ iso: String) -> String {
        guard let d = date(iso) else { return iso }
        let f = DateFormatter()
        f.locale = locale
        f.timeZone = TimeZone(identifier: "UTC")
        f.setLocalizedDateFormatFromTemplate("MMMd")
        return f.string(from: d).replacingOccurrences(of: ".", with: "")
    }

    /// "Oggi", "Ieri", "Mercoledì 29 ottobre" (with the year if not this one).
    static func longDay(_ iso: String, today: String) -> String {
        guard let d = date(iso), let t = date(today) else { return iso }
        let ago = utc.dateComponents([.day], from: d, to: t).day ?? 0
        if ago == 0 { return tr("Oggi", "Today") }
        if ago == 1 { return tr("Ieri", "Yesterday") }
        let f = DateFormatter()
        f.locale = locale
        f.timeZone = TimeZone(identifier: "UTC")
        let sameYear = utc.component(.year, from: d) == utc.component(.year, from: t)
        f.setLocalizedDateFormatFromTemplate(sameYear ? "EEEEMMMMd" : "EEEEMMMMdy")
        let s = f.string(from: d)
        return s.prefix(1).uppercased() + s.dropFirst()
    }

    /// "HH:mm", with the short day before it when not today (a time on the phone).
    static func stamp(_ t: Date) -> String {
        let f = DateFormatter()
        f.locale = locale
        f.dateFormat = "HH:mm"
        let c = Calendar.current.dateComponents([.year, .month, .day], from: t)
        let day = String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
        return (day == localToday() ? "" : shortDay(day) + " ") + f.string(from: t)
    }

    static func decimal(_ v: Double) -> String {
        let s = String(format: "%.1f", v)
        return OmyLanguage.english ? s : s.replacingOccurrences(of: ".", with: ",")
    }

    static func duration(_ minutes: Double) -> String {
        minutes < 1 ? "<1 min" : minutes < 90 ? "\(Int(minutes.rounded())) min" : "\(decimal(minutes / 60)) h"
    }
}
