import Foundation
import Testing
@testable import OmyKit

private let sseHeaders = ["Content-Type": "text/event-stream"]  // without it URLSession sniffs/buffers the first bytes

private let stream = """
: keep-alive

data: {"type":"status","stage":"waking"}

data: {"type":"citations","items":[{"n":1,"kind":"nota","session":"s1","day":"2026-11-21","session_day":"2026-11-20","time":"18:22","t":512.0,"row_id":"s1/3","title":"T","snippet":"S"},"bad"]}

data: {"type":"delta","text":"Ciao "}
data: {"type":"delta","text":"è ☃ 😀 [1]"}
event: ignored
data: {"type":"future","x":1}
data: not json
data:
data: {"type":"done","not_found":false,"used":[1],"timings":{"retrieval_ms":120,"ttft_ms":1800.4,"total_ms":3400}}

data: {"type":"error","message":"boom"}

"""

private let expected: [AskEvent] = {
    let c = try! JSONDecoder().decode(Citation.self, from: Data(#"{"n":1,"kind":"nota","session":"s1","day":"2026-11-21","session_day":"2026-11-20","time":"18:22","t":512.0,"row_id":"s1/3","title":"T","snippet":"S"}"#.utf8))
    return [.status("waking"), .citations([c]), .delta("Ciao "), .delta("è ☃ 😀 [1]"),
            .done(notFound: false, used: [1], timings: ["retrieval_ms": 120, "ttft_ms": 1800, "total_ms": 3400]), .error("boom")]
}()

@Suite struct SSEParserTests {
    @Test func wholeStreamInOneChunkWithMultipleEvents() {
        var p = SSEParser()
        #expect(p.feed(Data(stream.utf8)) == expected)
        #expect(p.finish().isEmpty)
    }

    @Test func everySplitPointGivesTheSameEvents() {
        let bytes = Array(stream.utf8)  // includes multi-byte sequences: every cut falls somewhere, also inside UTF-8
        for cut in 0...bytes.count {
            var p = SSEParser()
            var got = p.feed(bytes[..<cut])
            got += p.feed(bytes[cut...])
            got += p.finish()
            #expect(got == expected, "cut at \(cut)")
        }
    }

    @Test func byteByByte() {
        var p = SSEParser()
        var got: [AskEvent] = []
        for b in stream.utf8 { got += p.feed([b]) }
        #expect(got == expected)
    }

    @Test func crlfAndTrailingLineWithoutNewline() {
        var p = SSEParser()
        var got = p.feed(Data("data: {\"type\":\"delta\",\"text\":\"a\"}\r\ndata: {\"type\":\"delta\",\"text\":\"b\"}".utf8))
        #expect(got == [.delta("a")])  // second line not complete yet
        got = p.finish()
        #expect(got == [.delta("b")])
    }

    @Test func tolerantEvents() {
        var p = SSEParser()
        let got = p.feed(Data("""
        data:{"type":"done"}
        data: {"type":"status"}
        data: {"type":"delta"}
        data: {"type":"citations"}
        data: {"type":"error"}

        """.utf8))
        #expect(got == [.done(notFound: false, used: [], timings: [:]), .status(""), .delta(""), .citations([]), .error("")])
        var q = SSEParser()
        #expect(q.feed(Data(#"data: {"type":"done","not_found":true}"#.utf8 + [10])) == [.done(notFound: true, used: [], timings: [:])])
    }
}

@Suite struct AskTests {
    @Test func streamsEventsEndToEnd() async throws {
        let (api, reqs) = FakeNet.make { _ in
            let bytes = Array(stream.utf8)
            let mid = bytes.count / 2
            return FakeResponse(headers: sseHeaders, chunks: [Data(bytes[..<mid]), Data(bytes[mid...])], delay: 0.01)
        }
        let history = (0..<10).map { AskTurn($0 % 2 == 0 ? "user" : "assistant", "m\($0)") }
        var got: [AskEvent] = []
        for try await e in api.ask(question: "Cosa ho promesso?", history: history, now: t0, timeZone: TimeZone(identifier: "Europe/Rome")!) { got.append(e) }
        #expect(got == expected)
        let r = try #require(reqs().first)
        #expect(r.method == "POST" && r.url.path == "/v1/ask" && r.headers["Accept"] == "text/event-stream" && r.headers["Authorization"] == "Bearer tok-123")
        let body = try #require(JSONSerialization.jsonObject(with: r.body) as? [String: Any])
        #expect(body["question"] as? String == "Cosa ho promesso?" && body["now"] as? String == "2023-11-14T23:13:20+01:00")
        let h = try #require(body["history"] as? [[String: String]])
        #expect(h.count == 8 && h.first?["content"] == "m2" && h.last == ["role": "assistant", "content": "m9"])  // last 8 only
    }

    @Test func httpErrorBeforeStream() async throws {
        let (api, _) = FakeNet.make { _ in .json("{}", status: 401) }
        await #expect(throws: MacError.status(401)) { for try await _ in api.ask(question: "x", history: [], now: t0) {} }
        let (a2, _) = FakeNet.make { _ in FakeResponse(error: URLError(.cannotConnectToHost)) }
        await #expect(throws: MacError.unreachable) { for try await _ in a2.ask(question: "x", history: [], now: t0) {} }
    }

    @Test func idleTimeoutAfterSomeData() async throws {
        let (api, _) = FakeNet.make { _ in
            FakeResponse(headers: sseHeaders, chunks: [Data(#"data: {"type":"delta","text":"a"}"#.utf8 + [10])], hang: true)
        }
        var got: [AskEvent] = []
        let started = ContinuousClock.now
        do {
            for try await e in api.ask(question: "x", history: [], now: t0, idle: .milliseconds(1500)) { got.append(e) }
            Issue.record("should time out")
        } catch {
            #expect((error as? MacError) == MacError.stoppedAnswering)
        }
        #expect(got == [.delta("a")])
        #expect(ContinuousClock.now - started < .seconds(5))
    }

    @Test func slowButAliveStreamDoesNotTimeOut() async throws {
        // total time 3 s > idle 1.5 s, but a byte arrives every 0.5 s
        let lines = (0..<6).map { Data(#"data: {"type":"delta","text":"\#($0)"}"#.utf8 + [10]) }
        let (api, _) = FakeNet.make { _ in FakeResponse(headers: sseHeaders, chunks: lines, delay: 0.5) }
        var text = ""
        for try await e in api.ask(question: "x", history: [], now: t0, idle: .milliseconds(1500)) { if case .delta(let t) = e { text += t } }
        #expect(text == "012345")
    }

    @Test func cancellingStopsTheStream() async throws {
        let (api, _) = FakeNet.make { _ in FakeResponse(headers: sseHeaders, chunks: [Data(#"data: {"type":"status","stage":"waking"}"#.utf8 + [10])], hang: true) }
        let task = Task { () -> Int in
            var n = 0
            for try await _ in api.ask(question: "x", history: [], now: t0) { n += 1 }
            return n
        }
        try await Task.sleep(for: .milliseconds(200))
        task.cancel()
        let r = await task.result
        if case .failure(let e) = r { #expect(e is CancellationError) }
    }
}
