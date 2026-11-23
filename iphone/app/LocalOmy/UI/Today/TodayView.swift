import SwiftUI

/// PLACEHOLDER: replaced by the Today agent. Keep `struct TodayView: View` and read `AppModel` from the environment.
struct TodayView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    var body: some View {
        NavigationStack {
            ContentUnavailableView("Oggi", systemImage: AppTab.today.symbol, description: Text("Coming soon"))
                .navigationTitle(AppTab.today.title)
        }
    }
}
