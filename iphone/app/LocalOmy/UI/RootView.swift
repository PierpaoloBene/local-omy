import OmyKit
import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect
    @State private var selection: AppTab = .today

    var body: some View {
        TabView(selection: selectionBinding) {
            Tab(AppTab.today.title, systemImage: AppTab.today.symbol, value: AppTab.today) { TodayView() }
            Tab(AppTab.conversations.title, systemImage: AppTab.conversations.symbol, value: AppTab.conversations) { ConversationsView() }
            Tab(AppTab.people.title, systemImage: AppTab.people.symbol, value: AppTab.people) { PeopleView() }
            Tab(AppTab.places.title, systemImage: AppTab.places.symbol, value: AppTab.places) { PlacesView() }
            Tab(AppTab.ask.title, systemImage: AppTab.ask.symbol, value: AppTab.ask) { AskView() }
        }
        .tabBarMinimizeBehavior(.onScrollDown)
        .tabViewBottomAccessory { PendantAccessory() }
    }

    private var selectionBinding: Binding<AppTab> {
        Binding(
            get: { selection },
            set: { new in
                if new == selection { reselect.bump(new) }
                selection = new
            })
    }
}

/// Compact pendant status in the bottom accessory, visible in every tab: status dot, battery, "in coda N".
struct PendantAccessory: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 10) {
            Circle().fill(model.pendantStatus.color).frame(width: 10, height: 10)
            Text(model.pendantStatus.title).font(.subheadline.weight(.medium))
            if let b = model.pendantBattery {
                Label("\(b)%", systemImage: "battery.75percent").labelStyle(.titleAndIcon).font(.subheadline)
            }
            Spacer(minLength: 0)
            Text(tr("in coda \(model.queueCount)", "\(model.queueCount) queued")).font(.subheadline).foregroundStyle(.secondary)
        }
        .padding(.horizontal)
        .accessibilityElement(children: .combine)
    }
}
