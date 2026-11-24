import OmyKit
import SwiftUI

/// "Conversazioni": every conversation in one timeline, filtered by a person or a kind, and a search over all the
/// transcripts (PARITY §5).
struct ConversationsView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    private struct Person: Hashable { let id: String; let name: String }
    private struct SearchKey: Hashable { let q: String; let person: String?; let type: String? }
    private struct FeedKey: Hashable { let person: String?; let type: String?; let generation: Int }

    @State private var feed: ConvFeed?
    @State private var pending: Pending?
    @State private var people: People?
    @State private var query = ""
    @State private var results: SearchResult?
    @State private var searched: String?
    @State private var searching = false
    @State private var person: Person?
    @State private var type: String?
    @State private var route: ConvRoute?
    @State private var alert: ConvAlertMessage?
    @State private var loaded = false

    private var feedKey: FeedKey { FeedKey(person: person?.id, type: type, generation: model.apiGeneration) }
    private var searchKey: SearchKey { SearchKey(q: query.trimmingCharacters(in: .whitespaces), person: person?.id, type: type) }

    var body: some View {
        NavigationStack {
            Group {
                if let results {
                    resultsView(results)
                } else {
                    ConvTimeline(
                        conversations: feed?.list?.conversations, hasMore: feed?.hasMore ?? false, onMore: { await feed?.more() },
                        error: feed?.error, onRetry: { Task { await reload() } },
                        empty: person != nil || type != nil ? tr("Nessuna conversazione con questi filtri.", "No conversations with these filters.") : tr("Ancora nessuna trascrizione.", "No transcripts yet."),
                        scrollToTop: reselect.count(for: .conversations),
                        onOpen: open
                    ) { header }
                }
            }
            .refreshable { await reload() }
            .navigationTitle(AppTab.conversations.title)
            .searchable(text: $query, prompt: tr("Cerca", "Search"))
            .onSubmit(of: .search) { let q = searchKey.q; if q.count >= 2 { Task { await search(q) } } }
            .toolbar { if searching { ToolbarItem(placement: .topBarTrailing) { ProgressView() } } }
            .navigationDestination(item: $route) { r in
                ConversationViewer(conversations: r.conversations, initial: r.initial, focus: r.focus, highlight: r.highlight)
            }
            .convAlert($alert)
        }
        .task(id: feedKey) {
            feed = ConvFeed(api: model.api, person: person?.id, type: type)
            await reload()
            loaded = true
        }
        .task(id: searchKey) {
            let q = searchKey.q
            guard q.count >= 2 else { results = nil; searched = nil; return }
            try? await Task.sleep(for: .milliseconds(400))
            if Task.isCancelled { return }
            await search(q)
        }
        .onChange(of: reselect.count(for: .conversations)) { route = nil }
        .onAppear { if loaded { Task { await reload() } } }
    }

    // MARK: pieces

    @ViewBuilder private var header: some View {
        VStack(spacing: 12) {
            HStack(spacing: 8) {
                personMenu
                ConvFilterPills(values: convTypes, selected: Binding(get: { type }, set: { type = $0 }), all: tr("Tutte", "All"))
            }
            .padding(.leading, 16)
            if results == nil, let p = pending, p.progress.running || p.sessions > 0 {
                ConvNotice(
                    icon: p.progress.running ? "hourglass" : "clock",
                    text: p.progress.running
                        ? tr("Il Mac sta trascrivendo (\(p.progress.sessionsDone) di \(p.progress.sessionsTotal) sessioni).", "The Mac is transcribing (\(p.progress.sessionsDone) of \(p.progress.sessionsTotal) sessions).")
                        : "\(counted(p.sessions, "sessione", "sessioni", "session", "sessions")) \(tr("da trascrivere", "to transcribe")) (\(convDuration(p.minutes))): \(tr("da Oggi puoi farle partire subito.", "you can start them now from Today."))"
                ).padding(.horizontal, 16)
            }
        }
        .padding(.bottom, 12)
    }

    private var personMenu: some View {
        let on = person != nil
        return Menu {
            if on { Button(tr("Tutti", "Everyone")) { person = nil } }
            if let p = people {
                Button(p.me.name.isEmpty ? tr("Io", "Me") : p.me.name) { person = Person(id: "me", name: p.me.name.isEmpty ? tr("Io", "Me") : p.me.name) }
                ForEach(p.people) { x in Button(x.name) { person = Person(id: x.id, name: x.name) } }
            }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "person.fill").font(.footnote)
                Text(person?.name ?? tr("Persona", "Person")).font(.subheadline.weight(on ? .semibold : .regular))
                Image(systemName: "chevron.down").font(.caption2)
            }
            .padding(.horizontal, 13).frame(minHeight: 32)
            .foregroundStyle(on ? Color.white : Color.primary)
            .background(Capsule().fill(on ? Color.accentColor : Color(.secondarySystemGroupedBackground)))
        }
        .menuOrder(.fixed)
    }

    private func resultsView(_ r: SearchResult) -> some View {
        ScrollView {
            if r.results.isEmpty {
                VStack(spacing: 10) {
                    Image(systemName: "magnifyingglass").font(.system(size: 44)).foregroundStyle(.tertiary)
                    Text(tr("Nessun risultato per «\(searched ?? "")»", "No results for “\(searched ?? "")”")).font(.headline)
                }.frame(maxWidth: .infinity, minHeight: 300)
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    Text("\(counted(r.results.count, "risultato", "risultati", "result", "results"))\(r.more ? "+" : "")".uppercased())
                        .font(.footnote).foregroundStyle(.secondary).padding(.horizontal, 32).padding(.bottom, 6)
                    VStack(spacing: 0) {
                        ForEach(Array(r.results.enumerated()), id: \.offset) { i, p in
                            Button { openResult(p) } label: { resultRow(p) }.buttonStyle(.plain)
                            if i < r.results.count - 1 { Divider().padding(.leading, 62) }
                        }
                    }
                    .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemGroupedBackground)))
                    .padding(.horizontal, 16)
                }
                .padding(.vertical, 8)
            }
        }
        .background(Color(.systemGroupedBackground))
    }

    private func resultRow(_ p: Paragraph) -> some View {
        HStack(alignment: .top, spacing: 12) {
            ConvAvatar(name: p.speaker, me: p.isMe, size: 34)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(p.speaker ?? tr("Voce sconosciuta", "Unknown voice")).font(.subheadline.weight(.semibold))
                    Spacer()
                    Text("\(convShortDay(p.day ?? "")) \(convHM(p.start))").font(.footnote).foregroundStyle(.secondary)
                    Image(systemName: "chevron.right").font(.footnote).foregroundStyle(.tertiary)
                }
                Text(convReadable(p.text, searched)).font(.subheadline).foregroundStyle(.secondary).lineLimit(3)
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 10).contentShape(Rectangle())
    }

    // MARK: actions

    private func open(_ c: Conversation) {
        guard let all = feed?.list?.conversations, let i = all.firstIndex(where: { $0.session == c.session }) else { return }
        route = ConvRoute(conversations: all, initial: i)
    }

    /// A search result: its conversation, among those of the other results.
    private func openResult(_ p: Paragraph) {
        guard let r = results else { return }
        var seen = Set<String>()
        var convs: [Conversation] = []
        for x in r.results where seen.insert(x.session).inserted {
            convs.append(feed?.list?.conversations.first { $0.session == x.session } ?? Conversation(of: x))
        }
        route = ConvRoute(conversations: convs, initial: convs.firstIndex { $0.session == p.session } ?? 0, focus: p.id, highlight: searched)
    }

    private func reload() async {
        await feed?.refresh()
        guard let api = model.api else { return }
        if let d = try? await api.days() {
            pending = d.pending
            if let t = d.today { ConvArchive.today = t }
        }
        if let p = try? await api.people() { people = p }
    }

    private func search(_ q: String) async {
        guard let api = model.api else { return }
        searching = true
        defer { searching = false }
        let key = searchKey
        do {
            let r = try await api.search(q, person: key.person, type: key.type)
            if searchKey == key { results = r; searched = q }
        } catch is CancellationError {
        } catch {
            alert = ConvAlertMessage(title: tr("Ricerca non riuscita", "Search failed"), message: convMessage(error))
        }
    }
}

/// Every conversation with a person or of a type, as a timeline (Flutter `ConversationsPage`).
struct ConvListPage: View {
    let route: ConvListRoute
    @Environment(AppModel.self) private var model
    @State private var feed: ConvFeed?

    var body: some View {
        ConvTimeline(
            conversations: feed?.list?.conversations, hasMore: feed?.hasMore ?? false, onMore: { await feed?.more() },
            error: feed?.error, onRetry: { Task { await feed?.refresh() } },
            onOpen: { c in
                guard let all = feed?.list?.conversations, let i = all.firstIndex(where: { $0.session == c.session }) else { return }
                path = ConvRoute(conversations: all, initial: i)
            }
        ) { Color.clear.frame(height: 8) }
        .refreshable { await feed?.refresh() }
        .navigationTitle(route.title)
        .navigationBarTitleDisplayMode(.inline)
        .navigationDestination(item: $path) { r in
            ConversationViewer(conversations: r.conversations, initial: r.initial, focus: r.focus, highlight: r.highlight)
        }
        .task(id: model.apiGeneration) {
            feed = ConvFeed(api: model.api, person: route.person, type: route.type)
            await feed?.refresh()
        }
    }

    @State private var path: ConvRoute?
}
