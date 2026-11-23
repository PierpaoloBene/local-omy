# local-omy per iPhone, nativo (SwiftUI, iOS 27)

Sostituisce l'app Flutter in `iphone/src` con lo **stesso bundle id** (`$(LOCAL_OMY_BUNDLE_ID)`,
`com.pierpaolobene.local-omy`): si installa sopra, conserva il contenitore dell'app e quindi la coda dei
segmenti in `Documents/segments`. Paragone di riferimento: `PARITY.md` (inventario dell'app Flutter, da
rispettare punto per punto salvo dove indicato qui).

## Struttura
```
iphone/app/
  project.yml                 XcodeGen: target LocalOmy (iOS 27.0), scheme LocalOmy; `xcodegen generate`
  Packages/OmyKit/            Swift package, piattaforme iOS 27 + macOS 27, Swift 6, zero dipendenze
    Sources/OmyKit/           logica senza UI (testabile con `swift test` sul Mac)
    Tests/OmyKitTests/        Swift Testing (`import Testing`)
  LocalOmy/
    App/                      LocalOmyApp, AppModel (@Observable), lifecycle, migrazione all'avvio
    Platform/                 BLEService (CoreBluetooth), Recorder, CallLog, LocationLog, AudioPlayer, Places (MapKit/geocoder), Keychain
    UI/                       una cartella per scheda: Today/, Conversations/, People/, Places/, Ask/, Settings/, Common/
    Resources/                Assets.xcassets (AppIcon copiata da iphone/src/ios/Runner/Assets.xcassets), Localizable.xcstrings (it, en), InfoPlist.xcstrings
```
Info.plist (in project.yml): UIBackgroundModes `bluetooth-central`, `location` NON aggiunto (come oggi), NSBluetoothAlwaysUsageDescription,
NSLocationWhenInUseUsageDescription, NSLocationAlwaysAndWhenInUseUsageDescription (testi da iphone/src/ios/Runner/Info.plist e
it.lproj), solo verticale, CFBundleLocalizations it/en, CFBundleDisplayName "local-omy". Firma: Personal Team (DEVELOPMENT_TEAM e
LOCAL_OMY_BUNDLE_ID passati da `omy iphone install`, come oggi).

