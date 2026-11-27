import Foundation
import Testing
@testable import OmyKit

// Fixtures are crafted from models.dart / PARITY §6 field lists: fake data only.

private let dayList = #"""
{"days":[{"day":"2026-11-21","minutes":125,"sessions":4,"paragraphs":60,"from":"08:10","to":"21:40",
 "speakers":[{"name":"Io","person":"me","minutes":50.5},{"name":"Anna","person":"p1","minutes":20},{"name":"Voce 2","person":null,"minutes":3}],
 "places":["Casa","Ufficio"],"unnamed_places":true},
 {"day":"2026-11-20","minutes":0,"sessions":0,"paragraphs":0,"from":null,"to":null,"speakers":[],"places":[],"unnamed_places":false}],
 "pending":{"sessions":2,"open_sessions":1,"minutes":14.5,"progress":{"running":true,"sessions_total":3,"sessions_done":1,"minutes_total":30,"minutes_done":7.5}},
 "today":"2026-11-23"}
"""#

private let dayJSON = #"""
{"day":"2026-11-21","summary":{"minutes":5,"sessions":1,"paragraphs":2,"from":"09:00","to":"09:05","speakers":[],"places":[],"unnamed_places":false},
 "records":[
  {"id":"s1/0","day":"2026-11-21","session":"s1","start":"2026-11-21T09:00:00+01:00","end":"2026-11-21T09:00:20+01:00","text":"Ciao, come stai?","speaker":"S0","person":"me","language":"it",
   "via":{"kind":"call","app":"WhatsApp"},"place":{"name":"Casa","lat":45.1,"lon":9.2,"acc":30}},
  {"id":"s1/1","session":"s1","start":"2026-11-21T09:00:20+01:00","end":"2026-11-21T09:00:40+01:00","text":"Bene è ☃","speaker":null,"person":null,"language":null,"via":null,"place":null}
 ]}
"""#

private let conversationsJSON = #"""
{"conversations":[{"day":"2026-11-21","session":"s1","focus":"s1/0","start":"2026-11-21T09:00:00+01:00","end":"2026-11-21T09:05:00+01:00","snippet":"ciao","place":null,"app":"Zoom","minutes":5,
  "types":["call"],"participants":[{"name":"Anna","person":"p1","type":"call","minutes":4.5}]}],"more":true,"next":"2026-11-20"}
"""#

private let unlabeledJSON = #"""
{"sessions":[{"session":"s1","start":"2026-11-23T09:00:00+01:00","day":"2026-11-23","voices":[
 {"label":"S1","name":"Voce 1","sample":"s1-1.wav","person":null,"kind":"call","app":"Zoom","source":null,"pinned":false,"unknown":false,"seconds":12.5},
 {"label":"S2","name":"Voce 2","sample":"s1-2.wav","person":"p9","kind":null,"app":null,"source":"room","pinned":true,"seconds":3}]}]}
"""#

private let peopleJSON = #"""
{"me":{"name":"Pierpaolo","enrolled":true,"minutes":300.5,"sessions":40,"last_seen":"2026-11-23"},
 "people":[{"id":"p1","name":"Anna","samples":3,"sessions":5,"minutes":22.25,"last_seen":null}],
 "index":{"p1":{"types":["room","call"],"places":["Casa"],"conversations":5}}}
"""#

private let placesJSON = #"""
{"places":[{"name":"Casa","lat":45.1,"lon":9.2,"radius":100,"minutes":900,"last_seen":"2026-11-23"}],
 "unnamed":[{"lat":45.2,"lon":9.3,"minutes":50,"days":3,"last_seen":"2026-11-22","from":"2026-11-20","to":"2026-11-22","sample":"s/1",
  "nearest":{"name":"Casa","meters":340}},{"lat":1,"lon":2,"minutes":1,"days":1,"last_seen":null,"from":"a","to":"b","sample":"c","nearest":null}]}
"""#

private let noteJSON = #"""
{"titolo":"Riunione","data":"2026-11-21","ora_inizio":"09:00","riassunto":"Si è parlato di X.",
 "punti_chiave":[{"testo":"Punto uno"},"Punto due"],
 "impegni_e_todo":[{"chi":"Io","verso_chi":"Anna","cosa":"Mandare il file","scadenza":"venerdi"},{"cosa":"Solo cosa","scadenza_testo":null},"testo libero"],
 "date_ed_eventi_citati":[{"data":"3 novembre","evento":"Cena"},{"testo_data":"domani","evento":"Call"},{"evento":"Senza data"}],
 "entita":{"persone":[{"nome":"Anna"},{"nome":"Marco"}],"luoghi":[{"nome":"Roma"}]}}
