import Foundation
import Testing
@testable import OmyKit

private func d(_ s: String) -> Date { ISO8601UTC.parse(s)! }

@Suite struct SidecarTests {
    @Test func jsonKeysExactAndOptionalsOmitted() throws {
        let bin = SegmentFormat.header(codec: 21, startMs: 1_700_000_000_123) + SegmentFormat.record(offsetMs: 0, frame: Data([1]))  // 1-byte frame: no packet index
        let seg = try #require(SegmentFormat.parse(bin))
        let sc = Sidecar.make(id: "20231114T221320Z-00000001", deviceID: "d", data: bin, segment: seg, extras: SidecarExtras())
        let o = try #require(JSONSerialization.jsonObject(with: try sc.jsonData()) as? [String: Any])
        #expect(Set(o.keys) == ["id", "device_id", "codec", "start", "end", "frame_count", "first_packet_index", "last_packet_index", "sha256"])
        #expect(o["first_packet_index"] is NSNull && o["last_packet_index"] is NSNull)  // explicit null, as Dart
        #expect(o["codec"] as? Int == 21 && o["frame_count"] as? Int == 1)
        let text = String(decoding: try sc.jsonData(), as: UTF8.self)
        #expect(text.contains("\n  \"id\""))  // indent 2
    }

    @Test func optionalFieldsAndValidation() throws {
        let bin = SegmentFormat.header(codec: 21, startMs: 1_700_000_000_123) + SegmentFormat.record(offsetMs: 60_000, frame: frame(3, 0))
        let seg = try #require(SegmentFormat.parse(bin))
        let calls = [CallInterval(start: d("2023-11-14T22:13:30Z"), end: d("2023-11-14T22:14:00Z")), CallInterval(start: d("2023-11-14T22:14:10Z"))]
        let loc = [LocationSample(kind: "visit", lat: 45.1, lon: 9.2, acc: 30.4, arrive: d("2023-11-14T20:00:00Z"))]
        let ok = Sidecar.make(id: "i", deviceID: "d", data: bin, segment: seg,
                              extras: SidecarExtras(battery: 100, firmware: "3.0.21-rc+1_x", calls: calls, locations: loc))
        #expect(ok.battery == 100 && ok.firmware == "3.0.21-rc+1_x" && ok.calls?.count == 2)
        #expect(ok.location == SidecarLocation(lat: 45.1, lon: 9.2, acc: 30, source: "visit", at: "2023-11-14T20:00:00.000Z"))
        let o = try #require(JSONSerialization.jsonObject(with: try ok.jsonData()) as? [String: Any])
        let c = try #require(o["calls"] as? [[String: Any]])
        #expect(c[0]["end"] as? String == "2023-11-14T22:14:00.000Z" && c[1]["end"] == nil && c[1]["start"] as? String == "2023-11-14T22:14:10.000Z")
        #expect(Set((o["location"] as? [String: Any] ?? [:]).keys) == ["lat", "lon", "acc", "source", "at"])

        let bad = Sidecar.make(id: "i", deviceID: "d", data: bin, segment: seg, extras: SidecarExtras(battery: 101, firmware: "has space"))
        #expect(bad.battery == nil && bad.firmware == nil)
        #expect(Sidecar.make(id: "i", deviceID: "d", data: bin, segment: seg, extras: SidecarExtras(battery: -1, firmware: String(repeating: "a", count: 33))).firmware == nil)
        #expect(Sidecar.make(id: "i", deviceID: "d", data: bin, segment: seg, extras: SidecarExtras(battery: 0)).battery == 0)
    }

    @Test func callsOverlapAndCap() throws {
        let bin = SegmentFormat.header(codec: 21, startMs: 1_700_000_000_000) + SegmentFormat.record(offsetMs: 300_000, frame: frame(1, 0))  // 22:13:20 -> 22:18:20
        let seg = try #require(SegmentFormat.parse(bin))
        let before = CallInterval(start: d("2023-11-14T21:00:00Z"), end: d("2023-11-14T22:13:00Z"))  // ended before
        let after = CallInterval(start: d("2023-11-14T22:19:00Z"), end: d("2023-11-14T22:20:00Z"))
        let openOld = CallInterval(start: d("2023-11-14T22:00:00Z"))  // open: counts as 4 h, overlaps
        let openAncient = CallInterval(start: d("2023-11-14T10:00:00Z"))  // open > 4 h ago: ended
        let inside = (0..<25).map { CallInterval(start: d("2023-11-14T22:14:00Z").addingTimeInterval(Double($0)), end: d("2023-11-14T22:15:00Z")) }
        let sc = Sidecar.make(id: "i", deviceID: "d", data: bin, segment: seg, extras: SidecarExtras(calls: [before, after, openAncient, openOld] + inside))
        #expect(sc.calls?.count == 20)  // capped, first 20
        #expect(sc.calls?.first == openOld)
        let none = Sidecar.make(id: "i", deviceID: "d", data: bin, segment: seg, extras: SidecarExtras(calls: [before, after, openAncient]))
        #expect(none.calls == nil)
    }

