import SwiftUI

/// PLACEHOLDER: replaced by the People agent. Keep `struct PeopleView: View` and read `AppModel` from the environment.
struct PeopleView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    var body: some View {
        NavigationStack {
            ContentUnavailableView("Persone", systemImage: AppTab.people.symbol, description: Text("Coming soon"))
                .navigationTitle(AppTab.people.title)
        }
    }
}
