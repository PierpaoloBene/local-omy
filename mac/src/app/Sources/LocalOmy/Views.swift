import SwiftUI

struct MainWindow: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let err = model.refreshError {
                Label("Stato non aggiornato" + (model.refreshedAt.map { " dalle \($0.formatted(date: .omitted, time: .shortened))" } ?? "")
                      + ": \(err)", systemImage: "exclamationmark.octagon")
                    .foregroundStyle(.red)
                    .lineLimit(3)
                    .textSelection(.enabled)
            }
            tabs
        }
        .padding()
        .toolbar {
            Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                .help("Aggiorna")
        }
    }

    private var tabs: some View {
        TabView {
            StatusTab(model: model).tabItem { Label("Stato", systemImage: "gauge") }
            SetupTab(model: model).tabItem { Label("Setup", systemImage: "checklist") }
            IPhoneTab(model: model).tabItem { Label("iPhone", systemImage: "iphone") }
            TranscriptsTab(model: model).tabItem { Label("Trascrizioni", systemImage: "text.quote") }
            PeopleTab(model: model).tabItem { Label("Persone", systemImage: "person.2") }
        }
    }
}

// MARK: - Shared pieces

struct Row: View {
    let title: String
    let value: String
    /// nil for plain information: no dot.
    var level: Level?

    var body: some View {
        GridRow {
            Circle().fill(level?.color ?? .clear).frame(width: 9, height: 9)
            Text(title).foregroundStyle(.secondary)
            Text(value).textSelection(.enabled)
        }
    }
}

/// Progress log of the running (or last) operation of a lane, plus its error.
struct OperationLog: View {
    let op: Job

    var body: some View {
        if op.running != nil || !op.log.isEmpty || op.lastError != nil {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    if let running = op.running {
                        ProgressView().controlSize(.small)
                        Text("\(running)…")
                    }
                    if let err = op.lastError {
                        Label(err, systemImage: "xmark.octagon").foregroundStyle(.red)
                    }
                }
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(Array(op.log.enumerated()), id: \.offset) { i, line in
                                Text(line).font(.system(.caption, design: .monospaced)).id(i)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                    }
                    .frame(minHeight: 120)
                    .background(.background.secondary)
                    .onChange(of: op.log.count) { _, n in proxy.scrollTo(n - 1, anchor: .bottom) }
                }
            }
        }
    }
}

/// Bar of the running transcription, whoever started it.
struct TranscriptionProgress: View {
    let progress: TranscribeProgress

    var body: some View {
        ProgressView(value: progress.fraction) {
            Text("Trascrizione in corso: \(progress.text)")
        }
    }
}

private func tail(_ path: String, lines: Int) -> String {
    guard let data = FileManager.default.contents(atPath: path) else { return "(nessun log)" }
    return String(decoding: data.suffix(64 * 1024), as: UTF8.self)
        .split(separator: "\n", omittingEmptySubsequences: false).suffix(lines).joined(separator: "\n")
}

private let isoParser = ISO8601DateFormatter()

/// "3 nov 2026, 10:48" in the local time zone, from an ISO 8601 timestamp.
func localDate(_ iso: String?) -> String {
    guard let iso, let d = isoParser.date(from: iso) else { return iso ?? "—" }
    return d.formatted(.dateTime.day().month(.abbreviated).year().hour().minute())
}

/// "giovedì 29 ottobre" from YYYY-MM-DD.
func longDay(_ day: String) -> String {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd"
    guard let d = f.date(from: day) else { return day }
    return d.formatted(.dateTime.weekday(.wide).day().month(.wide))
}

private func ago(_ minutes: Double?) -> String {
    guard let m = minutes else { return "—" }
    if m < 1 { return "meno di un minuto fa" }
    if m < 120 { return "\(Int(m)) min fa" }
    return String(format: "%.1f ore fa", m / 60)
}

// MARK: - Stato

