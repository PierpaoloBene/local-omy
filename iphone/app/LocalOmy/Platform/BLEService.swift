@preconcurrency import CoreBluetooth
import Foundation
import Observation
import OSLog

/// What the app shows: the link state seen from the outside (PARITY §1).
public enum PendantStatus: Sendable, Equatable {
    /// No pendant chosen, or not seen for a while: out of range, or flat.
    case notFound
    /// Was connected a moment ago; reconnecting by itself.
    case disconnected
    /// Connected, but no audio arriving.
    case idle
    /// Connected and sending audio.
    case connected
}

enum PendantState: Sendable { case disconnected, connecting, streaming }

/// A pendant found by a scan.
struct ScanResult: Identifiable, Sendable, Equatable {
    let id: String  // peripheral UUID string
    let name: String
    var rssi: Int
}

enum Omi {
    nonisolated(unsafe) static let service = CBUUID(string: "19b10000-e8f2-537e-4f6c-d104768a1214")
    nonisolated(unsafe) static let audioData = CBUUID(string: "19b10001-e8f2-537e-4f6c-d104768a1214")
    nonisolated(unsafe) static let audioCodec = CBUUID(string: "19b10002-e8f2-537e-4f6c-d104768a1214")
    nonisolated(unsafe) static let batteryService = CBUUID(string: "180f")
    nonisolated(unsafe) static let batteryLevel = CBUUID(string: "2a19")
    nonisolated(unsafe) static let deviceInfoService = CBUUID(string: "180a")
    nonisolated(unsafe) static let firmwareRevision = CBUUID(string: "2a26")
    static let restoreID = "com.pierpaolobene.local-omy.central"
}

private struct BLETimeout: Error {}

/// BLE link to the Omi pendant: scan, connect to the saved device, automatic reconnection (pending connect), codec,
/// audio stream, battery, firmware. Frames are handed over as they arrive, 3-byte header included. Port of
/// ble_service.dart; background through `bluetooth-central` and state restoration.
@MainActor @Observable
final class BLEService: NSObject {
    private static let audioQuiet: TimeInterval = 10
    private static let lostFor: TimeInterval = 10 * 60
    private static let log = Logger(subsystem: "local-omy", category: "ble")

    // Observed by the UI.
    private(set) var status: PendantStatus = .notFound
    private(set) var battery: Int?
    /// Read at the last connection; kept after a disconnection (still the same pendant).
    private(set) var firmware: String?
    private(set) var bluetoothOn = true
    private(set) var scanResults: [ScanResult] = []
    private(set) var isScanning = false
    private(set) var deviceID: String?
    private(set) var state: PendantState = .disconnected
    /// Codec read at the start of the current streaming session (nil = could not be read; never guessed).
    private(set) var codecID: Int?
    private(set) var lastAudioAt: Date?
    private var lastConnectedAtStored: Date?

    /// When the link dropped, while disconnected/notFound after a connection.
    var lastConnectedAt: Date? { state == .streaming ? nil : lastConnectedAtStored }
    /// Last sign of life: the last audio while streaming, else when the link dropped.
    var lastSeen: Date? { state == .streaming ? (lastAudioAt ?? Date()) : lastConnectedAtStored }

    // Consumers (the Recorder).
    @ObservationIgnored var onAudio: ((Data) -> Void)?
    @ObservationIgnored var onStateChange: ((PendantState) -> Void)?

    @ObservationIgnored private var central: CBCentralManager?
    @ObservationIgnored private var peripheral: CBPeripheral?
    @ObservationIgnored private var wantedID: UUID?
    @ObservationIgnored private var found: [UUID: ScanResult] = [:]
    @ObservationIgnored private var scanStop: Task<Void, Never>?
    @ObservationIgnored private var scanWaiters: [CheckedContinuation<Bool, Never>] = []
    @ObservationIgnored private var reads: [CBUUID: CheckedContinuation<Data, Error>] = [:]
    @ObservationIgnored private var setupTask: Task<Void, Never>?
    @ObservationIgnored private var ticker: Task<Void, Never>?
    @ObservationIgnored private var discoverWaiter: CheckedContinuation<Void, Error>?
    @ObservationIgnored private var pendingServices = 0

