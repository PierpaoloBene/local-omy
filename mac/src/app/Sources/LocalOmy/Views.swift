import SwiftUI

struct MainWindow: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let err = model.refreshError {
                Label(tr("Stato non aggiornato", "Status not up to date") + (model.refreshedAt.map { tr(" dalle ", " since ") + $0.formatted(Date.FormatStyle(date: .omitted, time: .shortened).locale(L10n.locale)) } ?? "")
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
                .help(tr("Aggiorna", "Refresh"))
        }
    }

    private var tabs: some View {
        TabView {
            StatusTab(model: model).tabItem { Label(tr("Stato", "Status"), systemImage: "gauge") }
            SetupTab(model: model).tabItem { Label(tr("Impostazioni", "Settings"), systemImage: "gearshape") }
            IPhoneTab(model: model).tabItem { Label("iPhone", systemImage: "iphone") }
            TranscriptsTab(model: model).tabItem { Label(tr("Conversazioni", "Conversations"), systemImage: "text.bubble") }
            PeopleTab(model: model).tabItem { Label(tr("Persone", "People"), systemImage: "person.2") }
            PlacesTab(model: model).tabItem { Label(tr("Luoghi", "Places"), systemImage: "mappin.and.ellipse") }
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
            Text(tr("Trascrizione in corso: ", "Transcribing: ") + progress.text)
        }
    }
}

private func tail(_ path: String, lines: Int) -> String {
    guard let data = FileManager.default.contents(atPath: path) else { return tr("(nessun log)", "(no log)") }
    return String(decoding: data.suffix(64 * 1024), as: UTF8.self)
        .split(separator: "\n", omittingEmptySubsequences: false).suffix(lines).joined(separator: "\n")
}

private let isoParser = ISO8601DateFormatter()

/// "3 nov 2026, 10:48" ("Nov 3, 2026, 10:48 AM") in the local time zone,
/// from an ISO 8601 timestamp.
func localDate(_ iso: String?) -> String {
    guard let iso, let d = isoParser.date(from: iso) else { return iso ?? "—" }
    return d.formatted(.dateTime.day().month(.abbreviated).year().hour().minute().locale(L10n.locale))
}

/// "giovedì 29 ottobre" ("Thursday, October 29") from YYYY-MM-DD.
func longDay(_ day: String) -> String {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd"
    guard let d = f.date(from: day) else { return day }
    return d.formatted(.dateTime.weekday(.wide).day().month(.wide).locale(L10n.locale))
}

private func ago(_ minutes: Double?) -> String {
    guard let m = minutes else { return "—" }
    if m < 1 { return tr("meno di un minuto fa", "less than a minute ago") }
    if m < 120 { return tr("\(Int(m)) min fa", "\(Int(m)) min ago") }
    return tr(String(format: "%.1f ore fa", m / 60), String(format: "%.1f hours ago", m / 60))
}

// MARK: - Stato

