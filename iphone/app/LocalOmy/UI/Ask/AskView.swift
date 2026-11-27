import Observation
import OmyKit
import SwiftUI

/// One message of the chat, kept in memory only.
struct AskMessage: Identifiable {
    let id = UUID()
    let mine: Bool
    var text: String
    var citations: [Citation] = []
    var stage: String?
    var error: String?
    var done: Bool
    var notFound = false
    var totalMs: Double?
    var firstMs: Double?
    /// For the retry of a failed answer.
    var question: String?
    /// The hub kept the question because the Mac was off ("in attesa del Mac"): its id, to fetch the answer.
    var queuedID: String?

    static func user(_ t: String) -> AskMessage { AskMessage(mine: true, text: t, done: true) }
    static func answer(_ q: String) -> AskMessage { AskMessage(mine: false, text: "", done: false, question: q) }
    var waiting: Bool { !mine && !done && error == nil }
}

/// The chat state and the stream of the current answer.
@MainActor @Observable
final class AskChat {
    var messages: [AskMessage] = []
    var busy = false
    @ObservationIgnored private var task: Task<Void, Never>?
    /// The questions waiting for the Mac: one task each, asking the hub every few seconds.
    @ObservationIgnored private var waiters: [UUID: Task<Void, Never>] = [:]

    func cancel() {
        task?.cancel()
        task = nil
        if busy, let i = messages.indices.last, messages[i].waiting {
            messages[i].stage = nil
            messages[i].error = tr("Risposta interrotta", "The answer was cut short")
        }
        busy = false
    }

    func reset() {
        task?.cancel()
        task = nil
        waiters.values.forEach { $0.cancel() }
        waiters = [:]
        messages = []
        busy = false
    }

    func send(_ q: String, api: MacAPI?) {
        let q = q.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !busy else { return }
        messages.append(.user(q))
        answer(q, at: messages.count, api: api)
    }

    func retry(_ id: UUID, api: MacAPI?) {
        guard !busy, let i = messages.firstIndex(where: { $0.id == id }), let q = messages[i].question else { return }
        messages.remove(at: i)
        answer(q, at: i, api: api)
    }

    /// Last 8 messages before `index`, without not-found answers and errors.
    private func history(before index: Int) -> [AskTurn] {
        let h = messages.prefix(index).filter { $0.mine || ($0.done && !$0.notFound && !$0.text.isEmpty) }
            .map { AskTurn($0.mine ? "user" : "assistant", $0.text) }
        return Array(h.suffix(8))
    }

    private func answer(_ q: String, at index: Int, api: MacAPI?) {
        // The question itself is the last user message before the answer: history excludes it.
        let hist = history(before: index - 1)
        let msg = AskMessage.answer(q)
        let id = msg.id
        messages.insert(msg, at: index)
        busy = true
        guard let api else {
            update(id) { $0.error = tr("Mac non configurato: URL e token in Impostazioni", "Mac not set up: URL and token in Settings") }
            busy = false
            return
        }
        let clock = ContinuousClock()
        let start = clock.now
        func ms(_ d: Duration) -> Double { Double(d.components.seconds) * 1000 + Double(d.components.attoseconds) / 1e15 }
        task = Task { [weak self] in
            do {
                for try await e in api.ask(question: q, history: hist, now: Date()) {
                    guard let self else { return }
                    switch e {
                    case .status(let s):
                        // "queued:<id>": the Mac is off, the hub keeps the question (the answer comes later)
                        if s.hasPrefix("queued:") { update(id) { $0.queuedID = String(s.dropFirst(7)); $0.stage = "queued" } }
                        else { update(id) { $0.stage = s } }
                    case .citations(let c): update(id) { $0.citations = c }
                    case .delta(let t):
                        if !t.isEmpty {
                            update(id) {
                                if $0.queuedID != nil { return }  // the hub's note for older apps
                                if $0.firstMs == nil { $0.firstMs = ms(clock.now - start) }
                                $0.stage = nil
                                $0.text += t
                            }
                        }
                    case .done(let notFound, _, _):
                        if let queued = messages.first(where: { $0.id == id })?.queuedID {
                            busy = false
                            waitForMac(id, askID: queued, api: api)  // not done: the answer arrives when the Mac does
                            return
                        }
                        update(id) { $0.done = true; $0.notFound = notFound; $0.totalMs = ms(clock.now - start); $0.stage = nil }
                        busy = false
                        return
                    case .error(let m):
                        update(id) { $0.error = m.isEmpty ? tr("Errore dal Mac", "Error from the Mac") : m; $0.stage = nil }
                        busy = false
                        return
                    }
                }
                guard let self else { return }
                // The stream ended with no done: don't wait forever.
                update(id) { if $0.waiting { $0.error = tr("Risposta interrotta", "The answer was cut short"); $0.stage = nil } }
                busy = false
            } catch is CancellationError {
            } catch {
                guard let self else { return }
                update(id) { $0.error = (error as? MacError)?.message ?? "\(error)"; $0.stage = nil }
                busy = false
            }
        }
    }

