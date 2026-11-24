import OmyKit
import SwiftUI

/// Conversations like Messages threads, one per page (PARITY §5 "Viewer conversazione").
/// CONTRACT shared by the UI agents: Today, Conversations, People and Ask open it with this initializer.
/// `conversations` newest first, as in the timeline; `focus` = a paragraph id (JSONL row id) to center and outline,
/// `highlight` = text to mark (search). Owned by the Conversations agent.
struct ConversationViewer: View {
    let conversations: [Conversation]
    let initial: Int
    var focus: String? = nil
    var highlight: String? = nil

    @Environment(AppModel.self) private var model
    @State private var position: Int?
    @State private var days = ConvDayCache()

    init(conversations: [Conversation], initial: Int, focus: String? = nil, highlight: String? = nil) {
        self.conversations = conversations
        self.initial = initial
        self.focus = focus
        self.highlight = highlight
        let start = conversations.isEmpty ? 0 : conversations.count - 1 - min(max(initial, 0), conversations.count - 1)
        _position = State(initialValue: start)
    }

    var body: some View {
        // Chronological: swiping right goes back in time, like Photos.
        let list = Array(conversations.reversed())
        let n = list.count
        let p = min(max(position ?? 0, 0), max(n - 1, 0))
        let focusSession = conversations.indices.contains(initial) ? conversations[initial].session : nil
        Group {
            if n == 0 {
                ContentUnavailableView(tr("Conversazione non trovata", "Conversation not found"), systemImage: "bubble.left.and.bubble.right")
            } else {
                ScrollView(.horizontal) {
                    LazyHStack(spacing: 0) {
                        ForEach(0..<n, id: \.self) { i in
                            ConvThread(
                                conversation: list[i], days: days, api: model.api,
                                focus: list[i].session == focusSession ? focus : nil, highlight: highlight,
                                earlier: i > 0 ? list[i - 1] : nil, later: i + 1 < n ? list[i + 1] : nil,
                                onEarlier: { go(i - 1) }, onLater: { go(i + 1) }
                            )
                            .containerRelativeFrame(.horizontal)
                            .id(i)
                        }
                    }
                    .scrollTargetLayout()
                }
                .scrollTargetBehavior(.paging)
                .scrollPosition(id: $position)
                .scrollIndicators(.hidden)
            }
        }
        .background(Color(.systemGroupedBackground))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if n > 0 {
                let c = list[p]
                ToolbarItem(placement: .principal) {
                    VStack(spacing: 0) {
                        Text("\(convLongDay(c.day)) \(convHM(c.start))").font(.headline)
                        if let sub = c.app ?? c.place { Text(sub).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                    }
                }
                if n > 1 {
                    ToolbarItem(placement: .topBarTrailing) {
                        Text("\(p + 1)/\(n)").font(.footnote).foregroundStyle(.secondary).monospacedDigit()
                    }
                }
            }
        }
    }

    private func go(_ i: Int) { withAnimation(.easeOut(duration: 0.3)) { position = i } }
}

/// Days loaded with `api.day(_:)`, kept in memory for the life of the viewer.
@MainActor @Observable
final class ConvDayCache {
    private(set) var days: [String: Day] = [:]
    private(set) var errors: [String: String] = [:]
    @ObservationIgnored private var loading = Set<String>()

    func load(_ day: String, api: MacAPI?) async {
        guard days[day] == nil, !loading.contains(day) else { return }
        guard let api else { errors[day] = tr("Mac non configurato.", "Mac not set up."); return }
        loading.insert(day)
        defer { loading.remove(day) }
        do {
            days[day] = try await api.day(day)
            errors[day] = nil
        } catch is CancellationError {
        } catch {
            errors[day] = convMessage(error)
        }
    }
}

/// One conversation: who was there, then the bubbles; at the end, the way to the one before and the one after.
private struct ConvThread: View {
    let conversation: Conversation
    let days: ConvDayCache
    let api: MacAPI?
    var focus: String?
    var highlight: String?
    let earlier: Conversation?
    let later: Conversation?
    let onEarlier: () -> Void
    let onLater: () -> Void

    private enum Item: Identifiable {
        case place(id: String, time: String, label: String, named: Bool)
        case bubble(Paragraph, first: Bool)
        var id: String {
            switch self {
            case .place(let id, _, _, _): "place-" + id
            case .bubble(let p, _): p.id
            }
        }
    }