struct StatusTab: View {
    let model: AppModel
    @State private var logName = "receiver"

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            if let s = model.status {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                    Row(title: tr("Ricevitore", "Receiver"), value: s.receiver.running ? tr("attivo", "running") + " (pid \(s.receiver.pid ?? 0))" : tr("fermo", "stopped"),
                        level: s.receiver.running ? .ok : .error)
                    Row(title: "Tailscale", value: s.tailscale.url ?? tr("serve non attivo", "serve not running"),
                        level: s.tailscale.serving ? .ok : .warning)
                    ForEach(s.tailscale.iphones, id: \.name) { p in
                        Row(title: "  \(p.name)", value: p.online ? tr("online sul tailnet", "online on the tailnet") : "offline",
                            level: p.online ? .ok : .warning)
                    }
                    Row(title: tr("Ultimo segmento", "Last segment"), value: ago(s.archive.minutesSinceLast),
                        level: (s.archive.minutesSinceLast ?? 999) < 15 ? .ok : .unknown)
                    Row(title: tr("Ultimo upload", "Last upload"), value: uploadText(s.upload))
                    Row(title: tr("Oggi", "Today"), value: tr("\(s.archive.todaySegments) segmenti, \(s.archive.todaySessions) sessioni, \(Int(s.archive.todayMinutes)) min registrati",
                                                   "\(s.archive.todaySegments) segments, \(s.archive.todaySessions) sessions, \(Int(s.archive.todayMinutes)) min recorded"))
                    Row(title: tr("Trascrizione", "Transcription"), value: transcriptionText(s.transcripts),
                        level: model.progress != nil || s.transcripts.pendingSessions == 0 ? .ok : .unknown)
                    Row(title: tr("App iPhone", "iPhone app"), value: iphoneText(s.iphone), level: iphoneLevel(s.iphone))
                    Row(title: tr("Batteria ciondolo", "Pendant battery"), value: batteryText(s.battery))
                    Row(title: tr("Firmware ciondolo", "Pendant firmware"), value: s.firmware?.version.map { v in
                        tr("\(v), visto alle \(localDate(s.firmware?.at))", "\(v), seen at \(localDate(s.firmware?.at))")
                    } ?? tr("— (arriva con i prossimi segmenti)", "— (comes with the next segments)"))
                    Row(title: tr("Job orario", "Hourly job"), value: s.periodic.loaded ? tr("installato", "installed") : tr("non installato (vedi Impostazioni)", "not installed (see Settings)"),
                        level: s.periodic.loaded ? .ok : .warning)
                }
            } else if model.refreshError == nil {
                ProgressView()
            }

            Picker("Log", selection: $logName) {
                Text(tr("Ricevitore", "Receiver")).tag("receiver")
                Text(tr("Job orario", "Hourly job")).tag("periodic")
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
        case 0?: t += tr(", coda dell'iPhone vuota", ", iPhone queue empty")
        case let n?: t += tr(", ne restavano \(n) in coda sull'iPhone", ", \(n) were still queued on the iPhone")
        }
        return t
    }

    private func transcriptionText(_ t: Status.Transcripts) -> String {
        if let p = model.progress { return tr("in corso, ", "running, ") + p.text }
        if t.pendingSessions == 0 {
            return tr("al passo", "up to date") + (t.openSessions > 0 ? tr(" (1 sessione in registrazione)", " (1 session recording)") : "")
        }
        return tr("\(t.pendingSessions) sessioni da trascrivere, \(Int(t.pendingMinutes)) min",
                  "\(t.pendingSessions) sessions to transcribe, \(Int(t.pendingMinutes)) min")
    }

    private func batteryText(_ b: Status.Battery?) -> String {
        guard let b, let level = b.level else { return tr("— (arriva con i prossimi segmenti)", "— (comes with the next segments)") }
        var t = tr("\(level)% alle \(localDate(b.at))", "\(level)% at \(localDate(b.at))")
        if let full = b.fullChargeHours, let left = b.remainingHours, let rate = b.drainPerHour {
            t += String(format: tr(", %.1f%%/ora: una carica dura ~%.0f ore, ne restano ~%.0f",
                                   ", %.1f%%/hour: a charge lasts ~%.0f hours, ~%.0f left"), rate, full, left)
        } else {
            t += tr(", consumo ancora da misurare", ", drain not measured yet")
        }
        return t
    }

    private func iphoneText(_ i: Status.IPhone) -> String {
        guard let days = i.daysLeft else { return tr("mai installata da qui", "never installed from here") }
        var t = String(format: tr("firma valida ancora %.1f giorni", "signature valid for %.1f more days"), days)
        if i.needsReopen { t += tr(" — riaprila sull'iPhone", " — reopen it on the iPhone") }
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
                            Button(tr("Sistema", "Fix")) { model.perform(c.label, ["setup", "fix", c.id]) }
                                .disabled(model.task.running != nil)
                        } else if c.id == "receiver" || c.id == "periodic" {
                            Button(tr("Reinstalla", "Reinstall")) { model.perform(c.label, ["setup", "fix", c.id]) }
                                .disabled(model.task.running != nil)
                        }
                    }
                }
            }

            Divider()
            Text(tr("Da inserire nell'app iPhone", "To enter in the iPhone app")).font(.headline)
            HStack {
                Button(tr("Copia URL", "Copy URL")) { model.copy(model.status?.tailscale.url ?? "") }
                    .disabled(model.status?.tailscale.url == nil)
                Button(tr("Copia token", "Copy token")) { model.copyToken() }
                Text(tr("Il token non viene mai mostrato: va negli appunti.", "The token is never shown: it goes to the clipboard."))
                    .font(.caption).foregroundStyle(.secondary)
            }
            Divider()
            FeaturesSection(model: model)
            Divider()
            Toggle(tr("Apri local-omy al login", "Open local-omy at login"), isOn: Binding(
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
                    Row(title: "iPhone", value: i.device.map { "\($0.name ?? "?") (\($0.transport ?? "?"))" } ?? tr("non trovato", "not found"),
                        level: i.device?.paired == true ? .ok : .warning)
                    Row(title: "Team", value: i.teamId ?? "—")
                    Row(title: tr("Firma scade", "Signature expires"), value: i.expires.map { localDate($0) + String(format: tr(" (tra %.1f giorni)", " (in %.1f days)"), i.daysLeft ?? 0) } ?? "—",
                        level: (i.daysLeft ?? 0) < 1 ? .error : (i.daysLeft ?? 0) < 3 ? .warning : .ok)
                    Row(title: tr("Installata", "Installed"), value: localDate(i.installedAt))
                    if i.needsReopen {
                        Row(title: tr("Attenzione", "Warning"), value: tr("riapri local-omy sull'iPhone: dall'installazione non è arrivato nessun segmento",
                                                                  "reopen local-omy on the iPhone: no segment has arrived since the install"), level: .warning)
                    }
                }
            }
            Text(tr("Reinstallare chiude l'app sull'iPhone: va riaperta a mano. Telefono sbloccato e sulla stessa rete.",
                     "Reinstalling closes the app on the iPhone: reopen it by hand. Phone unlocked and on the same network."))
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button(tr("Compila e installa", "Build and install")) { confirm = (tr("Installazione", "Install"), ["iphone", "install"]) }
                Button(tr("Rinnova ora", "Renew now")) { confirm = (tr("Rinnovo", "Renewal"), ["iphone", "renew", "--force"]) }
                Button(tr("Reinstalla l'ultima build", "Reinstall the last build")) { confirm = (tr("Installazione", "Install"), ["iphone", "install", "--no-build"]) }
            }
            .disabled(model.task.running != nil)
            OperationLog(op: model.task)
            Spacer()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .confirmationDialog(tr("L'app sull'iPhone verrà chiusa e andrà riaperta.", "The app on the iPhone will close and must be reopened."),
                            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } })) {
            Button(tr("Procedi", "Continue")) {
                if let c = confirm { model.perform(c.name, c.args) }
            }
        }
    }
}

