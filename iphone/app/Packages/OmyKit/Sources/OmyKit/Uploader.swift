import Foundation

public struct UploadStatus: Sendable, Equatable {
    /// Closed segments waiting for upload.
    public var inCoda: Int = 0
    public var uploading: Bool = false
    public var ultimoInvio: Date?
    public var ultimoErrore: String?
    public var ultimoErroreAt: Date?
    /// Earliest time the periodic timer tries again after a failure.
    public var prossimoTentativo: Date?
    public var failures: Int = 0
}

/// Sends closed segments to the Mac, oldest first. A segment is deleted locally only after a 200 whose body reports the
/// same sha256 (checked by `MacAPI.uploadSegment`). The first error stops the round; backoff 30 s doubled (exponent max
/// 6) up to 30 min, reset on success. Kicks (segment closed, foreground, "send now", pull-to-refresh, settings saved)
/// always try; the 30 s timer (`tick()` or `kick(reason: "timer")`) tries only once the backoff has passed.
public actor Uploader {
    public static let minBackoff: TimeInterval = 30
    public static let maxBackoff: TimeInterval = 30 * 60

    private let store: SegmentStore
    private var api: MacAPI?
    private let now: @Sendable () -> Date

    public private(set) var status = UploadStatus()
    private var failures = 0
    private var nextAttempt = Date.distantPast
    private var uploading = false
    private var again = false
    private var timer: Task<Void, Never>?
    private var observers: [UUID: AsyncStream<UploadStatus>.Continuation] = [:]

    /// `api` nil = Mac URL or token not set (every attempt fails with that error); change it with `update(api:)`.
    public init(store: SegmentStore, api: MacAPI?, now: @escaping @Sendable () -> Date = { Date() }) {
        self.store = store
        self.api = api
        self.now = now
    }

    public func update(api: MacAPI?) { self.api = api }

    /// Backoff after the n-th consecutive failure.
    public static func backoff(failures: Int) -> TimeInterval {
        min(maxBackoff, minBackoff * pow(2, Double(min(max(failures, 1) - 1, 6))))
    }

    /// Try to drain the queue now. `reason` is for logs; "timer" respects the backoff.
    public func kick(reason: String) async {
        if reason == "timer", now() < nextAttempt { return }
        await drain()
    }

    /// The 30 s timer: tries only after the backoff expired.
    public func tick() async { await kick(reason: "timer") }

    /// Runs `tick()` every `interval` until `stopTimer()`.
    public func startTimer(interval: Duration = .seconds(30)) {
        timer?.cancel()
        timer = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: interval)
                if Task.isCancelled { return }
                await self?.tick()
            }
        }
    }

    public func stopTimer() { timer?.cancel(); timer = nil }

    /// Status changes, starting with the current one.
    public func statusUpdates() -> AsyncStream<UploadStatus> {
        let id = UUID()
        let (stream, cont) = AsyncStream<UploadStatus>.makeStream(bufferingPolicy: .bufferingNewest(1))
        observers[id] = cont
        cont.yield(status)
        cont.onTermination = { [weak self] _ in Task { await self?.removeObserver(id) } }
        return stream
    }

    private func removeObserver(_ id: UUID) { observers[id] = nil }

    private func publish() {
        status.uploading = uploading
        status.failures = failures
        status.prossimoTentativo = failures > 0 ? nextAttempt : nil
        for c in observers.values { c.yield(status) }
    }

    private func refreshQueued() async {
        status.inCoda = await store.queue().count
        publish()
    }

    private func drain() async {
        if uploading {
            again = true
            return
        }
        uploading = true
        await refreshQueued()
        do {
            repeat {
                again = false
                let q = await store.queue()
                for (i, item) in q.enumerated() {
                    try Task.checkCancellation()
                    try await upload(item, remaining: q.count - i - 1)
                    await refreshQueued()
                }
            } while again
            failures = 0
            nextAttempt = .distantPast
        } catch is CancellationError {
            // stopped by the app: not a failure
        } catch {
            failures += 1
            nextAttempt = now().addingTimeInterval(Self.backoff(failures: failures))
            status.ultimoErrore = (error as? MacError)?.message ?? error.localizedDescription
            status.ultimoErroreAt = now()
        }
        uploading = false
        await refreshQueued()
    }

    private func upload(_ q: QueuedSegment, remaining: Int) async throws {
        guard let api else { throw MacError(tr("Mac non configurato: URL e token in Impostazioni", "Mac not set up: URL and token in Settings")) }
        try await api.uploadSegment(q, remaining: remaining)
        try await store.delete(q.id)
        status.ultimoInvio = now()
    }
}
