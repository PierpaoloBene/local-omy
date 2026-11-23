import Foundation
import Observation
import OmyKit
import OSLog
import SwiftUI
import UIKit

/// Everything the UI reads and does. See `iphone/app/APPMODEL.md`.
@MainActor @Observable
final class AppModel {
    private static let log = Logger(subsystem: "local-omy", category: "app")

    // MARK: owned services
    @ObservationIgnored let documents: URL
    @ObservationIgnored let settings: SettingsStore
    @ObservationIgnored let featuresStore: FeaturesStore
    @ObservationIgnored let segmentStore: SegmentStore
    @ObservationIgnored let uploader: Uploader
    let ble: BLEService
    let recorder: Recorder
    let callLog: CallLog
    let locationLog: LocationLog
    let player: AudioPlayer

    // MARK: observed state
    /// Mac client for the tabs, recreated when URL or token change; nil while either is missing.
    private(set) var api: MacAPI?
    /// Bumped each time `api` is recreated, so views can `.task(id:)` on it.
    private(set) var apiGeneration = 0
    private(set) var macURL: String
    private(set) var hasToken: Bool
    private(set) var features: Features
    private(set) var featuresSyncError: String?
    private(set) var featuresLastSync: Date?
    private(set) var uploadStatus: UploadStatus?
    var scenePhase: ScenePhase = .active

    @ObservationIgnored private var statusTask: Task<Void, Never>?
    @ObservationIgnored private var syncTask: Task<Void, Never>?
    @ObservationIgnored private var terminateObserver: NSObjectProtocol?

    init(documents: URL? = nil, defaults: UserDefaults = .standard, keychain: KeychainStore = SystemKeychain()) {
        let docs = documents ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        self.documents = docs
        let settings = SettingsStore(defaults: defaults, keychain: keychain)
        self.settings = settings
        featuresStore = FeaturesStore(settings: settings, documents: docs)
        segmentStore = SegmentStore(directory: docs.appendingPathComponent("segments"))
        let api = settings.makeAPI()
        self.api = api
        uploader = Uploader(store: segmentStore, api: api)
        macURL = settings.macURL ?? ""
        hasToken = !(settings.token ?? "").isEmpty
        features = Features(json: settings.featuresJSON)

        ble = BLEService()
        recorder = Recorder(ble: ble, store: segmentStore)
        callLog = CallLog(documents: docs)
        locationLog = LocationLog(documents: docs)
        player = AudioPlayer()

        recorder.extras = { [unowned self] in
            SidecarExtras.load(documents: self.documents, features: self.features, battery: self.ble.battery, firmware: self.ble.firmware)
        }
        recorder.onSegmentClosed = { [unowned self] in self.kick("segment closed") }
        recorder.start()
        callLog.start()
        locationLog.start()
        ble.start(savedDeviceID: settings.deviceID)

        terminateObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in MainActor.assumeIsolated { self?.recorder.terminate() } }

