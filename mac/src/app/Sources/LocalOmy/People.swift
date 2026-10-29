import AppKit
import Observation
import SwiftUI

// Mirrors of `omy --json people list` and `omy --json speakers list`.

struct PeopleList: Decodable {
    struct Me: Decodable { let name: String; let enrolled: Bool; let minutes: Double; let sessions: Int; let lastSeen: String? }
    struct Person: Decodable, Identifiable {
        let id: String
        let name: String
        let samples: Int
        let minutes: Double
        let sessions: Int
        let lastSeen: String?
    }
    let me: Me
    let people: [Person]
}

struct DayVoices: Decodable {
    struct Voice: Decodable, Identifiable {
        let label: String
        let name: String
        let person: String?
        let pinned: Bool
        let seconds: Double
        let kind: String?
        let app: String?
        let source: String?
        /// Somebody you don't know (omy speakers unknown): not asked again.
        let unknown: Bool
        let sample: String
        var id: String { label }

        /// "room", "meeting" or "media": set by hand, or what the Mac found.
        var type: String { source ?? (kind == "media" ? "media" : kind == "call" ? "meeting" : "room") }
        /// Still to label: nobody yet, not a video, not unknown.
        var todo: Bool { person == nil && type != "media" && !unknown }
        var typeLabel: String {
            switch type {
            case "media": return app == nil || app == "Video" ? "Video" : "Video · \(app!)"
            case "meeting":
                // "Riunione" or "Meeting": the archive's language.
                return app == "iPhone" ? tr("Chiamata", "Call")
                    : app == nil || app == "Riunione" || app == "Meeting" ? tr("Riunione online", "Online meeting")
                    : tr("Riunione", "Meeting") + " · \(app!)"
            default: return tr("Di persona", "In person")
            }
        }
    }
    struct Session: Decodable, Identifiable {
        let session: String
        let start: String?
        let voices: [Voice]
        var id: String { session }
    }
    let day: String
    let sessions: [Session]
}

private struct Sample: Decodable { let path: String }

/// The "Persone" tab: who omy recognizes, and naming the voices of a day.
/// Every change is an omy command that also rewrites the day markdowns.
@MainActor
@Observable
final class PeopleModel {
    var people: PeopleList?
    var voices: DayVoices?
    var busy: String?
    var error: String?
    private var sound: NSSound?

    func load(day: String?) async {
        await run(nil) {
            self.people = try await Omy.decode(PeopleList.self, ["people", "list"])
            if let day { self.voices = try await Omy.decode(DayVoices.self, ["speakers", "list", "--day", day]) }
        }
    }

    func play(_ session: String, _ label: String) {
        Task {
            await run(tr("Ascolto", "Playing")) {
                let s = try await Omy.decode(Sample.self, ["speakers", "play", session, label])
                self.sound?.stop()
                // Read into memory, then the file goes: no voice stays on disk.
                self.sound = NSSound(contentsOfFile: s.path, byReference: false)
                try? FileManager.default.removeItem(atPath: s.path)
                self.sound?.play()
            }
        }
    }

    func rename(_ session: String, _ label: String, to name: String, day: String?) {
        change(tr("Rinomina", "Rename"), ["speakers", "rename", session, label, name], day: day)
    }

    /// Where these voices come from: room, meeting, media, or auto.
    func setKind(_ session: String, _ labels: [String], _ source: String, day: String?) {
        change(tr("Tipo", "Type"), ["speakers", "kind", session, source] + labels, day: day)
    }

    /// Somebody you don't know: no name, no voice print, not asked again.
    func setUnknown(_ session: String, _ labels: [String], day: String?) {
        change(tr("Sconosciuto", "Unknown"), ["speakers", "unknown", session] + labels, day: day)
    }

