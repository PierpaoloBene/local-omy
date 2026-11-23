import Foundation
import Testing
@testable import OmyKit

private func sha(of q: QueuedSegment) throws -> String {
    let o = try JSONSerialization.jsonObject(with: Data(contentsOf: q.jsonURL)) as! [String: Any]
    return o["sha256"] as! String
}

@Suite struct UploaderTests {
    @Test func successDeletesAndSendsExactRequest() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let a = try await makeSegment(store, at: t0)
        let b = try await makeSegment(store, at: t0.addingTimeInterval(600))
        let shaA = try sha(of: a), shaB = try sha(of: b)
        let (api, requests) = FakeNet.make { r in .json(#"{"sha256":"\#(r.headers["X-Sha256"] ?? "")","stored":true}"#) }
        let clock = Clock(t0)
        let up = Uploader(store: store, api: api, now: { clock.now })
        await up.kick(reason: "test")
        #expect(await store.queue().isEmpty)
        let reqs = requests()
        #expect(reqs.count == 2)
        // oldest first, remaining counts down
        #expect(reqs[0].url.path == "/v1/segments/\(a.id.raw)" && reqs[1].url.path == "/v1/segments/\(b.id.raw)")
        #expect(reqs[0].headers["X-Queue-Remaining"] == "1" && reqs[1].headers["X-Queue-Remaining"] == "0")
        #expect(reqs[0].method == "POST")
        #expect(reqs[0].headers["Authorization"] == "Bearer tok-123")
        #expect(reqs[0].headers["Content-Type"] == "application/octet-stream")
        #expect(reqs[0].headers["X-Segment-Id"] == a.id.raw)
        #expect(reqs[0].headers["X-Sha256"] == shaA && reqs[1].headers["X-Sha256"] == shaB)
        // X-Segment-Meta = base64url of the sidecar bytes
        let meta = reqs[0].headers["X-Segment-Meta"]!
        #expect(!meta.contains("+") && !meta.contains("/"))
        var b64 = meta.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        while b64.count % 4 != 0 { b64 += "=" }
        let decoded = try JSONDecoder().decode(Sidecar.self, from: Data(base64Encoded: b64)!)
        #expect(decoded.id == a.id.raw && decoded.sha256 == shaA)
        let st = await up.status
        #expect(st.inCoda == 0 && st.ultimoInvio == t0 && st.ultimoErrore == nil && st.prossimoTentativo == nil)
        #expect(reqs[0].body.count > 14)
    }

    @Test func bodyIsTheBin() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let a = try await makeSegment(store, at: t0)
        let bin = try Data(contentsOf: a.binURL)
        let (api, requests) = FakeNet.make { r in .json(#"{"sha256":"\#(r.headers["X-Sha256"] ?? "")"}"#) }
        await Uploader(store: store, api: api).kick(reason: "t")
        #expect(requests().first?.body == bin)
    }

    @Test func failureKeepsFilesStopsRoundAndBacksOff() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        _ = try await makeSegment(store, at: t0)
        _ = try await makeSegment(store, at: t0.addingTimeInterval(600))
        let (api, requests) = FakeNet.make { _ in .json(#"{"error":"boom"}"#, status: 500) }
        let clock = Clock(t0)
        let up = Uploader(store: store, api: api, now: { clock.now })
        await up.kick(reason: "close")
        #expect(requests().count == 1)  // first error stops the round
        #expect(await store.queue().count == 2)
        var st = await up.status
        #expect(st.inCoda == 2 && st.ultimoErrore?.contains("boom") == true && st.failures == 1)
        #expect(st.prossimoTentativo == t0.addingTimeInterval(30))

        // the timer respects the backoff, an explicit kick does not
        clock.advance(10)
        await up.tick()
        #expect(requests().count == 1)
        await up.kick(reason: "send now")
        #expect(requests().count == 2)
        st = await up.status
        #expect(st.failures == 2 && st.prossimoTentativo == t0.addingTimeInterval(10 + 60))
        clock.advance(61)
        await up.tick()
        #expect(requests().count == 3)
    }

    @Test func backoffSchedule() {
        let expected: [TimeInterval] = [30, 60, 120, 240, 480, 960, 1800, 1800, 1800]  // 30*2^6 = 1920 capped at 30 min
        #expect((1...9).map { Uploader.backoff(failures: $0) } == expected)
    }

    @Test func backoffResetsOnSuccess() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        _ = try await makeSegment(store, at: t0)
        let ok = Flag()
        let (api, _) = FakeNet.make { r in
            ok.value ? .json(#"{"sha256":"\#(r.headers["X-Sha256"] ?? "")"}"#) : FakeResponse(status: 503)
        }
        let clock = Clock(t0)
        let up = Uploader(store: store, api: api, now: { clock.now })
        await up.kick(reason: "a")
        await up.kick(reason: "b")
        #expect(await up.status.failures == 2)
        ok.value = true
        await up.kick(reason: "c")
        let st = await up.status
        #expect(st.failures == 0 && st.prossimoTentativo == nil && st.inCoda == 0 && st.ultimoInvio != nil)
    }

    @Test func shaMismatchOrInvalidBodyKeepsSegment() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        _ = try await makeSegment(store, at: t0)
        let mode = Counter()
        let (api, _) = FakeNet.make { _ in
            switch mode.next() {
            case 0: .json(#"{"sha256":"0000"}"#)
            case 1: .json("not json")
            default: .json("{}")
            }
        }
        let up = Uploader(store: store, api: api)
        for _ in 0..<3 { await up.kick(reason: "x") }
        #expect(await store.queue().count == 1)
        #expect(await up.status.failures == 3)
    }

    @Test func notConfiguredFails() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        _ = try await makeSegment(store, at: t0)
        let up = Uploader(store: store, api: nil)
        await up.kick(reason: "x")
        let notConfigured = await up.status.ultimoErrore
        #expect(notConfigured != nil)
        #expect(await store.queue().count == 1)
        let (api, _) = FakeNet.make { r in .json(#"{"sha256":"\#(r.headers["X-Sha256"] ?? "")"}"#) }
        await up.update(api: api)
        await up.kick(reason: "y")
        #expect(await store.queue().isEmpty)
    }

    @Test func networkErrorIsMapped() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        _ = try await makeSegment(store, at: t0)
        let (api, _) = FakeNet.make { _ in FakeResponse(error: URLError(.cannotConnectToHost)) }
        let up = Uploader(store: store, api: api)
        await up.kick(reason: "x")
        #expect(await up.status.ultimoErrore == MacError.unreachable.message)
    }
}

final class Flag: @unchecked Sendable {
    private let l = NSLock()
    private var v = false
    var value: Bool { get { l.withLock { v } } set { l.withLock { v = newValue } } }
}

final class Counter: @unchecked Sendable {
    private let l = NSLock()
    private var n = 0
    func next() -> Int { l.withLock { defer { n += 1 }; return n } }
}
