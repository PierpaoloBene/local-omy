import CallKit
import CoreLocation
import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private let callLog = CallLog()
  private let locationLog = LocationLog()

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    callLog.start()
    locationLog.start()
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    // features.dart rewrote Documents/features.json: apply it.
    if let registrar = engineBridge.pluginRegistry.registrar(forPlugin: "LocalOmyFeatures") {
      let channel = FlutterMethodChannel(name: "local_omy/features", binaryMessenger: registrar.messenger())
      channel.setMethodCallHandler { [weak self] call, result in
        if call.method == "reload" { self?.locationLog.reload() }
        result(nil)
      }
    }
  }
}

/// The phone features on or off, from Documents/features.json written by
/// features.dart ({"location": true, "calls": false, …}). Missing: on.
enum PhoneFeatures {
  private static let file = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    .appendingPathComponent("features.json")

  static func on(_ key: String) -> Bool {
    guard let data = try? Data(contentsOf: file),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Bool] else { return true }
    return obj[key] ?? true
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
    guard PhoneFeatures.on("calls") else { return }
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

/// Where the phone was, in Documents/locations.json, cheaply: iOS visits
/// (arrived at / left a place) and significant location changes (~500 m),
/// which work in background and relaunch the app, without GPS running.
///   {"kind": "visit", "lat", "lon", "acc", "arrive"?, "depart"?}
///   {"kind": "move", "lat", "lon", "acc", "t"}
/// UTC ISO 8601, acc in meters. The Dart side puts the best fix for each
/// segment into its sidecar (segment_store.dart); the Mac names it with its
/// own list of places. Nothing is geocoded: coordinates never leave the
/// phone and the Mac.
final class LocationLog: NSObject, CLLocationManagerDelegate {
  private let manager = CLLocationManager()
  private let file: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    .appendingPathComponent("locations.json")
  private let iso = ISO8601DateFormatter()
  private static let keep: TimeInterval = 3 * 86400

  func start() {
    manager.delegate = self
    reload()
  }

  /// Start or stop following the feature switch.
  func reload() {
    guard PhoneFeatures.on("location") else {
      manager.stopMonitoringVisits()
      manager.stopMonitoringSignificantLocationChanges()
      return
    }
    // iOS first grants "while using", then later offers to keep "always".
    if manager.authorizationStatus == .notDetermined { manager.requestAlwaysAuthorization() }
    begin()
  }

  func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
    if PhoneFeatures.on("location") { begin() }
  }

  private func begin() {
    guard [.authorizedAlways, .authorizedWhenInUse].contains(manager.authorizationStatus) else { return }
    manager.startMonitoringVisits()
    manager.startMonitoringSignificantLocationChanges()
  }

  func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
    guard PhoneFeatures.on("location") else { return }
    var e = point(visit.coordinate, visit.horizontalAccuracy)
    e["kind"] = "visit"
    if visit.arrivalDate != .distantPast { e["arrive"] = iso.string(from: visit.arrivalDate) }
    if visit.departureDate != .distantFuture { e["depart"] = iso.string(from: visit.departureDate) }
    var list = load()
    // The departure comes as the same visit again, now with depart.
    list.removeAll { $0["kind"] as? String == "visit" && $0["arrive"] as? String == e["arrive"] as? String
      && e["arrive"] != nil }
    list.append(e)
    save(list)
  }

  func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
    guard PhoneFeatures.on("location") else { return }
    var list = load()
    for l in locations where l.horizontalAccuracy >= 0 {
      var e = point(l.coordinate, l.horizontalAccuracy)
      e["kind"] = "move"
      e["t"] = iso.string(from: l.timestamp)
      list.append(e)
    }
    save(list)
  }

  func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

  private func point(_ c: CLLocationCoordinate2D, _ acc: CLLocationAccuracy) -> [String: Any] {
    ["lat": (c.latitude * 1e6).rounded() / 1e6, "lon": (c.longitude * 1e6).rounded() / 1e6,
     "acc": Int(acc.rounded())]
  }

  private func load() -> [[String: Any]] {
    guard let data = try? Data(contentsOf: file),
          let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return [] }
    return list
  }

  private func save(_ list: [[String: Any]]) {
    let cutoff = Date().addingTimeInterval(-Self.keep)
    let kept = list.filter { e in
      // A visit still on (no depart) is kept: it is where the phone is.
      if e["kind"] as? String == "visit", e["depart"] == nil { return true }
      let when = (e["depart"] ?? e["t"] ?? e["arrive"]) as? String
      return when.flatMap { iso.date(from: $0) }.map { $0 >= cutoff } ?? false
    }
    guard let data = try? JSONSerialization.data(withJSONObject: kept) else { return }
    try? data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
  }
}