    /// New name and, if given, where all the voices come from (room,
    /// meeting; media: it was a video, the name and voice print go). The
    /// kind first: a rename that merges changes the id.
    func edit(_ p: PeopleList.Person, name: String, kind: String?, day: String?) {
        let name = name.trimmingCharacters(in: .whitespaces)
        Task {
            await run(tr("Modifica", "Edit")) {
                if let kind { _ = try await Omy.run(["people", "kind", p.id, kind]) }
                if kind != "media", !name.isEmpty, name != p.name { _ = try await Omy.run(["people", "rename", p.id, name]) }
            }
            await load(day: day)
        }
    }

    func delete(_ id: String, day: String?) {
        change(tr("Eliminazione", "Deleting"), ["people", "delete", id], day: day)
    }

    private func change(_ name: String, _ args: [String], day: String?) {
        Task {
            await run(name) { _ = try await Omy.run(args) }
            await load(day: day)
        }
    }

    private func run(_ name: String?, _ body: () async throws -> Void) async {
        if let name { busy = name; AppLog.write("[persone] \(name)") }
        do {
            try await body()
            error = nil
        } catch {
            self.error = error.localizedDescription
            AppLog.write("[persone] failed: \(error.localizedDescription)")
        }
        if name != nil { busy = nil }
    }
}

struct PeopleTab: View {
    let model: AppModel
    @State private var people = PeopleModel()
    @State private var day: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.features.first(where: { $0.key == "voice_prints" })?.on == false {
                Label(tr("Impronte vocali disattivate (Impostazioni, Funzioni): le voci nuove non vengono salvate né riconosciute, e non si possono dare nomi.",
                         "Voice prints are off (Settings, Features): new voices are not saved or recognized, and cannot be named."),
                      systemImage: "info.circle")
                    .foregroundStyle(.secondary)
            }
            HStack {
                if let busy = people.busy {
                    ProgressView().controlSize(.small)
                    Text("\(busy)…")
                }
                if let err = people.error {
                    Label(err, systemImage: "xmark.octagon").foregroundStyle(.red).lineLimit(2).textSelection(.enabled)
                }
            }
            HSplitView {
                KnownPeople(people: people, day: day)
                    .frame(minWidth: 230, idealWidth: 260, maxWidth: 320)
                VStack(alignment: .leading) {
                    Picker(tr("Giorno", "Day"), selection: $day) {
                        ForEach(model.status?.transcripts.days ?? [], id: \.self) { Text(longDay($0)).tag(Optional($0)) }
                    }
                    .frame(maxWidth: 280)
                    DayVoicesList(people: people, day: day)
                }
                .padding(.leading, 8)
            }
        }
        .disabled(people.busy != nil)
        .onAppear { if day == nil { day = model.status?.transcripts.days.first } }
        .task(id: day) { await people.load(day: day) }
    }
}

/// You and the people you named, with their voice prints.
private struct KnownPeople: View {
    let people: PeopleModel
    let day: String?
    @State private var deleting: PeopleList.Person?
    @State private var editing: PeopleList.Person?