    @Test func extrasLoadFromDocuments() throws {
        let tmp = TempDir()
        tmp.write("calls.json", #"[{"id":"A","start":"2023-11-14T22:14:00Z","end":"2023-11-14T22:15:00Z"},{"id":"B","start":"2023-11-14T22:16:00Z"},{"id":"C"},{"start":"bad"}]"#)
        tmp.write("locations.json", #"[{"kind":"visit","lat":45.0,"lon":9.0,"acc":10,"arrive":"2023-11-14T20:00:00Z"},{"kind":"move","lat":45.5,"lon":9.5,"acc":65,"t":"2023-11-14T22:00:00Z"},{"kind":"move","lat":"x"},{"lat":1}]"#)
        var f = Features()
        let ex = SidecarExtras.load(documents: tmp.url, features: f, battery: 42, firmware: "1.2")
        #expect(ex.calls.count == 2 && ex.calls[1].end == nil && ex.locations.count == 2 && ex.battery == 42 && ex.firmware == "1.2")
        f.set("calls", false); f.set("location", false); f.set("battery", false)
        let off = SidecarExtras.load(documents: tmp.url, features: f, battery: 42, firmware: "1.2")
        #expect(off.calls.isEmpty && off.locations.isEmpty && off.battery == nil && off.firmware == "1.2")
        #expect(SidecarExtras.load(documents: tmp.url.appendingPathComponent("nope")).calls.isEmpty)
    }
}

@Suite struct BestLocationTests {
    // segment 12:00 - 12:10, middle 12:05
    let s = d("2023-11-14T12:00:00Z"), e = d("2023-11-14T12:10:00Z")

    @Test func visitCoveringMiddleWins() {
        let visit = LocationSample(kind: "visit", lat: 1, lon: 2, acc: 10, arrive: d("2023-11-14T11:00:00Z"), depart: d("2023-11-14T13:00:00Z"))
        let move = LocationSample(kind: "move", lat: 3, lon: 4, acc: 50, t: d("2023-11-14T12:04:00Z"))
        let r = bestLocation([move, visit], start: s, end: e)
        #expect(r == SidecarLocation(lat: 1, lon: 2, acc: 10, source: "visit", at: "2023-11-14T11:00:00.000Z"))
    }

    @Test func latestArrivalAmongCoveringVisits() {
        let a = LocationSample(kind: "visit", lat: 1, lon: 1, acc: 10, arrive: d("2023-11-14T10:00:00Z"))
        let b = LocationSample(kind: "visit", lat: 2, lon: 2, acc: 10, arrive: d("2023-11-14T11:30:00Z"), depart: d("2023-11-14T12:30:00Z"))
        #expect(bestLocation([a, b], start: s, end: e)?.lat == 2)
        #expect(bestLocation([b, a], start: s, end: e)?.lat == 2)
    }

    @Test func openVisitWithoutArriveCountsFromEpoch() {
        let v = LocationSample(kind: "visit", lat: 1, lon: 1, acc: 10)
        let r = bestLocation([v], start: s, end: e)
        #expect(r?.at == "1970-01-01T00:00:00.000Z" && r?.source == "visit")
    }

    @Test func moveWithin2HoursElseNothing() {
        let recent = LocationSample(kind: "move", lat: 3, lon: 4, acc: 49.6, t: d("2023-11-14T10:30:00Z"))  // 4.5h? no: 1h35 before the middle
        let r = bestLocation([recent], start: s, end: e)
        #expect(r?.source == "move" && r?.acc == 50 && r?.at == "2023-11-14T10:30:00.000Z")
        let old = LocationSample(kind: "move", lat: 3, lon: 4, acc: 5, t: d("2023-11-14T09:00:00Z"))  // 3h05 before the middle
        #expect(bestLocation([old], start: s, end: e) == nil)
        let future = LocationSample(kind: "move", lat: 3, lon: 4, acc: 5, t: d("2023-11-14T12:20:00Z"))  // after the end
        #expect(bestLocation([future], start: s, end: e) == nil)
        let newer = LocationSample(kind: "move", lat: 9, lon: 9, acc: 5, t: d("2023-11-14T12:08:00Z"))
        #expect(bestLocation([recent, newer], start: s, end: e)?.lat == 9)
    }

    @Test func moveBeforeLeavingAVisitIsIgnored() {
        let left = LocationSample(kind: "visit", lat: 1, lon: 1, acc: 10, arrive: d("2023-11-14T08:00:00Z"), depart: d("2023-11-14T11:50:00Z"))
        let moveBefore = LocationSample(kind: "move", lat: 3, lon: 4, acc: 5, t: d("2023-11-14T11:40:00Z"))
        #expect(bestLocation([left, moveBefore], start: s, end: e) == nil)  // departed 11:50, move older than that
        let moveAfter = LocationSample(kind: "move", lat: 3, lon: 4, acc: 5, t: d("2023-11-14T11:55:00Z"))
        #expect(bestLocation([left, moveAfter], start: s, end: e)?.lat == 3)
        #expect(bestLocation([], start: s, end: e) == nil)
    }
}