    /// Creates the central manager (with the restore identifier: iOS relaunches the app in background for BLE events
    /// after killing it, not after a user force-quit) and connects to the saved device.
    func start(savedDeviceID: String?) {
        guard central == nil else { return }
        central = CBCentralManager(delegate: self, queue: .main, options: [
            CBCentralManagerOptionRestoreIdentifierKey: Omi.restoreID,
            CBCentralManagerOptionShowPowerAlertKey: true,
        ])
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                self?.refreshStatus()
            }
        }
        if let savedDeviceID { connect(savedDeviceID) }
    }

    // MARK: status

    private func computeStatus(now: Date) -> PendantStatus {
        if wantedID == nil { return .notFound }
        if state == .streaming {
            if let a = lastAudioAt, now.timeIntervalSince(a) < Self.audioQuiet { return .connected }
            return .idle
        }
        if let c = lastConnectedAtStored, now.timeIntervalSince(c) < Self.lostFor { return .disconnected }
        return .notFound
    }

    func refreshStatus() {
        let s = computeStatus(now: Date())
        if s != status { status = s }
    }

    private func setState(_ s: PendantState) {
        guard state != s else { return }
        state = s
        refreshStatus()
        onStateChange?(s)
    }

    // MARK: scan

    /// Scans `timeout` seconds for devices whose name contains "omi", sorted by RSSI. Waits up to 10 s for the adapter.
    func startScan(timeout: Duration = .seconds(15)) {
        guard !isScanning, let central else { return }
        isScanning = true
        found = [:]
        scanResults = []
        scanStop = Task { [weak self] in
            guard let self else { return }
            let on = await self.waitForPoweredOn(seconds: 10)
            guard on, !Task.isCancelled, let central = self.central else {
                self.isScanning = false
                return
            }
            central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
            try? await Task.sleep(for: timeout)
            self.stopScan()
        }
        _ = central
    }

    func stopScan() {
        scanStop?.cancel()
        scanStop = nil
        if central?.isScanning == true { central?.stopScan() }
        isScanning = false
    }

    private func waitForPoweredOn(seconds: Double) async -> Bool {
        if central?.state == .poweredOn { return true }
        return await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
            scanWaiters.append(c)
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(seconds))
                self?.resolveWaiters(false)
            }
        }
    }

    private func resolveWaiters(_ on: Bool) {
        let w = scanWaiters
        scanWaiters = []
        for c in w { c.resume(returning: on) }
    }

    // MARK: connect

    /// Connect to `remoteID` with a pending connect: it never times out and iOS completes it whenever the pendant
    /// is in range; it is re-issued after every disconnection and whenever the adapter turns back on.
    func connect(_ remoteID: String) {
        guard let uuid = UUID(uuidString: remoteID) else { return }
        if wantedID == uuid { return }
        forget()
        wantedID = uuid
        deviceID = remoteID
        setState(.connecting)
        issueConnect()
    }

    private func issueConnect() {
        guard let central, central.state == .poweredOn, let uuid = wantedID else { return }
        let p = peripheral?.identifier == uuid ? peripheral : central.retrievePeripherals(withIdentifiers: [uuid]).first
        guard let p else { return }  // unknown to iOS until scanned: retried when scan results arrive
        peripheral = p
        p.delegate = self
        if p.state == .connected {
            beginSetup(p)
        } else if p.state != .connecting {
            central.connect(p, options: [CBConnectPeripheralOptionEnableAutoReconnect: true])
        }
    }

    /// Drop the current device and stop auto-reconnecting to it.
    func forget() {
        setupTask?.cancel()
        setupTask = nil
        failReads()
        let p = peripheral
        wantedID = nil
        deviceID = nil
        peripheral = nil
        firmware = nil
        lastConnectedAtStored = nil
        codecID = nil
        if let p { central?.cancelPeripheralConnection(p) }
        setState(.disconnected)
        refreshStatus()
    }

    // MARK: setup (after connect)

    private func beginSetup(_ p: CBPeripheral) {
        setupTask?.cancel()
        setupTask = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.setup(p)
            } catch is CancellationError {
            } catch {
                Self.log.error("setup error: \(String(describing: error))")
                guard self.peripheral === p, self.wantedID != nil else { return }
                // Force a clean disconnect; the pending connect will retry.
                self.central?.cancelPeripheralConnection(p)
                self.setState(.connecting)
                self.issueConnect()
            }
        }
    }

    private func setup(_ p: CBPeripheral) async throws {
        try await discover(p)
        func find(_ s: CBUUID, _ c: CBUUID) -> CBCharacteristic? {
            p.services?.first { $0.uuid == s }?.characteristics?.first { $0.uuid == c }
        }
        guard let audio = find(Omi.service, Omi.audioData) else {
            Self.log.error("audio characteristic not found, not an Omi device")
            if peripheral === p { forget() }
            return
        }
        if let codec = find(Omi.service, Omi.audioCodec) {
            let v = try await read(codec, on: p)
            codecID = v.first.map { Int($0) }
        }
        if let fw = find(Omi.deviceInfoService, Omi.firmwareRevision) {
            if let v = try? await read(fw, on: p) {
                let s = String(decoding: v, as: UTF8.self).replacingOccurrences(of: "\u{0}", with: "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                firmware = s.isEmpty ? nil : s
            }
        }
        if let bat = find(Omi.batteryService, Omi.batteryLevel) {
            let v = try await read(bat, on: p)
            if let b = v.first { battery = Int(b) }
            if bat.properties.contains(.notify) { p.setNotifyValue(true, for: bat) }
        }
        try Task.checkCancellation()
        p.setNotifyValue(true, for: audio)
        if peripheral === p { setState(.streaming) }
    }

    private func discover(_ p: CBPeripheral) async throws {
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            discoverWaiter = c
            p.discoverServices(nil)
        }
    }

    private func read(_ c: CBCharacteristic, on p: CBPeripheral) async throws -> Data {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Data, Error>) in
            reads[c.uuid] = cont
            p.readValue(for: c)
            let id = c.uuid
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(15))
                self?.reads.removeValue(forKey: id)?.resume(throwing: BLETimeout())
            }
        }
    }

    private func failReads() {
        let r = reads
        reads = [:]
        for (_, c) in r { c.resume(throwing: CancellationError()) }
        discoverWaiter?.resume(throwing: CancellationError())
        discoverWaiter = nil
    }
}