## OmyKit: API pubblica (contratto tra gli agenti)
```swift
// Segmenti (formato v1 identico a PARITY §2: "OMIS", v=1, codec u8, start ms u64 LE; record: offset ms u32 LE, len u16 LE, frame)
public struct SegmentID: Hashable, Sendable { public let raw: String; public static func new(now: Date) -> SegmentID }
public final class SegmentWriter { public init(url: URL, codec: UInt8, start: Date) throws
    public func append(frame: Data, at: Date) throws; public var frameCount: Int { get }; public func close() throws }
public struct Sidecar: Codable, Sendable { id, device_id, codec, start, end, frame_count, first_packet_index, last_packet_index,
    sha256, calls?, location?, battery?, firmware? }        // chiavi JSON esatte come PARITY §2, indent 2
public actor SegmentStore {
    public init(directory: URL)                             // Documents/segments
    public func begin(deviceID: String, codec: UInt8, now: Date) throws -> OpenSegment
    public func finish(_ s: OpenSegment, extras: SidecarExtras, recovering: Bool) throws   // .part → .bin + .json (atomico)
    public func recoverOrphans(extras: SidecarExtras) throws -> Int
    public func queue() -> [QueuedSegment]                  // .bin+.json chiusi, più vecchi prima
    public func delete(_ id: SegmentID) throws
}
public struct SidecarExtras: Sendable { battery: Int?; firmware: String?; calls: [CallInterval]; locations: [LocationSample] }  // bestLocation come PARITY
public actor Uploader {                                     // backoff 30 s ×2 fino a 30 min, reset al successo; stop al primo errore
    public init(store: SegmentStore, api: MacAPI)
    public func kick(reason: String) async                  // chiusura segmento, resume, timer 30 s, "Invia ora", salvataggio impostazioni
    public var status: UploadStatus { get }                 // inCoda, ultimoInvio, ultimoErrore, prossimoTentativo
}
// API del Mac: TUTTI gli endpoint di PARITY §6, async/await su URLSession, Bearer token, errori come oggi (401, 502-504, 404 → testi).
public struct MacAPI: Sendable { public init(baseURL: URL, token: String, session: URLSession = .shared)
    public func uploadSegment(_ q: QueuedSegment, remaining: Int) async throws
    public func days() async throws -> DayList; func day(_:) ; func search(_:person:type:) ; func conversations(...) ; func unlabeled(hours:)
    func transcribeNow() ; people() ; renamePerson ; deletePerson ; setPersonKind ; voices(day:) ; nameVoice ; setVoiceKind ; setVoiceUnknown
    func voiceSample(session:label:) async throws -> Data ; places() ; addPlace ; renamePlace ; removePlace ; putSettings(features:)
    func note(session:) async throws -> Note?
    func ask(question: String, history: [AskTurn], now: Date) -> AsyncThrowingStream<AskEvent, Error>   // SSE, timeout di inattività 90 s
}
public enum AskEvent: Sendable { case status(String), citations([Citation]), delta(String), done(notFound: Bool, used: [Int], timings: [String: Int]), error(String) }
// Modelli Codable: nomi dei campi JSON esatti di PARITY §6 (CodingKeys snake_case), tolleranti a null/campi mancanti.
public struct Features { ... }                              // sync last-writer-wins come PARITY §4, chiavi iphone/mac
public enum Migration {                                     // una sola volta, all'avvio
    public static func fromFlutter(defaults: UserDefaults, keychain: KeychainStore) -> MigrationReport
    // UserDefaults "flutter.device_id", "flutter.mac_url", "flutter.features" → chiavi nuove "deviceID", "macURL", "features";
    // Keychain service "flutter_secure_storage_service" account "upload_token" → service "local-omy" account "upload_token"
    // (AfterFirstUnlockThisDeviceOnly). Non cancella le vecchie voci (rollback possibile).
}
public protocol KeychainStore: Sendable { func read(service: String, account: String) -> String?; func write(_ v: String, service: String, account: String) throws }
public func tr(_ it: String, _ en: String) -> String        // lingua: la prima tra it/en in Locale.preferredLanguages, default en (come oggi)
```

## App
- `AppModel` (@Observable, @MainActor): possiede SegmentStore, Uploader, MacAPI (ricreata quando cambiano URL/token), BLEService,
  Recorder, Features, stato per la UI (pendant, coda, ultimo invio, errori). Le schede ricevono `AppModel` via `@Environment`.
- BLE (PARITY §1): stessi UUID, scan 15 s con filtro "omi", RSSI, connect con riconnessione automatica (CBCentralManager con
  `CBCentralManagerOptionRestoreIdentifierKey`, ripristino in background), batteria e firmware, stati notFound/connected/idle/disconnected.
- Recorder (PARITY §2): rotazione a 5 min su frame con byte 2 == 0, chiusura forzata a 5 min 10 s, chiusura su perdita streaming,
  background e terminazione; segmenti chiusi → `Uploader.kick`.
- CallLog/LocationLog: come AppDelegate.swift attuale (Documents/calls.json, locations.json, pulizia 3 giorni).
- UI: Liquid Glass nativo (vedi `LIQUID_GLASS.md`): `TabView` con le 5 schede (Oggi, Conversazioni, Persone, Luoghi, Chiedi),
  `NavigationStack`, liste e sheet di sistema; vetro esplicito solo per controlli flottanti (barra di input di Chiedi, pulsanti
  sopra le mappe). Mappe con SwiftUI `Map` al posto degli snapshot. Testi in Localizable.xcstrings (it, en).
