import OmyKit
import SwiftUI

/// "Oggi": the pendant, recording and uploads on the phone; what the Mac has to transcribe; today in short.
struct TodayView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect
    @Environment(\.scenePhase) private var scenePhase

    @State private var mac: DayList?
    @State private var macError: String?
    @State private var macAt: Date?
    @State private var starting = false
    @State private var showSettings = false
    @State private var viewer: ViewerTarget?
    @State private var failure: String?

    private struct ViewerTarget: Identifiable, Hashable {
        let id = UUID()
        let conversations: [Conversation]
        static func == (a: Self, b: Self) -> Bool { a.id == b.id }
        func hash(into h: inout Hasher) { h.combine(id) }
    }

    private var running: Bool { mac?.pending.progress.running ?? false }

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                List {
                    pendantSection.id("top")
                    phoneSection
                    macSection
                    todaySection
                    errorSection
                }
                .onChange(of: reselect.count(for: .today)) {
                    withAnimation { proxy.scrollTo("top", anchor: .top) }
                }
            }
            .navigationTitle(AppTab.today.title)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button(tr("Impostazioni", "Settings"), systemImage: "gearshape") { showSettings = true }
                }
            }
            .refreshable {
                async let up: Void = model.uploadNow()
                await loadMac()
                await up
            }
            .navigationDestination(item: $viewer) { v in ConversationViewer(conversations: v.conversations, initial: 0) }
            .sheet(isPresented: $showSettings, onDismiss: { Task { await loadMac() } }) { SettingsView() }
            .alert(tr("Non riuscito", "Failed"), isPresented: Binding(get: { failure != nil }, set: { if !$0 { failure = nil } })) {
                Button("OK", role: .cancel) {}
            } message: { Text(failure ?? "") }
        }
        .task(id: model.apiGeneration) { await loadMac() }
        .onChange(of: scenePhase) { if scenePhase == .active { Task { await loadMac() } } }
        .task(id: running) {
            // Follow a transcription while it runs.
            while running, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(5))
                if Task.isCancelled { break }
                await loadMac()
            }
        }
    }

    // MARK: loading

    private func loadMac() async {
        guard let api = model.api else { mac = nil; macError = nil; return }
        do {
            mac = try await api.days()
            macError = nil
            macAt = Date()
        } catch is CancellationError {
        } catch {
            macError = (error as? MacError)?.message ?? error.localizedDescription
        }
    }

    private func transcribe() async {
        guard let api = model.api else { return }
        starting = true
        defer { starting = false }
        do {
            try await api.transcribeNow()
            try? await Task.sleep(for: .seconds(2))
            await loadMac()
        } catch {
            failure = (error as? MacError)?.message ?? error.localizedDescription
        }
    }

    private func openToday() async {
        guard let api = model.api else { return }
        do {
            let l = try await api.conversations(day: archiveDay, limit: 1000)
            if !l.conversations.isEmpty { viewer = ViewerTarget(conversations: l.conversations) }
        } catch {
            failure = (error as? MacError)?.message ?? error.localizedDescription
        }
    }

    private var archiveDay: String { mac?.today ?? TAFormat.localToday() }

    // MARK: pendant

    private var pendantDetail: String {
        let at = model.pendantLostAt.map { TAFormat.stamp($0) }
        switch model.pendantStatus {
        case .connected: return tr("Sta registrando", "Recording")
        case .idle: return tr("Collegato, ma non arriva audio", "Connected, but no audio is coming")
        case .disconnected:
            return tr("Si ricollega da solo", "Reconnects by itself") + (at.map { tr(" · perso alle \($0)", " · lost at \($0)") } ?? "")
        case .notFound:
            if model.pendantDeviceID == nil { return tr("Nessun ciondolo scelto: cercalo in Impostazioni", "No pendant chosen: find it in Settings") }
            if !model.bluetoothOn { return tr("Bluetooth spento", "Bluetooth is off") }
            return tr("Lontano o scarico", "Out of range or flat") + (at.map { tr(" · visto alle \($0)", " · seen at \($0)") } ?? "")
        }
    }

    private var pendantIcon: String {
        switch model.pendantStatus {
        case .connected: "waveform"
        case .idle: "pause.fill"
        case .disconnected: "antenna.radiowaves.left.and.right"
        case .notFound: "wave.3.right"
        }
    }

    private func batteryIcon(_ l: Int) -> String {
        l >= 75 ? "battery.100percent" : l >= 40 ? "battery.75percent" : l >= 15 ? "battery.25percent" : "battery.0percent"
    }

    private var pendantSection: some View {
        let live = model.pendantStatus == .connected || model.pendantStatus == .idle
        return Section {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 14) {
                    Image(systemName: pendantIcon)
                        .font(.title2).foregroundStyle(.white)
                        .frame(width: 54, height: 54)
                        .background(model.pendantStatus.color.gradient, in: .circle)
                    VStack(alignment: .leading) {
                        Text(tr("Ciondolo", "Pendant")).font(.footnote).foregroundStyle(.secondary)
                        Text(model.pendantStatus.title).font(.title2.weight(.bold))
                    }
                }
                Text(pendantDetail).font(.subheadline)
                if model.pendantBattery != nil || model.pendantFirmware != nil {
                    HStack(spacing: 8) {
                        if let b = model.pendantBattery {
                            Label("\(b)%" + (live ? "" : tr(" (ultimo)", " (last)")), systemImage: batteryIcon(b))
                                .foregroundStyle(b < 20 ? .red : .green)
                                .pill()
                        }
                        if let f = model.pendantFirmware { Text("Firmware \(f)").pill() }
                    }
                    .font(.subheadline)
                }
            }
            .padding(.vertical, 6)
            .accessibilityElement(children: .combine)
        }
    }

    // MARK: iPhone

    private var phoneSection: some View {
        Section("iPhone") {
            LabeledContent {
                Text(model.isRecording ? tr("Attiva", "On") : tr("Ferma", "Off"))
            } label: {
                Label(tr("Registrazione", "Recording"), systemImage: model.isRecording ? "mic.fill" : "mic.slash")
                    .tint(model.isRecording ? .red : .gray)
            }
            LabeledContent {
                HStack {
                    if model.isUploading { ProgressView() }
                    Text("\(model.queueCount)")
                }
            } label: { Label(tr("In coda", "Queued"), systemImage: "tray.and.arrow.up.fill") }
            LabeledContent {
                Text(model.lastUpload.map { TAFormat.stamp($0) } ?? "—")
            } label: { Label(tr("Ultimo invio", "Last upload"), systemImage: "checkmark") }
            Button(tr("Invia ora", "Upload now")) { Task { await model.uploadNow() } }
        }
    }

    // MARK: Mac

    @ViewBuilder private var macBody: some View {
        if model.api == nil {
            VStack(alignment: .leading, spacing: 2) {
                Text(tr("Non configurato", "Not set up"))
                Text(tr("Imposta URL e token del Mac in Impostazioni.", "Set the Mac's URL and token in Settings."))
                    .font(.subheadline).foregroundStyle(.secondary)
            }
        } else if let macError, mac == nil {
            Label {
                VStack(alignment: .leading, spacing: 2) {
                    Text(tr("Non raggiungibile", "Not reachable"))
                    Text(macError).font(.subheadline).foregroundStyle(.secondary)
                }
            } icon: { Image(systemName: "wifi.slash").foregroundStyle(.red) }
        } else if let p = mac?.pending {
            let run = p.progress.running
            Label {
                VStack(alignment: .leading, spacing: 4) {
                    Text(run ? tr("Trascrizione in corso", "Transcribing")
                         : p.sessions == 0 ? tr("Tutto trascritto", "All transcribed")
                         : tr("\(counted(p.sessions, "sessione", "sessioni", "", "")) da trascrivere",
                              "\(counted(p.sessions, "", "", "session", "sessions")) to transcribe"))
                    let sub = macSubtitle(p)
                    if !sub.isEmpty { Text(sub).font(.subheadline).foregroundStyle(.secondary) }
                    if run {
                        if let f = p.progress.fraction { ProgressView(value: f) } else { ProgressView() }
                    }
                }
            } icon: {
                Image(systemName: run ? "hourglass" : p.sessions == 0 ? "checkmark.seal.fill" : "waveform")
                    .foregroundStyle(run || p.sessions > 0 ? .purple : .green)
            }
        } else {
            HStack { Spacer(); ProgressView(); Spacer() }
        }
    }

    private func macSubtitle(_ p: Pending) -> String {
        var parts: [String] = []
        if p.progress.running {
            parts.append(tr("\(p.progress.sessionsDone) di \(p.progress.sessionsTotal) sessioni",
                            "\(p.progress.sessionsDone) of \(p.progress.sessionsTotal) sessions"))
        } else if p.sessions > 0 {
            parts.append(TAFormat.duration(p.minutes))
        }
        if p.openSessions > 0 {
            let n = p.openSessions
            parts.append(tr("\(n == 1 ? "una sessione ancora aperta" : "\(n) sessioni ancora aperte"), si trascrive quando finisce",
                            "\(n == 1 ? "a session still open" : "\(n) sessions still open"), transcribed when it ends"))
        }
        return parts.joined(separator: " · ")
    }

    private var macSection: some View {
        Section {
            macBody
            if let p = mac?.pending, !p.progress.running, p.sessions > 0 {
                Button {
                    Task { await transcribe() }
                } label: {
                    HStack {
                        Text(tr("Trascrivi ora", "Transcribe now"))
                        if starting { Spacer(); ProgressView() }
                    }
                }
                .disabled(starting)
            }
        } header: {
            Text("Mac")
        } footer: {
            if let macError, mac != nil {
                let since = macAt.map { TAFormat.stamp($0) }
                Text(tr("Non aggiornato\(since.map { " dalle \($0)" } ?? ""): \(macError)",
                        "Not up to date\(since.map { " since \($0)" } ?? ""): \(macError)"))
            }
        }
    }

    // MARK: today in short

    @ViewBuilder private var todaySection: some View {
        if let s = mac?.days.first(where: { $0.day == archiveDay }) {
            Section(tr("Oggi in breve", "Today in short")) {
                Button { Task { await openToday() } } label: { DaySummaryRow(summary: s, today: archiveDay) }
                    .tint(.primary)
            }
        }
    }

    // MARK: last error

    @ViewBuilder private var errorSection: some View {
        if let e = model.lastError {
            Section(tr("Ultimo errore", "Last error")) {
                Label {
                    Text(e).font(.subheadline)
                } icon: { Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red) }
            }
        }
    }
}

