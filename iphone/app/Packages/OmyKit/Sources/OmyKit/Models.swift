import Foundation

// Mirrors of the Mac's JSON (PARITY §6, models.dart). Decoding is tolerant: null or missing fields become defaults,
// numbers may arrive as int or double, a malformed list element is skipped. Property names are camelCase, CodingKeys
// are the exact JSON keys.

extension KeyedDecodingContainer {
    func lenient<T: Decodable>(_ key: Key, _ def: T) -> T { ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? def }
    func opt<T: Decodable>(_ key: Key) -> T? { ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) }
    func int(_ key: Key, _ def: Int = 0) -> Int {
        if let v = (try? decodeIfPresent(Int.self, forKey: key)) ?? nil { return v }
        if let d = (try? decodeIfPresent(Double.self, forKey: key)) ?? nil { return Int(d.rounded()) }
        return def
    }
    func list<T: Decodable>(_ key: Key) -> [T] {
        guard let arr = try? nestedUnkeyedContainer(forKey: key) else { return [] }
        var arr2 = arr
        var out: [T] = []
        while !arr2.isAtEnd {
            if let v = try? arr2.decode(T.self) { out.append(v) } else { _ = try? arr2.decode(Skip.self) }
        }
        return out
    }
}

struct Skip: Decodable { init(from decoder: Decoder) throws {} }

/// Any JSON value, for the parts of the Mac's answers whose shape varies (the note).
enum JSONValue: Decodable, Sendable, Equatable {
    case null, bool(Bool), number(Double), string(String), array([JSONValue]), object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null } else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) } else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) } else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { self = .null }
    }

    var text: String? {
        switch self {
        case .string(let s): s
        case .number(let n): n == n.rounded() && abs(n) < 1e15 ? String(Int(n)) : String(n)
        case .bool(let b): String(b)
        default: nil
        }
    }
    subscript(_ k: String) -> JSONValue? { if case .object(let o) = self { o[k] } else { nil } }
    var items: [JSONValue] { if case .array(let a) = self { a } else { [] } }
}

public struct Speaker: Codable, Sendable, Equatable {
    public var name: String
    public var person: String?
    public var minutes: Double
    public var isMe: Bool { person == "me" }
    enum CodingKeys: String, CodingKey { case name, person, minutes }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        name = c.lenient(.name, ""); person = c.opt(.person); minutes = c.lenient(.minutes, 0)
    }
    public init(name: String, person: String? = nil, minutes: Double = 0) { self.name = name; self.person = person; self.minutes = minutes }
}

public struct DaySummary: Codable, Sendable, Equatable {
    public var day: String
    public var minutes: Int
    public var sessions: Int
    public var paragraphs: Int
    public var from: String?
    public var to: String?
    public var speakers: [Speaker]
    public var places: [String]
    public var unnamedPlaces: Bool
    enum CodingKeys: String, CodingKey { case day, minutes, sessions, paragraphs, from, to, speakers, places, unnamedPlaces = "unnamed_places" }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        day = c.lenient(.day, ""); minutes = c.int(.minutes); sessions = c.int(.sessions); paragraphs = c.int(.paragraphs)
        from = c.opt(.from); to = c.opt(.to); speakers = c.list(.speakers); places = c.list(.places)
        unnamedPlaces = c.lenient(.unnamedPlaces, false)
    }
}

public struct Progress: Codable, Sendable, Equatable {
    public var running: Bool
    public var sessionsTotal: Int
    public var sessionsDone: Int
    public var minutesTotal: Double
    public var minutesDone: Double
    public var fraction: Double? { minutesTotal > 0 ? min(1, max(0, minutesDone / minutesTotal)) : nil }
    enum CodingKeys: String, CodingKey {
        case running, sessionsTotal = "sessions_total", sessionsDone = "sessions_done", minutesTotal = "minutes_total", minutesDone = "minutes_done"
    }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        running = c.lenient(.running, false); sessionsTotal = c.int(.sessionsTotal); sessionsDone = c.int(.sessionsDone)
        minutesTotal = c.lenient(.minutesTotal, 0); minutesDone = c.lenient(.minutesDone, 0)
    }
    public init() { running = false; sessionsTotal = 0; sessionsDone = 0; minutesTotal = 0; minutesDone = 0 }
}

public struct Pending: Codable, Sendable, Equatable {
    public var sessions: Int
    public var openSessions: Int
    public var minutes: Double
    public var progress: Progress
    enum CodingKeys: String, CodingKey { case sessions, openSessions = "open_sessions", minutes, progress }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        sessions = c.int(.sessions); openSessions = c.int(.openSessions); minutes = c.lenient(.minutes, 0)
        progress = c.lenient(.progress, Progress())
    }
    public init() { sessions = 0; openSessions = 0; minutes = 0; progress = Progress() }
}


