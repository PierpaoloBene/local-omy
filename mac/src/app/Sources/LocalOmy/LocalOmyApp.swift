import SwiftUI

@main
struct LocalOmyApp: App {
    @State private var model = AppModel()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model)
        } label: {
            Image(systemName: model.level.symbol)
        }

        Window("local-omy", id: "main") {
            MainWindow(model: model)
                .frame(minWidth: 720, minHeight: 520)
        }
    }
}

extension Level {
    var symbol: String {
        switch self {
        case .ok: "waveform"
        case .warning: "exclamationmark.triangle"
        case .error: "xmark.octagon"
        case .unknown: "ellipsis"
        }
    }

    var color: Color {
        switch self {
        case .ok: .green
        case .warning: .orange
        case .error: .red
        case .unknown: .secondary
        }
    }
}

struct MenuContent: View {
    let model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if let s = model.status {
            Text(s.receiver.running ? "Ricevitore attivo" : "Ricevitore FERMO")
            Text("Oggi: \(s.archive.todaySegments) segmenti, \(Int(s.archive.todayMinutes)) min")
            if let days = s.iphone.daysLeft {
                Text("App iPhone: scade tra \(String(format: "%.1f", days)) giorni")
            }
            if s.iphone.needsReopen { Text("Riapri local-omy sull'iPhone") }
        } else if let err = model.refreshError {
            Text("Errore: \(err)")
        }
        if let busy = model.busy { Text("In corso: \(busy)…") }
        Divider()
        Button("Apri local-omy") {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Button("Aggiorna") { Task { await model.refresh() } }
        Button("Trascrivi ora") { model.perform("Trascrizione", ["transcribe", "--pending"]) }
            .disabled(model.busy != nil)
        Divider()
        Button("Esci") { NSApp.terminate(nil) }
    }
}