    var body: some View {
        Group {
            if let day = days.days[conversation.day] {
                content(day)
            } else if days.errors[conversation.day] != nil {
                Text(tr("Il Mac non ha risposto: torna indietro e riprova.", "The Mac did not answer: go back and try again."))
                    .font(.subheadline).multilineTextAlignment(.center).padding(32)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task(id: conversation.day) { await days.load(conversation.day, api: api) }
    }

    private func content(_ day: Day) -> some View {
        let items = build(day.paragraphs.filter { $0.session == conversation.session })
        let focused = focus.flatMap { f in items.first { if case .bubble(let p, _) = $0 { p.id == f } else { false } }?.id }
        return ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 0) {
                    Who(conversation: conversation).padding(.top, 12)
                    if items.isEmpty {
                        ConvEmpty(tr("Nessun paragrafo: forse la conversazione è stata trascritta di nuovo.", "No paragraphs: maybe the conversation was transcribed again."))
                    }
                    ForEach(items) { item in
                        switch item {
                        case .place(_, let time, let label, let named): PlaceHeader(time: time, place: label, named: named)
                        case .bubble(let p, let first): Bubble(paragraph: p, highlight: highlight, focused: p.id == focus, first: first)
                        }
                    }
                    Neighbours(earlier: earlier, later: later, onEarlier: onEarlier, onLater: onLater).padding(.top, 24)
                    Color.clear.frame(height: 24)
                }
            }
            .task(id: focused) {
                guard let focused else { return }
                try? await Task.sleep(for: .milliseconds(150))
                proxy.scrollTo(focused, anchor: UnitPoint(x: 0.5, y: 0.3))
            }
        }
    }

    /// The bubbles, with a line where the place changes.
    private func build(_ paragraphs: [Paragraph]) -> [Item] {
        var out: [Item] = []
        var place: String?
        var speaker: String?
        for p in paragraphs {
            let label = p.place?.label
            if let label, let current = place, label != current {
                out.append(.place(id: p.id, time: convHM(p.start), label: label, named: p.place?.name != nil))
                speaker = nil
            }
            place = label ?? place
            out.append(.bubble(p, first: p.speaker != speaker || p.speaker == nil))
            speaker = p.speaker
        }
        return out
    }
}

/// Who spoke, and for how long.
private struct Who: View {
    let conversation: Conversation

    var body: some View {
        let c = conversation
        VStack(spacing: 8) {
            Text((["\(convHM(c.start))–\(convHM(c.end))", convDuration(Double(c.minutes))] + (c.place != nil && c.app != nil ? [c.place!] : [])).joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary)
            if !c.participants.isEmpty {
                FlowLayout(spacing: 6) {
                    ForEach(Array(c.participants.enumerated()), id: \.offset) { _, p in
                        HStack(spacing: 6) {
                            ConvAvatar(name: p.name, me: p.isMe, size: 22)
                            Text("\(p.name) · \(convDuration(p.minutes))").font(.subheadline).lineLimit(1)
                        }
                        .padding(.leading, 4).padding(.trailing, 10).padding(.vertical, 4)
                        .background(Capsule().fill(Color(.tertiarySystemFill)))
                    }
                }
                .padding(.horizontal, 16)
            }
        }
        .padding(.bottom, 8)
    }
}

/// Wraps its children onto lines, centered.
private struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    private func rows(_ subviews: Subviews, width: CGFloat) -> [[Int]] {
        var rows: [[Int]] = [[]]
        var x: CGFloat = 0
        for (i, s) in subviews.enumerated() {
            let w = s.sizeThatFits(.unspecified).width
            if x > 0 && x + w > width { rows.append([]); x = 0 }
            rows[rows.count - 1].append(i)
            x += w + spacing
        }
        return rows
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? 320
        let rs = rows(subviews, width: width)
        let h = rs.map { r in r.map { subviews[$0].sizeThatFits(.unspecified).height }.max() ?? 0 }
        return CGSize(width: width, height: h.reduce(0, +) + spacing * CGFloat(max(rs.count - 1, 0)))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for r in rows(subviews, width: bounds.width) {
            let sizes = r.map { subviews[$0].sizeThatFits(.unspecified) }
            let total = sizes.reduce(0) { $0 + $1.width } + spacing * CGFloat(r.count - 1)
            var x = bounds.minX + (bounds.width - total) / 2
            for (k, i) in r.enumerated() {
                subviews[i].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(sizes[k]))
                x += sizes[k].width + spacing
            }
            y += (sizes.map(\.height).max() ?? 0) + spacing
        }
    }
}

private struct Neighbours: View {
    let earlier: Conversation?
    let later: Conversation?
    let onEarlier: () -> Void
    let onLater: () -> Void

    var body: some View {
        HStack {
            side(earlier, before: true).frame(maxWidth: .infinity, alignment: .leading)
            side(later, before: false).frame(maxWidth: .infinity, alignment: .trailing)
        }
    }

