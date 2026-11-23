import SwiftUI

/// PLACEHOLDER: replaced by the Places agent. Keep `struct PlacesView: View` and read `AppModel` from the environment.
struct PlacesView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    var body: some View {
        NavigationStack {
            ContentUnavailableView("Luoghi", systemImage: AppTab.places.symbol, description: Text("Coming soon"))
                .navigationTitle(AppTab.places.title)
        }
    }
}
