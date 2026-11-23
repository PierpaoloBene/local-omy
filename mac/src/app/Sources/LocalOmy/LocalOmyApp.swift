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

        .menuBarExtraStyle(.window)

        Window("local-omy", id: "main") {
            MainWindow(model: model)
                .frame(minWidth: 860, minHeight: 560)
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
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                if let s = model.status {
                    header(s.receiver.running ? tr("Ricevitore attivo", "Receiver running") : tr("Ricevitore FERMO", "Receiver STOPPED"),
                           ok: s.receiver.running)
                    header(s.worker.running ? tr("Worker attivo", "Worker running") : tr("Worker fermo", "Worker stopped"),
                           ok: s.worker.running)
                    Text(tr("Oggi: \(s.archive.todaySegments) segmenti, \(Int(s.archive.todayMinutes)) min",
                             "Today: \(s.archive.todaySegments) segments, \(Int(s.archive.todayMinutes)) min"))
                        .font(.callout).foregroundStyle(.secondary)
                    if let days = s.iphone.daysLeft {
                        Text(tr("App iPhone: scade tra \(String(format: "%.1f", days)) giorni",
                                 "iPhone app: expires in \(String(format: "%.1f", days)) days"))
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    if s.iphone.needsReopen {
                        Label(tr("Riapri local-omy sull'iPhone", "Reopen local-omy on the iPhone"), systemImage: "exclamationmark.triangle")
                            .font(.callout).foregroundStyle(.orange)
                    }
                } else if let err = model.refreshError {
                    Text(tr("Errore: \(err)", "Error: \(err)")).font(.callout).foregroundStyle(.red)
                }
                if let running = model.task.running { Text(tr("In corso: \(running)…", "Running: \(running)…")).font(.callout) }
                if let p = model.progress {
                    Text(tr("Trascrizione: \(p.text) (\(Int(p.fraction * 100))%)", "Transcription: \(p.text) (\(Int(p.fraction * 100))%)"))
                        .font(.callout)
                }
            }
            Divider()
            BrainSummary(model: model)
            Divider()
            HStack {
                Button(tr("Apri local-omy", "Open local-omy")) {
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                }
                .buttonStyle(.glassProminent)
                Button(tr("Trascrivi ora", "Transcribe now")) { model.transcribeNow() }
                    .buttonStyle(.glass)
                    .disabled(model.transcribing)
                Spacer()
                Button { Task { await model.refresh(); await model.refreshBrain() } } label: { Image(systemName: "arrow.clockwise") }
                    .help(tr("Aggiorna", "Refresh"))
                Button { NSApp.terminate(nil) } label: { Image(systemName: "power") }
                    .help(tr("Esci", "Quit"))
            }
            .buttonStyle(.borderless)
            .controlSize(.regular)
        }
        .padding(14)
        .frame(width: 340)
        .tint(.indigo)
    }

    private func header(_ text: String, ok: Bool) -> some View {
        Label {
            Text(text).font(.headline)
        } icon: {
            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                .foregroundStyle(ok ? .green : .red)
        }
    }
}
