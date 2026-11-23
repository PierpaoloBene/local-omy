import Foundation
import Observation
import OmyKit
import OSLog

/// Writes every audio notification from the pendant into the current segment and rotates segments (PARITY §2):
/// every 5 minutes (only on a frame whose byte 2 == 0, so fragments of one codec frame never end up in different
/// segments), forced at 5 min 10 s of age by a 15 s timer (a stream that stopped while connected), on every BLE
/// state other than streaming, in background and at termination.
@MainActor @Observable
final class Recorder {
    static let segmentLength: TimeInterval = 5 * 60
    static let forceAfter: TimeInterval = segmentLength + 10
    private static let log = Logger(subsystem: "local-omy", category: "recorder")

    private(set) var recording = false
    private(set) var lastError: String?
    private(set) var lastErrorAt: Date?

    /// Called after a segment has been closed into the upload queue.
    @ObservationIgnored var onSegmentClosed: (() -> Void)?
    /// Battery, firmware, calls and locations for the sidecar of the segment being closed.
    @ObservationIgnored var extras: () -> SidecarExtras = { SidecarExtras() }

    @ObservationIgnored private let ble: BLEService
    @ObservationIgnored private let store: SegmentStore
    @ObservationIgnored private var current: OpenSegment?
    @ObservationIgnored private var startInstant = ContinuousClock.now
    @ObservationIgnored private var startDate = Date()
    @ObservationIgnored private var opening = false
    @ObservationIgnored private var pending: [(Data, ContinuousClock.Instant)] = []
    @ObservationIgnored private var timer: Task<Void, Never>?
    @ObservationIgnored private var closing = 0

    init(ble: BLEService, store: SegmentStore) {
        self.ble = ble
        self.store = store
    }

    func start() {
        ble.onAudio = { [weak self] frame in self?.onFrame(frame) }
        ble.onStateChange = { [weak self] s in if s != .streaming { self?.rotate() } }
        timer = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(15))
                self?.tick()
            }
        }
    }

    private var age: TimeInterval {
        let d = ContinuousClock.now - startInstant
        return Double(d.components.seconds) + Double(d.components.attoseconds) / 1e18
    }

    private func tick() {
        if current != nil, age >= Self.forceAfter { rotate() }
    }

    private func onFrame(_ frame: Data) {
        if current != nil, age >= Self.segmentLength, frame.count >= 3, frame[frame.startIndex + 2] == 0 { rotate() }
        let now = ContinuousClock.now
        if let c = current {
            write(frame, at: now, to: c)
            return
        }
        if pending.isEmpty && !opening {
            startInstant = now
            startDate = Date()
        }
        pending.append((Data(frame), now))
        guard !opening else { return }
        opening = true
        recording = true
        let device = ble.deviceID ?? ""
        let codec = UInt8(clamping: ble.codecID ?? 0)
        let begin = startDate
        Task {
            do {
                let seg = try await store.begin(deviceID: device, codec: codec, now: begin)
                opening = false
                current = seg
                let frames = pending
                pending = []
                for (f, at) in frames { write(f, at: at, to: seg) }
            } catch {
                opening = false
                pending = []
                recording = false
                setError("Write failed: \(error)")
            }
        }
    }

    private func write(_ frame: Data, at: ContinuousClock.Instant, to seg: OpenSegment) {
        let d = at - startInstant
        let secs = Double(d.components.seconds) + Double(d.components.attoseconds) / 1e18
        do {
            try seg.append(frame: frame, at: startDate.addingTimeInterval(secs))
        } catch {
            setError("Write failed: \(error)")
            current = nil
            recording = false
        }
    }

    /// Closes the current segment into the queue.
    func rotate() {
        guard let seg = current else { return }
        current = nil
        recording = opening
        closing += 1
        let ex = extras()
        Task {
            do {
                try await store.finish(seg, extras: ex, recovering: false)
            } catch {
                setError("Closing segment \(seg.id) failed: \(error)")
            }
            closing -= 1
            onSegmentClosed?()
        }
    }

    /// iOS gives no reliable termination callback: flush the file synchronously and finish; anything left as
    /// `.part` is finalized by `recoverOrphans` at the next launch.
    func terminate() {
        try? current?.writer.close()
        rotate()
    }

    private func setError(_ message: String) {
        Self.log.error("\(message)")
        lastError = message
        lastErrorAt = Date()
    }
}
