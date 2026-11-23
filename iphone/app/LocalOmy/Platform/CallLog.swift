import CallKit
import Foundation

/// The phone features on or off, from Documents/features.json (`{"location": true, "calls": false, ...}`,
/// written by `FeaturesStore`). Missing: on.
struct PhoneFeatures {
    let file: URL
    init(documents: URL) { file = documents.appendingPathComponent("features.json") }

    func on(_ key: String) -> Bool {
        guard let data = try? Data(contentsOf: file),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Bool] else { return true }
        return obj[key] ?? true
    }
}

/// When calls (phone, FaceTime, WhatsApp, any CallKit app) were connected, in Documents/calls.json:
/// `[{"id", "start", "end"?}]`, UTC ISO 8601. CallKit says nothing about who is calling, and nothing is recorded
/// about it. Only seen while the app runs, which it does in background for BLE.
@MainActor
final class CallLog: NSObject, CXCallObserverDelegate {
    private static let keep: TimeInterval = 3 * 86400
    private let observer = CXCallObserver()
    private let file: URL
    private let features: PhoneFeatures
    private let iso = ISO8601DateFormatter()

    init(documents: URL) {
        file = documents.appendingPathComponent("calls.json")
        features = PhoneFeatures(documents: documents)
        super.init()
    }

    func start() {
        observer.setDelegate(self, queue: nil)
    }

    nonisolated func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
        let id = call.uuid.uuidString
        let ended = call.hasEnded, connected = call.hasConnected
        MainActor.assumeIsolated { changed(id: id, hasEnded: ended, hasConnected: connected) }
    }

    private func changed(id: String, hasEnded: Bool, hasConnected: Bool) {
        guard features.on("calls") else { return }
        var calls = load()
        let now = iso.string(from: Date())
        if hasEnded {
            if let i = calls.firstIndex(where: { $0["id"] == id }), calls[i]["end"] == nil {
                calls[i]["end"] = now
            }
        } else if hasConnected, !calls.contains(where: { $0["id"] == id }) {
            calls.append(["id": id, "start": now])
        } else {
            return
        }
        let cutoff = Date().addingTimeInterval(-Self.keep)
        calls.removeAll { c in (c["end"] ?? c["start"]).flatMap { iso.date(from: $0) }.map { $0 < cutoff } ?? true }
        save(calls)
    }

    private func load() -> [[String: String]] {
        guard let data = try? Data(contentsOf: file),
              let list = try? JSONSerialization.jsonObject(with: data) as? [[String: String]] else { return [] }
        return list
    }

    private func save(_ calls: [[String: String]]) {
        guard let data = try? JSONSerialization.data(withJSONObject: calls) else { return }
        try? data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
}
