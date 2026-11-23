import Foundation
import Testing
@testable import OmyKit

@Suite struct SegmentFormatTests {
    @Test func goldenBytes() throws {
        let tmp = TempDir()
        let url = tmp.url.appendingPathComponent("a.part")
        let w = try SegmentWriter(url: url, codec: 21, start: t0)
        try w.append(frame: frame(0, 0, payload: [0xAA, 0xBB]), at: t0)
        try w.append(frame: Data([1, 0, 0, 0xCC]), at: t0.addingTimeInterval(0.020))
        try w.close()
        try w.close()  // idempotent
        #expect(w.frameCount == 2)
        let got = hex(try Data(contentsOf: url))
        // "OMIS" v1 codec 0x15 start 1700000000123 LE | off 0, len 5, frame | off 20, len 4, frame
        #expect(got == "4f4d495301157b68e5cf8b010000" + "00000000" + "0500" + "000000aabb" + "14000000" + "0400" + "010000cc")
        #expect(throws: SegmentError.closed) { try w.append(frame: Data([1]), at: t0) }
    }

    @Test func roundTrip() throws {
        let tmp = TempDir()
        let url = tmp.url.appendingPathComponent("a.part")
        let w = try SegmentWriter(url: url, codec: 20, start: t0)
        var frames: [Data] = []
        for i in 0..<200 {
            let f = frame(65_530 + i, i % 3, payload: [UInt8](repeating: UInt8(i & 0xFF), count: i % 40))
            frames.append(f)
            try w.append(frame: f, at: t0.addingTimeInterval(Double(i) * 0.02))
        }
        try w.close()
        let seg = try #require(SegmentFormat.parse(try Data(contentsOf: url)))
        #expect(seg.codec == 20 && seg.version == 1 && seg.startMs == 1_700_000_000_123)
        #expect(seg.records.map(\.frame) == frames)
        #expect(seg.records.map { Int($0.offsetMs) } == (0..<200).map { $0 * 20 })
        #expect(seg.records[10].packetIndex == (65_540 & 0xFFFF))  // wraps
        #expect(seg.records[10].fragmentIndex == 1)
        #expect(seg.validLength == (try Data(contentsOf: url)).count)
    }

    @Test func parserRejectsBadHeaderAndStopsAtTornRecord() throws {
        #expect(SegmentFormat.parse(Data()) == nil)
        #expect(SegmentFormat.parse(Data("XXXX".utf8 + [1, 0] + [UInt8](repeating: 0, count: 8))) == nil)
        var bad = SegmentFormat.header(codec: 1, startMs: 5)
        bad[4] = 2
        #expect(SegmentFormat.parse(bad) == nil)
        var d = SegmentFormat.header(codec: 1, startMs: 5)
        d.append(SegmentFormat.record(offsetMs: 0, frame: Data([1, 2, 3])))
        let good = d.count
        d.append(SegmentFormat.record(offsetMs: 9, frame: Data([1, 2, 3, 4])).dropLast(2))  // torn
        let p = try #require(SegmentFormat.parse(d))
        #expect(p.records.count == 1 && p.validLength == good)
    }