// MARK: - Conversazioni

// Mirror of `omy --json transcripts search`.
struct SearchResults: Decodable {
    struct Paragraph: Decodable, Identifiable {
        let id: String
        let day: String
        let start: String
        let speaker: String?
        let text: String
    }
    let results: [Paragraph]
    let more: Bool
}

struct TranscriptsTab: View {
    let model: AppModel
    @State private var day: String?
    @State private var query = ""
    @State private var results: SearchResults?
    @State private var searched = ""
    @State private var searching = false
    @State private var hit: String?

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Button(tr("Trascrivi ora", "Transcribe now")) { model.transcribeNow() }
                    .disabled(model.transcribing)
                Button(tr("Apri nel Finder", "Show in Finder")) {
                    if let p = model.status?.transcripts.path { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
                }
                Spacer()
                HStack(spacing: 4) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField(tr("Cerca", "Search"), text: $query)
                        .textFieldStyle(.plain)
                        .onSubmit(search)
                    if searching { ProgressView().controlSize(.small) }
                    if !query.isEmpty {
                        Button { clear() } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary) }
                            .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(RoundedRectangle(cornerRadius: 6).fill(.quaternary))
                .frame(width: 280)
            }
            if let p = model.progress { TranscriptionProgress(progress: p) }
            OperationLog(op: model.transcription)
            HSplitView {
                Group {
                    if let results {
                        List(results.results, selection: $hit) { r in
                            VStack(alignment: .leading, spacing: 2) {
                                Text("\(longDay(r.day)) \(String(r.start.dropFirst(11).prefix(5))) · \(r.speaker ?? "?")")
                                    .font(.caption).foregroundStyle(.secondary)
                                Text(marked(r.text, searched)).lineLimit(3)
                            }
                        }
                        .overlay {
                            if results.results.isEmpty { Text(tr("Nessun risultato per «\(searched)»", "No results for “\(searched)”")).foregroundStyle(.secondary) }
                        }
                    } else {
                        List(model.status?.transcripts.days ?? [], id: \.self, selection: $day) { d in
                            Text(longDay(d))
                        }
                    }
                }
                .frame(minWidth: 170, idealWidth: results == nil ? 190 : 300, maxWidth: results == nil ? 220 : 420)
                MarkdownDay(
                    path: day.flatMap { d in model.status.map { "\($0.transcripts.path)/\(d).md" } },
                    highlight: results == nil ? nil : searched,
                    focus: results?.results.first(where: { $0.id == hit })?.text
                )
            }
        }
        .onAppear { if day == nil { day = model.status?.transcripts.days.first } }
        .onChange(of: hit) {
            if let r = results?.results.first(where: { $0.id == hit }) { day = r.day }
        }
    }

    private func search() {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard q.count >= 2 else { return clear() }
        searching = true
        Task {
            do {
                results = try await Omy.decode(SearchResults.self, ["transcripts", "search", q])
                searched = q
                hit = nil
            } catch {
                AppLog.write("[ricerca] failed: \(error.localizedDescription)")
            }
            searching = false
        }
    }

    private func clear() {
        query = ""
        results = nil
        hit = nil
    }
}

