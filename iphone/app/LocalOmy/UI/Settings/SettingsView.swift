import OmyKit
import SwiftUI

/// Pendant, Mac URL and token, feature switches (presented from Today).
struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    @State private var url = ""
    @State private var token = ""
    @State private var urlError: String?
    @State private var saved = false
    @State private var loaded = false

    private static let icons: [String: (String, Color)] = [
        "location": ("location.fill", .blue), "calls": ("phone.fill", .green), "battery": ("battery.100percent", .green),
        "mac_audio": ("speaker.wave.2.fill", .pink), "diarization": ("person.2.fill", .orange),
        "voice_prints": ("waveform", .purple), "auto_transcribe": ("text.bubble.fill", .indigo),
    ]

    var body: some View {
        NavigationStack {
            Form {
                pendantSection
                macSection
                featuresSection(device: "iphone", title: tr("Funzioni sull'iPhone", "Features on the iPhone"))
                featuresSection(device: "mac", title: tr("Funzioni sul Mac", "Features on the Mac"))
                Section {
                    LabeledContent(tr("Versione", "Version"), value: Self.version)
                }
            }
            .navigationTitle(tr("Impostazioni", "Settings"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button(tr("Fine", "Done")) { dismiss() } }
            }
            .alert(tr("Salvato", "Saved"), isPresented: $saved) { Button("OK", role: .cancel) {} } message: {
                Text(tr("URL e token salvati.", "URL and token saved."))
            }
        }
        .onAppear {
            if !loaded { url = model.macURL; loaded = true }
        }
        .onDisappear { model.stopScan() }
    }

    static var version: String {
        let d = Bundle.main.infoDictionary
        let v = d?["CFBundleShortVersionString"] as? String ?? "?"
        let b = d?["CFBundleVersion"] as? String ?? "?"
        return "\(v) (\(b))"
    }

    // MARK: pendant

    private var pendantSection: some View {
        Section(tr("Ciondolo", "Pendant")) {
            Label {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.pendantDeviceID == nil ? tr("Nessuno scelto", "None chosen") : "Omi")
                    if let id = model.pendantDeviceID { Text(id).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                }
            } icon: { Image(systemName: "wave.3.right").foregroundStyle(.blue) }
            Button {
                model.startScan()
            } label: {
                HStack {
                    Text(tr("Cerca ciondoli", "Find pendants"))
                    if model.isScanning { Spacer(); ProgressView() }
                }
            }
            ForEach(model.scanResults) { r in
                Button {
                    model.select(r)
                } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(r.name).foregroundStyle(.primary)
                            Text("\(r.id) · \(r.rssi) dBm").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer()
                        if r.id == model.pendantDeviceID { Image(systemName: "checkmark").foregroundStyle(.tint) }
                    }
                }
            }
            if model.pendantDeviceID != nil {
                Button(tr("Dimentica questo ciondolo", "Forget this pendant"), role: .destructive) { model.forgetPendant() }
            }
        }
    }

    // MARK: Mac

    private var macSection: some View {
        Section {
            LabeledContent("URL") {
                TextField("https://mac.tailnet.ts.net", text: $url)
                    .keyboardType(.URL).textContentType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    .multilineTextAlignment(.trailing)
            }
            LabeledContent("Token") {
                SecureField(model.hasToken ? "••••••••" : tr("Incolla il token", "Paste the token"), text: $token)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().textContentType(.password)
                    .multilineTextAlignment(.trailing)
            }
            Button(tr("Salva", "Save")) {
                urlError = model.saveMac(url: url, token: token)
                if urlError == nil {
                    url = model.macURL
                    token = ""
                    saved = true
                }
            }
            .buttonStyle(.borderedProminent)
            .frame(maxWidth: .infinity)
            .listRowBackground(Color.clear)
        } header: {
            Text("MAC")
        } footer: {
            if let urlError {
                Text(urlError).foregroundStyle(.red)
            } else {
                Text(model.hasToken
                     ? tr("Token impostato: lascia vuoto il campo per non cambiarlo.", "Token set: leave the field empty to keep it.")
                     : tr("Token non impostato.", "Token not set."))
            }
        }
    }

    // MARK: features

    private var syncFooter: String {
        if let e = model.featuresSyncError {
            return tr("Sincronizzazione con il Mac non riuscita: \(e)", "Sync with the Mac failed: \(e)")
        }
        guard let at = model.featuresLastSync else { return tr("Non ancora sincronizzate con il Mac.", "Not synced with the Mac yet.") }
        let t = TAFormat.stamp(at)
        return tr("Sincronizzate con il Mac alle \(t). Spegnere una funzione interrompe la raccolta, ma non cancella quello che c'è già.",
                  "Synced with the Mac at \(t). Turning a feature off stops collecting, but does not delete what is already there.")
    }

    private var locationProblem: String? {
        guard model.locationHasProblem else { return nil }
        let s = model.locationStatus
        switch s.auth {
        case .always:
            return s.precise ? nil : tr("Posizione approssimata: in Impostazioni attiva \"Posizione esatta\".",
                                        "Approximate location: turn on \"Precise Location\" in Settings.")
        case .whenInUse:
            return tr("iOS dà la posizione solo mentre l'app è aperta, quindi le registrazioni in tasca restano senza luogo. In Impostazioni scegli \"Sempre\" e attiva \"Posizione esatta\".",
                      "iOS gives the location only while the app is open, so recordings made with the phone in your pocket have no place. In Settings choose \"Always\" and turn on \"Precise Location\".")
        case .notDetermined:
            return tr("Permesso non ancora chiesto: riapri l'app.", "Permission not asked yet: reopen the app.")
        case .denied:
            return tr("Posizione negata: in Impostazioni scegli \"Sempre\" e attiva \"Posizione esatta\".",
                      "Location denied: in Settings choose \"Always\" and turn on \"Precise Location\".")
        }
    }

    private func featuresSection(device: String, title: String) -> some View {
        Section {
            ForEach(model.featureRows.filter { $0.info.device == device }, id: \.info.key) { row in
                let icon = Self.icons[row.info.key] ?? ("gearshape.fill", .gray)
                Toggle(isOn: Binding(get: { model.isOn(row.info.key) },
                                     set: { v in Task { await model.setFeature(row.info.key, on: v) } })) {
                    Label {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.info.name)
                            Text(row.info.description).font(.caption).foregroundStyle(.secondary)
                        }
                    } icon: { Image(systemName: icon.0).foregroundStyle(icon.1) }
                }
            }
            if device == "iphone", let p = locationProblem {
                HStack {
                    Label {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(tr("Posizione in background", "Location in background"))
                            Text(p).font(.caption).foregroundStyle(.secondary)
                        }
                    } icon: { Image(systemName: "location.slash.fill").foregroundStyle(.red) }
                    Button(tr("Apri", "Open")) { model.openSystemSettings() }.buttonStyle(.borderless)
                }
            }
        } header: {
            Text(title)
        } footer: {
            if device == "mac" { Text(syncFooter) }
        }
    }
}

#Preview {
    SettingsView().environment(AppModel.preview())
}
