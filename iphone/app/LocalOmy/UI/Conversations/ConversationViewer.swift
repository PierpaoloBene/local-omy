import SwiftUI
import OmyKit

/// Conversations like Messages threads, one per page (PARITY §5 "Viewer conversazione").
/// CONTRACT shared by the UI agents: Today, Conversations, People and Ask open it with this initializer.
/// `conversations` newest first, as in the timeline; `focus` = a paragraph id (JSONL row id) to center and outline,
/// `highlight` = text to mark (search). Owned by the Conversations agent, which replaces this body.
struct ConversationViewer: View {
    let conversations: [Conversation]
    let initial: Int
    var focus: String? = nil
    var highlight: String? = nil

    var body: some View {
        Text(conversations.indices.contains(initial) ? conversations[initial].session : "")
    }
}

/// Opening a cited/linked paragraph: finds the session among the conversations of `days` (tried in order),
/// then shows ConversationViewer focused on `rowID`. CONTRACT for Ask and Today; implemented by the Conversations agent.
struct ConversationLink: View {
    let session: String
    let days: [String]
    var rowID: String? = nil
    var highlight: String? = nil

    var body: some View { ProgressView() }
}
