import AppKit
import Foundation
import Observation
import ServiceManagement

// Mirrors of the `omy --json` outputs (snake_case converted by the decoder).

struct Status: Decodable {
    struct Agent: Decodable { let loaded: Bool; let running: Bool; let pid: Int?; let upToDate: Bool }
    struct Peer: Decodable { let name: String; let online: Bool }
    struct Tailscale: Decodable { let installed: Bool; let serving: Bool; let url: String?; let iphones: [Peer] }
    struct Archive: Decodable {
        let path: String
        let todaySegments: Int
        let todayMinutes: Double
        let todaySessions: Int
        let lastSegmentId: String?
        let minutesSinceLast: Double?
    }
    struct Device: Decodable { let name: String?; let paired: Bool; let transport: String? }
    struct IPhone: Decodable {
        let teamId: String?
        let deviceUdid: String?
        let expires: String?
        let daysLeft: Double?
        let installedAt: String?
        let needsReopen: Bool
        let device: Device?
    }
    struct Upload: Decodable { let lastUploadAt: String?; let minutesSince: Double?; let queueRemaining: Int? }
    struct Transcripts: Decodable {
        let path: String
        let days: [String]
        let pendingSessions: Int
        let pendingMinutes: Double
        let openSessions: Int
        let progressFile: String
    }
    struct Battery: Decodable {
        let level: Int?
        let at: String?
        let hoursMeasured: Double?
        let drainPerHour: Double?
        let fullChargeHours: Double?
        let remainingHours: Double?
    }
    struct Firmware: Decodable {
        let version: String?
        let at: String?
    }

    let receiver: Agent
    let periodic: Agent
    let tailscale: Tailscale
    let archive: Archive
    let upload: Upload
    let iphone: IPhone
    let transcripts: Transcripts
    let battery: Battery?
    let firmware: Firmware?
    let logs: [String: String]
}

struct Check: Decodable, Identifiable {
    let id: String
    let label: String
    let ok: Bool
    let detail: String
}

/// cache/transcribe-progress.json, written by whichever omy transcribe is
/// running (app, terminal or hourly job).
struct TranscribeProgress: Decodable {
    let pid: Int32
    let sessionsTotal: Int
    let sessionsDone: Int
    let minutesTotal: Double
    let minutesDone: Double
    let session: String?
    let day: String?

    var fraction: Double { minutesTotal > 0 ? min(minutesDone / minutesTotal, 1) : 0 }

    var text: String {
        var t = tr("sessione", "session") + " \(min(sessionsDone + 1, sessionsTotal))/\(sessionsTotal)"
        if let day { t += " · \(day)" }
        return t + " · \(Int(minutesDone))/\(Int(minutesTotal)) " + tr("min di audio", "min of audio")
    }

    /// The file, if the process that wrote it is still alive.
    static func read(_ path: String) -> TranscribeProgress? {
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let p = try? decoder.decode(TranscribeProgress.self, from: data) else { return nil }
        return kill(p.pid, 0) == 0 || errno == EPERM ? p : nil
    }
}

/// ~/Library/Logs/local-omy-app.log: every operation's log, timestamped.
enum AppLog {
    static let path = "\(Omy.home)/Library/Logs/local-omy-app.log"
    private static let queue = DispatchQueue(label: "local-omy.applog")

    static func write(_ line: String) {
        let text = "\(Date().formatted(.iso8601)) \(line)\n"
        queue.async {
            if !FileManager.default.fileExists(atPath: path) {
                FileManager.default.createFile(atPath: path, contents: nil)
            }
            guard let h = FileHandle(forWritingAtPath: path) else { return }
            h.seekToEndOfFile()
            h.write(Data(text.utf8))
            try? h.close()
        }
    }
}

/// One long omy command at a time per lane, with its streamed log.
@MainActor
@Observable
final class Job {
    private(set) var running: String?
    private(set) var log: [String] = []
    var lastError: String?

