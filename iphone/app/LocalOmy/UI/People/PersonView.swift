import OmyKit
import SwiftUI

/// One person (or you, with id "me"): how much and where you talk, and every conversation with them.
struct PersonView: View {
    let route: PersonRoute

    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var feed: ConvFeed?
    @State private var type: String?
    @State private var busy = false
    @State private var name: String
    @State private var editing = false
    @State private var alert: ConvAlertMessage?
    @State private var confirm: ConvConfirm?
    @State private var open: ConvRoute?

    init(route: PersonRoute) {
        self.route = route
        _name = State(initialValue: route.name)
    }

    private var isMe: Bool { route.id == "me" }

    /// The kind of all their voices, when it is one: room or meeting.
    private var kind: String? {
        var t = Set((route.index?.types ?? []).map { $0 == "call" ? "meeting" : $0 })
        t.remove("media")
        return t.count == 1 ? t.first : nil
    }

    var body: some View {
        ConvTimeline(
            conversations: feed?.list?.conversations, hasMore: feed?.hasMore ?? false, onMore: { await feed?.more() },
            error: feed?.error, onRetry: { Task { await feed?.refresh() } },
            onOpen: { c in
                guard let all = feed?.list?.conversations, let i = all.firstIndex(where: { $0.session == c.session }) else { return }
                open = ConvRoute(conversations: all, initial: i)
            }
        ) { header }
        .refreshable { await feed?.refresh() }
        .navigationTitle(name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if busy { ToolbarItem(placement: .topBarTrailing) { ProgressView() } }
            else if !isMe { ToolbarItem(placement: .topBarTrailing) { Button(tr("Modifica", "Edit")) { editing = true } } }
        }
        .navigationDestination(item: $open) { r in
            ConversationViewer(conversations: r.conversations, initial: r.initial, focus: r.focus, highlight: r.highlight)
        }
        .sheet(isPresented: $editing) {
            PersonEditSheet(name: name, kind: kind) { r in Task { await apply(r) } }
        }
        .convAlert($alert)
        .convConfirm($confirm)
        .task(id: TypeKey(type: type, generation: model.apiGeneration)) {
            feed = ConvFeed(api: model.api, person: route.id, type: type)
            await feed?.refresh()
        }
    }

    private struct TypeKey: Hashable { let type: String?; let generation: Int }

