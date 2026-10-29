import Foundation
import SwiftUI

/// A row of `omy --json features list`: a feature on or off, synced with the
/// iPhone through the receiver (omy_receiver/features.py).
struct Feature: Decodable, Identifiable {
    let key: String
    let device: String
    let name: String
    let description: String
    let on: Bool
    let at: String?
    var id: String { key }
}

private struct FeatureList: Decodable { let features: [Feature] }

enum Features {
    static func list() async throws -> [Feature] {
        try await Omy.decode(FeatureList.self, ["features", "list"]).features
    }

    static func set(_ key: String, _ on: Bool) async throws -> [Feature] {
        try await Omy.decode(FeatureList.self, ["features", "set", key, on ? "on" : "off"]).features
    }

    /// Read straight from <archive>/settings.json, for checks every few
    /// seconds without running omy. Never changed: on.
    static func isOn(_ key: String, archive: URL) -> Bool {
        guard let data = try? Data(contentsOf: archive.appendingPathComponent("settings.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let all = obj["features"] as? [String: Any],
              let f = all[key] as? [String: Any],
              let on = f["on"] as? Bool else { return true }
        return on
    }
}

/// The "Funzioni" section of the Impostazioni tab.
struct FeaturesSection: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(tr("Funzioni", "Features")).font(.headline)
            Text(tr("Le stesse dell'app iPhone: una modifica arriva all'altro dispositivo alla prossima sincronizzazione (apertura dell'app, o entro 5 minuti).",
                     "The same as in the iPhone app: a change reaches the other device at the next sync (when the app opens, or within 5 minutes)."))
                .font(.caption).foregroundStyle(.secondary)
            ForEach([("iphone", "iPhone"), ("mac", "Mac")], id: \.0) { device, title in
                Text(title).font(.subheadline.bold()).padding(.top, 6)
                ForEach(model.features.filter { $0.device == device }) { f in
                    Toggle(isOn: Binding(get: { f.on }, set: { model.setFeature(f.key, $0) })) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(f.name)
                            Text(f.description).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            Text(tr("Spegnere una funzione interrompe la raccolta, ma non cancella quello che c'è già.",
                     "Turning a feature off stops collecting, but does not delete what is already there."))
                .font(.caption).foregroundStyle(.secondary).padding(.top, 6)
        }
    }
}
