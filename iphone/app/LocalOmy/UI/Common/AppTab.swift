import Observation
import OmyKit
import SwiftUI

enum AppTab: String, CaseIterable, Identifiable, Hashable {
    case today, conversations, people, places, ask
    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: tr("Oggi", "Today")
        case .conversations: tr("Conversazioni", "Conversations")
        case .people: tr("Persone", "People")
        case .places: tr("Luoghi", "Places")
        case .ask: tr("Chiedi", "Ask")
        }
    }

    var symbol: String {
        switch self {
        case .today: "waveform.circle"
        case .conversations: "bubble.left.and.bubble.right"
        case .people: "person.2"
        case .places: "mappin.and.ellipse"
        case .ask: "sparkles"
        }
    }
}

/// Tapping the active tab again: each tab watches its counter (`.onChange(of: reselect.count(for: .today))`) and
/// pops to its root / scrolls to the top.
@MainActor @Observable
final class TabReselect {
    private var counts: [AppTab: Int] = [:]
    func count(for tab: AppTab) -> Int { counts[tab, default: 0] }
    func bump(_ tab: AppTab) { counts[tab, default: 0] += 1 }
}

extension PendantStatus {
    var color: Color {
        switch self {
        case .connected: .green
        case .idle: .yellow
        case .disconnected: .orange
        case .notFound: .gray
        }
    }

    var title: String {
        switch self {
        case .connected: tr("Connesso", "Connected")
        case .idle: tr("Inattivo", "Idle")
        case .disconnected: tr("Disconnesso", "Disconnected")
        case .notFound: tr("Non trovato", "Not found")
        }
    }
}