/// [text] with every occurrence of [query] (any case) on yellow.
func marked(_ text: String, _ query: String?) -> AttributedString {
    var s = (try? AttributedString(markdown: text)) ?? AttributedString(text)
    guard let query, !query.isEmpty else { return s }
    var from = s.startIndex
    while let r = s[from...].range(of: query, options: .caseInsensitive) {
        s[r].backgroundColor = .yellow.opacity(0.6)
        from = r.upperBound
    }
    return s
}

/// Renders the day markdown written by omy: "# day", "## session", paragraphs
/// with **bold** times. With [focus] (a paragraph's text) it scrolls there.
struct MarkdownDay: View {
    let path: String?
    var highlight: String?
    var focus: String?

    var body: some View {
        if let path, let text = try? String(contentsOfFile: path, encoding: .utf8) {
            let lines = text.split(separator: "\n").map(String.init)
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { i, line in
                            Group {
                                if line.hasPrefix("## ") {
                                    Text(line.dropFirst(3)).font(.headline).padding(.top, 8)
                                } else if line.hasPrefix("# ") {
                                    Text(line.dropFirst(2)).font(.title2.bold())
                                } else {
                                    Text(marked(line, highlight))
                                }
                            }
                            .id(i)
                        }
                    }
                    .textSelection(.enabled)
                    .padding()
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: "\(path)|\(focus ?? "")", initial: true) {
                    guard let focus else { return }
                    let needle = String(focus.prefix(40))
                    if let i = lines.firstIndex(where: { $0.contains(needle) }) {
                        withAnimation { proxy.scrollTo(i, anchor: .center) }
                    }
                }
            }
        } else {
            Text(tr("Scegli un giorno", "Choose a day")).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}