struct StatusTab: View {
    let model: AppModel
    @State private var logName = "receiver"

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let s = model.status {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                    Row(title: "Ricevitore", value: s.receiver.running ? "attivo (pid \(s.receiver.pid ?? 0))" : "fermo",
                        level: s.receiver.running ? .ok : .error)
                    Row(title: "Tailscale", value: s.tailscale.url ?? "serve non attivo",
                        level: s.tailscale.serving ? .ok : .warning)
                    ForEach(s.tailscale.iphones, id: \.name) { p in
                        Row(title: "  \(p.name)", value: p.online ? "online sul tailnet" : "offline",
                            level: p.online ? .ok : .warning)
                    }
                    Row(title: "Ultimo segmento", value: ago(s.archive.minutesSinceLast),
                        level: (s.archive.minutesSinceLast ?? 999) < 15 ? .ok : .unknown)
                    Row(title: "Ultimo upload", value: uploadText(s.upload))
                    Row(title: "Oggi", value: "\(s.archive.todaySegments) segmenti, \(s.archive.todaySessions) sessioni, \(Int(s.archive.todayMinutes)) min registrati")
                    Row(title: "Trascrizione", value: transcriptionText(s.transcripts),
                        level: model.progress != nil || s.transcripts.pendingSessions == 0 ? .ok : .unknown)
                    Row(title: "App iPhone", value: iphoneText(s.iphone), level: iphoneLevel(s.iphone))
                    Row(title: "Batteria ciondolo", value: batteryText(s.battery))
                    Row(title: "Job orario", value: s.periodic.loaded ? "installato" : "non installato (vedi Setup)",
                        level: s.periodic.loaded ? .ok : .warning)
                }
            } else if model.refreshError == nil {
                ProgressView()
            }

            Picker("Log", selection: $logName) {
                Text("Ricevitore").tag("receiver")
                Text("Job orario").tag("periodic")
            }
            .pickerStyle(.segmented)
            .frame(width: 260)
            ScrollView {
                Text(model.status.flatMap { tail($0.logs[logName] ?? "", lines: 40) } ?? "")
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .background(.background.secondary)
        }
    }

    private func uploadText(_ u: Status.Upload) -> String {
        guard u.lastUploadAt != nil else { return "—" }
        var t = ago(u.minutesSince)
        switch u.queueRemaining {
        case nil: break
        case 0?: t += ", coda dell'iPhone vuota"
        case let n?: t += ", ne restavano \(n) in coda sull'iPhone"
        }
        return t
    }

    private func transcriptionText(_ t: Status.Transcripts) -> String {
        if let p = model.progress { return "in corso, \(p.text)" }
        if t.pendingSessions == 0 { return "al passo" + (t.openSessions > 0 ? " (1 sessione in registrazione)" : "") }
        return "\(t.pendingSessions) sessioni da trascrivere, \(Int(t.pendingMinutes)) min"
    }

    private func batteryText(_ b: Status.Battery?) -> String {
        guard let b, let level = b.level else { return "— (arriva con i prossimi segmenti)" }
        var t = "\(level)% alle \(localDate(b.at))"
        if let full = b.fullChargeHours, let left = b.remainingHours, let rate = b.drainPerHour {
            t += String(format: ", %.1f%%/ora: una carica dura ~%.0f ore, ne restano ~%.0f", rate, full, left)
        } else {
            t += ", consumo ancora da misurare"
        }
        return t
    }

    private func iphoneText(_ i: Status.IPhone) -> String {
        guard let days = i.daysLeft else { return "mai installata da qui" }
        var t = String(format: "firma valida ancora %.1f giorni", days)
        if i.needsReopen { t += " — riaprila sull'iPhone" }
        return t
    }

    private func iphoneLevel(_ i: Status.IPhone) -> Level {
        guard let days = i.daysLeft else { return .warning }
        return days < 1 ? .error : (days < 3 || i.needsReopen) ? .warning : .ok
    }
}

// MARK: - Setup

struct SetupTab: View {
    let model: AppModel