    @Test func offsetsNeverGoBackOrNegative() throws {
        let tmp = TempDir()
        let w = try SegmentWriter(url: tmp.url.appendingPathComponent("a.part"), codec: 1, start: t0)
        try w.append(frame: frame(0, 0), at: t0.addingTimeInterval(1))
        try w.append(frame: frame(1, 0), at: t0.addingTimeInterval(0.5))  // clock went back
        try w.append(frame: frame(2, 0), at: t0.addingTimeInterval(-3))
        try w.close()
        let seg = try #require(SegmentFormat.parse(try Data(contentsOf: tmp.url.appendingPathComponent("a.part"))))
        #expect(seg.records.map(\.offsetMs) == [1000, 1000, 1000])
        #expect(throws: SegmentError.frameTooLong(70_000)) {
            let w2 = try SegmentWriter(url: tmp.url.appendingPathComponent("b.part"), codec: 1, start: t0)
            try w2.append(frame: Data(count: 70_000), at: t0)
        }
    }

    @Test func segmentID() {
        let id = SegmentID.new(now: t0)
        #expect(id.raw.hasPrefix("20231114T221320Z-"))
        #expect(id.isValid)
        #expect(SegmentID.new(now: t0) != id)
        #expect(!SegmentID(raw: "20231114T221320Z-XYZ").isValid)
        #expect(SegmentID(raw: "20231114T221320Z-00000000") < SegmentID(raw: "20231114T221321Z-00000000"))
    }

    @Test func timeFormatting() {
        #expect(ISO8601UTC.string(t0) == "2023-11-14T22:13:20.123Z")
        #expect(ISO8601UTC.string(Date(timeIntervalSince1970: 0)) == "1970-01-01T00:00:00.000Z")
        #expect(ISO8601UTC.string(ms: -1) == "1969-12-31T23:59:59.999Z")
        #expect(ISO8601UTC.parse("2023-11-14T22:13:20.123Z") == Date(timeIntervalSince1970: 1_700_000_000.123))
        #expect(ISO8601UTC.parse("2023-11-14T23:13:20+01:00") == Date(timeIntervalSince1970: 1_700_000_000))
        #expect(ISO8601UTC.parse("2023-11-14T22:13:20.123456Z")!.timeIntervalSince1970 - 1_700_000_000.123456 < 1e-6)
        #expect(ISO8601UTC.parse("not a date") == nil)
        #expect(ISO8601UTC.parse("2023-13-14T22:13:20Z") == nil)
        #expect(ISO8601UTC.withOffset(t0, timeZone: TimeZone(identifier: "Europe/Rome")!) == "2023-11-14T23:13:20+01:00")
        #expect(ISO8601UTC.withOffset(t0, timeZone: TimeZone(identifier: "America/St_Johns")!) == "2023-11-14T18:43:20-03:30")
    }

    @Test func language() {
        #expect(OmyLanguage.isEnglish(["it-IT", "en-US"]) == false)
        #expect(OmyLanguage.isEnglish(["en-GB", "it"]) == true)
        #expect(OmyLanguage.isEnglish(["fr-FR", "it-IT"]) == false)
        #expect(OmyLanguage.isEnglish(["fr-FR"]) == true)
        #expect(OmyLanguage.isEnglish([]) == true)
        #expect(["a", "b"].contains(tr("a", "b")))
        #expect(counted(1, "sessione", "sessioni", "session", "sessions").hasPrefix("1 "))
    }
}

