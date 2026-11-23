import OmyKit
import SwiftUI

/// Every conversation, newest first, in one scroll: the day stays at the top while you scroll through it, and the
/// index on the trailing edge jumps to a day, like the letters in Contacts. Older pages load as the end comes near.
struct ConvTimeline<Top: View>: View {
    /// nil while loading.
    let conversations: [Conversation]?
    var hasMore = false
    var onMore: () async -> Void = {}
    /// With `conversations`, they are out of date: shown above them.
    var error: String?
    var onRetry: () -> Void = {}
    var empty: String?
    /// Increases when the tab is tapped again: back to the top.
    var scrollToTop = 0
    let onOpen: (Conversation) -> Void
    @ViewBuilder var top: () -> Top

    @State private var scrubbing: String?
    @State private var scrubIndex: Int?

    private struct Day: Identifiable {
        let day: String
        var items: [Conversation]
        var id: String { day }
    }

    private var days: [Day] {
        var out: [Day] = []
        for c in conversations ?? [] {
            if out.last?.day == c.day { out[out.count - 1].items.append(c) } else { out.append(Day(day: c.day, items: [c])) }
        }
        return out
    }

    var body: some View {
        let days = days
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
                    Color.clear.frame(height: 1).id("convTop")
                    top()
                    if let all = conversations {
                        if let error { ConvNotice(icon: "exclamationmark.circle", text: tr("Non aggiornato: \(error)", "Not up to date: \(error)"), error: false).padding(.horizontal, 16).padding(.bottom, 12) }
                        if all.isEmpty { ConvEmpty(empty ?? tr("Nessuna conversazione.", "No conversations.")) }
                        ForEach(days) { d in
                            Section {
                                ForEach(d.items) { c in
                                    ConvTimelineRow(c: c) { onOpen(c) }
                                }
                            } header: {
                                DayHeader(day: d.day, items: d.items).id(d.day)
                            }
                        }
                        if hasMore {
                            ProgressView().frame(maxWidth: .infinity).padding(16)
                                .task(id: all.count) { await onMore() }
                        }
                    } else {
                        ConvLoadState(loading: error == nil, error: error, retry: onRetry)
                    }
                    Color.clear.frame(height: 40)
                }
            }
            .background(Color(.systemGroupedBackground))
            .overlay(alignment: .trailing) {
                if days.count > 1 {
                    DayIndex(days: days.map(\.day), at: $scrubIndex) { i in
                        proxy.scrollTo(days[i].day, anchor: .top)
                        scrubbing = days[i].day
                    } onEnd: { scrubbing = nil }
                    .padding(.vertical, 56)
                }
            }
            .overlay {
                if let scrubbing {
                    Text(convLongDay(scrubbing)).font(.title2.bold())
                        .padding(.horizontal, 22).padding(.vertical, 14)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
                        .allowsHitTesting(false)
                }
            }
            .onChange(of: scrollToTop) { withAnimation { proxy.scrollTo("convTop", anchor: .top) } }
        }
    }
}

private struct DayHeader: View {
    let day: String
    let items: [Conversation]

    var body: some View {
        HStack(spacing: 8) {
            Text(convLongDay(day)).font(.headline)
            Text("\(counted(items.count, "conversazione", "conversazioni", "conversation", "conversations")) · \(convDuration(Double(items.reduce(0) { $0 + $1.minutes })))")
                .font(.footnote).foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.leading, 20).padding(.trailing, 36).frame(height: 40)
        .background(Color(.systemGroupedBackground).opacity(0.96))
        .accessibilityAddTraits(.isHeader)
    }
}

/// A conversation in the timeline.
struct ConvTimelineRow: View {
    let c: Conversation
    let onTap: () -> Void

    var body: some View {
        let others = c.participants.filter { !$0.isMe }
        Button(action: onTap) {
            HStack(spacing: 12) {
                ConvFaces(people: others.isEmpty ? c.participants : others)
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(([convHM(c.start)] + [c.app ?? c.place].compactMap { $0 }).joined(separator: " · "))
                            .font(.subheadline.weight(.semibold)).lineLimit(1)
                        Spacer(minLength: 4)
                        Text(convDuration(Double(c.minutes))).font(.footnote).foregroundStyle(.secondary)
                    }
                    HStack(spacing: 4) {
                        Image(systemName: convTypeIcon(convKind(c))).font(.caption2).foregroundStyle(.secondary)
                        Text(others.isEmpty ? tr("Solo tu", "Just you") : others.map(\.name).joined(separator: ", "))
                            .font(.footnote).lineLimit(1)
                    }
                    if !c.snippet.isEmpty {
                        Text("«\(c.snippet)»").font(.footnote).italic().foregroundStyle(.secondary).lineLimit(1)
                    }
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 12).fill(Color(.secondarySystemGroupedBackground)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.leading, 16).padding(.trailing, 36).padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}

/// The days down the trailing edge: touch or drag to jump. Labels as many as fit: the day of the month, the month
/// where it changes.
private struct DayIndex: View {
    let days: [String]
    @Binding var at: Int?
    let onDay: (Int) -> Void
    let onEnd: () -> Void

    var body: some View {
        GeometryReader { box in
            let h = box.size.height, n = days.count
            let fit = max(1, min(n, Int(h / 18)))
            let step = Int((Double(n) / Double(fit)).rounded(.up))
            ZStack(alignment: .top) {
                ForEach(Array(stride(from: 0, to: n, by: step)), id: \.self) { i in
                    Text(label(i))
                        .font(.system(size: 11, weight: .semibold)).foregroundStyle(Color.accentColor)
                        .frame(maxWidth: .infinity)
                        .offset(y: CGFloat(i) / CGFloat(n) * h)
                }
            }
            .frame(width: 30, height: h, alignment: .top)
            .background(RoundedRectangle(cornerRadius: 15).fill(at == nil ? Color.clear : Color(.systemFill)))
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { v in
                        let i = min(n - 1, max(0, Int(v.location.y / h * CGFloat(n))))
                        if i != at { at = i; onDay(i) }
                    }
                    .onEnded { _ in at = nil; onEnd() }
            )
            .sensoryFeedback(.selection, trigger: at)
        }
        .frame(width: 30)
        .accessibilityHidden(true)
    }

    private func label(_ i: Int) -> String {
        let m = convMonthShort(days[i])
        let prev = i > 0 ? convMonthShort(days[i - 1]) : nil
        return i > 0 && m != prev ? m : "\(convDayOfMonth(days[i]))"
    }
}