    var body: some View {
        ScrollView {
        VStack(alignment: .leading, spacing: 16) {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 10) {
                ForEach(model.checks) { c in
                    GridRow {
                        Image(systemName: c.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(c.ok ? .green : .red)
                        VStack(alignment: .leading) {
                            Text(c.label)
                            Text(c.detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        }
                        if !c.ok {
                            Button("Sistema") { model.perform(c.label, ["setup", "fix", c.id]) }
                                .disabled(model.task.running != nil)
                        } else if c.id == "receiver" || c.id == "periodic" {
                            Button("Reinstalla") { model.perform(c.label, ["setup", "fix", c.id]) }
                                .disabled(model.task.running != nil)
                        }
                    }
                }
            }

            Divider()
            Text("Da inserire nell'app iPhone").font(.headline)
            HStack {
                Button("Copia URL") { model.copy(model.status?.tailscale.url ?? "") }
                    .disabled(model.status?.tailscale.url == nil)
                Button("Copia token") { model.copyToken() }
                Text("Il token non viene mai mostrato: va negli appunti.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Divider()
            FeaturesSection(model: model)
            Divider()
            Toggle("Apri local-omy al login", isOn: Binding(
                get: { model.loginItem }, set: { model.setLoginItem($0) }))
            if let err = model.lastError {
                Label(err, systemImage: "xmark.octagon").foregroundStyle(.red)
            }

            OperationLog(op: model.task)
            Spacer()
        }
        .padding(.trailing, 12)
        }
    }
}

// MARK: - iPhone

struct IPhoneTab: View {
    let model: AppModel
    @State private var confirm: (name: String, args: [String])?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let i = model.status?.iphone {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                    Row(title: "iPhone", value: i.device.map { "\($0.name ?? "?") (\($0.transport ?? "?"))" } ?? "non trovato",
                        level: i.device?.paired == true ? .ok : .warning)
                    Row(title: "Team", value: i.teamId ?? "—")
                    Row(title: "Firma scade", value: i.expires.map { "\(localDate($0)) (tra \(String(format: "%.1f", i.daysLeft ?? 0)) giorni)" } ?? "—",
                        level: (i.daysLeft ?? 0) < 1 ? .error : (i.daysLeft ?? 0) < 3 ? .warning : .ok)
                    Row(title: "Installata", value: localDate(i.installedAt))
                    if i.needsReopen {
                        Row(title: "Attenzione", value: "riapri local-omy sull'iPhone: dall'installazione non è arrivato nessun segmento", level: .warning)
                    }
                }
            }
            Text("Reinstallare chiude l'app sull'iPhone: va riaperta a mano. Telefono sbloccato e sulla stessa rete.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Compila e installa") { confirm = ("Installazione", ["iphone", "install"]) }
                Button("Rinnova ora") { confirm = ("Rinnovo", ["iphone", "renew", "--force"]) }
                Button("Reinstalla l'ultima build") { confirm = ("Installazione", ["iphone", "install", "--no-build"]) }
            }
            .disabled(model.task.running != nil)
            OperationLog(op: model.task)
            Spacer()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .confirmationDialog("L'app sull'iPhone verrà chiusa e andrà riaperta.",
                            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } })) {
            Button("Procedi") {
                if let c = confirm { model.perform(c.name, c.args) }
            }
        }
    }
}

// MARK: - Trascrizioni

struct TranscriptsTab: View {
    let model: AppModel
    @State private var day: String?

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Button("Trascrivi ora") { model.transcribeNow() }
                    .disabled(model.transcribing)
                Button("Apri nel Finder") {
                    if let p = model.status?.transcripts.path { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
                }
            }
            if let p = model.progress { TranscriptionProgress(progress: p) }
            OperationLog(op: model.transcription)
            HSplitView {
                List(model.status?.transcripts.days ?? [], id: \.self, selection: $day) { d in
                    Text(longDay(d))
                }
                .frame(minWidth: 170, maxWidth: 220)
                ScrollView {
                    MarkdownDay(path: day.flatMap { d in model.status.map { "\($0.transcripts.path)/\(d).md" } })
                        .padding()
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .onAppear { if day == nil { day = model.status?.transcripts.days.first } }
    }
}

/// Renders the day markdown written by omy: "# day", "## session", paragraphs
/// with **bold** times.
struct MarkdownDay: View {
    let path: String?

    var body: some View {
        if let path, let text = try? String(contentsOfFile: path, encoding: .utf8) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(text.split(separator: "\n").enumerated()), id: \.offset) { _, line in
                    if line.hasPrefix("## ") {
                        Text(line.dropFirst(3)).font(.headline).padding(.top, 8)
                    } else if line.hasPrefix("# ") {
                        Text(line.dropFirst(2)).font(.title2.bold())
                    } else {
                        Text((try? AttributedString(markdown: String(line))) ?? AttributedString(String(line)))
                    }
                }
            }
            .textSelection(.enabled)
        } else {
            Text("Scegli un giorno").foregroundStyle(.secondary)
        }
    }
}
