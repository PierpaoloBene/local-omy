import OmyKit
import SwiftUI

// Shared helpers of the Conversations, People and Places tabs: Italian/English formatting of the Mac's
// days and times, avatars and colors, conversation types, the paged feed of conversations, alerts.

// MARK: formatting

/// Today in the archive (the Mac's day, wherever the phone is), set when the day list is loaded.
enum ConvArchive {
    nonisolated(unsafe) static var today: String?
}

private let convCalendar: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: "UTC")!
    return c
}()

private let weekdaysIt = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
private let weekdaysEn = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
private let monthsIt = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
private let monthsEn = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]

private func ymd(_ iso: String) -> (y: Int, m: Int, d: Int)? {
    let p = iso.prefix(10).split(separator: "-")
    guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]), (1...12).contains(m) else { return nil }
    return (y, m, d)
}

private func convDate(_ iso: String) -> Date? {
    guard let t = ymd(iso) else { return nil }
    return convCalendar.date(from: DateComponents(year: t.y, month: t.m, day: t.d))
}

private func localToday() -> String {
    let c = Calendar.current.dateComponents([.year, .month, .day], from: Date())
    return String(format: "%04d-%02d-%02d", c.year ?? 1970, c.month ?? 1, c.day ?? 1)
}

/// "Oggi", "Ieri", or "Mercoledì 29 ottobre" ("Today", "Yesterday", "Wednesday, October 29"), with the year if not this one.
func convLongDay(_ iso: String, relative: Bool = true) -> String {
    guard let t = ymd(iso), let d = convDate(iso) else { return iso }
    let todayIso = ConvArchive.today ?? localToday()
    let today = convDate(todayIso) ?? d
    let ago = convCalendar.dateComponents([.day], from: d, to: today).day ?? 99
    if relative && ago == 0 { return tr("Oggi", "Today") }
    if relative && ago == 1 { return tr("Ieri", "Yesterday") }
    let year = (ymd(todayIso)?.y == t.y) ? "" : " \(t.y)"
    let wd = (convCalendar.component(.weekday, from: d) + 5) % 7  // Monday = 0
    if OmyLanguage.english { return "\(weekdaysEn[wd]), \(monthsEn[t.m - 1]) \(t.d)\(year)" }
    let s = "\(weekdaysIt[wd]) \(t.d) \(monthsIt[t.m - 1])\(year)"
    return s.prefix(1).uppercased() + s.dropFirst()
}

/// "29 ott", "Oct 29".
func convShortDay(_ iso: String) -> String {
    guard let t = ymd(iso) else { return iso }
    let m = String((OmyLanguage.english ? monthsEn : monthsIt)[t.m - 1].prefix(3))
    return OmyLanguage.english ? "\(m) \(t.d)" : "\(t.d) \(m)"
}

func convDayOfMonth(_ iso: String) -> Int { ymd(iso)?.d ?? 0 }
func convMonthShort(_ iso: String) -> String { convShortDay(iso).split(separator: " ").first { Int($0) == nil }.map(String.init) ?? "" }

/// "HH:MM" of a local ISO time as the Mac writes it.
func convHM(_ iso: String) -> String { iso.count >= 16 ? String(iso.dropFirst(11).prefix(5)) : iso }

private func decimal(_ v: Double) -> String {
    let s = String(format: "%.1f", v)
    return OmyLanguage.english ? s : s.replacingOccurrences(of: ".", with: ",")
}

func convDuration(_ minutes: Double) -> String {
    minutes < 1 ? "<1 min" : minutes < 90 ? "\(Int(minutes.rounded())) min" : "\(decimal(minutes / 60)) h"
}

func convMeters(_ m: Int) -> String { m < 1000 ? "\(m) m" : "\(decimal(Double(m) / 1000)) km" }

/// The text of any error: the Mac's message when it is one.
func convMessage(_ e: Error) -> String { (e as? MacError)?.message ?? e.localizedDescription }

// MARK: people

private let personPalette: [Color] = [.orange, .green, .teal, .blue, .purple, .pink, .red, .brown, .mint, .cyan]

/// A stable system color per name; you are the accent.
func convPersonColor(_ name: String?, me: Bool = false) -> Color {
    if me { return .accentColor }
    guard let name else { return .gray }
    let h = name.utf16.reduce(0) { ($0 &* 31 &+ Int($1)) & 0xffff }
    return personPalette[h % personPalette.count]
}

/// Like Contacts: initials on a gradient.
struct ConvAvatar: View {
    let name: String?
    var me = false
    var size: CGFloat = 36