    /// Runs `omy --json <args>`, then onDone (e.g. a refresh).
    func start(_ name: String, _ args: [String], onDone: @escaping @MainActor () async -> Void) {
        guard running == nil else { return }
        running = name
        log = []
        lastError = nil
        AppLog.write("[\(name)] start: omy \(args.joined(separator: " "))")
        Task {
            do {
                _ = try await Omy.run(args) { line in
                    AppLog.write("[\(name)] \(line)")
                    Task { @MainActor in self.append(line) }
                }
                AppLog.write("[\(name)] done")
            } catch {
                lastError = error.localizedDescription
                AppLog.write("[\(name)] failed: \(error.localizedDescription)")
            }
            running = nil
            await onDone()
        }
    }

    private func append(_ line: String) {
        log.append(line)
        if log.count > 2000 { log.removeFirst(log.count - 2000) }
    }
}

private struct Checks: Decodable { let checks: [Check] }
private struct Token: Decodable { let token: String }

enum Level { case ok, warning, error, unknown }

@MainActor
@Observable
final class AppModel {
    var status: Status?
    var checks: [Check] = []
    var features: [Feature] = []
    var refreshError: String?
    /// When status was last read successfully.
    var refreshedAt: Date?
    /// Setup fixes and iPhone installs: one at a time.
    let task = Job()
    /// Transcription, independent of the above: it can take an hour.
    let transcription = Job()
    /// Progress of any running transcription, also one started by the
    /// hourly job or a terminal.
    var progress: TranscribeProgress?
    /// Errors of short actions (token, login item).
    var lastError: String?
    var loginItem = SMAppService.mainApp.status == .enabled

    private var refreshing = false
    private var timers: [Timer] = []
    private let audio = AudioActivity(
        archive: ProcessInfo.processInfo.environment["OMY_ARCHIVE"] ?? "\(Omy.home)/OmiArchive")

    init() {
        audio.start()
        Task { await refresh() }
        timers.append(Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.refresh() }
        })
        // Cheap: one small file read.
        timers.append(Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.readProgress() }
        })
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            async let s = Omy.decode(Status.self, ["status"])
            async let c = Omy.decode(Checks.self, ["setup", "check"])
            async let f = Features.list()
            (status, checks, features) = try await (s, c.checks, f)
            refreshError = nil
            refreshedAt = Date()
        } catch {
            if refreshError == nil { AppLog.write("refresh failed: \(error.localizedDescription)") }
            refreshError = error.localizedDescription
        }
        readProgress()
    }

    private func readProgress() {
        progress = status.flatMap { TranscribeProgress.read($0.transcripts.progressFile) }
    }

    var transcribing: Bool { transcription.running != nil || progress != nil }

    func transcribeNow() {
        guard !transcribing else { return }
        transcription.start(tr("Trascrizione", "Transcription"), ["transcribe", "--pending"]) { [weak self] in await self?.refresh() }
    }

    func perform(_ name: String, _ args: [String]) {
        task.start(name, args) { [weak self] in await self?.refresh() }
    }

    func setFeature(_ key: String, _ on: Bool) {
        AppLog.write("feature \(key) \(on ? "on" : "off")")
        Task {
            do {
                features = try await Features.set(key, on)
            } catch {
                lastError = tr("Funzioni", "Features") + ": \(error.localizedDescription)"
            }
        }
    }

    /// Copies the receiver token to the clipboard; it is never displayed.
    func copyToken() {
        Task {
            do {
                let t = try await Omy.decode(Token.self, ["setup", "token"])
                copy(t.token)
            } catch {
                lastError = error.localizedDescription
            }
        }
    }

    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func setLoginItem(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            lastError = tr("Avvio al login", "Open at login") + ": \(error.localizedDescription)"
        }
        loginItem = SMAppService.mainApp.status == .enabled
    }

    var level: Level {
        guard let s = status else { return refreshError == nil ? .unknown : .error }
        let days = s.iphone.daysLeft ?? 99
        if !s.receiver.running || days < 1 { return .error }
        if !s.tailscale.serving || !s.periodic.loaded || s.iphone.needsReopen || days < 3
            || checks.contains(where: { !$0.ok }) { return .warning }
        return .ok
    }
}
