import SwiftUI

/// PLACEHOLDER: replaced by the Ask agent. Keep `struct AskView: View` and read `AppModel` from the environment.
struct AskView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    var body: some View {
        NavigationStack {
            ContentUnavailableView("Chiedi", systemImage: AppTab.ask.symbol, description: Text("Coming soon"))
                .navigationTitle(AppTab.ask.title)
        }
    }
}
