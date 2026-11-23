import SwiftUI

/// PLACEHOLDER: replaced by the Conversations agent. Keep `struct ConversationsView: View` and read `AppModel` from the environment.
struct ConversationsView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    var body: some View {
        NavigationStack {
            ContentUnavailableView("Conversazioni", systemImage: AppTab.conversations.symbol, description: Text("Coming soon"))
                .navigationTitle(AppTab.conversations.title)
        }
    }
}
