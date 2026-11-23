import CryptoKit
import Foundation

/// A call connected during a segment (Documents/calls.json: `{"id", "start", "end"?}`, UTC ISO 8601).
public struct CallInterval: Codable, Sendable, Equatable {
    public var start: Date
    public var end: Date?
    public init(start: Date, end: Date? = nil) { self.start = start; self.end = end }

    enum CodingKeys: String, CodingKey { case start, end }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let s = ISO8601UTC.parse(try c.decode(String.self, forKey: .start)) else {
            throw DecodingError.dataCorruptedError(forKey: .start, in: c, debugDescription: "bad date")
        }
        start = s
        end = (try c.decodeIfPresent(String.self, forKey: .end)).flatMap(ISO8601UTC.parse)
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(ISO8601UTC.string(start), forKey: .start)
        try c.encodeIfPresent(end.map(ISO8601UTC.string), forKey: .end)
    }
}

/// One entry of Documents/locations.json: `{"kind":"visit","lat","lon","acc","arrive"?,"depart"?}` or
/// `{"kind":"move","lat","lon","acc","t"}`.
public struct LocationSample: Sendable, Equatable {
    public var kind: String
    public var lat: Double
    public var lon: Double
    public var acc: Double
    public var arrive: Date?
    public var depart: Date?
    public var t: Date?
    public init(kind: String, lat: Double, lon: Double, acc: Double, arrive: Date? = nil, depart: Date? = nil, t: Date? = nil) {
        self.kind = kind; self.lat = lat; self.lon = lon; self.acc = acc
        self.arrive = arrive; self.depart = depart; self.t = t
    }
}

/// Where the phone was: the sidecar's `location` object.
public struct SidecarLocation: Codable, Sendable, Equatable {
    public var lat: Double
    public var lon: Double
    public var acc: Int
    public var source: String  // "visit" | "move"
    public var at: String  // ISO UTC
    public init(lat: Double, lon: Double, acc: Int, source: String, at: String) {
        self.lat = lat; self.lon = lon; self.acc = acc; self.source = source; self.at = at
    }
}

/// What goes into the sidecar besides what the file says.
public struct SidecarExtras: Sendable {
    public var battery: Int?
    public var firmware: String?
    public var calls: [CallInterval]
    public var locations: [LocationSample]
    public init(battery: Int? = nil, firmware: String? = nil, calls: [CallInterval] = [], locations: [LocationSample] = []) {
        self.battery = battery; self.firmware = firmware; self.calls = calls; self.locations = locations
    }

    /// Reads `calls.json` / `locations.json` from `documents` (empty if missing or broken); a feature that is off
    /// gives no calls / no locations / no battery, as in the Flutter app.
    public static func load(documents: URL, features: Features = Features(), battery: Int? = nil, firmware: String? = nil) -> SidecarExtras {
        SidecarExtras(
            battery: features.on("battery") ? battery : nil,
            firmware: firmware,
            calls: features.on("calls") ? readCalls(documents.appendingPathComponent("calls.json")) : [],
            locations: features.on("location") ? readLocations(documents.appendingPathComponent("locations.json")) : [])
    }

    static func readList(_ url: URL) -> [[String: Any]] {
        guard let d = try? Data(contentsOf: url), let o = try? JSONSerialization.jsonObject(with: d) as? [[String: Any]] else { return [] }
        return o
    }

    public static func readCalls(_ url: URL) -> [CallInterval] {
        readList(url).compactMap { e in
            guard let s = (e["start"] as? String).flatMap(ISO8601UTC.parse) else { return nil }
            return CallInterval(start: s, end: (e["end"] as? String).flatMap(ISO8601UTC.parse))
        }
    }

    public static func readLocations(_ url: URL) -> [LocationSample] {
        readList(url).compactMap { e in
            guard let kind = e["kind"] as? String, let lat = (e["lat"] as? NSNumber)?.doubleValue,
                  let lon = (e["lon"] as? NSNumber)?.doubleValue, let acc = (e["acc"] as? NSNumber)?.doubleValue
            else { return nil }
            func d(_ k: String) -> Date? { (e[k] as? String).flatMap(ISO8601UTC.parse) }
            return LocationSample(kind: kind, lat: lat, lon: lon, acc: acc, arrive: d("arrive"), depart: d("depart"), t: d("t"))
        }
    }
}

/// A call still open this long after its start is taken as ended (the app was killed before CallKit said so).
let maxCall: TimeInterval = 4 * 3600
/// A significant change older than this says little about where the phone is.
let maxMoveAge: Int64 = 2 * 3600 * 1000
let maxCalls = 20

/// The best location fix for [start, end]: a visit covering the middle of the segment (the latest arrival), else the
/// latest significant change up to the end of the segment, not older than 2 h before the middle and not before leaving
/// a visit. Port of bestLocation in segment_store.dart.
public func bestLocation(_ entries: [LocationSample], start: Date, end: Date) -> SidecarLocation? {
    bestLocation(entries, startMs: ISO8601UTC.ms(start), endMs: ISO8601UTC.ms(end))
}