public struct DayList: Codable, Sendable, Equatable {
    public var days: [DaySummary]
    public var pending: Pending
    /// Today on the Mac (its time zone).
    public var today: String?
    enum CodingKeys: String, CodingKey { case days, pending, today }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        days = c.list(.days); pending = c.lenient(.pending, Pending()); today = c.opt(.today)
    }
}

public struct Place: Codable, Sendable, Equatable {
    public var name: String?
    public var lat: Double
    public var lon: Double
    public var acc: Double
    public var label: String { name ?? String(format: "%.4f, %.4f", lat, lon) }
    public init(name: String?, lat: Double, lon: Double, acc: Double = 0) { self.name = name; self.lat = lat; self.lon = lon; self.acc = acc }
    enum CodingKeys: String, CodingKey { case name, lat, lon, acc }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        name = c.opt(.name); lat = c.lenient(.lat, 0); lon = c.lenient(.lon, 0); acc = c.lenient(.acc, 0)
    }
}

public struct Via: Codable, Sendable, Equatable {
    public var kind: String
    public var app: String?
    public init(kind: String, app: String? = nil) { self.kind = kind; self.app = app }
    enum CodingKeys: String, CodingKey { case kind, app }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        kind = c.lenient(.kind, ""); app = c.opt(.app)
    }
}

/// One paragraph of transcripts/DAY.jsonl.
public struct Paragraph: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var day: String?
    public var session: String
    public var start: String
    public var end: String
    public var text: String
    public var speaker: String?
    public var person: String?
    public var language: String?
    public var via: Via?
    public var place: Place?
    public var isMe: Bool { person == "me" }
    enum CodingKeys: String, CodingKey { case id, day, session, start, end, text, speaker, person, language, via, place }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        id = c.lenient(.id, ""); day = c.opt(.day); session = c.lenient(.session, ""); start = c.lenient(.start, "")
        end = c.lenient(.end, ""); text = c.lenient(.text, ""); speaker = c.opt(.speaker); person = c.opt(.person)
        language = c.opt(.language); via = c.opt(.via); place = c.opt(.place)
    }
}

/// GET /v1/transcripts/{day}: `{summary, records}` (and `day`).
public struct Day: Codable, Sendable, Equatable {
    public var summary: DaySummary
    public var paragraphs: [Paragraph]
    enum CodingKeys: String, CodingKey { case day, summary, paragraphs = "records" }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        var s: DaySummary = try c.decodeIfPresent(DaySummary.self, forKey: .summary) ?? DaySummary.empty
        if let day: String = c.opt(.day) { s.day = day }
        summary = s
        paragraphs = c.list(.paragraphs)
    }
    public func encode(to e: Encoder) throws {
        var c = e.container(keyedBy: CodingKeys.self)
        try c.encode(summary.day, forKey: .day)
        try c.encode(summary, forKey: .summary)
        try c.encode(paragraphs, forKey: .paragraphs)
    }
}

extension DaySummary {
    static var empty: DaySummary { try! JSONDecoder().decode(DaySummary.self, from: Data("{}".utf8)) }
}

public struct SearchResult: Codable, Sendable, Equatable {
    public var results: [Paragraph]
    public var more: Bool
    enum CodingKeys: String, CodingKey { case results, more }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        results = c.list(.results); more = c.lenient(.more, false)
    }
}

public struct Me: Codable, Sendable, Equatable {
    public var name: String
    public var enrolled: Bool
    public var minutes: Double
    public var sessions: Int
    public var lastSeen: String?
    enum CodingKeys: String, CodingKey { case name, enrolled, minutes, sessions, lastSeen = "last_seen" }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        name = c.lenient(.name, ""); enrolled = c.lenient(.enrolled, false); minutes = c.lenient(.minutes, 0)
        sessions = c.int(.sessions); lastSeen = c.opt(.lastSeen)
    }
    init() { name = ""; enrolled = false; minutes = 0; sessions = 0; lastSeen = nil }
}

public struct Person: Codable, Sendable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var samples: Int
    public var sessions: Int
    public var minutes: Double
    public var lastSeen: String?
    enum CodingKeys: String, CodingKey { case id, name, samples, sessions, minutes, lastSeen = "last_seen" }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        id = c.lenient(.id, ""); name = c.lenient(.name, ""); samples = c.int(.samples); sessions = c.int(.sessions)
        minutes = c.lenient(.minutes, 0); lastSeen = c.opt(.lastSeen)
    }
}

