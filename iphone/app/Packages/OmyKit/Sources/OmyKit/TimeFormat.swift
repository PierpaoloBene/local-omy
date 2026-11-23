import Foundation

/// ISO 8601 UTC with milliseconds, as Dart's `toUtc().toIso8601String()` (2026-11-27T08:15:30.123Z), and a tolerant
/// parser. Done by hand: no formatter objects, so it is thread-safe and fast.
public enum ISO8601UTC {
    /// Nearest millisecond (Doubles at 1.7e9 s are only good to ~0.2 µs: flooring would turn .123 into .122).
    public static func ms(_ d: Date) -> Int64 { Int64((d.timeIntervalSince1970 * 1000).rounded()) }

    public static func string(_ d: Date) -> String { string(ms: ms(d)) }

    public static func string(ms: Int64) -> String {
        let secs = floorDiv(ms, 1000)
        let milli = Int(ms - secs * 1000)
        let days = floorDiv(secs, 86400)
        let rem = Int(secs - days * 86400)
        let (y, m, d) = civil(fromDays: days)
        func p(_ v: Int, _ w: Int) -> String {
            let s = String(v)
            return String(repeating: "0", count: max(0, w - s.count)) + s
        }
        return "\(p(y, 4))-\(p(m, 2))-\(p(d, 2))T\(p(rem / 3600, 2)):\(p(rem % 3600 / 60, 2)):\(p(rem % 60, 2)).\(p(milli, 3))Z"
    }

    static func floorDiv(_ a: Int64, _ b: Int64) -> Int64 {
        let q = a / b
        return (a % b != 0 && (a < 0) != (b < 0)) ? q - 1 : q
    }

    /// yyyy-MM-ddTHH:mm:ss[.f+][Z|±hh[:mm]]; no zone = UTC.
    public static func parse(_ s: String) -> Date? {
        let u = Array(s.utf8)
        var i = 0
        func num(_ n: Int) -> Int? {
            guard i + n <= u.count else { return nil }
            var v = 0
            for k in 0..<n {
                let c = u[i + k]
                guard c >= 48 && c <= 57 else { return nil }
                v = v * 10 + Int(c - 48)
            }
            i += n
            return v
        }
        func lit(_ c: UInt8) -> Bool {
            guard i < u.count, u[i] == c else { return false }
            i += 1
            return true
        }
        guard let y = num(4), lit(45), let mo = num(2), lit(45), let d = num(2) else { return nil }
        guard i < u.count, u[i] == 84 || u[i] == 32 else { return nil }
        i += 1
        guard let h = num(2), lit(58), let mi = num(2), lit(58), let se = num(2) else { return nil }
        guard (1...12).contains(mo), (1...31).contains(d), h < 24, mi < 60, se < 61 else { return nil }
        var frac = 0.0
        if i < u.count, u[i] == 46 {
            i += 1
            var scale = 0.1
            var digits = 0
            while i < u.count, u[i] >= 48, u[i] <= 57 {
                frac += Double(u[i] - 48) * scale
                scale /= 10
                i += 1
                digits += 1
            }
            if digits == 0 { return nil }
        }
        var offset = 0
        if i < u.count {
            if u[i] == 90 || u[i] == 122 {
                i += 1
            } else if u[i] == 43 || u[i] == 45 {
                let sign = u[i] == 45 ? -1 : 1
                i += 1
                guard let oh = num(2) else { return nil }
                _ = lit(58)
                let om = num(2) ?? 0
                offset = sign * (oh * 3600 + om * 60)
            } else {
                return nil
            }
        }
        guard i == u.count else { return nil }
        let days = daysFromCivil(y, mo, d)
        let secs = Double(days) * 86400 + Double(h * 3600 + mi * 60 + se - offset) + frac
        return Date(timeIntervalSince1970: secs)
    }

    // Howard Hinnant's civil calendar algorithms.
    static func daysFromCivil(_ y0: Int, _ m: Int, _ d: Int) -> Int64 {
        let y = Int64(m <= 2 ? y0 - 1 : y0)
        let era = (y >= 0 ? y : y - 399) / 400
        let yoe = y - era * 400
        let doy = Int64((153 * (m > 2 ? m - 3 : m + 9) + 2) / 5 + d - 1)
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
        return era * 146097 + doe - 719468
    }

    static func civil(fromDays z0: Int64) -> (Int, Int, Int) {
        let z = z0 + 719468
        let era = (z >= 0 ? z : z - 146096) / 146097
        let doe = z - era * 146097
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365
        let y = yoe + era * 400
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
        let mp = (5 * doy + 2) / 153
        let d = Int(doy - (153 * mp + 2) / 5 + 1)
        let m = Int(mp < 10 ? mp + 3 : mp - 9)
        return (Int(m <= 2 ? y + 1 : y), m, d)
    }

    /// 2026-11-23T14:30:00+01:00: local time with its offset (the `now` of POST /v1/ask).
    public static func withOffset(_ t: Date, timeZone: TimeZone = .current) -> String {
        let off = timeZone.secondsFromGMT(for: t)
        let local = Int64(t.timeIntervalSince1970.rounded(.down)) + Int64(off)
        let base = string(ms: local * 1000).prefix(19)
        let a = abs(off)
        func p(_ v: Int) -> String { v < 10 ? "0\(v)" : "\(v)" }
        return "\(base)\(off < 0 ? "-" : "+")\(p(a / 3600)):\(p(a % 3600 / 60))"
    }
}
