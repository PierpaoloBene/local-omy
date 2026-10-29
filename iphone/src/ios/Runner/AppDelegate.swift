import CallKit
import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private let callLog = CallLog()

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    callLog.start()
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
  }
}

/// When calls (phone, FaceTime, WhatsApp, any CallKit app) were connected,
/// in Documents/calls.json: [{"id", "start", "end"?}], UTC ISO 8601. CallKit
/// says nothing about who is calling, and nothing is recorded about it. The
/// Dart side copies the calls overlapping each segment into its sidecar
/// (segment_store.dart), so the Mac can tell a call from people in the room.
/// Only seen while the app runs, which it does in background for BLE.
final class CallLog: NSObject, CXCallObserverDelegate {
  private let observer = CXCallObserver()
  private let file: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    .appendingPathComponent("calls.json")
  private let iso = ISO8601DateFormatter()
  private static let keep: TimeInterval = 3 * 86400

  func start() {
    observer.setDelegate(self, queue: nil)
  }

  func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
    let id = call.uuid.uuidString
    var calls = load()
    let now = iso.string(from: Date())
    if call.hasEnded {
      if let i = calls.firstIndex(where: { $0["id"] == id }), calls[i]["end"] == nil {
        calls[i]["end"] = now
      }
    } else if call.hasConnected, !calls.contains(where: { $0["id"] == id }) {
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
