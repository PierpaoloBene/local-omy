import SwiftUI

// The "Second brain" tab and its compact twin in the menu bar popover. State comes
// from `omy jobs` and `omy brain status`, polled by AppModel every 15 s only while
// one of them is visible (brainWatch).

private let kindOrder = ["transcribe", "diarize", "render", "organize", "index"]

private func kindName(_ kind: String) -> String {
    switch kind {
    case "transcribe": tr("trascrizione", "transcription")
    case "diarize": tr("chi parla", "who speaks")
    case "render": tr("vault", "vault")
    case "organize": tr("organizzazione", "organize")
    case "index": tr("indice", "index")
    default: kind
    }
}

private func since(_ epoch: Double?) -> String {
    guard let epoch else { return "—" }
    let style = Date.RelativeFormatStyle(presentation: .numeric, unitsStyle: .wide, locale: L10n.locale)
    return Date(timeIntervalSince1970: epoch).formatted(style)
}

private func currentJobText(_ job: JobsInfo.Current?) -> String {
    guard let job else { return tr("nessuno", "none") }
    var t = kindName(job.kind)
    if let key = job.key { t += " · \(key)" }
    if let started = job.started { t += " · " + tr("da ", "since ") + since(started) }
    return t
}

private func queueText(_ jobs: JobsInfo.Queue) -> String {
    let parts = kindOrder.compactMap { kind -> String? in
        let n = jobs.byKind[kind]?["queued"] ?? 0
        return n > 0 ? "\(kindName(kind)) \(n)" : nil
    }
    return parts.isEmpty ? tr("vuota", "empty") : parts.joined(separator: ", ")
}

/// Floating controls over content (the map): the one place with explicit glass.
/// With Reduce Transparency the glass becomes an opaque background.
struct FloatingGlass<S: Shape>: ViewModifier {
    let shape: S
    @Environment(\.accessibilityReduceTransparency) private var reduce

    func body(content: Content) -> some View {
        if reduce {
            content.background(Color(nsColor: .windowBackgroundColor), in: shape)
                .overlay(shape.stroke(.separator))
        } else {
            content.glassEffect(.regular.interactive(), in: shape)
        }
    }
}

// MARK: - Tab

struct BrainTab: View {
    let model: AppModel
    @State private var organizing = false
    @State private var confirmIndex = false

