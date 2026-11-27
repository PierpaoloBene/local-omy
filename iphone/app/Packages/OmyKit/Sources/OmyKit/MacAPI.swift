import Foundation

/// An error to show, already in the app's language.
public struct MacError: Error, Sendable, Equatable, LocalizedError, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
    public var description: String { message }

    static var notConfigured: MacError { MacError(tr("Mac non configurato: URL e token in Impostazioni", "Mac not set up: URL and token in Settings")) }
    static var unreachable: MacError { MacError(tr("Server non raggiungibile (Mac o hub): Tailscale è attivo?", "Server not reachable (Mac or hub): is Tailscale on?")) }
    static var timedOut: MacError { MacError(tr("Il Mac non ha risposto in tempo", "The Mac did not answer in time")) }
    static var stoppedAnswering: MacError { MacError(tr("Il Mac ha smesso di rispondere", "The Mac stopped answering")) }
    static var invalidResponse: MacError {
        MacError(tr("Risposta del Mac non valida: aggiorna l'app sul Mac e sull'iPhone",
                    "The Mac's answer is not valid: update the app on the Mac and on the iPhone"))
    }

    /// 401 token; 502-504 receiver stopped (from tailscale serve); 404 Mac to update; other: generic.
    static func status(_ code: Int) -> MacError {
        if code == 401 { return MacError(tr("Token non valido (Impostazioni)", "Invalid token (Settings)")) }
        if (502...504).contains(code) {
            return MacError(tr("Il ricevitore sul Mac è fermo: riavvialo dall'app Mac (Impostazioni)",
                               "The receiver on the Mac is stopped: restart it from the Mac app (Settings)"))
        }
        if code == 404 { return MacError(tr("Il Mac va aggiornato: non conosce questa richiesta", "The Mac needs an update: it does not know this request")) }
        return MacError(tr("Errore dal Mac (HTTP \(code))", "Error from the Mac (HTTP \(code))"))
    }

    /// Status plus the `error` field of the body, translated (omy speaks English).
    static func from(status code: Int, body: Data) -> MacError {
        if code == 401 || code == 404 || (502...504).contains(code) { return .status(code) }
        let detail = ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any])?["error"] as? String
        return translated(detail).map(MacError.init) ?? .status(code)
    }

    static func translated(_ e: String?) -> String? {
        guard let e else { return nil }
        if e.contains("voice prints are turned off") { return tr("Impronte vocali disattivate (Impostazioni, Funzioni)", "Voice prints are off (Settings, Features)") }
        if e.contains("no voice print") {
            return tr("Questa voce non ha un'impronta (troppo breve o cancellata)", "This voice has no voice print (too short, or deleted)")
        }
        if e.contains("already used") { return tr("Nome già usato da un altro luogo", "Name already used by another place") }
        if e.contains("invalid name") { return tr("Nome non valido", "Invalid name") }
        if e.hasPrefix("no place") { return tr("Luogo non trovato: forse è stato cambiato dal Mac", "Place not found: maybe it was changed on the Mac") }
        if e.hasPrefix("no person") { return tr("Persona non trovata: forse è stata cambiata dal Mac", "Person not found: maybe it was changed on the Mac") }
        return e
    }

    static func transport(_ e: Error) -> Error {
        if e is CancellationError { return e }
        if e is MacError { return e }
        if let u = e as? URLError {
            if u.code == .cancelled { return CancellationError() }
            return u.code == .timedOut ? MacError.timedOut : MacError.unreachable
        }
        return MacError.timedOut
    }
}

/// The Mac's receiver (omy_receiver/app.py), same URL and token for uploads and review. Nothing fetched is stored on
/// the phone.
public struct MacAPI: Sendable {
    static let readTimeout: TimeInterval = 30
    /// A change rewrites every day with that person or place.
    static let changeTimeout: TimeInterval = 300
    static let uploadTimeout: TimeInterval = 60
    /// No byte for this long in an answer: the Mac is stuck.
    public static let askIdle: Duration = .seconds(90)

    public let baseURL: URL
    public let token: String
    public let session: URLSession

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    // MARK: uploads