    /// The question is in the hub's queue: ask every 15 s whether the Mac has answered.
    private func waitForMac(_ id: UUID, askID: String, api: MacAPI) {
        waiters[id]?.cancel()
        waiters[id] = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                guard let self, !Task.isCancelled else { return }
                guard let r = try? await api.askResult(askID) else { continue }
                switch r.state {
                case "done":
                    update(id) {
                        $0.text = r.answer; $0.citations = r.citations; $0.notFound = r.notFound
                        $0.done = true; $0.stage = nil; $0.queuedID = nil
                    }
                    waiters[id] = nil
                    return
                case "failed", "expired", "cancelled":
                    update(id) {
                        $0.error = r.error ?? tr("Il Mac non ha risposto", "The Mac did not answer")
                        $0.stage = nil; $0.queuedID = nil
                    }
                    waiters[id] = nil
                    return
                default: continue
                }
            }
        }
    }

    private func update(_ id: UUID, _ f: (inout AskMessage) -> Void) {
        if let i = messages.firstIndex(where: { $0.id == id }) { f(&messages[i]) }
    }
}

/// The Chiedi tab: ask the archive, the answer streams in with its sources.
struct AskView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    @State private var chat = AskChat()
    @State private var input = ""
    @State private var opened: CiteTarget?
    @State private var noteFor: CiteTarget?
    @FocusState private var focused: Bool

    struct CiteTarget: Identifiable, Hashable {
        let id = UUID()
        let session: String
        let days: [String]
        let rowID: String
        let citation: Citation
        static func == (a: Self, b: Self) -> Bool { a.id == b.id }
        func hash(into h: inout Hasher) { h.combine(id) }

        init(_ c: Citation) {
            session = c.session
            rowID = c.rowID
            citation = c
            // The day of the session (it may cross midnight), then the day of the passage, then its neighbours.
            var d = [c.sessionDay, c.day] + TAFormat.neighbours(c.sessionDay)
            var seen = Set<String>()
            d = d.filter { !$0.isEmpty && seen.insert($0).inserted }
            days = d
        }
    }

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    if chat.messages.isEmpty {
                        emptyState
                    } else {
                        LazyVStack(spacing: 6) {
                            ForEach(chat.messages) { m in
                                if m.mine { UserBubble(text: m.text) } else {
                                    AnswerView(msg: m, onRetry: { chat.retry(m.id, api: model.api) },
                                               onOpen: { opened = CiteTarget($0) }, onNote: { noteFor = CiteTarget($0) })
                                }
                            }
                            Color.clear.frame(height: 1).id("end")
                        }
                        .padding(.vertical, 12)
                    }
                }
                .defaultScrollAnchor(.bottom)
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: chat.messages.last?.text) { proxy.scrollTo("end", anchor: .bottom) }
                .onChange(of: chat.messages.count) { withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
                .onChange(of: reselect.count(for: .ask)) { withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
            }
            .safeAreaInset(edge: .bottom) { inputBar }
            .navigationTitle(AppTab.ask.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if !chat.messages.isEmpty {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button(tr("Nuova chat", "New chat"), systemImage: "square.and.pencil") { chat.reset() }
                    }
                }
            }
            .navigationDestination(item: $opened) { t in
                ConversationLink(session: t.session, days: t.days, rowID: t.rowID.isEmpty ? nil : t.rowID)
            }
            .sheet(item: $noteFor) { t in NoteSheet(citation: t.citation) }
        }
        .onDisappear { chat.cancel() }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label(tr("Chiedi all'archivio", "Ask the archive"), systemImage: AppTab.ask.symbol)
        } description: {
            Text(tr("Chiedi qualcosa sulle tue conversazioni: \"Cosa ho promesso a Marco la settimana scorsa?\"",
                    "Ask about your conversations: \"What did I promise Marco last week?\""))
        }
        .containerRelativeFrame(.vertical)
    }

    private var canSend: Bool { !chat.busy && !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private func send() {
        guard canSend else { return }
        let q = input
        input = ""
        chat.send(q, api: model.api)
    }

    /// Floating input bar in glass; messages scroll beneath it.
    private var inputBar: some View {
        GlassEffectContainer(spacing: 8) {
            HStack(alignment: .bottom, spacing: 8) {
                TextField(tr("Chiedi all'archivio…", "Ask the archive…"), text: $input, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($focused)
                    .submitLabel(.send)
                    .onSubmit(send)
                    .padding(.horizontal, 16).padding(.vertical, 11)
                    .disabled(chat.busy)
                    .modifier(BarGlass(opaque: reduceTransparency, shape: .capsule))
                Button(action: send) {
                    Image(systemName: "arrow.up").font(.headline).frame(width: 26, height: 26)
                }
                .buttonStyle(.glassProminent)
                .buttonBorderShape(.circle)
                .disabled(!canSend)
                .accessibilityLabel(tr("Invia", "Send"))
            }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 6)
    }
}

/// Interactive glass, or an opaque capsule with Reduce Transparency.
private struct BarGlass: ViewModifier {
    let opaque: Bool
    let shape: Capsule
    func body(content: Content) -> some View {
        if opaque {
            content.background(Color(.secondarySystemBackground), in: shape)
        } else {
            content.glassEffect(.regular.interactive(), in: shape)
        }
    }
}

private struct UserBubble: View {
    let text: String
    var body: some View {
        Text(text)
            .foregroundStyle(.white)
            .padding(.horizontal, 14).padding(.vertical, 9)
            .background(Color.accentColor, in: .rect(cornerRadius: 18))
            .frame(maxWidth: .infinity, alignment: .trailing)
            .padding(.leading, 60).padding(.trailing, 12)
            .textSelection(.enabled)
    }
}

private struct AnswerView: View {
    let msg: AskMessage
    let onRetry: () -> Void
    let onOpen: (Citation) -> Void
    let onNote: (Citation) -> Void

    private var status: String? {
        switch msg.stage {
        case "queued": return tr("In attesa del Mac: ti rispondo appena è acceso…", "Waiting for the Mac: the answer comes when it is on…")
        case "waking": return tr("Sveglio il modello…", "Waking the model…")
        case "searching": return tr("Cerco nell'archivio…", "Searching the archive…")
        case "answering": return tr("Scrivo la risposta…", "Writing the answer…")
        default: return msg.waiting && msg.text.isEmpty ? tr("Cerco nell'archivio…", "Searching the archive…") : nil
        }
    }

    private static func seconds(_ ms: Double) -> String { TAFormat.decimal(ms / 1000) }

    private var timing: String? {
        guard let t = msg.totalMs else { return nil }
        guard let f = msg.firstMs else { return "\(Self.seconds(t)) s" }
        return "\(Self.seconds(t)) s · \(tr("primo testo", "first text")) \(Self.seconds(f)) s"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if msg.notFound {
                Label(msg.text, systemImage: "magnifyingglass")
                    .font(.subheadline).foregroundStyle(.secondary)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(Color.orange.opacity(0.12), in: .rect(cornerRadius: 18))
            } else if !msg.text.isEmpty {
                AnswerText(msg: msg, onOpen: onOpen)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(Color(.secondarySystemGroupedBackground), in: .rect(cornerRadius: 18))
            }
            if let status {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(status).font(.caption).foregroundStyle(.secondary)
                }
                .padding(.leading, 14)
            }
            if let e = msg.error {
                HStack {
                    Text(e).font(.subheadline)
                    Button(tr("Riprova", "Retry"), action: onRetry).buttonStyle(.borderless)
                }
                .padding(.leading, 14).padding(.trailing, 6).padding(.vertical, 9)
                .background(Color.red.opacity(0.12), in: .rect(cornerRadius: 18))
            }
            if !msg.citations.isEmpty, !msg.text.isEmpty || msg.done {
                FlowLayout(spacing: 6) {
                    ForEach(msg.citations, id: \.n) { c in CitationChip(c: c, onOpen: onOpen, onNote: onNote) }
                }
            }
            if let timing { Text(timing).font(.caption).foregroundStyle(.secondary).padding(.leading, 14) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.leading, 12).padding(.trailing, 40)
    }
}