    private var header: some View {
        let i = route.index
        return VStack(spacing: 16) {
            HStack(spacing: 16) {
                ConvAvatar(name: name, me: isMe, size: 64)
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(convDuration(route.minutes)) · \(convCountedSessions(route.sessions))").font(.headline)
                    if let last = route.lastSeen {
                        Text(tr("Ultima volta \(convLongDay(last).lowercased())", "Last seen \(convLongDay(last))")).font(.footnote).foregroundStyle(.secondary)
                    }
                    if let i, !i.places.isEmpty {
                        Label(i.places.joined(separator: ", "), systemImage: "location.fill").font(.footnote).foregroundStyle(.secondary).padding(.top, 4)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(16)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemGroupedBackground)))
            .padding(.horizontal, 16)
            ConvFilterPills(values: convTypes.filter { i == nil || isMe || i!.types.contains($0.id) }, selected: $type)
        }
        .padding(.top, 8).padding(.bottom, 12)
    }

    // MARK: edit

    private func apply(_ r: PersonEditSheet.Result) async {
        // The sheet is still closing: an alert now would be lost.
        try? await Task.sleep(for: .milliseconds(450))
        if r.delete { return delete() }
        let newName = r.name.isEmpty ? name : r.name
        let newKind = r.kind != kind ? r.kind : nil
        if newName == name && newKind == nil { return }
        if newKind == "media" {
            confirm = ConvConfirm(
                title: tr("\(name) è un video?", "Is \(name) a video?"),
                message: tr("Tutte le sue voci diventano «Video», in ogni sessione, e non vengono più riconosciute. Si cancellano il nome e l'impronta vocale.",
                            "All their voices become “Video”, in every session, and are no longer recognized. The name and the voice print are deleted."),
                action: tr("È un video", "It's a video")
            ) {
                Task {
                    guard let api = model.api else { return }
                    if await convChange(busy: $busy, alert: $alert, { try await api.setPersonKind(route.id, source: "media") }) { dismiss() }
                }
            }
            return
        }
        let merging = newName != name && route.others.contains { $0.id != route.id && $0.name.lowercased() == newName.lowercased() }
        if merging {
            confirm = ConvConfirm(
                title: tr("Unire a \(newName)?", "Merge with \(newName)?"),
                message: tr("Esiste già \(newName): le due persone diventano una sola, con le voci di entrambe.", "\(newName) already exists: the two become one person, with the voices of both."),
                action: tr("Unisci", "Merge"), destructive: false
            ) { Task { await save(newName: newName, newKind: newKind, merging: true) } }
        } else {
            await save(newName: newName, newKind: newKind, merging: false)
        }
    }

    private func save(newName: String, newKind: String?, merging: Bool) async {
        guard let api = model.api else { return }
        // The kind first: a rename that merges changes the id.
        let ok = await convChange(busy: $busy, alert: $alert) {
            if let newKind { try await api.setPersonKind(route.id, source: newKind) }
            if newName != name { try await api.renamePerson(route.id, name: newName) }
        }
        guard ok else { return }
        if merging { dismiss() } else { name = newName; await feed?.refresh() }
    }

    private func delete() {
        confirm = ConvConfirm(
            title: tr("Eliminare \(name)?", "Delete \(name)?"),
            message: tr("Si cancellano il nome e l'impronta vocale. Le sue voci tornano senza nome e si possono nominare di nuovo.",
                        "The name and the voice print are deleted. Their voices go back to unnamed and can be named again.")
        ) {
            Task {
                guard let api = model.api else { return }
                if await convChange(busy: $busy, alert: $alert, { try await api.deletePerson(route.id) }) { dismiss() }
            }
        }
    }
}

/// Name and kind of a person, like editing a contact; delete at the bottom.
struct PersonEditSheet: View {
    struct Result { let name: String; let kind: String?; let delete: Bool }

    let onDone: (Result) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name: String
    @State private var kind: String?

    init(name: String, kind: String?, onDone: @escaping (Result) -> Void) {
        self.onDone = onDone
        _name = State(initialValue: name)
        _kind = State(initialValue: kind)
    }

    private func done(delete: Bool = false) {
        onDone(Result(name: name.trimmingCharacters(in: .whitespaces), kind: kind, delete: delete))
        dismiss()
    }

    var body: some View {
        NavigationStack {
            Form {
                Section(tr("Nome", "Name").uppercased()) {
                    TextField(tr("Nome", "Name"), text: $name).textInputAutocapitalization(.words).submitLabel(.done).onSubmit { done() }
                }
                Section {
                    Picker("", selection: $kind) {
                        Text(tr("Di persona", "In person")).tag(String?.some("room"))
                        Text("Online").tag(String?.some("meeting"))
                        Text("Video").tag(String?.some("media"))
                    }
                    .pickerStyle(.segmented).labelsHidden()
                } header: { Text(tr("Tipo", "Type").uppercased()) } footer: { Text(explanation) }
                Section {
                    Button(tr("Elimina persona", "Delete person"), role: .destructive) { done(delete: true) }
                }
            }
            .navigationTitle(tr("Modifica", "Edit")).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(tr("Annulla", "Cancel")) { dismiss() } }
                ToolbarItem(placement: .confirmationAction) { Button(tr("Fine", "Done")) { done() } }
            }
        }
    }

    private var explanation: String {
        switch kind {
        case "media": tr("Era un video: tutte le sue voci diventano «Video», e il nome e l'impronta vocale si cancellano.", "It was a video: all their voices become “Video”, and the name and voice print are deleted.")
        case "meeting": tr("Tutte le sue voci: in una chiamata o una riunione online.", "All their voices: in a call or an online meeting.")
        case "room": tr("Tutte le sue voci: di persona, nella stanza con te.", "All their voices: in person, in the room with you.")
        default: tr("Le sue voci sono di tipi diversi: scegline uno per cambiarle tutte.", "Their voices are of different types: choose one to change them all.")
        }
    }
}
