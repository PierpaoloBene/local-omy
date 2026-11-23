import Foundation

/// On-disk segment format (.bin), v1, all integers little-endian (segment_format.dart):
///
///     header (14 bytes): "OMIS" | u8 version (1) | u8 codec | u64 start unix ms UTC
///     record:            u32 offset ms from start | u16 frame length N | N bytes (BLE notification as received,
///                        3-byte Omi header included)
public enum SegmentFormat {
    public static let magic: [UInt8] = [0x4F, 0x4D, 0x49, 0x53]
    public static let version: UInt8 = 1
    public static let headerSize = 14
    public static let recordHeaderSize = 6

    public static func header(codec: UInt8, startMs: UInt64) -> Data {
        var d = Data(magic)
        d.append(version)
        d.append(codec)
        withUnsafeBytes(of: startMs.littleEndian) { d.append(contentsOf: $0) }
        return d
    }

    public static func record(offsetMs: UInt32, frame: Data) -> Data {
        var d = Data()
        d.reserveCapacity(recordHeaderSize + frame.count)
        withUnsafeBytes(of: offsetMs.littleEndian) { d.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt16(frame.count).littleEndian) { d.append(contentsOf: $0) }
        d.append(frame)
        return d
    }

    /// Parses `data`; nil if the file header is missing or invalid.
    public static func parse(_ data: Data) -> ParsedSegment? {
        let b = [UInt8](data)
        guard b.count >= headerSize, Array(b[0..<4]) == magic, b[4] == version else { return nil }
        func u16(_ p: Int) -> Int { Int(b[p]) | Int(b[p + 1]) << 8 }
        func u32(_ p: Int) -> UInt32 { UInt32(b[p]) | UInt32(b[p + 1]) << 8 | UInt32(b[p + 2]) << 16 | UInt32(b[p + 3]) << 24 }
        var start: UInt64 = 0
        for i in (0..<8).reversed() { start = start << 8 | UInt64(b[6 + i]) }
        var records: [SegmentRecord] = []
        var pos = headerSize
        while pos + recordHeaderSize <= b.count {
            let end = pos + recordHeaderSize + u16(pos + 4)
            if end > b.count { break }
            records.append(SegmentRecord(offsetMs: u32(pos), frame: Data(b[(pos + recordHeaderSize)..<end])))
            pos = end
        }
        return ParsedSegment(version: b[4], codec: b[5], startMs: start, records: records, validLength: pos)
    }
}

public struct SegmentRecord: Sendable, Equatable {
    public let offsetMs: UInt32
    public let frame: Data
    public init(offsetMs: UInt32, frame: Data) { self.offsetMs = offsetMs; self.frame = frame }
    /// Omi packet index (bytes 0-1 of the frame), nil if the frame is too short.
    public var packetIndex: Int? { frame.count >= 3 ? Int(frame[frame.startIndex]) | Int(frame[frame.startIndex + 1]) << 8 : nil }
    /// Fragment index inside one codec frame (byte 2).
    public var fragmentIndex: Int? { frame.count >= 3 ? Int(frame[frame.startIndex + 2]) : nil }
}

public struct ParsedSegment: Sendable {
    public let version: UInt8
    public let codec: UInt8
    public let startMs: UInt64
    public let records: [SegmentRecord]
    /// Bytes up to the end of the last complete record; anything after is a torn write.
    public let validLength: Int
}

/// Sortable id: UTC start time + 32 random bits, e.g. `20261027T081530Z-3fa2c91b`. The Mac validates it with
/// `^\d{8}T\d{6}Z-[0-9a-f]{8}$`.
public struct SegmentID: Hashable, Sendable, Comparable, CustomStringConvertible {
    public let raw: String
    public init(raw: String) { self.raw = raw }
    public var description: String { raw }
    public static func < (a: SegmentID, b: SegmentID) -> Bool { a.raw < b.raw }

    public static func new(now: Date) -> SegmentID {
        let iso = ISO8601UTC.string(now)  // 2026-10-27T08:15:30.123Z
        let c = Array(iso.utf8)
        func s(_ r: Range<Int>) -> String { String(decoding: c[r], as: UTF8.self) }
        let ts = s(0..<4) + s(5..<7) + s(8..<10) + "T" + s(11..<13) + s(14..<16) + s(17..<19) + "Z"
        var rnd = ""
        for _ in 0..<4 {
            let byte = UInt8.random(in: 0...255)
            rnd += String(byte, radix: 16).count == 1 ? "0" + String(byte, radix: 16) : String(byte, radix: 16)
        }
        return SegmentID(raw: "\(ts)-\(rnd)")
    }

    /// True if `raw` has the shape the Mac accepts.
    public var isValid: Bool {
        let c = Array(raw.utf8)
        guard c.count == 25, c[8] == 84, c[15] == 90, c[16] == 45 else { return false }
        for (i, ch) in c.enumerated() where ![8, 15, 16].contains(i) {
            let digit = ch >= 48 && ch <= 57
            let hex = ch >= 97 && ch <= 102
            if i < 15 ? !digit : !(digit || hex) { return false }
        }
        return true
    }
}

public enum SegmentError: Error, Sendable, Equatable {
    case frameTooLong(Int)
    case closed
    case io(String)
}

/// Appends records to a `.part` file. Writes are synchronous (frames from the BLE stream are never reordered);
/// the offset of each record is `at - start` in ms, never negative and never going back (so a wall-clock jump can't
/// corrupt the file). Callers should pass a monotonic-derived date, e.g. `start + ContinuousClock elapsed`.
public final class SegmentWriter: @unchecked Sendable {
    public let url: URL
    public let codec: UInt8
    public let start: Date
    private let lock = NSLock()
    private var handle: FileHandle?
    private var count = 0
    private var lastOffset: UInt32 = 0

    public init(url: URL, codec: UInt8, start: Date) throws {
        self.url = url
        self.codec = codec
        self.start = start
        guard FileManager.default.createFile(atPath: url.path, contents: nil) else {
            throw SegmentError.io("cannot create \(url.lastPathComponent)")
        }
        let h = try FileHandle(forWritingTo: url)
        try h.write(contentsOf: SegmentFormat.header(codec: codec, startMs: UInt64(max(0, ISO8601UTC.ms(start)))))
        handle = h
    }

    public var frameCount: Int { lock.withLock { count } }

    /// Time since `start` of the last record written, in ms.
    public var lastOffsetMs: UInt32 { lock.withLock { lastOffset } }

    public func append(frame: Data, at: Date) throws {
        let ms = max(0, min(Int64(UInt32.max), ISO8601UTC.ms(at) - ISO8601UTC.ms(start)))
        try append(frame: frame, offsetMs: UInt32(ms))
    }

    public func append(frame: Data, offsetMs: UInt32) throws {
        guard frame.count <= Int(UInt16.max) else { throw SegmentError.frameTooLong(frame.count) }
        try lock.withLock {
            guard let h = handle else { throw SegmentError.closed }
            let off = max(offsetMs, lastOffset)
            try h.write(contentsOf: SegmentFormat.record(offsetMs: off, frame: frame))
            lastOffset = off
            count += 1
        }
    }

    /// fsync and close. Idempotent.
    public func close() throws {
        try lock.withLock {
            guard let h = handle else { return }
            handle = nil
            try h.synchronize()
            try h.close()
        }
    }

    deinit { try? handle?.close() }
}
