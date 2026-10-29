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
            Text(s.receiver.running ? tr("Ricevitore attivo", "Receiver running") : tr("Ricevitore FERMO", "Receiver STOPPED"))
            Text(tr("Oggi: \(s.archive.todaySegments) segmenti, \(Int(s.archive.todayMinutes)) min",
                     "Today: \(s.archive.todaySegments) segments, \(Int(s.archive.todayMinutes)) min"))
            if let days = s.iphone.daysLeft {
                Text(tr("App iPhone: scade tra \(String(format: "%.1f", days)) giorni",
                         "iPhone app: expires in \(String(format: "%.1f", days)) days"))
            }
            if s.iphone.needsReopen { Text(tr("Riapri local-omy sull'iPhone", "Reopen local-omy on the iPhone")) }
        } else if let err = model.refreshError {
            Text(tr("Errore: \(err)", "Error: \(err)"))
        }
        if let running = model.task.running { Text(tr("In corso: \(running)…", "Running: \(running)…")) }
        if let p = model.progress { Text(tr("Trascrizione: \(p.text) (\(Int(p.fraction * 100))%)", "Transcription: \(p.text) (\(Int(p.fraction * 100))%)")) }
        Divider()
        Button(tr("Apri local-omy", "Open local-omy")) {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        }
        Button(tr("Aggiorna", "Refresh")) { Task { await model.refresh() } }
        Button(tr("Trascrivi ora", "Transcribe now")) { model.transcribeNow() }
            .disabled(model.transcribing)
        Divider()
        Button(tr("Esci", "Quit")) { NSApp.terminate(nil) }
    }
}
