import Foundation
import SwiftUI

/// A row of `omy --json features list`: a feature on or off, synced with the
/// iPhone through the receiver (omy_receiver/features.py).
struct Feature: Decodable, Identifiable {
    let key: String
    let device: String
    let label: String
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

/// The "Funzioni" section of the Setup tab.
struct FeaturesSection: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Funzioni").font(.headline)
            Text("Le stesse sull'iPhone: le modifiche arrivano all'altro dispositivo alla prossima sincronizzazione (apertura dell'app, o entro 5 minuti).")
                .font(.caption).foregroundStyle(.secondary)
            ForEach(model.features) { f in
                Toggle(isOn: Binding(get: { f.on }, set: { model.setFeature(f.key, $0) })) {
                    HStack {
                        Text(f.label)
                        Text(f.device == "iphone" ? "iPhone" : "Mac")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }
}