/// For a named person: the kinds of conversation, how many, where.
public struct PersonIndex: Codable, Sendable, Equatable {
    public var types: [String]
    public var places: [String]
    public var conversations: Int
    enum CodingKeys: String, CodingKey { case types, places, conversations }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        types = c.list(.types); places = c.list(.places); conversations = c.int(.conversations)
    }
}

public struct People: Codable, Sendable, Equatable {
    public var me: Me
    public var people: [Person]
    /// By person id.
    public var index: [String: PersonIndex]
    enum CodingKeys: String, CodingKey { case me, people, index }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        me = c.lenient(.me, Me()); people = c.list(.people)
        index = ((try? c.decodeIfPresent([String: PersonIndex].self, forKey: .index)) ?? nil) ?? [:]
    }
}

public struct Voice: Codable, Sendable, Equatable {
    public var label: String
    public var name: String
    public var sample: String
    public var person: String?
    public var kind: String?
    public var app: String?
    public var source: String?
    public var pinned: Bool
    /// Somebody you don't know: no name, not asked again.
    public var unknown: Bool
    public var seconds: Double
    enum CodingKeys: String, CodingKey { case label, name, sample, person, kind, app, source, pinned, unknown, seconds }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        label = c.lenient(.label, ""); name = c.lenient(.name, ""); sample = c.lenient(.sample, ""); person = c.opt(.person)
        kind = c.opt(.kind); app = c.opt(.app); source = c.opt(.source); pinned = c.lenient(.pinned, false)
        unknown = c.lenient(.unknown, false); seconds = c.lenient(.seconds, 0)
    }
    /// "room", "meeting" or "media": set by hand, or what the Mac found.
    public var type: String { source ?? (kind == "media" ? "media" : kind == "call" ? "meeting" : "room") }
    /// Still to label: nobody yet, not a video, not unknown.
    public var todo: Bool { person == nil && type != "media" && !unknown }
}

public struct VoiceSession: Codable, Sendable, Equatable {
    public var session: String
    public var start: String?
    public var day: String?
    public var voices: [Voice]
    enum CodingKeys: String, CodingKey { case session, start, day, voices }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        session = c.lenient(.session, ""); start = c.opt(.start); day = c.opt(.day); voices = c.list(.voices)
    }
}

public struct DayVoices: Codable, Sendable, Equatable {
    public var day: String
    public var sessions: [VoiceSession]
    enum CodingKeys: String, CodingKey { case day, sessions }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        day = c.lenient(.day, ""); sessions = c.list(.sessions)
    }
}

public struct Unlabeled: Codable, Sendable, Equatable {
    public var sessions: [VoiceSession]
    enum CodingKeys: String, CodingKey { case sessions }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        sessions = c.list(.sessions)
    }
}

public struct NamedPlace: Codable, Sendable, Equatable {
    public var name: String
    public var lat: Double
    public var lon: Double
    public var radius: Int
    public var minutes: Int
    public var lastSeen: String?
    enum CodingKeys: String, CodingKey { case name, lat, lon, radius, minutes, lastSeen = "last_seen" }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        name = c.lenient(.name, ""); lat = c.lenient(.lat, 0); lon = c.lenient(.lon, 0); radius = c.int(.radius)
        minutes = c.int(.minutes); lastSeen = c.opt(.lastSeen)
    }
}

public struct NearestPlace: Codable, Sendable, Equatable {
    public var name: String
    public var meters: Int
    public init(name: String, meters: Int) { self.name = name; self.meters = meters }
    enum CodingKeys: String, CodingKey { case name, meters }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        name = c.lenient(.name, ""); meters = c.int(.meters)
    }
}

/// A place where you recorded, without a name.
public struct Spot: Codable, Sendable, Equatable {
    public var lat: Double
    public var lon: Double
    public var minutes: Int
    public var days: Int
    public var lastSeen: String?
    public var from: String
    public var to: String
    public var sample: String
    public var nearest: NearestPlace?
    enum CodingKeys: String, CodingKey { case lat, lon, minutes, days, lastSeen = "last_seen", from, to, sample, nearest }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        lat = c.lenient(.lat, 0); lon = c.lenient(.lon, 0); minutes = c.int(.minutes); days = c.int(.days)
        lastSeen = c.opt(.lastSeen); from = c.lenient(.from, ""); to = c.lenient(.to, ""); sample = c.lenient(.sample, "")
        nearest = c.opt(.nearest)
    }
}

public struct Places: Codable, Sendable, Equatable {
    public var places: [NamedPlace]
    public var unnamed: [Spot]
    enum CodingKeys: String, CodingKey { case places, unnamed }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        places = c.list(.places); unnamed = c.list(.unnamed)
    }
}