    var body: some View {
        List {
            Section(tr("Persone note", "Known people")) {
                if let me = people.people?.me {
                    VStack(alignment: .leading) {
                        Text(me.name).bold()
                        Text(me.enrolled ? tr("tu", "you") + " · \(heard(me.minutes, me.sessions))"
                             : tr("tu · impronta vocale assente (Impostazioni)", "you · no voice profile (Settings)"))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
                ForEach(people.people?.people ?? []) { p in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(p.name)
                            Text(heard(p.minutes, p.sessions) + (p.lastSeen.map { tr(", ultima ", ", last ") + longDay($0) } ?? ""))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Menu {
                            Button(tr("Modifica…", "Edit…")) { editing = p }
                            Button(tr("Elimina…", "Delete…"), role: .destructive) { deleting = p }
                        } label: { Image(systemName: "ellipsis.circle") }
                            .menuStyle(.borderlessButton).fixedSize()
                    }
                }
                if people.people?.people.isEmpty == true {
                    Text(tr("Nessuno: dai un nome a una voce qui accanto.", "Nobody: name a voice on the right.")).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .sheet(item: $editing) { p in
            EditPerson(person: p) { name, kind in people.edit(p, name: name, kind: kind, day: day) }
        }
        .confirmationDialog(
            tr("Eliminare \(deleting?.name ?? "")?", "Delete \(deleting?.name ?? "")?"),
            isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })
        ) {
            Button(tr("Elimina", "Delete"), role: .destructive) {
                if let p = deleting { people.delete(p.id, day: day) }
            }
        } message: {
            Text(tr("Viene cancellato il nome con i suoi campioni. Le sue voci restano, tornano senza nome nelle trascrizioni e puoi dar loro di nuovo un nome.",
                     "The name and its samples are deleted. Their voices stay, go back to unnamed in the transcripts, and can be named again."))
        }
    }

    private func heard(_ minutes: Double, _ sessions: Int) -> String {
        "\(minutes < 1 ? "<1" : String(Int(minutes))) min in \(sessions) " + tr("sessioni", "sessions")
    }
}

/// Name and kind of a person. The kind is left alone unless picked.
private struct EditPerson: View {
    let person: PeopleList.Person
    let save: (String, String?) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var kind = ""

    var body: some View {
        Form {
            TextField(tr("Nome", "Name"), text: $name)
            Picker(tr("Tipo", "Type"), selection: $kind) {
                Text(tr("Non cambiare", "Don't change")).tag("")
                Label(tr("Di persona", "In person"), systemImage: "person.fill").tag("room")
                Label(tr("Riunione online", "Online meeting"), systemImage: "video.fill").tag("meeting")
                Label("Video", systemImage: "play.rectangle.fill").tag("media")
            }
            Text(kind == "media"
                 ? tr("Era un video: tutte le sue voci diventano \"Video\", in ogni sessione, e non vengono più riconosciute. Si cancellano il nome e l'impronta vocale.",
                      "It was a video: all their voices become \"Video\", in every session, and are no longer recognized. The name and voice print are deleted.")
                 : kind.isEmpty ? tr("Il tipo vale per tutte le sue voci.", "The type applies to all their voices.")
                 : tr("Tutte le sue voci prendono questo tipo.", "All their voices take this type."))
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding()
        .frame(width: 380)
        .onAppear { name = person.name }
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button(tr("Annulla", "Cancel")) { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                Button(kind == "media" ? tr("È un video", "It's a video") : tr("Salva", "Save")) {
                    save(name, kind.isEmpty ? nil : kind)
                    dismiss()
                }
            }
        }
    }
}

/// The voices of each session of a day: listen, then name.
private struct DayVoicesList: View {
    let people: PeopleModel
    let day: String?
    @State private var todoOnly = true

    var body: some View {
        if let v = people.voices, v.day == day {
            if v.sessions.isEmpty {
                Text(tr("Nessuna voce in questo giorno (non ancora diarizzato?)", "No voices on this day (not diarized yet?)")).foregroundStyle(.secondary)
                Spacer()
            } else {
                let todo = v.sessions.reduce(0) { $0 + $1.voices.filter(\.todo).count }
                Picker("", selection: $todoOnly) {
                    Text(tr("Da etichettare", "To label") + " (\(todo))").tag(true)
                    Text(tr("Tutte", "All")).tag(false)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                List {
                    ForEach(v.sessions.filter { !todoOnly || $0.voices.contains(where: \.todo) }) { s in
                        Section {
                            ForEach(s.voices.filter { !todoOnly || $0.todo }) { voice in
                                VoiceRow(people: people, session: s.session, voice: voice, day: day)
                            }
                        } header: {
                            HStack {
                                Text(tr("Sessione delle ", "Session at ") + (s.start.map(localTime) ?? "?"))
                                Spacer()
                                let unnamed = s.voices.filter(\.todo).map(\.label)
                                if !unnamed.isEmpty {
                                    Menu(tr("Segna tutte come…", "Mark all as…")) {
                                        Section(unnamed.count == 1 ? tr("La voce da etichettare", "The voice to label")
                                                : tr("Le \(unnamed.count) voci da etichettare", "The \(unnamed.count) voices to label")) {
                                            Button(tr("Sconosciuti", "Unknown")) { people.setUnknown(s.session, unnamed, day: day) }
                                            Divider()
                                            Button("Video") { people.setKind(s.session, unnamed, "media", day: day) }
                                            Button(tr("Riunione online", "Online meeting")) { people.setKind(s.session, unnamed, "meeting", day: day) }
                                            Button(tr("Di persona", "In person")) { people.setKind(s.session, unnamed, "room", day: day) }
                                            Divider()
                                            Button(tr("Automatico", "Automatic")) { people.setKind(s.session, unnamed, "auto", day: day) }
                                        }
                                    }
                                    .menuStyle(.borderlessButton).fixedSize()
                                }
                            }
                        }
                    }
                    if todoOnly && todo == 0 {
                        Text(tr("Nessuna voce da etichettare in questo giorno: hanno un nome, sono sconosciute o video.",
                                 "No voices to label on this day: they have a name, are unknown or videos.")).foregroundStyle(.secondary)
                    }
                }
            }
        } else {
            ProgressView()
            Spacer()
        }
    }
}

private struct VoiceRow: View {
    let people: PeopleModel
    let session: String
    let voice: DayVoices.Voice
    let day: String?
    @State private var name = ""

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Button { people.play(session, voice.label) } label: { Image(systemName: "play.circle") }
                .buttonStyle(.borderless)
                .help(tr("Ascolta qualche secondo", "Listen to a few seconds"))
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    TextField(tr("Nome", "Name"), text: $name)
                        .onSubmit(submit)
                        .frame(maxWidth: 200)
                    .disabled(voice.type == "media")
                    Menu(tr("Chi è", "Who is it")) {
                        if let me = people.people?.me { Button(me.name) { name = me.name; submit() } }
                        ForEach(people.people?.people ?? []) { p in Button(p.name) { name = p.name; submit() } }
                        Divider()
                        Button(tr("Sconosciuto", "Unknown")) { people.setUnknown(session, [voice.label], day: day) }
                            .disabled(voice.unknown)
                    }
                    .fixedSize()
                    .disabled(voice.type == "media")
                    Picker(tr("Tipo", "Type"), selection: Binding(get: { voice.type }, set: { t in
                        if t != voice.type { people.setKind(session, [voice.label], t, day: day) }
                    })) {
                        Label(tr("Di persona", "In person"), systemImage: "person.fill").tag("room")
                        Label(tr("Riunione online", "Online meeting"), systemImage: "video.fill").tag("meeting")
                        Label("Video", systemImage: "play.rectangle.fill").tag("media")
                    }
                    .labelsHidden()
                    .fixedSize()
                    .help(tr("Da dove viene la voce. Video (o la TV): nessun nome, nessuna impronta vocale.", "Where the voice comes from. Video (or the TV): no name, no voice print."))
                    Text("\(voice.typeLabel) · \(Int(voice.seconds)) s"
                         + (voice.pinned ? tr(" · nominata a mano", " · named by you") : voice.person != nil ? tr(" · riconosciuta", " · recognized")
                            : voice.unknown ? tr(" · sconosciuta", " · unknown") : ""))
                        .font(.caption).foregroundStyle(.secondary)
                }
                if !voice.sample.isEmpty {
                    Text(voice.sample).font(.caption).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
                }
            }
        }
        .onAppear { name = voice.name }
        .onChange(of: voice.name) { _, n in name = n }
    }

    private func submit() {
        let n = name.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty, n != voice.name else { return }
        people.rename(session, voice.label, to: n, day: day)
    }
}

private func localTime(_ iso: String) -> String {
    guard let d = ISO8601DateFormatter.withFraction.date(from: iso) ?? ISO8601DateFormatter().date(from: iso) else { return iso }
    return d.formatted(date: .omitted, time: .shortened)
}

private extension ISO8601DateFormatter {
    static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
}
