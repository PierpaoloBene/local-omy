# AppModel: superficie pubblica per le schede

`LocalOmy/App/AppModel.swift`, `@MainActor @Observable final class AppModel`. Le viste lo leggono con
`@Environment(AppModel.self) private var model`. Anche `TabReselect` è in environment (vedi sotto).

## Ciondolo (BLE)
- `pendantStatus: PendantStatus` (`.notFound/.disconnected/.idle/.connected`, con `.color` e `.title` in `UI/Common/AppTab.swift`)
- `pendantBattery: Int?`, `pendantFirmware: String?`, `pendantLastSeen: Date?` (ultimo audio se in streaming, altrimenti quando è caduto il link),
  `pendantLostAt: Date?` (nil in streaming; per "perso alle …"), `pendantDeviceID: String?` (nil = nessun ciondolo scelto), `bluetoothOn: Bool`
- `isRecording: Bool` (c'è un segmento aperto)
- Scansione: `scanResults: [ScanResult]` (`id`, `name`, `rssi`, ordinati per RSSI), `isScanning`, `startScan()` (15 s), `stopScan()`,
  `select(_ result: ScanResult)` (salva e connette), `forgetPendant()`

## Coda e invio
- `queueCount: Int`, `lastUpload: Date?`, `isUploading: Bool`, `uploadStatus: UploadStatus?` (backoff, prossimoTentativo…)
- `lastError: String?` (il più recente tra uploader e recorder)
- `await uploadNow()` ("Invia ora", pull-to-refresh). Il timer di 30 s, la chiusura dei segmenti, il resume e il salvataggio delle impostazioni
  fanno già `kick`.

## Mac
- `api: MacAPI?` (nil finché URL o token mancano; ricreata a ogni cambio) e `apiGeneration: Int` (usare `.task(id: model.apiGeneration)` per ricaricare)
- `macURL: String`, `hasToken: Bool`, `token: String?`
- `saveMac(url:token:) -> String?`: valida (solo https, toglie gli `/` finali), salva, ricrea `api`, kick dell'uploader, sync features. Ritorna il
  testo d'errore per il campo URL o nil. Token vuoto = tiene quello salvato.

## Features
- `featureRows: [(info: FeatureInfo, on: Bool)]` (ordine di visualizzazione; `info.device` = "iphone" | "mac"), `isOn(_:)`
- `await setFeature(_ key:, on:)` (scrive, sincronizza con il Mac, ricarica LocationLog), `await syncFeatures()`
- `featuresSyncError: String?`, `featuresLastSync: Date?` (sync ogni 5 min, al resume e a ogni modifica)

## Permesso di posizione
- `locationStatus: LocationStatus` (`auth: .always/.whenInUse/.denied/.notDetermined`, `precise: Bool`), `locationHasProblem: Bool`
  (feature attiva e permesso diverso da "sempre"), `openSystemSettings()`

## Campioni audio
- `play(sample: Data) throws -> TimeInterval` (WAV in memoria, ritorna la durata), `stop()`, `isPlayingSample: Bool` (osservabile;
  torna false a fine riproduzione)

## Schede
- `TabReselect` (`@Environment(TabReselect.self)`): toccando la scheda attiva `count(for: .today)` ecc. aumenta; la scheda fa
  `.onChange(of: reselect.count(for: .today)) { … pop / scroll in cima }`. `AppTab` (`.today .conversations .people .places .ask`) ha `title` e `symbol`.
- Impostazioni non è una scheda: le apre Oggi (ingranaggio → sheet con `SettingsView()`).

## Ciclo di vita (gestito da AppModel, non dalle viste)
Migrazione Flutter una volta all'avvio (`LocalOmyApp.init`); recupero dei `.part` orfani; timer uploader 30 s; sync features ogni 5 min;
`scenePhase` active → kick + sync + ricarica LocationLog; background → il Recorder continua (bluetooth-central); terminazione → chiude il
segmento (best effort, il resto lo recupera l'avvio successivo).
