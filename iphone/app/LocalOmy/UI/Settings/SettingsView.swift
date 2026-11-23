import SwiftUI

/// PLACEHOLDER: replaced by the Settings agent (presented from Today). Keep `struct SettingsView: View`.
struct SettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        NavigationStack {
            List {
                Text(model.pendantStatus.title)
            }
            .navigationTitle("Settings")
        }
    }
}