    var body: some View {
        let c = convPersonColor(name, me: me)
        let initial = name.flatMap { $0.isEmpty ? nil : String($0.prefix(1)).uppercased() } ?? "?"
        Text(initial)
            .font(.system(size: size * 0.44, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(Circle().fill(LinearGradient(colors: [c.mix(with: .white, by: 0.25), c], startPoint: .top, endPoint: .bottom)))
            .accessibilityHidden(true)
    }
}

/// Up to three overlapping avatars, like a group in Messages.
struct ConvFaces: View {
    let people: [Participant]
    var body: some View {
        let faces = Array(people.prefix(3))
        ZStack(alignment: .topLeading) {
            ForEach(Array(faces.enumerated()), id: \.offset) { i, p in
                ConvAvatar(name: p.name, me: p.isMe, size: faces.count == 1 ? 34 : 24)
                    .padding(1)
                    .background(Circle().fill(Color(.secondarySystemGroupedBackground)))
                    .offset(x: faces.count == 1 ? 2 : CGFloat(i) * 8, y: faces.count == 1 ? 2 : CGFloat(i) * 6)
            }
        }
        .frame(width: 40, height: 40, alignment: .topLeading)
    }
}

// MARK: conversation types

/// The kinds of conversation, as `omy transcripts conversations --type`.
let convTypes: [(id: String, label: String, icon: String)] = [
    ("room", tr("Di persona", "In person"), "person.2.fill"),
    ("meeting", "Online", "video.fill"),
    ("call", tr("Chiamate", "Calls"), "phone.fill"),
    ("media", "Video", "play.rectangle.fill"),
]

func convTypeIcon(_ type: String) -> String { (convTypes.first { $0.id == type } ?? convTypes[0]).icon }

/// The kind shown for a conversation: video, call, meeting, else in person.
func convKind(_ c: Conversation) -> String {
    c.types.contains("media") ? "media" : c.types.contains("call") ? "call" : c.types.contains("meeting") ? "meeting" : "room"
}

func convCountedSessions(_ n: Int) -> String { counted(n, "sessione", "sessioni", "session", "sessions") }

/// A row of rounded buttons, one selected (nil: "Tutti").
struct ConvFilterPills: View {
    let values: [(id: String, label: String, icon: String)]
    @Binding var selected: String?
    var all: String = tr("Tutti", "All")

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                pill(nil, all, nil)
                ForEach(values, id: \.id) { v in pill(v.id, v.label, v.icon) }
            }
            .padding(.horizontal, 16)
        }
        .scrollClipDisabled()
    }

    private func pill(_ id: String?, _ label: String, _ icon: String?) -> some View {
        let on = id == selected
        return Button {
            selected = on ? nil : id
        } label: {
            HStack(spacing: 5) {
                if let icon { Image(systemName: icon).font(.footnote) }
                Text(label).font(.subheadline.weight(on ? .semibold : .regular))
            }
            .padding(.horizontal, 13).frame(minHeight: 32)
            .foregroundStyle(on ? Color.white : Color.primary)
            .background(Capsule().fill(on ? Color.accentColor : Color(.secondarySystemGroupedBackground)))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// A note at the top of a page (a feature off, what is waiting).
struct ConvNotice: View {
    let icon: String
    let text: String
    var error = false

    var body: some View {
        let c: Color = error ? .red : .orange
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon).foregroundStyle(c).font(.title3)
            Text(text).font(.subheadline)
            Spacer(minLength: 0)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 12).fill(c.opacity(0.13)))
    }
}

/// While loading, or the error with a retry.
struct ConvLoadState: View {
    let loading: Bool
    let error: String?
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 12) {
            if loading {
                ProgressView().controlSize(.large)
            } else {
                Image(systemName: "wifi.slash").font(.system(size: 44)).foregroundStyle(.tertiary)
                Text(error ?? "").font(.subheadline).multilineTextAlignment(.center)
                Button(tr("Riprova", "Try again"), action: retry)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 240)
        .padding(32)
    }
}

/// Centered gray text, for an empty list.
struct ConvEmpty: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
            .frame(maxWidth: .infinity).padding(.horizontal, 32).padding(.vertical, 8)
    }
}

