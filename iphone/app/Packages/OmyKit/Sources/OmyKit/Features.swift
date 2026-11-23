import Foundation

public struct FeatureValue: Codable, Sendable, Equatable {
    public var on: Bool
    /// ISO 8601 UTC of the last change.
    public var at: String
    public init(on: Bool, at: String) { self.on = on; self.at = at }
}

public struct FeatureInfo: Sendable, Identifiable {
    public let key: String
    /// "iphone" or "mac".
    public let device: String
    public let name: String
    public let description: String
    public var id: String { key }
}

/// Features turned on and off here or in the Mac app, kept in sync through the Mac (PUT /v1/settings: our values in,
/// the merged ones out). Each value carries when it was changed; the most recent wins. Never changed anywhere = on.
public struct Features: Sendable, Equatable {
    /// Same keys, names and descriptions as omy_receiver/features.py on the Mac.
    public static var info: [FeatureInfo] {
        [
            FeatureInfo(key: "location", device: "iphone", name: tr("Posizione", "Location"), description: tr(
                "Salva dove è stata fatta ogni registrazione, con le visite e i cambi di posizione di iOS, senza tenere acceso il GPS.",
                "Saves where each recording was made, from iOS visits and location changes, without keeping the GPS on.")),
            FeatureInfo(key: "calls", device: "iphone", name: tr("Chiamate", "Calls"), description: tr(
                "Segna quando sei al telefono, su FaceTime o WhatsApp: solo inizio e fine, non chi chiama.",
                "Marks when you are on the phone, FaceTime or WhatsApp: only start and end, not who is calling.")),
            FeatureInfo(key: "battery", device: "iphone", name: tr("Batteria del ciondolo", "Pendant battery"), description: tr(
                "Salva il livello della batteria a ogni segmento, per stimare quanto dura una carica.",
                "Saves the battery level with each segment, to estimate how long a charge lasts.")),
            FeatureInfo(key: "mac_audio", device: "mac", name: tr("Chiamate e media sul Mac", "Calls and media on the Mac"), description: tr(
                "Segna quali app usano microfono e altoparlanti, per distinguere una chiamata da un video o dalla musica.",
                "Marks which apps use the microphone and speakers, to tell a call from a video or music.")),
            FeatureInfo(key: "diarization", device: "mac", name: tr("Chi parla", "Who speaks"), description: tr(
                "Separa le voci di ogni sessione e riconosce la tua.",
                "Separates the voices of each session and recognizes yours.")),
            FeatureInfo(key: "voice_prints", device: "mac", name: tr("Impronte vocali", "Voice prints"), description: tr(
                "Salva l'impronta delle altre voci, per dar loro un nome e riconoscerle nelle sessioni successive.",
                "Saves a print of the other voices, to name them and recognize them in later sessions.")),
            FeatureInfo(key: "auto_transcribe", device: "mac", name: tr("Trascrizione automatica", "Automatic transcription"), description: tr(
                "Trascrive ogni ora le sessioni concluse.", "Transcribes the finished sessions every hour.")),
        ]
    }

    public static let keys: [String] = ["location", "calls", "battery", "mac_audio", "diarization", "voice_prints", "auto_transcribe"]
    public static let phoneKeys: [String] = ["location", "calls", "battery"]

    public private(set) var values: [String: FeatureValue]

    public init(values: [String: FeatureValue] = [:]) {
        self.values = values.filter { Self.keys.contains($0.key) }
    }

    /// From the stored JSON string `{key: {on, at}}`; unknown keys and malformed entries are dropped.
    public init(json: String?) {
        var v: [String: FeatureValue] = [:]
        if let json, let o = try? JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any] {
            v = Self.clean(o)
        }
        self.values = v
    }

    /// A JSON boolean, not a number (Dart: `is bool`).
    static func bool(_ v: Any?) -> Bool? {
        guard let n = v as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }
        return n.boolValue
    }

    static func clean(_ o: [String: Any]) -> [String: FeatureValue] {
        var v: [String: FeatureValue] = [:]
        for (k, x) in o where keys.contains(k) {
            if let m = x as? [String: Any], let on = bool(m["on"]), let at = m["at"] as? String { v[k] = FeatureValue(on: on, at: at) }
        }
        return v
    }

    /// Never changed anywhere: on.
    public func on(_ key: String) -> Bool { values[key]?.on ?? true }

    public mutating func set(_ key: String, _ on: Bool, at: Date = Date()) {
        guard Self.keys.contains(key) else { return }
        values[key] = FeatureValue(on: on, at: ISO8601UTC.string(at))
    }

    /// Adopts the values of `remote` that are newer than ours (last writer wins on `at`). True if something changed.
    @discardableResult
    public mutating func merge(_ remote: [String: FeatureValue]) -> Bool {
        var changed = false
        for (k, r) in remote where Self.keys.contains(k) {
            guard let rt = ISO8601UTC.parse(r.at) else { continue }
            if let mine = values[k], let mt = ISO8601UTC.parse(mine.at), !(rt > mt) { continue }
            values[k] = r
            changed = true
        }
        return changed
    }

    public func jsonString() -> String {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys]
        let d = (try? e.encode(values)) ?? Data("{}".utf8)
        return String(decoding: d, as: UTF8.self)
    }

    /// Body of PUT /v1/settings.
    public func settingsBody() throws -> Data { try JSONSerialization.data(withJSONObject: ["features": try values.mapValues { try JSONSerialization.jsonObject(with: JSONEncoder().encode($0)) }]) }

    /// Documents/features.json for the native side: `{"location": true, "calls": true, "battery": true}`.
    public func phoneJSON() -> Data {
        let o = Dictionary(uniqueKeysWithValues: Self.phoneKeys.map { ($0, on($0)) })
        return (try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys])) ?? Data("{}".utf8)
    }
}

/// Features persisted in `SettingsStore`, mirrored to `Documents/features.json`, synced with the Mac every 5 min, at
/// resume and at each change (the app drives the timer and calls `sync`).
public actor FeaturesStore {
    public private(set) var features: Features
    public private(set) var lastSyncAt: Date?
    public private(set) var lastSyncError: String?
    private let settings: SettingsStore
    private let documents: URL
    private var syncing = false

    public init(settings: SettingsStore, documents: URL) {
        self.settings = settings
        self.documents = documents
        self.features = Features(json: settings.featuresJSON)
        Self.writePhoneFile(features, documents)
    }

    public func set(_ key: String, _ on: Bool, api: MacAPI?, now: Date = Date()) async {
        features.set(key, on, at: now)
        save()
        await sync(api: api)
    }

    /// Sends ours, adopts the newer ones from the Mac.
    public func sync(api: MacAPI?) async {
        guard let api, !syncing else { return }
        syncing = true
        defer { syncing = false }
        do {
            let merged = try await api.putSettings(features: features)
            if features.merge(merged) { save() }
            lastSyncAt = Date()
            lastSyncError = nil
        } catch {
            lastSyncError = (error as? MacError)?.message ?? "\(error)"
        }
    }

    private func save() {
        settings.featuresJSON = features.jsonString()
        Self.writePhoneFile(features, documents)
    }

    static func writePhoneFile(_ f: Features, _ documents: URL) {
        try? f.phoneJSON().write(to: documents.appendingPathComponent("features.json"), options: .atomic)
    }
}
