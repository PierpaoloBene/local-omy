import AVFoundation
import CallKit
import CoreLocation
import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private let callLog = CallLog()
  private let locationLog = LocationLog()
  private let playback = Playback()

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
        switch call.method {
        case "reload":
          self?.locationLog.reload()
          result(nil)
        case "locationStatus":
          result(self?.locationLog.status())
        case "openSettings":
          if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
          result(nil)
        default:
          result(FlutterMethodNotImplemented)
        }
      }
      // A voice sample from the Mac (mac_api.dart), played from memory.
      let audio = FlutterMethodChannel(name: "local_omy/audio", binaryMessenger: registrar.messenger())
      audio.setMethodCallHandler { [weak self] call, result in
        switch call.method {
        case "play":
          guard let wav = call.arguments as? FlutterStandardTypedData else {
            result(FlutterError(code: "args", message: "WAV bytes expected", details: nil))
            return
          }
          do {
            result(try self?.playback.play(wav.data))
          } catch {
            result(FlutterError(code: "play", message: error.localizedDescription, details: nil))
          }
        case "stop":
          self?.playback.stop()
          result(nil)
        default:
          result(FlutterMethodNotImplemented)
        }
      }
    }
  }
}

/// Plays a WAV held in memory: nothing is written to disk.
final class Playback: NSObject, AVAudioPlayerDelegate {
  private var player: AVAudioPlayer?

  /// Seconds of audio.
  func play(_ data: Data) throws -> Double {
    stop()
    let session = AVAudioSession.sharedInstance()
    try session.setCategory(.playback)
    try session.setActive(true)
    let p = try AVAudioPlayer(data: data)
    p.delegate = self
    p.play()
    player = p
    return p.duration
  }

  func stop() {
    guard let p = player else { return }
    p.stop()
    finished()
  }

  func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) { finished() }

  private func finished() {
    player = nil
    try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
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
    // iOS first grants only "while using" (no background visits or changes);
    // asking again from there shows, once, the prompt to upgrade to "always".
    if [.notDetermined, .authorizedWhenInUse].contains(manager.authorizationStatus) {
      manager.requestAlwaysAuthorization()
    }
    begin()
  }

  /// For the settings page: {"auth": "always"|"when_in_use"|"denied"|"not_determined", "precise": Bool}.
  func status() -> [String: Any] {
    let auth: String
    switch manager.authorizationStatus {
    case .authorizedAlways: auth = "always"
    case .authorizedWhenInUse: auth = "when_in_use"
    case .notDetermined: auth = "not_determined"
    default: auth = "denied"
    }
    return ["auth": auth, "precise": manager.accuracyAuthorization == .fullAccuracy]
  }

  func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
    if PhoneFeatures.on("location") { begin() }
  }

  private func begin() {
    guard [.authorizedAlways, .authorizedWhenInUse].contains(manager.authorizationStatus) else { return }
    manager.startMonitoringVisits()
    manager.startMonitoringSignificantLocationChanges()
    // Significant changes start with iOS's cached fix (possibly hours old)
    // and then wait for ~500 m of movement, visits come late: one fresh fix
    // now, so a phone that stays put has a position too.
    manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
    manager.requestLocation()
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
      // The cached fix comes again at every start.
      if list.contains(where: { $0["kind"] as? String == "move" && $0["t"] as? String == e["t"] as? String }) {
        continue
      }
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