    /// POST /v1/segments/{id}: Bearer, X-Segment-Id, X-Sha256, X-Segment-Meta (base64url of the sidecar bytes),
    /// X-Queue-Remaining. Succeeds only on 200 with a JSON body whose sha256 equals the sidecar's; throws otherwise.
    public func uploadSegment(_ q: QueuedSegment, remaining: Int) async throws {
        let bin: Data, metaBytes: Data
        do {
            bin = try Data(contentsOf: q.binURL)
            metaBytes = try Data(contentsOf: q.jsonURL)
        } catch { throw MacError("\(q.id.raw): \(error.localizedDescription)") }
        guard let sha = ((try? JSONSerialization.jsonObject(with: metaBytes)) as? [String: Any])?["sha256"] as? String else {
            throw MacError("\(q.id.raw): sidecar without sha256")
        }
        var req = try makeRequest("POST", "/v1/segments/\(q.id.raw)", timeout: Self.uploadTimeout)
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        req.setValue(q.id.raw, forHTTPHeaderField: "X-Segment-Id")
        req.setValue(sha, forHTTPHeaderField: "X-Sha256")
        req.setValue(Self.base64URL(metaBytes), forHTTPHeaderField: "X-Segment-Meta")
        req.setValue(String(remaining), forHTTPHeaderField: "X-Queue-Remaining")
        let (data, status): (Data, Int)
        do {
            let (d, r) = try await session.upload(for: req, from: bin)
            (data, status) = (d, (r as? HTTPURLResponse)?.statusCode ?? 0)
        } catch { throw MacError.transport(error) }
        guard status == 200 else {
            let m = MacError.from(status: status, body: data)
            throw MacError("\(q.id.raw): \(m.message)")
        }
        guard let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw MacError("\(q.id.raw): 200 with invalid body")
        }
        guard body["sha256"] as? String == sha else { throw MacError("\(q.id.raw): 200 but sha256 mismatch, kept locally") }
    }

    static func base64URL(_ d: Data) -> String {
        d.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
    }

    // MARK: transcripts

    public func days() async throws -> DayList { try await get("/v1/transcripts") }
    public func day(_ day: String) async throws -> Day {
        var d: Day = try await get("/v1/transcripts/\(Self.component(day))")
        if d.summary.day.isEmpty { d.summary.day = day }
        return d
    }

    /// Paragraphs with `q`, only in the conversations with `person` and of `type`.
    public func search(_ q: String, person: String? = nil, type: String? = nil) async throws -> SearchResult {
        var b: [String: Any] = ["q": q]
        if let person { b["person"] = person }
        if let type { b["type"] = type }
        return try Self.decode(try await send("POST", "/v1/search", body: b, timeout: Self.readTimeout))
    }

    /// The sessions with `person` (an id or "me"), of `type` (room, meeting, call, media), of `day` or of the days
    /// before `before` (the next page), newest first: whole days, at least `limit` (Mac default 100).
    public func conversations(person: String? = nil, type: String? = nil, day: String? = nil, before: String? = nil, limit: Int? = nil) async throws -> ConversationList {
        try await get("/v1/conversations", query: [("person", person), ("type", type), ("day", day), ("before", before), ("limit", limit.map(String.init))])
    }

    /// The sessions started in the last `hours` with voices still to label.
    public func unlabeled(hours: Int) async throws -> Unlabeled { try await get("/v1/unlabeled", query: [("hours", String(hours))]) }

    /// The note of `session`; nil if the Mac has none (404).
    public func note(session: String) async throws -> Note? {
        let req = try makeRequest("GET", "/v1/brain/note/\(Self.component(session))", timeout: Self.readTimeout)
        let (data, status) = try await perform(req)
        if status == 404 { return nil }
        guard status == 200 else { throw MacError.from(status: status, body: data) }
        return try Self.decode(data)
    }

    public func transcribeNow() async throws { _ = try await send("POST", "/v1/transcribe", body: nil, timeout: Self.changeTimeout) }

    // MARK: people and voices

    public func people() async throws -> People { try await get("/v1/people") }
    public func renamePerson(_ id: String, name: String) async throws { _ = try await send("PUT", "/v1/people/\(id)", body: ["name": name], timeout: Self.changeTimeout) }
    public func deletePerson(_ id: String) async throws { _ = try await send("DELETE", "/v1/people/\(id)", body: nil, timeout: Self.changeTimeout) }

    /// Where all the voices of a person come from: room, meeting, auto, or media (it was a video: the name and voice
    /// print go).
    public func setPersonKind(_ id: String, source: String) async throws {
        _ = try await send("POST", "/v1/people/\(id)/kind", body: ["source": source], timeout: Self.changeTimeout)
    }

    public func voices(day: String) async throws -> DayVoices { try await get("/v1/voices/\(day)") }
    public func nameVoice(session: String, label: String, name: String) async throws {
        _ = try await send("PUT", "/v1/voices/\(session)/\(label)", body: ["name": name], timeout: Self.changeTimeout)
    }

    /// Where these voices come from: room, meeting, media, or auto.
    public func setVoiceKind(session: String, labels: [String], source: String) async throws {
        _ = try await send("POST", "/v1/voices/\(session)/kind", body: ["source": source, "labels": labels], timeout: Self.changeTimeout)
    }

    /// Somebody you don't know: no name, no voice print, not asked again.
    public func setVoiceUnknown(session: String, labels: [String]) async throws {
        _ = try await send("POST", "/v1/voices/\(session)/unknown", body: ["labels": labels, "unknown": true], timeout: Self.changeTimeout)
    }

    /// A few seconds of the voice, as WAV.
    public func voiceSample(session: String, label: String) async throws -> Data {
        try await send("GET", "/v1/voices/\(session)/\(label)/sample", body: nil, timeout: Self.readTimeout)
    }

    // MARK: places and settings

    public func places() async throws -> Places { try await get("/v1/places") }
    public func addPlace(name: String, lat: Double, lon: Double, radius: Int) async throws {
        _ = try await send("POST", "/v1/places", body: ["name": name, "lat": lat, "lon": lon, "radius": radius], timeout: Self.changeTimeout)
    }
    public func renamePlace(_ name: String, to: String) async throws {
        _ = try await send("PUT", "/v1/places/\(Self.component(name))", body: ["name": to], timeout: Self.changeTimeout)
    }
    public func removePlace(_ name: String) async throws { _ = try await send("DELETE", "/v1/places/\(Self.component(name))", body: nil, timeout: Self.changeTimeout) }

    /// PUT /v1/settings {features: {key: {on, at}}}: ours in, the merged ones out.
    public func putSettings(features: Features) async throws -> [String: FeatureValue] {
        let req = try makeRequest("PUT", "/v1/settings", body: try features.settingsBody(), timeout: 20)
        let (data, status) = try await perform(req)
        guard status == 200 else { throw MacError.from(status: status, body: data) }
        guard let o = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let f = o["features"] as? [String: Any] else {
            throw MacError.invalidResponse
        }
        return Features.clean(f)
    }

    // MARK: ask (SSE)

    /// Asks the archive: the answer streams as events (status, citations, deltas, done). `history`: the last turns of
    /// the chat (at most 8 are sent). Throws `MacError` on HTTP errors, on 30 s without a response and on `idle`
    /// without a byte (default 90 s).
    public func ask(question: String, history: [AskTurn], now: Date, idle: Duration = MacAPI.askIdle, timeZone: TimeZone = .current) -> AsyncThrowingStream<AskEvent, Error> {
        AsyncThrowingStream { cont in
            let task = Task {
                do {
                    try await self.runAsk(question, history, now, idle, timeZone, cont)
                    cont.finish()
                } catch {
                    cont.finish(throwing: error)
                }
            }
            cont.onTermination = { _ in task.cancel() }
        }
    }

    private func runAsk(_ question: String, _ history: [AskTurn], _ now: Date, _ idle: Duration, _ tz: TimeZone,
                        _ cont: AsyncThrowingStream<AskEvent, Error>.Continuation) async throws {
        let turns = history.count > 8 ? Array(history.suffix(8)) : history
        let body: [String: Any] = [
            "question": question,
            "history": turns.map { ["role": $0.role, "content": $0.content] },
            "now": ISO8601UTC.withOffset(now, timeZone: tz),
        ]
        let headerLimit = min(Duration.seconds(30), idle)
        var req = try makeRequest("POST", "/v1/ask", body: try JSONSerialization.data(withJSONObject: body),
                                  timeout: Double(max(idle.components.seconds, 30)) + 30)
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let gate = IdleGate()
        let session = self.session
        let reader = Task { () throws -> Void in
            let (bytes, resp) = try await session.bytes(for: req)
            gate.responded()
            let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard status == 200 else { throw MacError.status(status) }
            var parser = SSEParser()
            var line: [UInt8] = []
            for try await b in bytes {
                gate.touch()
                line.append(b)
                if b == 0x0A {
                    for e in parser.feed(line) { cont.yield(e) }
                    line.removeAll(keepingCapacity: true)
                }
            }
            for e in parser.feed(line) { cont.yield(e) }
            for e in parser.finish() { cont.yield(e) }
        }
        let tick = min(idle / 4, .seconds(1))
        let dog = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: max(tick, .milliseconds(5)))
                if gate.idleFor() > (gate.isResponded ? idle : headerLimit) {
                    gate.markTimedOut()
                    reader.cancel()
                    return
                }
            }
        }
        defer { dog.cancel() }
        do {
            try await withTaskCancellationHandler { try await reader.value } onCancel: { reader.cancel() }
        } catch {
            if gate.didTimeOut { throw gate.isResponded ? MacError.stoppedAnswering : MacError.timedOut }
            throw MacError.transport(error)
        }
    }

    // MARK: a question the hub kept for the Mac

    /// GET /v1/ask/{id}: a question that was queued because the Mac was off. `state` is "pending" (waiting for the
    /// Mac), "leased" (the Mac is writing it), "done", "failed", "expired" or "cancelled".
    public struct AskResult: Decodable, Sendable {
        public var state: String
        public var answer: String
        public var citations: [Citation]
        public var notFound: Bool
        public var error: String?
        enum CodingKeys: String, CodingKey { case state, answer, citations, notFound = "not_found", error }
    }

    public func askResult(_ id: String) async throws -> AskResult { try await get("/v1/ask/\(Self.component(id))") }

    // MARK: plumbing

    static func component(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")) ?? s
    }

    func makeRequest(_ method: String, _ path: String, query: [(String, String?)] = [], body: Data? = nil, timeout: TimeInterval) throws -> URLRequest {
        var base = baseURL.absoluteString
        while base.hasSuffix("/") { base.removeLast() }
        guard var c = URLComponents(string: base + path) else { throw MacError.notConfigured }
        let items = query.compactMap { k, v in v.map { URLQueryItem(name: k, value: $0) } }
        if !items.isEmpty { c.queryItems = items }
        guard let url = c.url else { throw MacError.notConfigured }
        var r = URLRequest(url: url, timeoutInterval: timeout)
        r.httpMethod = method
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = body
        }
        return r
    }

    func perform(_ req: URLRequest) async throws -> (Data, Int) {
        do {
            let (d, r) = try await session.data(for: req)
            return (d, (r as? HTTPURLResponse)?.statusCode ?? 0)
        } catch { throw MacError.transport(error) }
    }

    /// Returns the body of a 200; maps every other status like the Flutter app.
    @discardableResult
    func send(_ method: String, _ path: String, query: [(String, String?)] = [], body: [String: Any]?, timeout: TimeInterval) async throws -> Data {
        let data = try body.map { try JSONSerialization.data(withJSONObject: $0) }
        let req = try makeRequest(method, path, query: query, body: data, timeout: timeout)
        let (d, status) = try await perform(req)
        guard status == 200 else { throw MacError.from(status: status, body: d) }
        return d
    }

    func get<T: Decodable>(_ path: String, query: [(String, String?)] = []) async throws -> T {
        try Self.decode(try await send("GET", path, query: query, body: nil, timeout: Self.readTimeout))
    }

    /// An answer the phone doesn't understand (the two apps at different versions) is an error to show.
    static func decode<T: Decodable>(_ d: Data) throws -> T {
        do { return try JSONDecoder().decode(T.self, from: d) } catch { throw MacError.invalidResponse }
    }
}

/// Last activity of a streaming answer, read by the watchdog.
final class IdleGate: @unchecked Sendable {
    private let lock = NSLock()
    private var last = ContinuousClock.now
    private var gotResponse = false
    private var timedOut = false
    func touch() { lock.withLock { last = .now } }
    func responded() { lock.withLock { gotResponse = true; last = .now } }
    var isResponded: Bool { lock.withLock { gotResponse } }
    func idleFor() -> Duration { lock.withLock { .now - last } }
    func markTimedOut() { lock.withLock { timedOut = true } }
    var didTimeOut: Bool { lock.withLock { timedOut } }
}
