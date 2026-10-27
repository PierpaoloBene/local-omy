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
    struct Transcripts: Decodable { let path: String; let days: [String] }

    let receiver: Agent
    let periodic: Agent
    let tailscale: Tailscale
    let archive: Archive
    let iphone: IPhone
    let transcripts: Transcripts
    let logs: [String: String]
}

struct Check: Decodable, Identifiable {
    let id: String
    let label: String
    let ok: Bool
    let detail: String
}

private struct Checks: Decodable { let checks: [Check] }
private struct Token: Decodable { let token: String }

enum Level { case ok, warning, error, unknown }

@MainActor
@Observable
final class AppModel {
    var status: Status?
    var checks: [Check] = []
    var refreshError: String?
    /// Name of the long operation in progress (one at a time), and its log.
    var busy: String?
    var log: [String] = []
    var lastError: String?
    var loginItem = SMAppService.mainApp.status == .enabled

    private var timer: Timer?

    init() {
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in
                if self?.busy == nil { await self?.refresh() }
            }
        }
    }

    func refresh() async {
        do {
            async let s = Omy.decode(Status.self, ["status"])
            async let c = Omy.decode(Checks.self, ["setup", "check"])
            (status, checks) = try await (s, c.checks)
            refreshError = nil
        } catch {
            refreshError = error.localizedDescription
        }
    }

    /// Run a long omy command, streaming its log, then refresh.
    func perform(_ name: String, _ args: [String]) {
        guard busy == nil else { return }
        busy = name
        log = []
        lastError = nil
        Task {
            do {
                _ = try await Omy.run(args) { line in
                    Task { @MainActor in self.appendLog(line) }
                }
            } catch {
                lastError = error.localizedDescription
            }
            busy = nil
            await refresh()
        }
    }

    private func appendLog(_ line: String) {
        log.append(line)
        if log.count > 2000 { log.removeFirst(log.count - 2000) }
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
            lastError = "Avvio al login: \(error.localizedDescription)"
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
