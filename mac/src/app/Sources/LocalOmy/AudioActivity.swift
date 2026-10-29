import CoreAudio
import Darwin
import Foundation

/// Which apps use the microphone and the speakers, so omy can tell a call
/// (an app using the microphone: Teams, Zoom, FaceTime, Meet in a browser)
/// from media (an app only playing: YouTube in a browser, Music).
///
/// Every 10 s, while any app uses audio, appends one line to
/// ~/OmiArchive/context/audio-YYYY-MM-DD.jsonl (local day, mode 600):
///   {"t":"2026-10-29T10:16:20Z","apps":{"com.microsoft.teams2":"io"}}
/// "i" input, "o" output. Gaps between lines mean nothing was active (or the
/// app wasn't running): omy joins lines at most 30 s apart into intervals.
final class AudioActivity: @unchecked Sendable {
    static let interval: TimeInterval = 10

    private let dir: URL
    private var timer: Timer?
    private let queue = DispatchQueue(label: "local-omy.audio-activity")
    private let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"  // local day, like the archive folders
        return f
    }()

    init(archive: String) {
        dir = URL(fileURLWithPath: archive).appendingPathComponent("context")
    }

    func start() {
        timer = Timer.scheduledTimer(withTimeInterval: Self.interval, repeats: true) { [weak self] _ in
            self?.queue.async { self?.sample() }
        }
    }

    private func sample() {
        guard Features.isOn("mac_audio", archive: dir.deletingLastPathComponent()) else { return }
        let apps = Self.activeApps()
        guard !apps.isEmpty else { return }
        let now = Date()
        let line: [String: Any] = ["t": now.formatted(.iso8601), "apps": apps]
        guard var data = try? JSONSerialization.data(withJSONObject: line, options: [.sortedKeys]) else { return }
        data.append(0x0A)
        let file = dir.appendingPathComponent("audio-\(dayFormatter.string(from: now)).jsonl")
        let fm = FileManager.default
        if !fm.fileExists(atPath: dir.path) {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        }
        if !fm.fileExists(atPath: file.path) {
            fm.createFile(atPath: file.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        guard let h = try? FileHandle(forWritingTo: file) else { return }
        defer { try? h.close() }
        _ = try? h.seekToEnd()
        try? h.write(contentsOf: data)
    }

    /// Bundle id (or process name) -> "i", "o" or "io", for processes using audio now.
    static func activeApps() -> [String: String] {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyProcessObjectList,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr else { return [:] }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return [:] }

        var apps: [String: String] = [:]
        for id in ids {
            let input: UInt32 = get(id, kAudioProcessPropertyIsRunningInput) ?? 0
            let output: UInt32 = get(id, kAudioProcessPropertyIsRunningOutput) ?? 0
            guard input != 0 || output != 0 else { continue }
            let pid: pid_t = get(id, kAudioProcessPropertyPID) ?? 0
            var name = (get(id, kAudioProcessPropertyBundleID) as CFString?).map { $0 as String } ?? ""
            if name.isEmpty { name = processName(pid) }
            let flags = (input != 0 ? "i" : "") + (output != 0 ? "o" : "")
            // A browser and its helpers: keep the union of what they do.
            let merged = Set(apps[name, default: ""] + flags)
            apps[name] = (merged.contains("i") ? "i" : "") + (merged.contains("o") ? "o" : "")
        }
        return apps
    }

    private static func get<T>(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> T? {
        var addr = AudioObjectPropertyAddress(
            mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<T>.size)
        let value = UnsafeMutablePointer<T>.allocate(capacity: 1)
        defer { value.deallocate() }
        guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, value) == noErr else { return nil }
        return value.pointee
    }

    private static func processName(_ pid: pid_t) -> String {
        var buf = [CChar](repeating: 0, count: 256)
        return proc_name(pid, &buf, UInt32(buf.count)) > 0 ? String(cString: buf) : "pid \(pid)"
    }
}
