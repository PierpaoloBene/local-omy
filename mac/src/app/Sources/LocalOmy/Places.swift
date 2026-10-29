import MapKit
import Observation
import SwiftUI

// Mirror of `omy --json places list`.

struct PlaceList: Decodable {
    struct Place: Decodable, Identifiable {
        let name: String
        let lat: Double
        let lon: Double
        let radius: Int
        let minutes: Int
        let lastSeen: String?
        var id: String { name }
    }
    struct Spot: Decodable, Identifiable {
        struct Nearest: Decodable { let name: String; let meters: Int }
        let lat: Double
        let lon: Double
        let minutes: Int
        let lastSeen: String?
        let days: Int
        let from: String
        let to: String
        let nearest: Nearest?
        let sample: String
        var id: String { "\(lat),\(lon)" }
    }
    let places: [Place]
    let unnamed: [Spot]
}

/// Addresses of places, from Apple (the phone's positions already come from
/// Apple's location services). In memory only; one request at a time, as
/// Apple throttles them.
@MainActor
@Observable
final class Addresses {
    static let shared = Addresses()
    private var cache: [String: String] = [:]
    @ObservationIgnored private var queue: [(Double, Double)] = []
    @ObservationIgnored private var running = false
    @ObservationIgnored private let geocoder = CLGeocoder()

    /// nil while it loads, "" if Apple has none.
    func of(_ lat: Double, _ lon: Double) -> String? {
        if let a = cache["\(lat),\(lon)"] { return a }
        if !queue.contains(where: { $0 == (lat, lon) }) {
            queue.append((lat, lon))
            Task { self.next() }
        }
        return nil
    }

    private func next() {
        guard !running, let (lat, lon) = queue.first else { return }
        running = true
        geocoder.reverseGeocodeLocation(CLLocation(latitude: lat, longitude: lon),
                                        preferredLocale: Locale(identifier: "it_IT")) { marks, error in
            // A failure (offline, throttled) is not kept: asked again next time it is shown.
            let failed = (error as? CLError).map { $0.code != .geocodeFoundNoResult } ?? false
            let m = marks?.first
            let street = [m?.thoroughfare, m?.subThoroughfare].compactMap { $0 }.joined(separator: " ")
            let text = [street.isEmpty ? m?.name : street, m?.locality].compactMap { $0 }.filter { !$0.isEmpty }
                .joined(separator: ", ")
            Task { @MainActor in
                if !failed { self.cache["\(lat),\(lon)"] = text }
                self.queue.removeFirst()
                self.running = false
                self.next()
            }
        }
    }
}

/// What the map shows: a named place or a spot.
struct MapFocus: Hashable {
    let lat: Double
    let lon: Double
    let radius: Int
    let title: String
}

private struct PlaceMap: View {
    let focus: MapFocus

    var body: some View {
        let c = CLLocationCoordinate2D(latitude: focus.lat, longitude: focus.lon)
        let meters = Double(max(focus.radius * 5, 400))
        Map(initialPosition: .region(MKCoordinateRegion(center: c, latitudinalMeters: meters, longitudinalMeters: meters * 2))) {
            MapCircle(center: c, radius: CLLocationDistance(focus.radius))
                .foregroundStyle(.indigo.opacity(0.15))
                .stroke(.indigo, lineWidth: 1.5)
            Marker(focus.title, coordinate: c).tint(.indigo)
        }
        .id(focus)
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}

private struct AddressLine: View {
    let lat: Double
    let lon: Double

    var body: some View {
        let a = Addresses.shared.of(lat, lon)
        Text(a == nil ? tr("Cerco l'indirizzo…", "Looking up the address…") : a!.isEmpty ? tr("Indirizzo non trovato", "Address not found") : a!)
            .font(.callout)
            .foregroundStyle(a?.isEmpty == false ? .primary : .secondary)
            .lineLimit(1)
    }
}

/// The "Luoghi" tab: names for where you record, from the phone's positions.
/// Every change is an omy command that also rewrites the day transcripts.
@MainActor
@Observable
final class PlacesModel {
    var list: PlaceList?
    var busy: String?
    var error: String?

    func load() async {
        await run(nil) { self.list = try await Omy.decode(PlaceList.self, ["places", "list"]) }
    }

    func add(_ name: String, _ spot: PlaceList.Spot, radius: Int) {
        change(tr("Salvataggio", "Saving"), ["places", "add", name, String(spot.lat), String(spot.lon), "--radius", String(radius)])
    }

