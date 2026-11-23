import Foundation

/// A segment being written: `{id}.part` plus `{id}.part.json` (the device id, needed to finish it after a crash).
public struct OpenSegment: Sendable {
    public let id: SegmentID
    public let deviceID: String
    public let codec: UInt8
    public let start: Date
    public let partURL: URL
    public let writer: SegmentWriter

    public func append(frame: Data, at: Date) throws { try writer.append(frame: frame, at: at) }
    public var frameCount: Int { writer.frameCount }
    /// Age on the clock of the frames (last frame time since start); the recorder uses its own monotonic clock too.
    public func age(now: Date) -> TimeInterval { now.timeIntervalSince(start) }
}

/// A closed segment waiting for upload: `.bin` + `.json`.
public struct QueuedSegment: Sendable, Hashable, Identifiable {
    public let id: SegmentID
    public let binURL: URL
    public let jsonURL: URL
    public init(id: SegmentID, binURL: URL, jsonURL: URL) { self.id = id; self.binURL = binURL; self.jsonURL = jsonURL }
}

/// Segment files in Documents/segments:
///
///     {id}.part        segment being written
///     {id}.part.json   device id of a .part
///     {id}.bin         closed and fsynced segment
///     {id}.json        its sidecar
///
/// The closed .bin/.json pairs ARE the upload queue: no database. `calls.json` / `locations.json` live in the parent
/// directory (Documents); see `SidecarExtras.load`.
public actor SegmentStore {
    public nonisolated let directory: URL
    private var begun: Set<SegmentID> = []

    public init(directory: URL) {
        self.directory = directory
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    nonisolated func url(_ id: SegmentID, _ ext: String) -> URL { directory.appendingPathComponent("\(id.raw).\(ext)") }

    public func begin(deviceID: String, codec: UInt8, now: Date) throws -> OpenSegment {
        let id = SegmentID.new(now: now)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let meta = try JSONSerialization.data(withJSONObject: ["device_id": deviceID])
        let metaURL = url(id, "part.json")
        try meta.write(to: metaURL, options: .atomic)
        let part = url(id, "part")
        let w = try SegmentWriter(url: part, codec: codec, start: now)
        begun.insert(id)
        return OpenSegment(id: id, deviceID: deviceID, codec: codec, start: now, partURL: part, writer: w)
    }

    /// `.part` -> `.bin` + `.json`, atomically as far as the file system allows: close and fsync; truncate a torn last
    /// record; write the sidecar to `.json.tmp` and fsync; rename to `.json`; rename `.part` to `.bin`; remove
    /// `.part.json`. Empty or invalid parts are deleted. Safe to re-run if interrupted at any step.
    public func finish(_ s: OpenSegment, extras: SidecarExtras, recovering: Bool = false) throws {
        try? s.writer.close()
        begun.remove(s.id)
        try finalize(id: s.id, deviceID: s.deviceID, extras: extras, recovering: recovering)
    }

    /// Finishes every `.part` left by a crash or kill (call at launch, before `begin`), without battery/firmware
    /// (the level is unknown). Returns how many were finalized. Also removes leftovers of an interrupted finish.
    @discardableResult
    public func recoverOrphans(extras: SidecarExtras) throws -> Int {
        let fm = FileManager.default
        var n = 0
        let names = (try? fm.contentsOfDirectory(atPath: directory.path)) ?? []
        for name in names.sorted() where name.hasSuffix(".part") {
            let id = SegmentID(raw: String(name.dropLast(5)))
            if begun.contains(id) { continue }
            var device = ""
            if let d = try? Data(contentsOf: url(id, "part.json")),
               let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let s = o["device_id"] as? String {
                device = s
            }
            try finalize(id: id, deviceID: device, extras: extras, recovering: true)
            n += 1
        }
        for name in (try? fm.contentsOfDirectory(atPath: directory.path)) ?? [] {
            let p = directory.appendingPathComponent(name)
            if name.hasSuffix(".json.tmp") {
                try? fm.removeItem(at: p)
            } else if name.hasSuffix(".part.json"),
                      !fm.fileExists(atPath: directory.appendingPathComponent(String(name.dropLast(5))).path),
                      !begun.contains(SegmentID(raw: String(name.dropLast(10)))) {
                try? fm.removeItem(at: p)
            }
        }
        return n
    }

    private func finalize(id: SegmentID, deviceID: String, extras: SidecarExtras, recovering: Bool) throws {
        let fm = FileManager.default
        let part = url(id, "part"), partMeta = url(id, "part.json"), tmp = url(id, "json.tmp")
        guard fm.fileExists(atPath: part.path) else {
            try? fm.removeItem(at: partMeta)
            return
        }
        var data = try Data(contentsOf: part)
        guard let seg = SegmentFormat.parse(data), !seg.records.isEmpty else {
            try? fm.removeItem(at: part)
            try? fm.removeItem(at: partMeta)
            return
        }
        if seg.validLength < data.count {
            let h = try FileHandle(forUpdating: part)
            try h.truncate(atOffset: UInt64(seg.validLength))
            try h.synchronize()
            try h.close()
            data = data.prefix(seg.validLength)
        }
        var ex = extras
        if recovering {  // a .part recovered at launch was closed long ago: level and firmware unknown
            ex.battery = nil
            ex.firmware = nil
        }
        let sidecar = Sidecar.make(id: id.raw, deviceID: deviceID, data: data, segment: seg, extras: ex)
        try Self.writeFsynced(try sidecar.jsonData(), to: tmp)
        try Self.rename(tmp, url(id, "json"))
        try Self.rename(part, url(id, "bin"))
        try? fm.removeItem(at: partMeta)
        Self.syncDirectory(directory)
    }

    /// Closed `.bin`+`.json` pairs, oldest first (by id).
    public func queue() -> [QueuedSegment] {
        let fm = FileManager.default
        let names = (try? fm.contentsOfDirectory(atPath: directory.path)) ?? []
        return names.filter { $0.hasSuffix(".bin") }
            .map { SegmentID(raw: String($0.dropLast(4))) }
            .filter { fm.fileExists(atPath: url($0, "json").path) }
            .sorted()
            .map { QueuedSegment(id: $0, binURL: url($0, "bin"), jsonURL: url($0, "json")) }
    }

    public func delete(_ id: SegmentID) throws {
        let fm = FileManager.default
        for ext in ["bin", "json"] where fm.fileExists(atPath: url(id, ext).path) {
            try fm.removeItem(at: url(id, ext))
        }
    }

    // MARK: file helpers

    static func writeFsynced(_ data: Data, to url: URL) throws {
        guard FileManager.default.createFile(atPath: url.path, contents: nil) else { throw SegmentError.io("cannot create \(url.lastPathComponent)") }
        let h = try FileHandle(forWritingTo: url)
        try h.write(contentsOf: data)
        try h.synchronize()
        try h.close()
    }

    static func rename(_ from: URL, _ to: URL) throws {
        if Darwin.rename(from.path, to.path) != 0 {
            throw SegmentError.io("rename \(from.lastPathComponent): errno \(errno)")
        }
    }

    static func syncDirectory(_ dir: URL) {
        let fd = open(dir.path, O_RDONLY)
        if fd >= 0 { fsync(fd); close(fd) }
    }
}