/// The answer with its [n] as small tappable numbers raised on the line, and a soft cursor while it streams.
private struct AnswerText: View {
    let msg: AskMessage
    let onOpen: (Citation) -> Void

    private var attributed: AttributedString {
        var out = AttributedString()
        let text = msg.text
        let re = try! NSRegularExpression(pattern: #"\[(\d+)\]"#)
        var last = text.startIndex
        for m in re.matches(in: text, range: NSRange(text.startIndex..., in: text)) {
            guard let r = Range(m.range, in: text), let nr = Range(m.range(at: 1), in: text) else { continue }
            out += AttributedString(String(text[last..<r.lowerBound]))
            last = r.upperBound
            var chip = AttributedString(String(text[nr]))
            chip.font = .caption2
            chip.baselineOffset = 6
            if let n = Int(text[nr]), msg.citations.contains(where: { $0.n == n }) {
                chip.foregroundColor = .accentColor
                chip.link = URL(string: "omy-cite://\(n)")
            }
            out += chip
        }
        out += AttributedString(String(text[last...]))
        if !msg.done && msg.error == nil {
            var cursor = AttributedString(" ▍")
            cursor.foregroundColor = Color.accentColor.opacity(0.45)
            out += cursor
        }
        return out
    }

    var body: some View {
        Text(attributed)
            .environment(\.openURL, OpenURLAction { url in
                if url.scheme == "omy-cite", let n = Int(url.host() ?? ""), let c = msg.citations.first(where: { $0.n == n }) {
                    onOpen(c)
                }
                return .handled
            })
    }
}

private struct CitationChip: View {
    let c: Citation
    let onOpen: (Citation) -> Void
    let onNote: (Citation) -> Void