@Suite struct SegmentStoreTests {
    @Test func finishWritesBinAndSidecarAtomically() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let s = try await store.begin(deviceID: "dev-1", codec: 21, now: t0)
        #expect(ls(tmp.segments) == ["\(s.id.raw).part", "\(s.id.raw).part.json"])
        try s.append(frame: frame(7, 0), at: t0)
        try s.append(frame: frame(8, 1), at: t0.addingTimeInterval(2))
        try await store.finish(s, extras: SidecarExtras(battery: 80, firmware: "3.0.21"), recovering: false)
        #expect(ls(tmp.segments) == ["\(s.id.raw).bin", "\(s.id.raw).json"])
        let bin = try Data(contentsOf: tmp.segments.appendingPathComponent("\(s.id.raw).bin"))
        let sc = try JSONDecoder().decode(Sidecar.self, from: Data(contentsOf: tmp.segments.appendingPathComponent("\(s.id.raw).json")))
        #expect(sc.id == s.id.raw && sc.device_id == "dev-1" && sc.codec == 21)
        #expect(sc.start == "2023-11-14T22:13:20.123Z" && sc.end == "2023-11-14T22:13:22.123Z")
        #expect(sc.frame_count == 2 && sc.first_packet_index == 7 && sc.last_packet_index == 8)
        #expect(sc.battery == 80 && sc.firmware == "3.0.21")
        let digest = Sidecar.make(id: "x", deviceID: "", data: bin, segment: SegmentFormat.parse(bin)!, extras: SidecarExtras()).sha256
        #expect(sc.sha256 == digest && sc.sha256.count == 64)
        // the sha256 is the real one
        let sh = Process()
        sh.executableURL = URL(fileURLWithPath: "/usr/bin/shasum")
        sh.arguments = ["-a", "256", tmp.segments.appendingPathComponent("\(s.id.raw).bin").path]
        let pipe = Pipe()
        sh.standardOutput = pipe
        try sh.run()
        sh.waitUntilExit()
        #expect(String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self).hasPrefix(sc.sha256))
    }

    @Test func emptyOrInvalidPartIsDeleted() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let s = try await store.begin(deviceID: "d", codec: 1, now: t0)
        try await store.finish(s, extras: SidecarExtras(), recovering: false)  // header only: no records
        #expect(ls(tmp.segments).isEmpty)
        let id = "20231114T221320Z-deadbeef"
        try Data("garbage".utf8).write(to: tmp.segments.appendingPathComponent("\(id).part"))
        #expect(try await store.recoverOrphans(extras: SidecarExtras()) == 1)
        #expect(ls(tmp.segments).isEmpty)
    }

    @Test func recoverOrphansTruncatesTornTailAndDropsBatteryFirmware() async throws {
        let tmp = TempDir()
        let id = "20231114T221320Z-deadbeef"
        var d = SegmentFormat.header(codec: 21, startMs: 1_700_000_000_123)
        d.append(SegmentFormat.record(offsetMs: 0, frame: frame(1, 0)))
        d.append(SegmentFormat.record(offsetMs: 20, frame: frame(2, 1)))
        let valid = d.count
        d.append(SegmentFormat.record(offsetMs: 40, frame: frame(3, 0)).dropLast(1))
        try FileManager.default.createDirectory(at: tmp.segments, withIntermediateDirectories: true)
        try d.write(to: tmp.segments.appendingPathComponent("\(id).part"))
        try Data(#"{"device_id":"dev-9"}"#.utf8).write(to: tmp.segments.appendingPathComponent("\(id).part.json"))
        try Data("x".utf8).write(to: tmp.segments.appendingPathComponent("20231114T000000Z-00000000.json.tmp"))  // leftover
        try Data(#"{"device_id":"z"}"#.utf8).write(to: tmp.segments.appendingPathComponent("20231113T000000Z-00000000.part.json"))  // leftover

        let store = SegmentStore(directory: tmp.segments)
        let n = try await store.recoverOrphans(extras: SidecarExtras(battery: 50, firmware: "1.0"))
        #expect(n == 1)
        #expect(ls(tmp.segments) == ["\(id).bin", "\(id).json"])
        #expect(try Data(contentsOf: tmp.segments.appendingPathComponent("\(id).bin")).count == valid)
        let sc = try JSONDecoder().decode(Sidecar.self, from: Data(contentsOf: tmp.segments.appendingPathComponent("\(id).json")))
        #expect(sc.device_id == "dev-9" && sc.frame_count == 2 && sc.battery == nil && sc.firmware == nil)
        #expect(await store.queue().count == 1)
    }

    @Test func recoverSkipsSegmentsBeingWritten() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let s = try await store.begin(deviceID: "d", codec: 1, now: t0)
        try s.append(frame: frame(0, 0), at: t0)
        #expect(try await store.recoverOrphans(extras: SidecarExtras()) == 0)
        try await store.finish(s, extras: SidecarExtras(), recovering: false)
        #expect(await store.queue().count == 1)
    }

    @Test func finishIsRerunnableAfterInterruption() async throws {
        // interrupted after the sidecar was renamed but before .part became .bin
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let s = try await store.begin(deviceID: "d", codec: 1, now: t0)
        try s.append(frame: frame(0, 0), at: t0)
        try s.writer.close()
        try Data("{}".utf8).write(to: tmp.segments.appendingPathComponent("\(s.id.raw).json"))  // stale sidecar
        #expect(await store.queue().isEmpty)  // no .bin yet: not queued
        let relaunched = SegmentStore(directory: tmp.segments)  // a new process
        #expect(try await relaunched.recoverOrphans(extras: SidecarExtras()) == 1)
        let q = await relaunched.queue()
        #expect(q.count == 1)
        let sc = try JSONDecoder().decode(Sidecar.self, from: Data(contentsOf: q[0].jsonURL))
        #expect(sc.frame_count == 1)
    }

    @Test func queueIsOldestFirstAndNeedsBothFiles() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let b = try await makeSegment(store, at: t0.addingTimeInterval(600))
        let a = try await makeSegment(store, at: t0)
        let c = try await makeSegment(store, at: t0.addingTimeInterval(1200))
        #expect(await store.queue().map(\.id) == [a.id, b.id, c.id])
        try FileManager.default.removeItem(at: b.jsonURL)  // .bin without sidecar is not queued
        #expect(await store.queue().map(\.id) == [a.id, c.id])
    }

    @Test func deleteRemovesBothFiles() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let a = try await makeSegment(store, at: t0)
        try await store.delete(a.id)
        #expect(ls(tmp.segments).isEmpty)
        try await store.delete(a.id)  // already gone: fine
    }
}