public func bestLocation(_ entries: [LocationSample], startMs: Int64, endMs: Int64) -> SidecarLocation? {
    func ms(_ d: Date?) -> Int64? { d.map(ISO8601UTC.ms) }
    let mid = (startMs + endMs) / 2
    var best: LocationSample?
    var bestT: Int64?
    var lastDepart: Int64 = -1
    for e in entries where e.kind == "visit" {
        let a = ms(e.arrive), d = ms(e.depart)
        if (a ?? 0) <= mid, d == nil || d! >= mid, bestT == nil || (a ?? 0) > bestT! {
            best = e
            bestT = a ?? 0
        } else if let d, d < mid, d > lastDepart {
            lastDepart = d
        }
    }
    if best == nil {
        for e in entries where e.kind == "move" {
            guard let t = ms(e.t) else { continue }
            if t <= endMs, mid - t <= maxMoveAge, t > lastDepart, bestT == nil || t > bestT! {
                best = e
                bestT = t
            }
        }
    }
    guard let best, let bestT else { return nil }
    return SidecarLocation(lat: best.lat, lon: best.lon, acc: Int(best.acc.rounded()), source: best.kind, at: ISO8601UTC.string(ms: bestT))
}

/// Sidecar `{id}.json`, exact keys of the Mac's META_FIELDS (omy_receiver/app.py). Optional fields are omitted when
/// absent ("older Macs reject unknown fields"); the two packet indexes are always present, null if unknown.
public struct Sidecar: Codable, Sendable, Equatable {
    public var id: String
    public var device_id: String
    public var codec: Int
    public var start: String
    public var end: String
    public var frame_count: Int
    public var first_packet_index: Int?
    public var last_packet_index: Int?
    public var sha256: String
    public var calls: [CallInterval]?
    public var location: SidecarLocation?
    public var battery: Int?
    public var firmware: String?

    public init(id: String, device_id: String, codec: Int, start: String, end: String, frame_count: Int,
                first_packet_index: Int?, last_packet_index: Int?, sha256: String, calls: [CallInterval]? = nil,
                location: SidecarLocation? = nil, battery: Int? = nil, firmware: String? = nil) {
        self.id = id; self.device_id = device_id; self.codec = codec; self.start = start; self.end = end
        self.frame_count = frame_count; self.first_packet_index = first_packet_index
        self.last_packet_index = last_packet_index; self.sha256 = sha256; self.calls = calls
        self.location = location; self.battery = battery; self.firmware = firmware
    }

    enum CodingKeys: String, CodingKey {
        case id, device_id, codec, start, end, frame_count, first_packet_index, last_packet_index, sha256, calls, location, battery, firmware
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(device_id, forKey: .device_id)
        try c.encode(codec, forKey: .codec)
        try c.encode(start, forKey: .start)
        try c.encode(end, forKey: .end)
        try c.encode(frame_count, forKey: .frame_count)
        try c.encode(first_packet_index, forKey: .first_packet_index)  // explicit null
        try c.encode(last_packet_index, forKey: .last_packet_index)
        try c.encode(sha256, forKey: .sha256)
        try c.encodeIfPresent(calls, forKey: .calls)
        try c.encodeIfPresent(location, forKey: .location)
        try c.encodeIfPresent(battery, forKey: .battery)
        try c.encodeIfPresent(firmware, forKey: .firmware)
    }

    static func isFirmware(_ s: String) -> Bool {
        let u = Array(s.utf8)
        guard (1...32).contains(u.count) else { return false }
        return u.allSatisfy { ($0 >= 48 && $0 <= 57) || ($0 >= 65 && $0 <= 90) || ($0 >= 97 && $0 <= 122) || [46, 95, 43, 45].contains($0) }
    }

    /// Builds the sidecar of a (trimmed, valid, non-empty) `.bin`.
    public static func make(id: String, deviceID: String, data: Data, segment seg: ParsedSegment, extras: SidecarExtras) -> Sidecar {
        let startMs = Int64(seg.startMs)
        let endMs = startMs + Int64(seg.records.last?.offsetMs ?? 0)
        let overlapping = extras.calls.filter { c in
            let s = ISO8601UTC.ms(c.start)
            let e = c.end.map(ISO8601UTC.ms) ?? s + Int64(maxCall * 1000)
            return s < endMs && e > startMs
        }.prefix(maxCalls)
        let location = bestLocation(extras.locations, startMs: startMs, endMs: endMs)
        let digest = SHA256.hash(data: data).map { $0 < 16 ? "0" + String($0, radix: 16) : String($0, radix: 16) }.joined()
        return Sidecar(
            id: id, device_id: deviceID, codec: Int(seg.codec), start: ISO8601UTC.string(ms: startMs),
            end: ISO8601UTC.string(ms: endMs), frame_count: seg.records.count,
            first_packet_index: seg.records.first?.packetIndex, last_packet_index: seg.records.last?.packetIndex,
            sha256: digest, calls: overlapping.isEmpty ? nil : Array(overlapping), location: location,
            battery: extras.battery.flatMap { (0...100).contains($0) ? $0 : nil },
            firmware: extras.firmware.flatMap { isFirmware($0) ? $0 : nil })
    }

    /// Pretty JSON, indent 2, sorted keys, as written to `{id}.json` (and sent base64url in X-Segment-Meta).
    public func jsonData() throws -> Data {
        let e = JSONEncoder()
        e.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return try e.encode(self)
    }
}
