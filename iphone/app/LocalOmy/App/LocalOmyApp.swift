import OmyKit
import OSLog
import SwiftUI
import UIKit

@main
struct LocalOmyApp: App {
    @State private var model: AppModel
    @State private var reselect = TabReselect()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // Once: copy the Flutter app's settings and token (same bundle id, same container).
        let report = Migration.fromFlutter(defaults: .standard, keychain: SystemKeychain(),
                                         protectedDataAvailable: UIApplication.shared.isProtectedDataAvailable)
        Logger(subsystem: "local-omy", category: "migration").info("\(String(describing: report))")
        _model = State(initialValue: AppModel())
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .environment(reselect)
        }
        .onChange(of: scenePhase) { _, phase in model.scenePhaseChanged(phase) }
    }
}