public struct Participant: Codable, Sendable, Equatable {
    public var name: String
    public var person: String?
    public var type: String
    public var minutes: Double
    public var isMe: Bool { person == "me" }
    enum CodingKeys: String, CodingKey { case name, person, type, minutes }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        name = c.lenient(.name, ""); person = c.opt(.person); type = c.lenient(.type, "room"); minutes = c.lenient(.minutes, 0)
    }
}

/// A session, seen from who spoke in it.
public struct Conversation: Codable, Sendable, Equatable, Identifiable {
    public var day: String
    public var session: String
    public var focus: String
    public var start: String
    public var end: String
    public var snippet: String
    public var place: String?
    public var app: String?
    public var minutes: Int
    public var types: [String]
    public var participants: [Participant]
    public var id: String { session }
    enum CodingKeys: String, CodingKey { case day, session, focus, start, end, snippet, place, app, minutes, types, participants }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        day = c.lenient(.day, ""); session = c.lenient(.session, ""); focus = c.lenient(.focus, ""); start = c.lenient(.start, "")
        end = c.lenient(.end, ""); snippet = c.lenient(.snippet, ""); place = c.opt(.place); app = c.opt(.app)
        minutes = c.int(.minutes); types = c.list(.types); participants = c.list(.participants)
    }
    /// Only where it is: for a paragraph found by a search.
    public init(of p: Paragraph) {
        day = p.day ?? ""; session = p.session; focus = p.id; start = p.start; end = p.end; snippet = ""
        place = p.place?.name; app = p.via?.kind == "call" ? p.via?.app : nil; minutes = 0; types = []; participants = []
    }
}

/// A page of conversations: whole days; `next` is where the next page starts (its `before`), nil after the last.
public struct ConversationList: Codable, Sendable, Equatable {
    public var conversations: [Conversation]
    public var next: String?
    public var more: Bool
    enum CodingKeys: String, CodingKey { case conversations, more, next }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        conversations = c.list(.conversations); more = c.lenient(.more, false)
        let n: String? = c.opt(.next)
        next = more ? n : nil
    }
    public init(conversations: [Conversation], next: String?) { self.conversations = conversations; self.next = next; more = next != nil }
    /// This page followed by `older`.
    public static func + (a: ConversationList, older: ConversationList) -> ConversationList {
        ConversationList(conversations: a.conversations + older.conversations, next: older.next)
    }
}

/// A source of an answer from POST /v1/ask: a note or a passage of text.
public struct Citation: Codable, Sendable, Equatable {
    public var n: Int
    public var kind: String
    public var session: String
    public var day: String
    /// The day the session started (it may cross midnight); old Macs omit it (then `day`).
    public var sessionDay: String
    public var time: String
    public var t: Double
    public var rowID: String
    public var title: String
    public var snippet: String
    enum CodingKeys: String, CodingKey {
        case n, kind, session, day, sessionDay = "session_day", time, t, rowID = "row_id", title, snippet
    }
    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        n = c.int(.n); kind = c.lenient(.kind, "testo"); session = c.lenient(.session, ""); day = c.lenient(.day, "")
        sessionDay = c.opt(.sessionDay) ?? day
        time = c.lenient(.time, ""); t = c.lenient(.t, 0); rowID = c.lenient(.rowID, ""); title = c.lenient(.title, "")
        snippet = c.lenient(.snippet, "")
    }
}

/// One turn sent back as context of the next question.
public struct AskTurn: Codable, Sendable, Equatable {
    public var role: String
    public var content: String
    public init(_ role: String, _ content: String) { self.role = role; self.content = content }
    public init(role: String, content: String) { self.init(role, content) }
}

/// The note of a session (GET /v1/brain/note/{session}): what the app shows, flattened to text like the Flutter app.
public struct Note: Codable, Sendable, Equatable {
    public var titolo: String
    public var data: String
    public var oraInizio: String
    public var riassunto: String
    public var puntiChiave: [String]
    public var impegni: [String]
    public var dateEventi: [String]
    public var entita: [String]

    enum CodingKeys: String, CodingKey {
        case titolo, data, oraInizio = "ora_inizio", riassunto, puntiChiave = "punti_chiave", impegni = "impegni_e_todo"
        case dateEventi = "date_ed_eventi_citati", entita
    }