/// [s] with every occurrence of [q] (any case) marked.
func convHighlighted(_ s: String, _ q: String?) -> AttributedString {
    var a = AttributedString(s)
    guard let needle = q?.trimmingCharacters(in: .whitespacesAndNewlines), !needle.isEmpty else { return a }
    var ranges: [Range<AttributedString.Index>] = []
    var from = a.startIndex
    while from < a.endIndex, let r = a[from...].range(of: needle, options: [.caseInsensitive, .diacriticInsensitive]) {
        ranges.append(r)
        from = r.upperBound
    }
    for r in ranges {
        a[r].backgroundColor = Color.yellow.opacity(0.85)
        a[r].foregroundColor = .black
    }
    return a
}

// MARK: alerts

struct ConvAlertMessage: Identifiable {
    let id = UUID()
    let title: String
    let message: String
}

struct ConvConfirm: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    var action: String = tr("Elimina", "Delete")
    var destructive = true
    let run: @MainActor () -> Void
}

extension View {
    func convAlert(_ item: Binding<ConvAlertMessage?>) -> some View {
        alert(item.wrappedValue?.title ?? "", isPresented: Binding(get: { item.wrappedValue != nil }, set: { if !$0 { item.wrappedValue = nil } }),
              presenting: item.wrappedValue) { _ in
            Button("OK", role: .cancel) {}
        } message: { Text($0.message) }
    }

    func convConfirm(_ item: Binding<ConvConfirm?>) -> some View {
        alert(item.wrappedValue?.title ?? "", isPresented: Binding(get: { item.wrappedValue != nil }, set: { if !$0 { item.wrappedValue = nil } }),
              presenting: item.wrappedValue) { c in
            Button(c.action, role: c.destructive ? .destructive : nil) { c.run() }
            Button(tr("Annulla", "Cancel"), role: .cancel) {}
        } message: { Text($0.message) }
    }
}

/// Runs a change on the Mac with `busy` around it; an error as an alert. True if it went through.
@MainActor
func convChange(busy: Binding<Bool>, alert: Binding<ConvAlertMessage?>, _ body: () async throws -> Void) async -> Bool {
    busy.wrappedValue = true
    defer { busy.wrappedValue = false }
    do {
        try await body()
        return true
    } catch {
        alert.wrappedValue = ConvAlertMessage(title: tr("Non riuscito", "Failed"), message: convMessage(error))
        return false
    }
}

// MARK: the paged feed

/// The conversations with a person and of a type, a page at a time (whole days): the first when opened, the next
/// ones as the timeline nears its end. Kept in memory only.
@MainActor @Observable
final class ConvFeed {
    static let pageSize = 1000
    let api: MacAPI?
    let person: String?
    let type: String?
    private(set) var list: ConversationList?
    /// The last failure; with `list` still there, it is out of date.
    private(set) var error: String?
    @ObservationIgnored private var loading = false

    init(api: MacAPI?, person: String? = nil, type: String? = nil) {
        self.api = api; self.person = person; self.type = type
    }

    var hasMore: Bool { list?.next != nil }

    /// The newest page again; the older pages already loaded stay.
    func refresh() async {
        guard let api else { error = tr("Mac non configurato.", "Mac not set up."); return }
        do {
            let first = try await api.conversations(person: person, type: type, limit: Self.pageSize)
            if let old = list, let cut = first.next {
                let older = old.conversations.filter { $0.day < cut }
                list = ConversationList(conversations: first.conversations + older, next: older.isEmpty ? cut : old.next)
            } else {
                list = first
            }
            error = nil
        } catch {
            if !(error is CancellationError) { self.error = convMessage(error) }
        }
    }

    func more() async {
        guard let api, let next = list?.next, !loading else { return }
        loading = true
        defer { loading = false }
        do {
            let page = try await api.conversations(person: person, type: type, before: next, limit: Self.pageSize)
            if let l = list { list = l + page }
            error = nil
        } catch {
            if !(error is CancellationError) { self.error = convMessage(error) }
        }
    }
}

/// A conversation opened in the viewer, from any stack.
struct ConvRoute: Hashable, Identifiable {
    let id = UUID()
    let conversations: [Conversation]
    var initial = 0
    var focus: String?
    var highlight: String?
    static func == (a: ConvRoute, b: ConvRoute) -> Bool { a.id == b.id }
    func hash(into h: inout Hasher) { h.combine(id) }
}

/// Every conversation with a person or of a type, as a timeline (pushed from People).
struct ConvListRoute: Hashable {
    var title: String
    var person: String?
    var type: String?
}

extension View {
    /// Registers the destination of `ConvListRoute` in the enclosing stack.
    func convListDestination() -> some View {
        navigationDestination(for: ConvListRoute.self) { ConvListPage(route: $0) }
    }
}