"""#

@Suite struct MacAPIDecodingTests {
    /// A question the hub kept while the Mac was off: waiting, then answered.
    @Test func queuedAsk() async throws {
        let waiting = #"{"id":"0123456789abcdef","question":"q","state":"pending","created":1,"finished":null,"answer":"","citations":[],"used":[],"not_found":false,"error":null}"#
        let answered = #"{"id":"0123456789abcdef","question":"q","state":"done","created":1,"finished":2,"answer":"Il giardino [1].","citations":[{"n":1,"kind":"testo","session":"s1","day":"2026-11-21","time":"09:00","t":0,"row_id":"s1/0","title":"T","snippet":"x"}],"used":[1],"not_found":false,"error":null}"#
        let (apiWaiting, _) = FakeNet.make { _ in .json(waiting) }
        let a = try await apiWaiting.askResult("0123456789abcdef")
        #expect(a.state == "pending" && a.answer.isEmpty && a.citations.isEmpty)
        let (api, reqs) = FakeNet.make { _ in .json(answered) }
        let b = try await api.askResult("0123456789abcdef")
        #expect(b.state == "done" && b.answer == "Il giardino [1]." && b.citations.first?.session == "s1" && !b.notFound)
        #expect(reqs().first?.url.path == "/v1/ask/0123456789abcdef" && reqs().first?.method == "GET")
    }

    @Test func days() async throws {
        let (api, reqs) = FakeNet.make { _ in .json(dayList) }
        let d = try await api.days()
        #expect(reqs().first?.url.path == "/v1/transcripts" && reqs().first?.method == "GET")
        #expect(reqs().first?.headers["Authorization"] == "Bearer tok-123")
        #expect(d.today == "2026-11-23" && d.days.count == 2)
        #expect(d.days[0].minutes == 125 && d.days[0].speakers[0].isMe && d.days[0].speakers[0].minutes == 50.5 && d.days[0].speakers[2].person == nil)
        #expect(d.days[0].places == ["Casa", "Ufficio"] && d.days[0].unnamedPlaces && d.days[1].from == nil)
        #expect(d.pending.openSessions == 1 && d.pending.progress.running && d.pending.progress.fraction == 0.25)
    }

    @Test func day() async throws {
        let (api, reqs) = FakeNet.make { _ in .json(dayJSON) }
        let d = try await api.day("2026-11-21")
        #expect(reqs().first?.url.path == "/v1/transcripts/2026-11-21")
        #expect(d.summary.day == "2026-11-21" && d.summary.paragraphs == 2 && d.paragraphs.count == 2)
        let p = d.paragraphs[0]
        #expect(p.isMe && p.via == Via(kind: "call", app: "WhatsApp") && p.place?.name == "Casa" && p.place?.acc == 30 && p.language == "it")
        #expect(d.paragraphs[1].text == "Bene è ☃" && d.paragraphs[1].via == nil && d.paragraphs[1].place == nil && d.paragraphs[1].day == nil)
        #expect(Place(name: nil, lat: 45.1236, lon: 9.1).label == "45.1236, 9.1000")
    }

    @Test func search() async throws {
        let (api, reqs) = FakeNet.make { _ in .json(#"{"results":[{"id":"s1/0","day":"2026-11-21","session":"s1","start":"a","end":"b","text":"ciao"}],"more":false}"#) }
        let r = try await api.search("ciao", person: "me", type: nil)
        #expect(r.results.count == 1 && r.results[0].text == "ciao" && !r.more)
        let body = try #require(JSONSerialization.jsonObject(with: reqs()[0].body) as? [String: String])
        #expect(body == ["q": "ciao", "person": "me"] && reqs()[0].method == "POST" && reqs()[0].headers["Content-Type"] == "application/json")
    }

    @Test func conversations() async throws {
        let (api, reqs) = FakeNet.make { _ in .json(conversationsJSON) }
        let c = try await api.conversations(person: "p1", type: "call", day: nil, before: "2026-11-21", limit: 1000)
        let items = URLComponents(url: reqs()[0].url, resolvingAgainstBaseURL: false)!.queryItems!
        #expect(Set(items.map { "\($0.name)=\($0.value!)" }) == ["person=p1", "type=call", "before=2026-11-21", "limit=1000"])
        #expect(c.next == "2026-11-20" && c.conversations.count == 1)
        let v = c.conversations[0]
        #expect(v.focus == "s1/0" && v.app == "Zoom" && v.place == nil && v.types == ["call"] && v.participants[0].minutes == 4.5 && !v.participants[0].isMe)
        // no "more" -> no next
        let (api2, _) = FakeNet.make { _ in .json(#"{"conversations":[],"more":false,"next":"2026-01-01"}"#) }
        #expect(try await api2.conversations().next == nil)
        let both = ConversationList(conversations: [v], next: "a") + ConversationList(conversations: [v, v], next: nil)
        #expect(both.conversations.count == 3 && both.next == nil)
    }

    @Test func unlabeledAndVoices() async throws {
        let (api, reqs) = FakeNet.make { r in r.url.path.hasPrefix("/v1/voices") ? .json(#"{"day":"2026-11-23","sessions":[{"session":"s1","voices":[]}]}"#) : .json(unlabeledJSON) }
        let u = try await api.unlabeled(hours: 24)
        #expect(reqs()[0].url.query == "hours=24")
        let v = u.sessions[0].voices
        #expect(v[0].todo && v[0].type == "meeting" && v[0].seconds == 12.5 && !v[0].unknown)
        #expect(!v[1].todo && v[1].type == "room" && v[1].pinned && v[1].unknown == false && v[1].kind == nil)
        let dv = try await api.voices(day: "2026-11-23")
        #expect(dv.day == "2026-11-23" && dv.sessions.count == 1 && reqs()[1].url.path == "/v1/voices/2026-11-23")
    }

    @Test func people() async throws {
        let (api, _) = FakeNet.make { _ in .json(peopleJSON) }
        let p = try await api.people()
        #expect(p.me.name == "Pierpaolo" && p.me.enrolled && p.me.sessions == 40 && p.me.lastSeen == "2026-11-23")
        #expect(p.people[0].id == "p1" && p.people[0].minutes == 22.25 && p.people[0].lastSeen == nil)
        #expect(p.index["p1"]?.conversations == 5 && p.index["p1"]?.types == ["room", "call"])
    }

    @Test func places() async throws {
        let (api, _) = FakeNet.make { _ in .json(placesJSON) }
        let p = try await api.places()
        #expect(p.places[0].radius == 100 && p.places[0].minutes == 900)
        #expect(p.unnamed[0].nearest == NearestPlace(name: "Casa", meters: 340) && p.unnamed[0].days == 3 && p.unnamed[1].nearest == nil && p.unnamed[1].lastSeen == nil)
    }

    @Test func note() async throws {
        let (api, reqs) = FakeNet.make { r in r.url.path.hasSuffix("none") ? FakeResponse(status: 404) : .json(noteJSON) }
        let n = try #require(try await api.note(session: "s 1/x"))
        #expect(reqs()[0].url.absoluteString.hasSuffix("/v1/brain/note/s%201%2Fx"))
        #expect(n.titolo == "Riunione" && n.oraInizio == "09:00" && n.puntiChiave == ["Punto uno", "Punto due"])
        #expect(n.impegni == ["Io -> Anna Mandare il file (venerdi)", "Solo cosa", "testo libero"])
        #expect(n.dateEventi == ["3 novembre: Cena", "domani: Call", "Senza data"])
        #expect(n.entita == ["Roma", "Anna", "Marco"])  // groups in key order (luoghi, persone)
        #expect(try await api.note(session: "none") == nil)
    }

    @Test func toleratesNullsAndMissingFields() async throws {
        let (api, _) = FakeNet.make { _ in .json(#"{"days":[{"day":"2026-01-01","minutes":null,"speakers":null,"places":[1,"a"]},"garbage",{"day":"2026-01-02","minutes":3.0}],"pending":null,"today":null}"#) }
        let d = try await api.days()
        #expect(d.days.count == 2 && d.days[0].minutes == 0 && d.days[0].speakers.isEmpty && d.days[1].minutes == 3)
        #expect(d.pending.sessions == 0 && d.today == nil && d.pending.progress.fraction == nil)
        let (a2, _) = FakeNet.make { _ in .json("{}") }
        let p = try await a2.people()
        #expect(p.people.isEmpty && p.index.isEmpty && p.me.name == "")
        let pl = try await a2.places(), cv = try await a2.conversations()
        #expect(pl.unnamed.isEmpty && cv.conversations.isEmpty)
        #expect(try await a2.day("2026-01-01").summary.day == "2026-01-01")
        let (a3, _) = FakeNet.make { _ in .json("[1,2]") }
        await #expect(throws: MacError.invalidResponse) { _ = try await a3.days() }
    }

    @Test func citationDefaultsAndRoundTrip() throws {
        let c = try JSONDecoder().decode(Citation.self, from: Data(#"{"n":1,"kind":"nota","session":"s","day":"2026-11-21","time":"18:22","t":512,"row_id":"s/3","title":"T","snippet":"S"}"#.utf8))
        #expect(c.sessionDay == "2026-11-21" && c.rowID == "s/3" && c.t == 512 && c.kind == "nota")
        let c2 = try JSONDecoder().decode(Citation.self, from: Data(#"{"n":2,"session_day":"2026-11-20","day":"2026-11-21"}"#.utf8))
        #expect(c2.sessionDay == "2026-11-20" && c2.kind == "testo" && c2.snippet == "")
        let again = try JSONDecoder().decode(Citation.self, from: try JSONEncoder().encode(c))
        #expect(again == c)  // Codable both ways, exact keys
        let json = String(decoding: try JSONEncoder().encode(c), as: UTF8.self)
        #expect(json.contains("\"row_id\"") && json.contains("\"session_day\""))
        let turn = try JSONEncoder().encode(AskTurn("user", "ciao"))
        #expect(try JSONSerialization.jsonObject(with: turn) as? [String: String] == ["role": "user", "content": "ciao"])
    }

    @Test func modelsEncodeWithServerKeys() throws {
        let d = try JSONDecoder().decode(DayList.self, from: Data(dayList.utf8))
        let o = try #require(JSONSerialization.jsonObject(with: try JSONEncoder().encode(d)) as? [String: Any])
        let first = try #require((o["days"] as? [[String: Any]])?.first)
        #expect(first["unnamed_places"] as? Bool == true)
        let pending = try #require(o["pending"] as? [String: Any])
        #expect(pending["open_sessions"] as? Int == 1 && (pending["progress"] as? [String: Any])?["sessions_total"] as? Int == 3)
    }
}

@Suite struct MacAPIRequestTests {
    private func body(_ r: CapturedRequest) -> [String: Any] { (try? JSONSerialization.jsonObject(with: r.body)) as? [String: Any] ?? [:] }

    @Test func mutationsUseTheRightVerbPathAndBody() async throws {
        let (api, reqs) = FakeNet.make { _ in .json("{}") }
        try await api.transcribeNow()
        try await api.renamePerson("p1", name: "Anna B")
        try await api.deletePerson("p1")
        try await api.setPersonKind("p1", source: "media")
        try await api.nameVoice(session: "s1", label: "S2", name: "Marco")
        try await api.setVoiceKind(session: "s1", labels: ["S1", "S2"], source: "meeting")
        try await api.setVoiceUnknown(session: "s1", labels: ["S3"])
        try await api.addPlace(name: "Casa", lat: 45.1, lon: 9.2, radius: 100)
        try await api.renamePlace("Casa mia/1", to: "Casa")
        try await api.removePlace("Casa mia/1")
        let r = reqs()
        func line(_ i: Int) -> String { "\(r[i].method) \(r[i].url.path)" }
        #expect(line(0) == "POST /v1/transcribe" && r[0].body.isEmpty)
        #expect(line(1) == "PUT /v1/people/p1" && (body(r[1]) as? [String: String]) == ["name": "Anna B"])
        #expect(line(2) == "DELETE /v1/people/p1")
        #expect(line(3) == "POST /v1/people/p1/kind" && (body(r[3]) as? [String: String]) == ["source": "media"])
        #expect(line(4) == "PUT /v1/voices/s1/S2" && (body(r[4]) as? [String: String]) == ["name": "Marco"])
        #expect(line(5) == "POST /v1/voices/s1/kind" && body(r[5])["source"] as? String == "meeting" && body(r[5])["labels"] as? [String] == ["S1", "S2"])
        #expect(line(6) == "POST /v1/voices/s1/unknown" && body(r[6])["unknown"] as? Bool == true && body(r[6])["labels"] as? [String] == ["S3"])
        #expect(line(7) == "POST /v1/places" && body(r[7])["name"] as? String == "Casa" && body(r[7])["radius"] as? Int == 100 && body(r[7])["lat"] as? Double == 45.1)
        #expect(r[8].method == "PUT" && r[8].url.absoluteString.hasSuffix("/v1/places/Casa%20mia%2F1") && body(r[8])["name"] as? String == "Casa")
        #expect(r[9].method == "DELETE" && r[9].url.absoluteString.hasSuffix("/v1/places/Casa%20mia%2F1"))
        #expect(r.allSatisfy { $0.headers["Authorization"] == "Bearer tok-123" })
    }

    @Test func voiceSampleReturnsBytes() async throws {
        let wav = Data([0x52, 0x49, 0x46, 0x46, 0, 1, 2, 3])
        let (api, reqs) = FakeNet.make { _ in FakeResponse(chunks: [wav]) }
        #expect(try await api.voiceSample(session: "s1", label: "S2") == wav)
        #expect(reqs()[0].url.path == "/v1/voices/s1/S2/sample")
    }

    @Test func putSettingsMerges() async throws {
        let (api, reqs) = FakeNet.make { _ in .json(#"{"features":{"location":{"on":false,"at":"2026-11-23T10:00:00.000Z"},"bogus":{"on":true,"at":"x"},"calls":{"on":"yes"},"mac_audio":{"on":true,"at":"2026-11-23T11:00:00.000Z"}}}"#) }
        var f = Features()
        f.set("battery", false, at: t0)
        let merged = try await api.putSettings(features: f)
        #expect(reqs()[0].method == "PUT" && reqs()[0].url.path == "/v1/settings")
        let sent = try #require((body(reqs()[0])["features"]) as? [String: [String: Any]])
        #expect(sent["battery"]?["on"] as? Bool == false && sent["battery"]?["at"] as? String == "2023-11-14T22:13:20.123Z" && sent.count == 1)
        #expect(Set(merged.keys) == ["location", "mac_audio"] && merged["location"]?.on == false)
    }

    @Test func errorMapping() async throws {
        func err(_ status: Int, _ body: String = "{}") async -> String? {
            let (api, _) = FakeNet.make { _ in .json(body, status: status) }
            do { _ = try await api.days(); return nil } catch { return (error as? MacError)?.message }
        }
        #expect(await err(401) == tr("Token non valido (Impostazioni)", "Invalid token (Settings)"))
        for s in [502, 503, 504] {
            #expect(await err(s) == tr("Il ricevitore sul Mac è fermo: riavvialo dall'app Mac (Impostazioni)", "The receiver on the Mac is stopped: restart it from the Mac app (Settings)"))
        }
        #expect(await err(404) == tr("Il Mac va aggiornato: non conosce questa richiesta", "The Mac needs an update: it does not know this request"))
        #expect(await err(500) == tr("Errore dal Mac (HTTP 500)", "Error from the Mac (HTTP 500)"))
        #expect(await err(400, #"{"error":"voice prints are turned off"}"#) == tr("Impronte vocali disattivate (Impostazioni, Funzioni)", "Voice prints are off (Settings, Features)"))
        #expect(await err(409, #"{"error":"name already used"}"#) == tr("Nome già usato da un altro luogo", "Name already used by another place"))
        #expect(await err(400, #"{"error":"no person with that id"}"#) == tr("Persona non trovata: forse è stata cambiata dal Mac", "Person not found: maybe it was changed on the Mac"))
        #expect(await err(400, #"{"error":"something else"}"#) == "something else")
        #expect(MacError.translated("no place named x") == tr("Luogo non trovato: forse è stato cambiato dal Mac", "Place not found: maybe it was changed on the Mac"))
        #expect(MacError.translated("no voice print for S1") != nil && MacError.translated("invalid name") == tr("Nome non valido", "Invalid name"))
    }

    @Test func transportErrors() async throws {
        let (a1, _) = FakeNet.make { _ in FakeResponse(error: URLError(.cannotConnectToHost)) }
        await #expect(throws: MacError.unreachable) { _ = try await a1.people() }
        let (a2, _) = FakeNet.make { _ in FakeResponse(error: URLError(.timedOut)) }
        await #expect(throws: MacError.timedOut) { _ = try await a2.people() }
        let (a3, _) = FakeNet.make { _ in FakeResponse(error: URLError(.notConnectedToInternet)) }
        await #expect(throws: MacError.unreachable) { try await a3.transcribeNow() }
    }

    @Test func baseURLTrailingSlashAndMissingConfig() async throws {
        let (api, reqs) = FakeNet.make { _ in .json(#"{"days":[],"pending":{}}"#) }
        let slashed = MacAPI(baseURL: URL(string: api.baseURL.absoluteString + "/")!, token: "t", session: api.session)
        _ = try await slashed.days()
        #expect(reqs()[0].url.path == "/v1/transcripts")
        let settings = SettingsStore(defaults: fakeDefaults(), keychain: FakeKeychain())
        #expect(settings.makeAPI() == nil)
    }

    @Test func uploadMapsStatus() async throws {
        let tmp = TempDir()
        let store = SegmentStore(directory: tmp.segments)
        let q = try await makeSegment(store, at: t0)
        let (api, _) = FakeNet.make { _ in .json("{}", status: 401) }
        do { try await api.uploadSegment(q, remaining: 0); Issue.record("should throw") } catch {
            #expect((error as? MacError)?.message.contains(tr("Token non valido", "Invalid token")) == true)
            #expect((error as? MacError)?.message.contains(q.id.raw) == true)
        }
    }
}