// MARK: delegates (callbacks arrive on the main queue)

extension BLEService: CBCentralManagerDelegate, CBPeripheralDelegate {
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        MainActor.assumeIsolated {
            let on = central.state == .poweredOn || central.state == .unknown
            bluetoothOn = on
            if central.state == .poweredOn {
                resolveWaiters(true)
                issueConnect()
            } else if central.state != .unknown {
                resolveWaiters(false)
                if state == .streaming { lastConnectedAtStored = Date() }
                setupTask?.cancel()
                failReads()
                if wantedID != nil { setState(.connecting) }
            }
            refreshStatus()
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, willRestoreState dict: [String: Any]) {
        let ids = (dict[CBCentralManagerRestoredStatePeripheralsKey] as? [CBPeripheral] ?? []).map(\.identifier)
        MainActor.assumeIsolated {
            guard let p = central.retrievePeripherals(withIdentifiers: ids).first else { return }
            Self.log.info("restored \(p.identifier.uuidString)")
            if wantedID == nil {
                wantedID = p.identifier
                deviceID = p.identifier.uuidString
            }
            guard p.identifier == wantedID else { return }
            peripheral = p
            p.delegate = self
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDiscover p: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        let name = p.name ?? (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? ""
        MainActor.assumeIsolated {
            guard name.lowercased().contains("omi") else { return }
            found[p.identifier] = ScanResult(id: p.identifier.uuidString, name: name, rssi: RSSI.intValue)
            scanResults = found.values.sorted { $0.rssi > $1.rssi }
            if p.identifier == wantedID { issueConnect() }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didConnect p: CBPeripheral) {
        MainActor.assumeIsolated {
            guard p.identifier == wantedID else { central.cancelPeripheralConnection(p); return }
            peripheral = p
            p.delegate = self
            beginSetup(p)
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) {
        MainActor.assumeIsolated {
            if p.identifier == wantedID { issueConnect() }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, timestamp: CFAbsoluteTime, isReconnecting: Bool, error: Error?) {
        MainActor.assumeIsolated {
            guard p.identifier == wantedID else { return }
            setupTask?.cancel()
            failReads()
            codecID = nil
            if state == .streaming { lastConnectedAtStored = Date() }
            setState(.connecting)
            if !isReconnecting { issueConnect() }
        }
    }

    nonisolated func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        MainActor.assumeIsolated {
            if let error { discoverWaiter?.resume(throwing: error); discoverWaiter = nil; return }
            let services = p.services ?? []
            pendingServices = services.count
            if services.isEmpty { discoverWaiter?.resume(); discoverWaiter = nil; return }
            for s in services { p.discoverCharacteristics(nil, for: s) }
        }
    }

    nonisolated func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        MainActor.assumeIsolated {
            if let error { discoverWaiter?.resume(throwing: error); discoverWaiter = nil; return }
            pendingServices -= 1
            if pendingServices <= 0 { discoverWaiter?.resume(); discoverWaiter = nil }
        }
    }

    nonisolated func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
        MainActor.assumeIsolated {
            let value = c.value ?? Data()
            if let cont = reads.removeValue(forKey: c.uuid) {
                if let error { cont.resume(throwing: error) } else { cont.resume(returning: value) }
                return
            }
            guard error == nil, !value.isEmpty else { return }
            if c.uuid == Omi.batteryLevel {
                battery = Int(value[value.startIndex])
            } else if c.uuid == Omi.audioData {
                lastAudioAt = Date()
                onAudio?(Data(value))
            }
        }
    }

    nonisolated func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor c: CBCharacteristic, error: Error?) {
        MainActor.assumeIsolated {
            guard error != nil, c.uuid == Omi.audioData, p === peripheral else { return }
            Self.log.error("audio notify failed")
            central?.cancelPeripheralConnection(p)
        }
    }
}