    /// Same name and center: `places add` replaces it.
    func setRadius(_ p: PlaceList.Place, _ radius: Int) {
        guard radius != p.radius else { return }
        change(tr("Raggio", "Radius"), ["places", "add", p.name, String(p.lat), String(p.lon), "--radius", String(radius)])
    }

    func rename(_ name: String, to new: String) { change(tr("Rinomina", "Rename"), ["places", "rename", name, new]) }

    func remove(_ name: String) { change(tr("Eliminazione", "Deleting"), ["places", "remove", name]) }

    private func change(_ name: String, _ args: [String]) {
        Task {
            await run(name) { _ = try await Omy.run(args) }
            await load()
        }
    }

    private func run(_ name: String?, _ body: () async throws -> Void) async {
        if let name { busy = name; AppLog.write("[luoghi] \(name)") }
        do {
            try await body()
            error = nil
        } catch {
            self.error = error.localizedDescription
            AppLog.write("[luoghi] failed: \(error.localizedDescription)")
        }
        if name != nil { busy = nil }
    }
}

struct PlacesTab: View {
    let model: AppModel
    @State private var places = PlacesModel()
    @State private var focus: MapFocus?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.features.first(where: { $0.key == "location" })?.on == false {
                Label(tr("Posizione disattivata (Impostazioni, Funzioni): le registrazioni nuove non hanno il luogo.",
                         "Location is off (Settings, Features): new recordings have no place."),
                      systemImage: "info.circle")
                    .foregroundStyle(.secondary)
            }
            HStack {
                if let busy = places.busy {
                    ProgressView().controlSize(.small)
                    Text("\(busy)…")
                }
                if let err = places.error {
                    Label(err, systemImage: "xmark.octagon").foregroundStyle(.red).lineLimit(2).textSelection(.enabled)
                }
            }
            HSplitView {
                NamedPlaces(places: places, focus: $focus)
                    .frame(minWidth: 260, idealWidth: 300, maxWidth: 360)
                VStack(alignment: .leading, spacing: 8) {
                    if let focus {
                        PlaceMap(focus: focus).frame(minHeight: 220, idealHeight: 280)
                        HStack {
                            Text(focus.title).bold()
                            AddressLine(lat: focus.lat, lon: focus.lon)
                        }
                    }
                    UnnamedSpots(places: places, focus: $focus)
                }
                .padding(.leading, 8)
            }
        }
        .disabled(places.busy != nil)
        .task {
            await places.load()
            // Start from the spot to name first.
            if focus == nil, let s = places.list?.unnamed.first {
                focus = MapFocus(lat: s.lat, lon: s.lon, radius: 150, title: tr("Senza nome", "Unnamed"))
            } else if focus == nil, let p = places.list?.places.first {
                focus = MapFocus(lat: p.lat, lon: p.lon, radius: p.radius, title: p.name)
            }
        }
    }
}

private struct NamedPlaces: View {
    let places: PlacesModel
    @Binding var focus: MapFocus?
    @State private var deleting: PlaceList.Place?
    @State private var editing: String?
    @State private var newName = ""

