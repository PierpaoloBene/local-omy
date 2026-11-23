import CoreLocation
import Foundation
import Observation

/// For the settings page: iOS location permission.
enum LocationAuth: String, Sendable { case always, whenInUse = "when_in_use", denied, notDetermined = "not_determined" }

struct LocationStatus: Sendable, Equatable {
    var auth: LocationAuth
    var precise: Bool
}

/// Where the phone was, in Documents/locations.json, cheaply: iOS visits (arrived at / left a place) and
/// significant location changes (~500 m), which work in background and relaunch the app, without GPS running.
///
///     {"kind": "visit", "lat", "lon", "acc", "arrive"?, "depart"?}
///     {"kind": "move",  "lat", "lon", "acc", "t"}
///
/// UTC ISO 8601, acc in meters. Port of LocationLog in AppDelegate.swift.
@MainActor @Observable
final class LocationLog: NSObject, CLLocationManagerDelegate {
    private static let keep: TimeInterval = 3 * 86400

    private(set) var status: LocationStatus

    @ObservationIgnored private let manager = CLLocationManager()
    @ObservationIgnored private let file: URL
    @ObservationIgnored private let features: PhoneFeatures
    @ObservationIgnored private let iso = ISO8601DateFormatter()

    init(documents: URL) {
        file = documents.appendingPathComponent("locations.json")
        features = PhoneFeatures(documents: documents)
        status = LocationStatus(auth: .notDetermined, precise: true)
        super.init()
        status = Self.read(manager)
    }

    private static func read(_ m: CLLocationManager) -> LocationStatus {
        let auth: LocationAuth
        switch m.authorizationStatus {
        case .authorizedAlways: auth = .always
        case .authorizedWhenInUse: auth = .whenInUse
        case .notDetermined: auth = .notDetermined
        default: auth = .denied
        }
        return LocationStatus(auth: auth, precise: m.accuracyAuthorization == .fullAccuracy)
    }

    func start() {
        manager.delegate = self
        reload()
    }

    /// Start or stop following the feature switch (reads Documents/features.json).
    func reload() {
        status = Self.read(manager)
        guard features.on("location") else {
            manager.stopMonitoringVisits()
            manager.stopMonitoringSignificantLocationChanges()
            return
        }
        // iOS first grants only "while using" (no background visits or changes); asking again from there shows,
        // once, the prompt to upgrade to "always".
        if [.notDetermined, .authorizedWhenInUse].contains(manager.authorizationStatus) {
            manager.requestAlwaysAuthorization()
        }
        begin()
    }

    private func begin() {
        guard [.authorizedAlways, .authorizedWhenInUse].contains(manager.authorizationStatus) else { return }
        manager.startMonitoringVisits()
        manager.startMonitoringSignificantLocationChanges()
        // Significant changes start with iOS's cached fix (possibly hours old) and then wait for ~500 m of
        // movement, visits come late: one fresh fix now, so a phone that stays put has a position too.
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        manager.requestLocation()
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        MainActor.assumeIsolated {
            status = Self.read(self.manager)
            if features.on("location") { begin() }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
        let c = visit.coordinate, acc = visit.horizontalAccuracy
        let arrival = visit.arrivalDate, departure = visit.departureDate
        MainActor.assumeIsolated { didVisit(c, acc, arrival, departure) }
    }

    private func didVisit(_ c: CLLocationCoordinate2D, _ acc: CLLocationAccuracy, _ arrival: Date, _ departure: Date) {
        guard features.on("location") else { return }
        var e = point(c, acc)
        e["kind"] = "visit"
        if arrival != .distantPast { e["arrive"] = iso.string(from: arrival) }
        if departure != .distantFuture { e["depart"] = iso.string(from: departure) }
        var list = load()
        // The departure comes as the same visit again, now with depart.
        list.removeAll { $0["kind"] as? String == "visit" && $0["arrive"] as? String == e["arrive"] as? String
            && e["arrive"] != nil }
        list.append(e)
        save(list)
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        let fixes = locations.map { ($0.coordinate, $0.horizontalAccuracy, $0.timestamp) }
        MainActor.assumeIsolated { didUpdate(fixes) }
    }

    private func didUpdate(_ fixes: [(CLLocationCoordinate2D, CLLocationAccuracy, Date)]) {
        guard features.on("location") else { return }
        var list = load()
        for (c, acc, ts) in fixes where acc >= 0 {
            var e = point(c, acc)
            e["kind"] = "move"
            e["t"] = iso.string(from: ts)
            // The cached fix comes again at every start.
            if list.contains(where: { $0["kind"] as? String == "move" && $0["t"] as? String == e["t"] as? String }) {
                continue
            }
            list.append(e)
        }
        save(list)
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {}

    private func point(_ c: CLLocationCoordinate2D, _ acc: CLLocationAccuracy) -> [String: Any] {
        ["lat": (c.latitude * 1e6).rounded() / 1e6, "lon": (c.longitude * 1e6).rounded() / 1e6, "acc": Int(acc.rounded())]
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