    public init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        titolo = c.lenient(.titolo, ""); data = c.lenient(.data, ""); oraInizio = c.lenient(.oraInizio, ""); riassunto = c.lenient(.riassunto, "")
        func arr(_ k: CodingKeys) -> [JSONValue] { (try? c.decodeIfPresent([JSONValue].self, forKey: k)).flatMap { $0 } ?? [] }
        puntiChiave = arr(.puntiChiave).map { x in x["testo"] != nil ? (x["testo"]?.text ?? "") : (x.text ?? "") }
        impegni = arr(.impegni).map(Self.commitment)
        dateEventi = arr(.dateEventi).map(Self.event)
        var names: [String] = []
        if let groups = (try? c.decodeIfPresent(JSONValue.self, forKey: .entita)) ?? nil, case .object(let o) = groups {
            for g in o.keys.sorted() { for e in o[g]!.items { if let n = e["nome"], case .string(let s) = n { names.append(s) } } }
        }
        entita = names
    }

    private static func commitment(_ x: JSONValue) -> String {
        guard case .object = x else { return x.text ?? "" }
        let chi = x["chi"]?.text, verso = x["verso_chi"]?.text, due = x["scadenza"]?.text ?? x["scadenza_testo"]?.text
        var parts: [String] = []
        if let chi { parts.append(chi) }
        if let verso { parts.append("-> \(verso)") }
        parts.append(x["cosa"]?.text ?? "")
        if let due { parts.append("(\(due))") }
        return parts.joined(separator: " ")
    }

    private static func event(_ x: JSONValue) -> String {
        guard case .object = x else { return x.text ?? "" }
        let dt = x["data"]?.text ?? x["testo_data"]?.text
        let ev = x["evento"]?.text ?? ""
        return dt.map { "\($0): \(ev)" } ?? ev
    }
}

/// What the Mac streams while it answers (text/event-stream).
public enum AskEvent: Sendable, Equatable {
    /// "waking", "searching" or "answering".
    case status(String)
    case citations([Citation])
    case delta(String)
    case done(notFound: Bool, used: [Int], timings: [String: Int])
    case error(String)

    /// nil for events of a type this app doesn't know (newer Mac).
    static func from(json j: [String: Any]) -> AskEvent? {
        switch j["type"] as? String {
        case "status": return .status(j["stage"] as? String ?? "")
        case "citations":
            var items: [Citation] = []
            for i in (j["items"] as? [Any]) ?? [] {
                if JSONSerialization.isValidJSONObject(i), let d = try? JSONSerialization.data(withJSONObject: i),
                   let c = try? JSONDecoder().decode(Citation.self, from: d) { items.append(c) }
            }
            return .citations(items)
        case "delta": return .delta(j["text"] as? String ?? "")
        case "done":
            var timings: [String: Int] = [:]
            for (k, v) in (j["timings"] as? [String: Any]) ?? [:] { if let n = v as? NSNumber { timings[k] = Int(n.doubleValue.rounded()) } }
            let used = ((j["used"] as? [Any]) ?? []).compactMap { ($0 as? NSNumber)?.intValue }
            return .done(notFound: j["not_found"] as? Bool ?? false, used: used, timings: timings)
        case "error": return .error(j["message"] as? String ?? "")
        default: return nil
        }
    }
}

/// The events of a text/event-stream: one JSON per `data: {...}` line, fed in chunks cut anywhere (also inside a
/// multi-byte UTF-8 character: lines are decoded only when complete). Other lines (comments, keep-alives) and events
/// of unknown types are skipped.
public struct SSEParser: Sendable {
    private var buffer: [UInt8] = []
    public init() {}

    public mutating func feed(_ chunk: some Sequence<UInt8>) -> [AskEvent] {
        buffer.append(contentsOf: chunk)
        var out: [AskEvent] = []
        var from = 0
        var i = 0
        while i < buffer.count {
            if buffer[i] == 0x0A {
                if let e = Self.parse(line: buffer[from..<i]) { out.append(e) }
                from = i + 1
            }
            i += 1
        }
        if from > 0 { buffer.removeFirst(from) }
        return out
    }

    /// End of stream: a last line without a newline.
    public mutating func finish() -> [AskEvent] {
        defer { buffer.removeAll() }
        return Self.parse(line: buffer[...]).map { [$0] } ?? []
    }

    static func parse(line: ArraySlice<UInt8>) -> AskEvent? {
        var l = line
        if l.last == 0x0D { l = l.dropLast() }
        let prefix = Array("data:".utf8)
        guard l.count >= 5, Array(l.prefix(5)) == prefix else { return nil }
        let s = String(decoding: l.dropFirst(5), as: UTF8.self).trimmingCharacters(in: .whitespaces)
        guard !s.isEmpty, let o = try? JSONSerialization.jsonObject(with: Data(s.utf8)) as? [String: Any] else { return nil }
        return AskEvent.from(json: o)
    }
}
