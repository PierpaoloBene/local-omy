import OmyKit
import SwiftUI

/// A person (or "me") opened from the list.
struct PersonRoute: Hashable {
    let id: String
    let name: String
    let minutes: Double
    let sessions: Int
    var lastSeen: String?
    var index: PersonIndex?
    /// Every other named person, to warn before merging.
    var others: [Person] = []
    static func == (a: PersonRoute, b: PersonRoute) -> Bool { a.id == b.id }
    func hash(into h: inout Hasher) { h.combine(id) }
}

/// "Persone": two views. The people (you and those you named), filtered by the kind of conversation, each opening
/// their conversations; and the voices still to label of the last 24 hours, while you remember who spoke.
/// Every change runs on the Mac and rewrites the days where they speak.
struct PeopleView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    static let todoHours = 24

    @State private var people: People?
    @State private var convs: ConversationList?
    @State private var todo: [VoiceSession] = []
    @State private var type: String?
    @State private var labeling = false
    @State private var error: String?
    @State private var busy = false
    @State private var playing: String?
    @State private var labelTarget: LabelTarget?
    @State private var alert: ConvAlertMessage?
    @State private var path = NavigationPath()
    @State private var route: ConvRoute?
    @State private var loaded = false

    private struct LabelTarget: Identifiable {
        let session: String
        let voice: Voice
        var id: String { session + "/" + voice.label }
    }

    private var todoCount: Int { todo.reduce(0) { $0 + $1.voices.filter(\.todo).count } }

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if let people {
                    if let error { Section { ConvNotice(icon: "exclamationmark.circle", text: tr("Non aggiornato: \(error)", "Not up to date: \(error)")) }.listRowBackground(Color.clear) }
                    if !model.isOn("voice_prints") {
                        Section {
                            ConvNotice(icon: "info.circle.fill", text: tr(
                                "Impronte vocali disattivate: le voci nuove non vengono riconosciute, e non si possono dare nomi (Impostazioni, Funzioni).",
                                "Voice prints are off: new voices are not recognized, and cannot be named (Settings, Features)."))
                        }.listRowBackground(Color.clear).listRowInsets(.init())
                    }
                    Section {
                        Picker("", selection: $labeling) {
                            Text(tr("Persone", "People")).tag(false)
                            Text("\(tr("Da etichettare", "To label"))\(todoCount > 0 ? " · \(todoCount)" : "")").tag(true)
                        }
                        .pickerStyle(.segmented).labelsHidden()
                    }
                    .listRowBackground(Color.clear).listRowInsets(.init(top: 0, leading: 0, bottom: 0, trailing: 0))
                    if labeling { voiceSections } else { peopleSections(people) }
                } else {
                    ConvLoadState(loading: error == nil, error: error) { Task { await load() } }.listRowBackground(Color.clear)
                }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
            .navigationTitle(AppTab.people.title)
            .toolbar { if busy { ToolbarItem(placement: .topBarTrailing) { ProgressView() } } }
            .navigationDestination(for: PersonRoute.self) { PersonView(route: $0) }
            .navigationDestination(item: $route) { r in
                ConversationViewer(conversations: r.conversations, initial: r.initial, focus: r.focus, highlight: r.highlight)
            }
            .convListDestination()
            .convAlert($alert)
            .sheet(item: $labelTarget) { t in
                VoiceSheet(voice: t.voice, prints: model.isOn("voice_prints"), me: people?.me.name, known: people?.people.map(\.name) ?? []) { r in
                    Task { await apply(r, session: t.session, voice: t.voice) }
                }
            }
        }
        .task(id: model.apiGeneration) { await load(); loaded = true }
        .onAppear { if loaded { Task { await load() } } }
        .onDisappear { if playing != nil { model.stop(); playing = nil } }
        .onChange(of: model.isPlayingSample) { if !model.isPlayingSample { playing = nil } }
        .onChange(of: reselect.count(for: .people)) { path = NavigationPath(); route = nil }
        .onChange(of: type) { Task { await setType() } }
    }

    // MARK: people

    @ViewBuilder private func peopleSections(_ people: People) -> some View {
        let index = people.index
        let shown = people.people.filter { type == nil || (index[$0.id]?.types.contains(type!) ?? false) }
        let label = convTypes.first { $0.id == type }?.label ?? tr("Tutte", "All")
        Section {
            ConvFilterPills(values: convTypes, selected: $type)
        }
        .listRowBackground(Color.clear).listRowInsets(.init(top: 0, leading: 0, bottom: 0, trailing: 0))
        if type == nil { Section { meCard(people.me, people) } }
        if type != "media" {
            Section(type == nil ? tr("Persone note · \(people.people.count)", "Known people · \(people.people.count)") : tr("Persone · \(label)", "People · \(label)")) {
                if shown.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(type == nil ? tr("Nessuna ancora", "None yet") : tr("Nessuna persona con un nome", "No named people"))
                        Text(tr("Dai un nome alle voci in «Da etichettare».", "Name the voices in “To label”.")).font(.footnote).foregroundStyle(.secondary)
                    }
                } else {
                    ForEach(shown) { p in personRow(p, people) }
                }
            }
        }
        if let convs {
            Section(type == nil ? tr("Conversazioni recenti", "Recent conversations") : tr("Conversazioni · \(label)", "Conversations · \(label)")) {
                if convs.conversations.isEmpty { Text(tr("Nessuna conversazione", "No conversations")).foregroundStyle(.secondary) }
                ForEach(convs.conversations.prefix(8)) { c in
                    Button { route = ConvRoute(conversations: convs.conversations, initial: convs.conversations.firstIndex { $0.session == c.session } ?? 0) } label: { ConvListRow(c: c) }
                        .buttonStyle(.plain)
                }
                if convs.conversations.count > 8 || convs.next != nil {
                    NavigationLink(value: ConvListRoute(title: type == nil ? tr("Conversazioni", "Conversations") : label, type: type)) { Text(tr("Mostra tutte", "Show all")) }
                }
            }
        }
    }

    private func meCard(_ me: Me, _ all: People) -> some View {
        NavigationLink(value: PersonRoute(id: "me", name: me.name, minutes: me.minutes, sessions: me.sessions, lastSeen: me.lastSeen, index: nil, others: all.people)) {
            HStack(spacing: 14) {
                ConvAvatar(name: me.name, me: true, size: 58)
                VStack(alignment: .leading, spacing: 2) {
                    Text(me.name).font(.title2.bold())
                    Text(me.enrolled
                        ? "\(convDuration(me.minutes)) · \(convCountedSessions(me.sessions))\(me.lastSeen.map { " · \(tr("ultima", "last")) \(convShortDay($0))" } ?? "")"
                        : tr("Profilo vocale non ancora creato (sul Mac: omy voice enroll)", "No voice profile yet (on the Mac: omy voice enroll)"))
                        .font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func personRow(_ p: Person, _ all: People) -> some View {
        let i = all.index[p.id]
        return NavigationLink(value: PersonRoute(id: p.id, name: p.name, minutes: p.minutes, sessions: p.sessions, lastSeen: p.lastSeen, index: i, others: all.people)) {
            HStack(spacing: 14) {
                ConvAvatar(name: p.name)
                VStack(alignment: .leading, spacing: 2) {
                    Text(p.name)
                    Text(([convDuration(p.minutes), convCountedSessions(p.sessions)]
                        + [p.lastSeen.map { "\(tr("ultima", "last")) \(convShortDay($0))" }, i?.places.first].compactMap { $0 }).joined(separator: " · "))
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                HStack(spacing: 4) {
                    ForEach(i?.types ?? [], id: \.self) { t in Image(systemName: convTypeIcon(t)).font(.caption).foregroundStyle(.tertiary) }
                }
            }
        }
    }

    // MARK: to label

    @ViewBuilder private var voiceSections: some View {
        let sessions = todo.filter { $0.voices.contains(where: \.todo) }
        if sessions.isEmpty {
            Section {
                ConvEmpty(tr("Nessuna voce da etichettare nelle ultime \(Self.todoHours) ore. Quelle più vecchie si etichettano dal Mac.", "No voices to label in the last \(Self.todoHours) hours. Older ones are labeled on the Mac."))
            }.listRowBackground(Color.clear)
        } else {
            ForEach(sessions, id: \.session) { s in
                Section {
                    ForEach(s.voices.filter(\.todo), id: \.label) { v in voiceRow(s, v) }
                } header: {
                    HStack {
                        Text(([s.day.map { convLongDay($0) }, s.start.map(Self.localTime)].compactMap { $0 }).joined(separator: " · "))
                        Spacer()
                        markAll(s)
                    }
                }
            }
            Section {
                ConvEmpty(tr("Le voci senza nome delle ultime \(Self.todoHours) ore, finché ricordi chi parlava. Quelle più vecchie si etichettano dal Mac.",
                             "The unnamed voices of the last \(Self.todoHours) hours, while you remember who was speaking. Older ones are labeled on the Mac."))
            }.listRowBackground(Color.clear)
        }
    }

    private func markAll(_ s: VoiceSession) -> some View {
        let labels = s.voices.filter(\.todo).map(\.label)
        return Menu(tr("Segna tutte…", "Mark all…")) {
            Section(labels.count == 1 ? tr("Segna la voce come", "Mark the voice as") : tr("Segna le \(labels.count) voci come", "Mark the \(labels.count) voices as")) {
                Button(tr("Sconosciuti", "Unknown")) { Task { await mark(s, labels, "unknown") } }
                Button("Video") { Task { await mark(s, labels, "media") } }
                Button(tr("Riunione online", "Online meeting")) { Task { await mark(s, labels, "meeting") } }
                Button(tr("Di persona", "In person")) { Task { await mark(s, labels, "room") } }
                Button(tr("Automatico", "Automatic")) { Task { await mark(s, labels, "auto") } }
            }
        }
        .font(.footnote).textCase(nil).disabled(busy)
    }

    private func voiceRow(_ s: VoiceSession, _ v: Voice) -> some View {
        let key = "\(s.session)/\(v.label)"
        let on = playing == key
        return HStack(alignment: .top, spacing: 12) {
            Button { Task { await play(s, v) } } label: {
                Image(systemName: on ? "stop.fill" : "play.fill").font(.system(size: 16))
                    .foregroundStyle(on ? Color.white : Color.accentColor)
                    .frame(width: 36, height: 36)
                    .background(Circle().fill(Color.accentColor.opacity(on ? 1 : 0.14)))
            }
            .buttonStyle(.borderless).accessibilityLabel(on ? tr("Ferma", "Stop") : tr("Ascolta", "Play"))
            VStack(alignment: .leading, spacing: 2) {
                Text(v.name).font(.headline).foregroundStyle(v.person != nil ? convPersonColor(v.name, me: v.person == "me") : Color.primary)
                Text(([voiceTypeLabel(v), v.seconds < 60 ? "\(Int(v.seconds.rounded())) s" : convDuration(v.seconds / 60)]
                    + [v.pinned ? tr("nominata da te", "named by you") : v.person != nil ? tr("riconosciuta", "recognized") : v.unknown ? tr("sconosciuta", "unknown") : nil].compactMap { $0 }).joined(separator: " · "))
                    .font(.footnote).foregroundStyle(.secondary)
                if !v.sample.isEmpty { Text("«\(v.sample)»").font(.footnote).italic().foregroundStyle(.secondary).lineLimit(3) }
            }
            Spacer(minLength: 0)
            Button(v.todo ? tr("Etichetta", "Label") : tr("Cambia", "Change")) { labelTarget = LabelTarget(session: s.session, voice: v) }
                .buttonStyle(.borderless).disabled(busy)
        }
    }

    private static func localTime(_ iso: String) -> String {
        guard let d = ISO8601UTC.parse(iso) else { return convHM(iso) }
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    // MARK: loading and changes

    private func load() async {
        guard let api = model.api else { error = tr("Mac non configurato.", "Mac not set up."); return }
        error = nil
        do {
            let p = try await api.people()
            let c = try await api.conversations(type: type)
            let t = try await api.unlabeled(hours: Self.todoHours).sessions
            people = p; convs = c; todo = t
        } catch is CancellationError {
        } catch {
            self.error = convMessage(error)
        }
    }

    private func setType() async {
        guard let api = model.api else { return }
        let t = type
        do {
            let c = try await api.conversations(type: t)
            if type == t { convs = c }
        } catch is CancellationError {
        } catch {
            alert = ConvAlertMessage(title: tr("Non riuscito", "Failed"), message: convMessage(error))
        }
    }

    private func change(_ body: () async throws -> Void) async {
        if await convChange(busy: $busy, alert: $alert, body) { await load() }
    }

    private func play(_ s: VoiceSession, _ v: Voice) async {
        let key = "\(s.session)/\(v.label)"
        if playing == key { model.stop(); playing = nil; return }
        guard let api = model.api else { return }
        playing = key
        do {
            let data = try await api.voiceSample(session: s.session, label: v.label)
            guard playing == key else { return }
            _ = try model.play(sample: data)
        } catch {
            playing = nil
            alert = ConvAlertMessage(title: tr("Ascolto non riuscito", "Could not play"), message: convMessage(error))
        }
    }

    private func mark(_ s: VoiceSession, _ labels: [String], _ type: String) async {
        guard let api = model.api, !labels.isEmpty else { return }
        await change { type == "unknown" ? try await api.setVoiceUnknown(session: s.session, labels: labels) : try await api.setVoiceKind(session: s.session, labels: labels, source: type) }
    }

    private func apply(_ r: VoiceSheet.Result, session: String, voice v: Voice) async {
        guard let api = model.api else { return }
        if r.unknown { return await change { try await api.setVoiceUnknown(session: session, labels: [v.label]) } }
        let rename = r.type != "media" && !r.name.isEmpty && !(r.name == v.name && v.pinned)
        if r.type == v.type && !rename { return }
        await change {
            if r.type != v.type { try await api.setVoiceKind(session: session, labels: [v.label], source: r.type) }
            if rename { try await api.nameVoice(session: session, label: v.label, name: r.name) }
        }
    }
}

/// A conversation: when and where, who, a sentence.
struct ConvListRow: View {
    let c: Conversation

    var body: some View {
        let others = c.participants.filter { !$0.isMe }
        HStack(spacing: 14) {
            ConvFaces(people: c.participants)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text((["\(convLongDay(c.day)) \(convHM(c.start))"] + [c.app ?? c.place].compactMap { $0 }).joined(separator: " · "))
                        .font(.subheadline.weight(.semibold)).lineLimit(1)
                    Spacer(minLength: 4)
                    Text(convDuration(Double(c.minutes))).font(.body).foregroundStyle(.secondary)
                }
                HStack(spacing: 4) {
                    Image(systemName: convTypeIcon(convKind(c))).font(.caption2)
                    Text(others.isEmpty ? tr("Solo tu", "Just you") : others.map(\.name).joined(separator: ", ")).lineLimit(1)
                }
                .font(.footnote).foregroundStyle(.secondary)
                if !c.snippet.isEmpty { Text("«\(c.snippet)»").font(.footnote).italic().foregroundStyle(.secondary).lineLimit(2) }
            }
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
        }
        .contentShape(Rectangle())
    }
}

/// The app the Mac puts on a meeting it didn't see, in either language.
func voiceAnyMeeting(_ app: String?) -> Bool { app == nil || app == "Riunione" || app == "Meeting" }

func voiceTypeLabel(_ v: Voice) -> String {
    switch v.type {
    case "media": return v.app == nil || v.app == "Video" ? "Video" : "Video · \(v.app!)"
    case "meeting":
        return v.app == "iPhone" ? tr("Chiamata", "Call") : voiceAnyMeeting(v.app) ? tr("Riunione online", "Online meeting") : tr("Riunione · \(v.app!)", "Meeting · \(v.app!)")
    default: return tr("Di persona", "In person")
    }
}

/// Where a voice comes from and, unless a video, who it is: you first, then the known people, or somebody you
/// don't know.
struct VoiceSheet: View {
    struct Result { let type: String; let name: String; let unknown: Bool }

    let voice: Voice
    let prints: Bool
    let me: String?
    let known: [String]
    let onSave: (Result) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var name: String
    @State private var type: String

    init(voice: Voice, prints: Bool, me: String?, known: [String], onSave: @escaping (Result) -> Void) {
        self.voice = voice; self.prints = prints; self.me = me; self.known = known; self.onSave = onSave
        _name = State(initialValue: voice.person == nil ? "" : voice.name)
        _type = State(initialValue: voice.type)
    }

    private func done(_ n: String? = nil) {
        onSave(Result(type: type, name: (n ?? name).trimmingCharacters(in: .whitespaces), unknown: false))
        dismiss()
    }

    var body: some View {
        let q = name.trimmingCharacters(in: .whitespaces).lowercased()
        let names = ([me].compactMap { $0 } + known).filter { q.isEmpty || $0.lowercased().contains(q) }.prefix(20)
        let named = type != "media" && prints
        NavigationStack {
            Form {
                if !voice.sample.isEmpty {
                    Section { Text("«\(voice.sample)»").italic().lineLimit(5).font(.subheadline) }
                }
                Section {
                    Picker("", selection: $type) {
                        Label(tr("Di persona", "In person"), systemImage: "person.fill").tag("room")
                        Label("Online", systemImage: "video.fill").tag("meeting")
                        Label("Video", systemImage: "play.rectangle.fill").tag("media")
                    }
                    .pickerStyle(.segmented).labelsHidden()
                } footer: { Text(explanation) }
                if named {
                    Section { TextField(tr("Nome (facoltativo)", "Name (optional)"), text: $name).textInputAutocapitalization(.words).submitLabel(.done).onSubmit { done() } }
                    Section(tr("Scegli", "Choose")) {
                        ForEach(Array(names), id: \.self) { n in
                            Button { done(n) } label: {
                                HStack(spacing: 12) { ConvAvatar(name: n, me: n == me, size: 30); Text(n).foregroundStyle(.primary) }
                            }
                        }
                        unknownRow
                    }
                } else if type != "media" {
                    Section { Text(tr("Impronte vocali disattivate: si può scegliere il tipo, ma non dare un nome.", "Voice prints are off: you can choose the type, but not give a name.")).font(.footnote).foregroundStyle(.secondary) }
                    Section { unknownRow }
                }
            }
            .navigationTitle(tr("Chi è?", "Who is it?")).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(tr("Annulla", "Cancel")) { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button(tr("Salva", "Save")) { done() } }
            }
        }
    }

    private var unknownRow: some View {
        Button {
            onSave(Result(type: type, name: "", unknown: true))
            dismiss()
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "questionmark").foregroundStyle(.white).frame(width: 30, height: 30)
                    .background(RoundedRectangle(cornerRadius: 7).fill(Color.gray))
                VStack(alignment: .leading, spacing: 2) {
                    Text(tr("Sconosciuto", "Unknown")).foregroundStyle(.primary)
                    Text(tr("Non sai chi è: niente nome né impronta, non te lo chiede più", "You don't know who it is: no name or voice print, not asked again")).font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }

    private var explanation: String {
        switch type {
        case "media": tr("Una voce da uno schermo o da un altoparlante: nessun nome, nessuna impronta vocale. Nelle trascrizioni diventa «Video».", "A voice from a screen or a speaker: no name, no voice print. In the transcripts it becomes “Video”.")
        case "meeting":
            voice.kind == "call" && !voiceAnyMeeting(voice.app)
                ? tr("Riconosciuta dal Mac: \(voice.app ?? ""). Puoi dirle chi è.", "Recognized by the Mac: \(voice.app ?? ""). You can say who it is.")
                : tr("Qualcuno in una chiamata o una riunione online (Teams, Zoom, Meet).", "Someone in a call or an online meeting (Teams, Zoom, Meet).")
        default: tr("Qualcuno nella stanza con te.", "Someone in the room with you.")
        }
    }
}