    var body: some View {
        let label = "[\(c.n)] \(TAFormat.shortDay(c.day))\(c.time.isEmpty ? "" : " \(c.time)")\(c.title.isEmpty ? "" : " · \(c.title)")"
        Button { onOpen(c) } label: {
            Text(label).font(.caption).lineLimit(1)
                .foregroundStyle(Color.accentColor)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .frame(maxWidth: 260)
                .background(Color.accentColor.opacity(0.12), in: .rect(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .contextMenu {
            Button(tr("Nota", "Note"), systemImage: "note.text") { onNote(c) }
        }
        .accessibilityHint(tr("Tocca per aprire la conversazione, tieni premuto per la nota", "Tap to open the conversation, hold for the note"))
    }
}

/// Wrapping row layout for the chips.
private struct FlowLayout: Layout {
    var spacing: CGFloat = 6
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let w = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, maxX: CGFloat = 0
        for s in subviews {
            let z = s.sizeThatFits(.unspecified)
            if x > 0, x + z.width > w { x = 0; y += row + spacing; row = 0 }
            x += z.width + spacing; row = max(row, z.height); maxX = max(maxX, x - spacing)
        }
        return CGSize(width: maxX, height: y + row)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for s in subviews {
            let z = s.sizeThatFits(.unspecified)
            if x > bounds.minX, x + z.width > bounds.maxX { x = bounds.minX; y += row + spacing; row = 0 }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(z))
            x += z.width + spacing; row = max(row, z.height)
        }
    }
}

/// The note of a source: title, summary, key points, commitments, dates.
private struct NoteSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let citation: Citation
    @State private var state: LoadState = .loading

    enum LoadState { case loading, none, failed(String), note(Note) }

    var body: some View {
        NavigationStack {
            Group {
                switch state {
                case .loading: ProgressView()
                case .none:
                    Text(tr("Nessuna nota per questa conversazione", "No note for this conversation")).font(.subheadline).foregroundStyle(.secondary)
                case .failed(let e): Text(e).font(.subheadline).foregroundStyle(.secondary).padding()
                case .note(let n): noteBody(n)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .navigationTitle(tr("Nota", "Note"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button(tr("Chiudi", "Close")) { dismiss() } } }
        }
        .presentationDetents([.large, .medium])
        .task {
            guard let api = model.api else { state = .failed(tr("Mac non configurato: URL e token in Impostazioni", "Mac not set up: URL and token in Settings")); return }
            do {
                if let n = try await api.note(session: citation.session) { state = .note(n) } else { state = .none }
            } catch {
                state = .failed((error as? MacError)?.message ?? "\(error)")
            }
        }
    }

    private func noteBody(_ n: Note) -> some View {
        let when = [n.data, n.oraInizio].filter { !$0.isEmpty }.joined(separator: " ")
        return ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(n.titolo).font(.title2.weight(.bold))
                if !when.isEmpty { Text(when).font(.footnote).foregroundStyle(.secondary) }
                if !n.riassunto.isEmpty { Text(n.riassunto) }
                block(tr("Punti chiave", "Key points"), n.puntiChiave)
                block(tr("Impegni", "Commitments"), n.impegni)
                block(tr("Date ed eventi", "Dates and events"), n.dateEventi)
                block(tr("Persone e luoghi", "People and places"), n.entita)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
        }
    }

    @ViewBuilder private func block(_ title: String, _ items: [String]) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text(title.uppercased()).font(.caption).foregroundStyle(.secondary)
                ForEach(Array(items.enumerated()), id: \.offset) { _, s in Text("• \(s)").font(.subheadline) }
            }
        }
    }
}

#Preview {
    AskView().environment(AppModel.preview()).environment(TabReselect())
}