        statusTask = Task { [weak self] in
            guard let stream = await self?.uploader.statusUpdates() else { return }
            for await s in stream { self?.uploadStatus = s }
        }
        Task { [weak self] in await self?.bootstrap() }
    }

    /// Orphan recovery, upload timer, features sync loop (every 5 min).
    private func bootstrap() async {
        let ex = SidecarExtras.load(documents: documents, features: features)
        do {
            let n = try await segmentStore.recoverOrphans(extras: ex)
            if n > 0 { Self.log.info("recovered \(n) orphan segment(s)") }
        } catch {
            Self.log.error("orphan recovery failed: \(String(describing: error))")
        }
        await uploader.startTimer()
        await uploader.kick(reason: "launch")
        await syncFeatures()
        syncTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(300))
                await self?.syncFeatures()
            }
        }
    }

    // MARK: lifecycle

    func scenePhaseChanged(_ phase: ScenePhase) {
        scenePhase = phase
        switch phase {
        case .active:
            kick("foreground")
            Task { await syncFeatures() }
            ble.refreshStatus()
            locationLog.reload()
        case .background:
            // The recorder keeps running: bluetooth-central keeps the app alive while the pendant streams.
            break
        default:
            break
        }
    }

    // MARK: pendant

    var pendantStatus: PendantStatus { ble.status }
    var pendantBattery: Int? { ble.battery }
    var pendantFirmware: String? { ble.firmware }
    var pendantLastSeen: Date? { ble.lastSeen }
    /// When the link dropped (nil while streaming).
    var pendantLostAt: Date? { ble.lastConnectedAt }
    var pendantDeviceID: String? { ble.deviceID }
    var bluetoothOn: Bool { ble.bluetoothOn }
    var isRecording: Bool { recorder.recording }

    var scanResults: [ScanResult] { ble.scanResults }
    var isScanning: Bool { ble.isScanning }
    func startScan() { ble.startScan() }
    func stopScan() { ble.stopScan() }

    func select(_ device: ScanResult) {
        ble.stopScan()
        settings.deviceID = device.id
        ble.connect(device.id)
    }

    func forgetPendant() {
        settings.deviceID = nil
        ble.forget()
    }

    // MARK: uploads

    var queueCount: Int { (uploadStatus?.inCoda ?? 0) }
    var lastUpload: Date? { uploadStatus?.ultimoInvio }
    var isUploading: Bool { (uploadStatus?.uploading ?? false) }

    /// The most recent error of the uploader or the recorder.
    var lastError: String? {
        let u = uploadStatus?.ultimoErrore.map { ($0, uploadStatus?.ultimoErroreAt ?? .distantPast) }
        let r = recorder.lastError.map { ($0, recorder.lastErrorAt ?? .distantPast) }
        return [u, r].compactMap { $0 }.max { $0.1 < $1.1 }?.0
    }

    /// "Invia ora" and pull-to-refresh.
    func uploadNow() async { await uploader.kick(reason: "send now") }

    private func kick(_ reason: String) {
        Task { [uploader] in await uploader.kick(reason: reason) }
    }

    // MARK: Mac URL and token

    var token: String? { settings.token }

    /// Validates (https only), saves, recreates `api`, kicks the uploader and syncs features. Returns an error text
    /// for the URL field, or nil. An empty `token` keeps the saved one.
    @discardableResult
    func saveMac(url: String, token: String) -> String? {
        var u = url.trimmingCharacters(in: .whitespacesAndNewlines)
        while u.hasSuffix("/") { u.removeLast() }
        if !u.isEmpty {
            let parsed = URL(string: u)
            guard let parsed, parsed.scheme == "https", let host = parsed.host, !host.isEmpty else {
                return tr("Serve un URL https://", "An https:// URL is needed")
            }
        }
        settings.macURL = u
        let t = token.trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { try? settings.setToken(t) }
        macURL = u
        hasToken = !(settings.token ?? "").isEmpty
        rebuildAPI()
        kick("settings saved")
        Task { await syncFeatures() }
        return nil
    }

    private func rebuildAPI() {
        let new = settings.makeAPI()
        api = new
        apiGeneration += 1
        Task { [uploader] in await uploader.update(api: new) }
    }

    // MARK: features

    /// All features with their current value, in display order.
    var featureRows: [(info: FeatureInfo, on: Bool)] { Features.info.map { ($0, features.on($0.key)) } }
    func isOn(_ key: String) -> Bool { features.on(key) }

    func setFeature(_ key: String, on: Bool) async {
        await featuresStore.set(key, on, api: api)
        await refreshFeatures()
    }

    func syncFeatures() async {
        await featuresStore.sync(api: api)
        await refreshFeatures()
    }

    private func refreshFeatures() async {
        features = await featuresStore.features
        featuresSyncError = await featuresStore.lastSyncError
        featuresLastSync = await featuresStore.lastSyncAt
        locationLog.reload()
    }

    // MARK: location permission

    var locationStatus: LocationStatus { locationLog.status }

    /// True when the location feature is on but the permission is not "always" (a problem row in Settings).
    var locationHasProblem: Bool { isOn("location") && locationStatus.auth != .always }

    func openSystemSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
    }

    // MARK: audio samples

    /// Plays a WAV; returns its duration in seconds.
    @discardableResult
    func play(sample: Data) throws -> TimeInterval { try player.play(sample) }
    func stop() { player.stop() }
    var isPlayingSample: Bool { player.isPlaying }
}
