import Foundation
@testable import OmyKit

/// A fresh temp directory, removed on deinit (use `defer`-free via the returned object's lifetime).
final class TempDir: @unchecked Sendable {
    let url: URL
    init() {
        url = FileManager.default.temporaryDirectory.appendingPathComponent("omykit-test-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    }
    var segments: URL { url.appendingPathComponent("segments", isDirectory: true) }
    func write(_ name: String, _ s: String) { try? Data(s.utf8).write(to: url.appendingPathComponent(name)) }
    deinit { try? FileManager.default.removeItem(at: url) }
}

func ls(_ dir: URL) -> [String] { ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []).sorted() }

func frame(_ packet: Int, _ fragment: Int, payload: [UInt8] = [0xAA, 0xBB]) -> Data {
    Data([UInt8(packet & 0xFF), UInt8((packet >> 8) & 0xFF), UInt8(fragment)] + payload)
}

let t0 = Date(timeIntervalSince1970: 1_700_000_000.123)  // 2023-11-14T22:13:20.123Z

func hex(_ d: Data) -> String { d.map { $0 < 16 ? "0" + String($0, radix: 16) : String($0, radix: 16) }.joined() }

// MARK: fake network

struct CapturedRequest: @unchecked Sendable {
    let url: URL
    let method: String
    let headers: [String: String]
    let body: Data
}

struct FakeResponse: @unchecked Sendable {
    var status = 200
    var headers: [String: String] = [:]
    var chunks: [Data] = []
    /// Pause before each chunk.
    var delay: TimeInterval = 0
    /// Never finish after the chunks (until the client cancels).
    var hang = false
    var error: URLError?
    static func json(_ s: String, status: Int = 200) -> FakeResponse { FakeResponse(status: status, chunks: [Data(s.utf8)]) }
}

/// URLProtocol whose answers come from handlers registered per host, so tests can run in parallel.
final class FakeNet: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) private static var handlers: [String: @Sendable (CapturedRequest) -> FakeResponse] = [:]
    nonisolated(unsafe) private static var log: [String: [CapturedRequest]] = [:]
    private static let lock = NSLock()
    private let stopped = Flag()

    final class Flag: @unchecked Sendable {
        private let l = NSLock()
        private var v = false
        var value: Bool { get { l.withLock { v } } set { l.withLock { v = newValue } } }
    }

    /// A session + API on a unique host; `requests()` returns what the handler saw.
    static func make(_ handler: @escaping @Sendable (CapturedRequest) -> FakeResponse) -> (api: MacAPI, requests: @Sendable () -> [CapturedRequest]) {
        let host = "t-\(UUID().uuidString.lowercased()).test"
        lock.withLock { handlers[host] = handler; log[host] = [] }
        let cfg = URLSessionConfiguration.ephemeral
        cfg.protocolClasses = [FakeNet.self]
        let api = MacAPI(baseURL: URL(string: "https://\(host)")!, token: "tok-123", session: URLSession(configuration: cfg))
        return (api, { lock.withLock { log[host] ?? [] } })
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let req = request
        var body = req.httpBody ?? Data()
        if let s = req.httpBodyStream {
            s.open()
            var buf = [UInt8](repeating: 0, count: 65536)
            while s.hasBytesAvailable {
                let n = s.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                body.append(contentsOf: buf[0..<n])
            }
            s.close()
        }
        var headers: [String: String] = [:]
        for (k, v) in req.allHTTPHeaderFields ?? [:] { headers[k] = v }
        let cap = CapturedRequest(url: req.url!, method: req.httpMethod ?? "GET", headers: headers, body: body)
        let host = req.url!.host!
        let (h, _) = Self.lock.withLock { () -> ((@Sendable (CapturedRequest) -> FakeResponse)?, Int) in
            Self.log[host, default: []].append(cap)
            return (Self.handlers[host], 0)
        }
        let r = h?(cap) ?? FakeResponse(status: 500)
        let stopped = self.stopped
        let client = self.client
        DispatchQueue.global().async { [self] in
            if let e = r.error {
                client?.urlProtocol(self, didFailWithError: e)
                return
            }
            let resp = HTTPURLResponse(url: cap.url, statusCode: r.status, httpVersion: "HTTP/1.1", headerFields: r.headers)!
            client?.urlProtocol(self, didReceive: resp, cacheStoragePolicy: .notAllowed)
            for c in r.chunks {
                if r.delay > 0 { Thread.sleep(forTimeInterval: r.delay) }
                if stopped.value { return }
                client?.urlProtocol(self, didLoad: c)
            }
            if r.hang {
                while !stopped.value { Thread.sleep(forTimeInterval: 0.01) }
                return
            }
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func stopLoading() { stopped.value = true }
}

final class FakeKeychain: KeychainStore, @unchecked Sendable {
    private let lock = NSLock()
    private var items: [String: String] = [:]
    var failWrites = false
    init(_ initial: [(service: String, account: String, value: String)] = []) {
        for i in initial { items["\(i.service)|\(i.account)"] = i.value }
    }
    func read(service: String, account: String) -> String? { lock.withLock { items["\(service)|\(account)"] } }
    func write(_ v: String, service: String, account: String) throws {
        if failWrites { throw KeychainError(status: -25308) }
        lock.withLock { items["\(service)|\(account)"] = v }
    }
    func delete(service: String, account: String) throws { lock.withLock { items["\(service)|\(account)"] = nil } }
}

func fakeDefaults() -> UserDefaults {
    let name = "omykit-test-\(UUID().uuidString)"
    let d = UserDefaults(suiteName: name)!
    d.removePersistentDomain(forName: name)
    return d
}

final class Clock: @unchecked Sendable {
    private let l = NSLock()
    private var t: Date
    init(_ t: Date) { self.t = t }
    var now: Date { l.withLock { t } }
    func advance(_ s: TimeInterval) { l.withLock { t = t.addingTimeInterval(s) } }
}

/// Writes a closed segment into `store` (via begin/append/finish) and returns its queue entry.
func makeSegment(_ store: SegmentStore, at start: Date, frames: Int = 3, extras: SidecarExtras = SidecarExtras()) async throws -> QueuedSegment {
    let s = try await store.begin(deviceID: "dev-1", codec: 21, now: start)
    for i in 0..<frames { try s.append(frame: frame(i, 0), at: start.addingTimeInterval(Double(i) * 0.02)) }
    try await store.finish(s, extras: extras, recovering: false)
    return await store.queue().first { $0.id == s.id }!
}