    @ViewBuilder private func side(_ c: Conversation?, before: Bool) -> some View {
        if let c {
            Button(action: before ? onEarlier : onLater) {
                VStack(alignment: before ? .leading : .trailing, spacing: 2) {
                    Label(before ? tr("Prima", "Earlier") : tr("Dopo", "Later"), systemImage: before ? "chevron.backward" : "chevron.forward")
                        .labelStyle(NeighbourLabel(leading: before))
                    Text("\(convLongDay(c.day)) \(convHM(c.start))").font(.caption).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 16).padding(.vertical, 8)
            }
            .buttonStyle(.plain)
            .foregroundStyle(Color.accentColor)
        }
    }
}

private struct NeighbourLabel: LabelStyle {
    let leading: Bool
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            if leading { configuration.icon.font(.footnote) }
            configuration.title
            if !leading { configuration.icon.font(.footnote) }
        }
    }
}

private struct PlaceHeader: View {
    let time: String
    let place: String
    let named: Bool

    var body: some View {
        HStack(spacing: 4) {
            Text(time).fontWeight(.semibold)
            Image(systemName: named ? "location.fill" : "location").font(.caption2).padding(.leading, 4)
            Text(place)
        }
        .font(.caption).foregroundStyle(.secondary)
        .frame(maxWidth: .infinity).padding(.horizontal, 20).padding(.top, 22).padding(.bottom, 6)
    }
}

/// A paragraph as a message bubble: you on the right in the accent color, the others on the left.
private struct Bubble: View {
    let paragraph: Paragraph
    var highlight: String?
    var focused = false
    /// First of a run by the same speaker: name and avatar.
    var first = true

    var body: some View {
        let p = paragraph
        let me = p.isMe
        HStack(alignment: .bottom, spacing: 6) {
            if me { Spacer(minLength: 56) } else {
                Group { if first { ConvAvatar(name: p.speaker, size: 30) } else { Color.clear } }.frame(width: 30, height: 30)
            }
            VStack(alignment: me ? .trailing : .leading, spacing: 3) {
                if first { meta(p, me: me).padding(.horizontal, 12) }
                if convIsUnclear(p.text) {
                    Text(tr("non si capisce", "unintelligible"))
                        .font(.footnote).italic().foregroundStyle(.secondary)
                        .padding(.horizontal, 12).padding(.vertical, 5)
                        .background(Capsule().fill(Color(.tertiarySystemFill)))
                } else {
                Text(convReadable(p.text, highlight))
                    .font(.system(size: 16)).lineSpacing(3)
                    .foregroundStyle(me ? Color.white : Color.primary)
                    .padding(.horizontal, 13).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 18).fill(me ? Color.accentColor : Color(.secondarySystemGroupedBackground)))
                    .overlay { if focused { RoundedRectangle(cornerRadius: 18).stroke(Color.yellow, lineWidth: 2.5) } }
                    .textSelection(.enabled)
                }
            }
            if !me { Spacer(minLength: 48) }
        }
        .padding(.leading, me ? 56 : 10).padding(.trailing, me ? 12 : 0).padding(.top, first ? 8 : 2)
        .id(p.id)
    }

    private func meta(_ p: Paragraph, me: Bool) -> some View {
        HStack(spacing: 6) {
            if !me {
                Text(p.speaker ?? tr("Voce sconosciuta", "Unknown voice"))
                    .fontWeight(.semibold).foregroundStyle(convPersonColor(p.speaker)).lineLimit(1)
            }
            if let via = p.via {
                HStack(spacing: 3) {
                    Image(systemName: via.kind == "call" ? (via.app == "iPhone" ? "phone.fill" : "video.fill") : "play.rectangle.fill").font(.system(size: 11))
                    if let app = via.app { Text(app) }
                }
            }
            Text(convHM(p.start))
        }
        .font(.caption).foregroundStyle(.secondary)
    }
}

/// Opening a cited/linked paragraph: finds the session among the conversations of `days` (tried in order),
/// then shows ConversationViewer focused on `rowID`. CONTRACT for Ask and Today; implemented by the Conversations agent.
struct ConversationLink: View {
    let session: String
    let days: [String]
    var rowID: String? = nil
    var highlight: String? = nil

    @Environment(AppModel.self) private var model
    @State private var found: (list: [Conversation], index: Int)?
    @State private var searching = true

    var body: some View {
        Group {
            if let found {
                ConversationViewer(conversations: found.list, initial: found.index, focus: rowID, highlight: highlight)
            } else if searching {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity).background(Color(.systemGroupedBackground))
            } else {
                ContentUnavailableView(tr("Conversazione non trovata", "Conversation not found"), systemImage: "bubble.left.and.bubble.right")
            }
        }
        .task(id: model.apiGeneration) { await find() }
    }

    private func find() async {
        searching = true
        defer { searching = false }
        guard let api = model.api else { return }
        for day in days {
            if let page = try? await api.conversations(day: day, limit: 1000),
               let i = page.conversations.firstIndex(where: { $0.session == session }) {
                found = (page.conversations, i)
                return
            }
        }
    }
}