    var body: some View {
        Form {
            if let err = model.brainError {
                Label(err, systemImage: "exclamationmark.octagon").foregroundStyle(.red).textSelection(.enabled)
            }
            if let j = model.jobs {
                let agent = model.status?.worker
                Section("Worker") {
                    Row(title: tr("Processo", "Process"),
                        value: agent.map { $0.running ? tr("attivo", "running") + " (pid \($0.pid ?? 0))" : tr("fermo", "stopped") } ?? "—",
                        level: agent.map { $0.running ? .ok : .warning })
                    Row(title: tr("Stato", "State"), value: j.worker.state ?? "—")
                    Row(title: tr("Lavoro in corso", "Current job"), value: currentJobText(j.worker.job))
                    if let e = j.worker.lastError { Row(title: tr("Ultimo errore", "Last error"), value: e, level: .error) }
                }
                Section(tr("Modelli caricati", "Loaded models")) {
                    ForEach(["chat", "embed", "organizer"], id: \.self) { role in
                        Row(title: role, value: j.isLoaded(role) ? tr("caricato", "loaded") : "—",
                            level: j.isLoaded(role) ? .ok : nil)
                    }
                }
                Section(tr("Coda", "Queue")) {
                    Row(title: tr("In attesa", "Waiting"), value: queueText(j.jobs))
                    if j.jobs.failed.isEmpty {
                        Row(title: tr("Falliti", "Failed"), value: "0", level: .ok)
                    }
                    ForEach(j.jobs.failed) { f in
                        VStack(alignment: .leading, spacing: 2) {
                            Label("\(kindName(f.kind)) · \(f.key ?? "")", systemImage: "xmark.octagon")
                                .foregroundStyle(.red)
                            if let e = f.error { Text(e).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                        }
                    }
                }
            } else if model.brainError == nil {
                ProgressView()
            }
            if let b = model.brain {
                Section(tr("Organizzazione notturna", "Night organizing")) {
                    Row(title: tr("Finestra", "Window"), value: b.window, level: b.inWindow ? .ok : nil)
                    Row(title: tr("Adesso", "Now"), value: b.inWindow ? tr("dentro la finestra", "inside the window") : tr("fuori dalla finestra", "outside the window"))
                    Row(title: tr("Alimentazione", "Power"), value: b.onAc ? tr("alla corrente", "on AC power") : tr("a batteria: non parte", "on battery: it won't start"),
                        level: b.onAc ? .ok : .warning)
                }
                Section(tr("Vault", "Vault")) {
                    Row(title: tr("Note", "Notes"), value: "\(b.notes)")
                    Row(title: tr("Ultima nota", "Last note"),
                        value: b.lastNote.map { [$0.data, $0.titolo].compactMap { $0 }.joined(separator: " · ") } ?? "—")
                    Row(title: tr("Indice", "Index"), value: tr("\(b.index.chunks) frammenti, versione \(b.index.version)",
                                                                 "\(b.index.chunks) chunks, version \(b.index.version)"))
                }
            }
            Section {
                HStack {
                    Button(tr("Organizza ora…", "Organize now…")) { organizing = true }
                        .buttonStyle(.glassProminent)
                    Button(tr("Apri vault", "Open vault")) { model.openVault() }
                        .buttonStyle(.glass)
                    Button(tr("Ricostruisci indice", "Rebuild index")) { confirmIndex = true }
                        .buttonStyle(.glass)
                }
                .disabled(model.task.running != nil)
                OperationLog(op: model.task)
            }
        }
        .formStyle(.grouped)
        .onAppear { model.brainWatch(true) }
        .onDisappear { model.brainWatch(false) }
        .sheet(isPresented: $organizing) { OrganizeSheet(model: model) }
        .confirmationDialog(tr("Ricostruire l'indice di ricerca?", "Rebuild the search index?"), isPresented: $confirmIndex) {
            Button(tr("Ricostruisci", "Rebuild")) {
                model.perform(tr("Indice", "Index"), ["index", "--all"])
                Task { await model.refreshBrain() }
            }
        } message: {
            Text(tr("Il worker lo rifà quando ha finito il resto.", "The worker does it when it has finished the rest."))
        }
    }
}

struct OrganizeSheet: View {
    let model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var date = Calendar.current.date(byAdding: .day, value: -7, to: .now) ?? .now

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(tr("Organizza le conversazioni", "Organize conversations")).font(.title3.bold())
            DatePicker(tr("Dal giorno", "Since"), selection: $date, in: ...Date.now, displayedComponents: .date)
                .datePickerStyle(.graphical)
                .environment(\.locale, L10n.locale)
            Label(tr("Carica il modello 27B e richiede tempo (anche ore), a qualsiasi ora e con qualsiasi alimentazione. Le note già aggiornate vengono saltate.",
                     "This loads the 27B model and takes time (possibly hours), at any hour and on any power. Notes already up to date are skipped."),
                  systemImage: "exclamationmark.triangle")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button(tr("Annulla", "Cancel"), role: .cancel) { dismiss() }
                Button(tr("Organizza", "Organize")) {
                    model.perform(tr("Organizzazione", "Organize"), ["organize", "--since", Self.ymd(date), "--now"])
                    dismiss()
                }
                .buttonStyle(.glassProminent)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 360)
    }

    static func ymd(_ d: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: d)
    }
}

// MARK: - Popover section

/// Compact second-brain lines for the menu bar popover.
struct BrainSummary: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label("Second brain", systemImage: "brain").font(.subheadline.bold())
            if let j = model.jobs {
                line(tr("Worker", "Worker"), j.worker.state ?? "—")
                line(tr("In corso", "Running"), currentJobText(j.worker.job))
                line(tr("Coda", "Queue"), queueText(j.jobs))
                if !j.jobs.failed.isEmpty {
                    Label(tr("\(j.jobs.failed.count) lavori falliti", "\(j.jobs.failed.count) failed jobs"),
                          systemImage: "xmark.octagon").foregroundStyle(.red).font(.caption)
                }
            }
            if let b = model.brain {
                line(tr("Note", "Notes"), "\(b.notes)" + (b.lastNote?.data.map { " · " + tr("ultima ", "last ") + $0 } ?? ""))
            }
            if model.jobs == nil && model.brain == nil {
                Text(model.brainError ?? "…").font(.caption).foregroundStyle(.secondary)
            }
        }
        .onAppear { model.brainWatch(true) }
        .onDisappear { model.brainWatch(false) }
    }

    private func line(_ title: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).foregroundStyle(.secondary)
            Spacer()
            Text(value).lineLimit(1).truncationMode(.middle)
        }
        .font(.caption)
    }
}