    var body: some View {
        List {
            Section(tr("Luoghi con nome", "Named places")) {
                ForEach(places.list?.places ?? []) { p in
                    HStack {
                        if editing == p.id {
                            TextField(tr("Nome", "Name"), text: $newName)
                                .onSubmit {
                                    let n = newName.trimmingCharacters(in: .whitespaces)
                                    if !n.isEmpty, n != p.name { places.rename(p.name, to: n) }
                                    editing = nil
                                }
                                .onExitCommand { editing = nil }
                        } else {
                            VStack(alignment: .leading) {
                                Text(p.name)
                                AddressLine(lat: p.lat, lon: p.lon)
                                Text(tr("raggio", "radius") + " \(p.radius) m · \(duration(p.minutes))" + (p.lastSeen.map { tr(", ultima ", ", last ") + longDay($0) } ?? ""))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            .contentShape(Rectangle())
                            .onTapGesture { focus = MapFocus(lat: p.lat, lon: p.lon, radius: p.radius, title: p.name) }
                        }
                        Spacer()
                        Menu {
                            Button(tr("Rinomina", "Rename")) { newName = p.name; editing = p.id }
                            Menu(tr("Raggio", "Radius")) {
                                ForEach([50, 100, 150, 300, 500, 1000], id: \.self) { r in
                                    Button("\(r) m" + (r == p.radius ? " ✓" : "")) { places.setRadius(p, r) }
                                }
                            }
                            Button(tr("Elimina…", "Delete…"), role: .destructive) { deleting = p }
                        } label: { Image(systemName: "ellipsis.circle") }
                            .menuStyle(.borderlessButton).fixedSize()
                    }
                }
                if places.list?.places.isEmpty == true {
                    Text(tr("Nessuno: dai un nome a un punto qui accanto.", "None: name a spot on the right.")).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
        .confirmationDialog(
            tr("Eliminare \(deleting?.name ?? "")?", "Delete \(deleting?.name ?? "")?"),
            isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })
        ) {
            Button(tr("Elimina", "Delete"), role: .destructive) {
                if let p = deleting { places.remove(p.name) }
            }
        } message: {
            Text(tr("Viene cancellato solo il nome: le posizioni restano, e nelle trascrizioni tornano coordinate.", "Only the name is deleted: the positions stay, and the transcripts show coordinates again."))
        }
    }
}

/// Where you recorded without a name, most time first.
private struct UnnamedSpots: View {
    let places: PlacesModel
    @Binding var focus: MapFocus?

    var body: some View {
        if let list = places.list {
            if list.unnamed.isEmpty {
                Text(tr("Nessun punto senza nome. Le posizioni arrivano dall'iPhone con i segmenti.", "No unnamed spots. Positions come from the iPhone with the segments."))
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                List {
                    Section(tr("Dove hai registrato, senza un nome", "Where you recorded, without a name")) {
                        ForEach(list.unnamed) { SpotRow(places: places, spot: $0, focus: $focus) }
                    }
                }
            }
        } else {
            ProgressView()
            Spacer()
        }
    }
}

private struct SpotRow: View {
    let places: PlacesModel
    let spot: PlaceList.Spot
    @Binding var focus: MapFocus?
    @State private var name = ""
    @State private var radius = 150

    private var shown: Bool { focus?.lat == spot.lat && focus?.lon == spot.lon }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Button {
                    focus = MapFocus(lat: spot.lat, lon: spot.lon, radius: radius, title: name.isEmpty ? tr("Senza nome", "Unnamed") : name)
                } label: { Image(systemName: shown ? "map.fill" : "map") }
                    .buttonStyle(.borderless)
                    .help(tr("Mostra sulla mappa", "Show on the map"))
                AddressLine(lat: spot.lat, lon: spot.lon)
            }
            HStack {
                TextField(tr("Nome", "Name"), text: $name)
                    .onSubmit(submit)
                    .frame(maxWidth: 200)
                Picker(tr("Raggio", "Radius"), selection: $radius) {
                    ForEach([50, 100, 150, 300, 500], id: \.self) { Text("\($0) m").tag($0) }
                }
                .fixedSize()
                .onChange(of: radius) {
                    if shown { focus = MapFocus(lat: spot.lat, lon: spot.lon, radius: radius, title: focus?.title ?? "") }
                }
            }
            Text(details).font(.caption).foregroundStyle(.secondary)
            if !spot.sample.isEmpty {
                Text("«\(spot.sample)»").font(.caption).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
            }
        }
    }

    private var details: String {
        var parts = [duration(spot.minutes),
                     spot.days == 1 ? tr("1 giorno", "1 day") : "\(spot.days) " + tr("giorni", "days"),
                     spot.from == spot.to ? tr("alle \(spot.from)", "at \(spot.from)")
                         : tr("tra le \(spot.from) e le \(spot.to)", "between \(spot.from) and \(spot.to)")]
        if let d = spot.lastSeen { parts.append(tr("ultima ", "last ") + longDay(d)) }
        if let n = spot.nearest {
            let distance = n.meters < 1000 ? "\(n.meters) m" : String(format: "%.1f km", Double(n.meters) / 1000)
            parts.append(tr("a \(distance) da \(n.name)", "\(distance) from \(n.name)"))
        }
        return parts.joined(separator: " · ")
    }

    private func submit() {
        let n = name.trimmingCharacters(in: .whitespaces)
        guard !n.isEmpty else { return }
        // A place has one center: the same name would move it here.
        if places.list?.places.contains(where: { $0.name.caseInsensitiveCompare(n) == .orderedSame }) == true {
            places.error = tr("\(n) esiste già: scegli un altro nome, o allarga il raggio di \(n)", "\(n) already exists: choose another name, or widen the radius of \(n)")
            return
        }
        places.add(n, spot, radius: radius)
    }
}

private func duration(_ minutes: Int) -> String {
    minutes < 1 ? "<1 min" : minutes < 120 ? "\(minutes) min" : "\(minutes / 60) h"
}