private extension View {
    func pill() -> some View {
        padding(.horizontal, 10).padding(.vertical, 5).background(.fill.tertiary, in: .capsule)
    }
}

/// A day in short: when, how long, who and where (DaySummaryCard).
private struct DaySummaryRow: View {
    let summary: DaySummary
    let today: String

    var body: some View {
        let s = summary
        let speakers = Array(s.speakers.prefix(4))
        let hidden = s.speakers.count - speakers.count
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(TAFormat.longDay(s.day, today: today)).font(.headline)
                    Text([s.from.map { "\($0)–\(s.to ?? "")" },
                          TAFormat.duration(Double(s.minutes)),
                          counted(s.sessions, "sessione", "sessioni", "session", "sessions")]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.forward").font(.footnote.weight(.semibold)).foregroundStyle(.tertiary)
            }
            if !speakers.isEmpty {
                HStack(spacing: 6) {
                    HStack(spacing: -8) {
                        ForEach(Array(speakers.enumerated()), id: \.offset) { _, sp in
                            Avatar(name: sp.name, me: sp.isMe).overlay(Circle().stroke(Color(.secondarySystemGroupedBackground), lineWidth: 1.5))
                        }
                    }
                    Text((speakers.map(\.name) + (hidden > 0 ? [tr("altri \(hidden)", "\(hidden) more")] : [])).joined(separator: ", "))
                        .font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            if !s.places.isEmpty || s.unnamedPlaces {
                Label((s.places + (s.unnamedPlaces ? [s.places.isEmpty ? tr("luoghi senza nome", "unnamed places") : tr("altri senza nome", "others without a name")] : []))
                    .joined(separator: " · "), systemImage: "location.fill")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
        .contentShape(Rectangle())
    }
}

/// Initials on a color: stable per name, the accent for you.
private struct Avatar: View {
    let name: String
    let me: Bool
    private static let palette: [Color] = [.orange, .green, .teal, .blue, .purple, .pink, .red, .brown, .mint, .cyan]

    var body: some View {
        let color: Color = me ? .accentColor : Self.palette[name.unicodeScalars.reduce(0) { ($0 &* 31 &+ Int($1.value)) & 0xffff } % Self.palette.count]
        Text(String(name.prefix(1)).uppercased())
            .font(.caption.weight(.semibold)).foregroundStyle(.white)
            .frame(width: 29, height: 29).background(color.gradient, in: .circle)
    }
}

#Preview {
    TodayView().environment(AppModel.preview()).environment(TabReselect())
}
