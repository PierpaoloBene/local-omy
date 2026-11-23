import CoreLocation
import MapKit
import OmyKit
import SwiftUI

private let placeRadii = [50, 100, 150, 300, 500, 1000]

/// "Luoghi": names for where you record, from the phone's positions, on Apple's maps to recognize them.
struct PlacesView: View {
    @Environment(AppModel.self) private var model
    @Environment(TabReselect.self) private var reselect

    @State private var places: Places?
    @State private var error: String?
    @State private var busy = false
    @State private var alert: ConvAlertMessage?
    @State private var confirm: ConvConfirm?
    @State private var naming: Spot?
    @State private var renaming: NamedPlace?
    @State private var renameText = ""
    @State private var loaded = false
    @State private var topTick = 0

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                List {
                    Color.clear.frame(height: 0).id("top").listRowInsets(.init()).listRowBackground(Color.clear)
                    if let p = places { content(p) } else {
                        ConvLoadState(loading: error == nil, error: error) { Task { await load() } }.listRowBackground(Color.clear)
                    }
                }
                .onChange(of: reselect.count(for: .places)) { withAnimation { proxy.scrollTo("top") } }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
            .navigationTitle(AppTab.places.title)
            .toolbar { if busy { ToolbarItem(placement: .topBarTrailing) { ProgressView() } } }
            .convAlert($alert)
            .convConfirm($confirm)
            .sheet(item: $naming) { s in
                PlaceNameSheet(spot: s) { name, radius in Task { await add(spot: s, name: name, radius: radius) } }
            }
            .alert(tr("Rinomina", "Rename"), isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
                TextField(tr("Nome", "Name"), text: $renameText).textInputAutocapitalization(.words)
                Button(tr("Annulla", "Cancel"), role: .cancel) {}
                Button(tr("Salva", "Save")) { if let p = renaming { Task { await rename(p, to: renameText.trimmingCharacters(in: .whitespaces)) } } }
            }
        }
        .task(id: model.apiGeneration) { await load(); loaded = true }
        .onAppear { if loaded { Task { await load() } } }
    }

    @ViewBuilder private func content(_ p: Places) -> some View {
        if let error { Section { ConvNotice(icon: "exclamationmark.circle", text: tr("Non aggiornato: \(error)", "Not up to date: \(error)")) }.listRowBackground(Color.clear) }
        if !model.isOn("location") {
            Section {
                ConvNotice(icon: "info.circle.fill", text: tr("Posizione disattivata: le registrazioni nuove non hanno il luogo (Impostazioni, Funzioni).", "Location is off: new recordings have no place (Settings, Features)."))
            }.listRowBackground(Color.clear).listRowInsets(.init())
        }
        Section(tr("Con un nome", "Named")) {
            if p.places.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text(tr("Nessuno", "None"))
                    Text(tr("Dai un nome a uno dei punti qui sotto.", "Name one of the spots below.")).font(.footnote).foregroundStyle(.secondary)
                }
            }
            ForEach(p.places, id: \.name) { place in placeRow(place, p) }
        }
        Section {
            if p.unnamed.isEmpty {
                ConvEmpty(tr("Nessun punto senza nome. Le posizioni arrivano con i segmenti.", "No unnamed spots. Positions arrive with the segments."))
            }
            ForEach(Array(p.unnamed.enumerated()), id: \.offset) { _, s in spotCard(s) }
        } header: {
            Text(tr("Dove hai registrato, senza un nome", "Where you recorded, without a name"))
        }
    }

    private func placeRow(_ p: NamedPlace, _ all: Places) -> some View {
        Menu {
            Button(tr("Rinomina", "Rename"), systemImage: "pencil") { renameText = p.name; renaming = p }
            Menu(tr("Cambia raggio", "Change radius"), systemImage: "circle.dashed") {
                ForEach(placeRadii, id: \.self) { r in
                    Button(r == p.radius ? "\(r) m ✓" : "\(r) m") { if r != p.radius { Task { await change { try await $0.addPlace(name: p.name, lat: p.lat, lon: p.lon, radius: r) } } } }
                }
            }
            Button(tr("Elimina", "Delete"), systemImage: "trash", role: .destructive) {
                confirm = ConvConfirm(
                    title: tr("Eliminare \(p.name)?", "Delete \(p.name)?"),
                    message: tr("Si cancella solo il nome: le posizioni restano, e nelle trascrizioni tornano coordinate.", "Only the name is deleted: the positions stay, and the transcripts show coordinates again.")
                ) { Task { await change { try await $0.removePlace(p.name) } } }
            }
        } label: {
            HStack(spacing: 14) {
                PlaceMap(lat: p.lat, lon: p.lon, radius: p.radius, name: p.name).frame(width: 52, height: 52).clipShape(RoundedRectangle(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 2) {
                    Text(p.name).font(.headline).foregroundStyle(.primary)
                    PlaceAddress(lat: p.lat, lon: p.lon).font(.footnote).foregroundStyle(.secondary)
                    Text("\(tr("Raggio", "Radius")) \(p.radius) m · \(convDuration(Double(p.minutes)))\(p.lastSeen.map { " · \(tr("ultima", "last")) \(convShortDay($0))" } ?? "")")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                Image(systemName: "ellipsis.circle").foregroundStyle(Color.accentColor)
            }
            .contentShape(Rectangle())
        }
        .disabled(busy)
    }

    private func spotCard(_ s: Spot) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            PlaceMap(lat: s.lat, lon: s.lon, radius: 150).frame(height: 150)
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    PlaceAddress(lat: s.lat, lon: s.lon, fallback: tr("Indirizzo non trovato", "Address not found")).font(.headline)
                    Text("\(convDuration(Double(s.minutes))) \(tr("in", "in")) \(counted(s.days, "giorno", "giorni", "day", "days")) · \(placeDetails(s))").font(.footnote).foregroundStyle(.secondary)
                    if !s.sample.isEmpty { Text("«\(s.sample)»").font(.footnote).italic().foregroundStyle(.secondary).lineLimit(2).padding(.top, 2) }
                }
                Spacer(minLength: 8)
                Button(tr("Nomina", "Name it")) { naming = s }.buttonStyle(.borderless).disabled(busy)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
        }
        .listRowInsets(.init())
    }

    // MARK: changes

    private func load() async {
        guard let api = model.api else { error = tr("Mac non configurato.", "Mac not set up."); return }
        error = nil
        do { places = try await api.places() } catch is CancellationError {} catch { self.error = convMessage(error) }
    }

    private func change(_ body: (MacAPI) async throws -> Void) async {
        guard let api = model.api else { return }
        if await convChange(busy: $busy, alert: $alert, { try await body(api) }) { await load() }
    }

    private func taken(_ name: String, except: String? = nil) -> Bool {
        places?.places.contains { $0.name.lowercased() == name.lowercased() && $0.name.lowercased() != except?.lowercased() } ?? false
    }

    private func add(spot s: Spot, name: String, radius: Int) async {
        guard !name.isEmpty else { return }
        try? await Task.sleep(for: .milliseconds(450))
        // A place has one center: the same name would move it here.
        if taken(name) {
            alert = ConvAlertMessage(title: tr("\(name) esiste già", "\(name) already exists"), message: tr("Scegli un altro nome, o allarga il raggio di \(name).", "Choose another name, or widen the radius of \(name)."))
            return
        }
        await change { try await $0.addPlace(name: name, lat: s.lat, lon: s.lon, radius: radius) }
    }

    private func rename(_ p: NamedPlace, to n: String) async {
        guard !n.isEmpty, n != p.name else { return }
        if taken(n, except: p.name) {
            alert = ConvAlertMessage(title: tr("\(n) esiste già", "\(n) already exists"), message: tr("Scegli un altro nome.", "Choose another name."))
            return
        }
        await change { try await $0.renamePlace(p.name, to: n) }
    }
}

extension Spot: @retroactive Identifiable { public var id: String { "\(lat),\(lon)" } }

private func placeDetails(_ s: Spot) -> String {
    ([s.from == s.to ? tr("alle \(s.from)", "at \(s.from)") : tr("tra le \(s.from) e le \(s.to)", "between \(s.from) and \(s.to)")]
        + [s.lastSeen.map { "\(tr("ultima", "last")) \(convShortDay($0))" }]
        + [s.nearest.map { tr("a \(convMeters($0.meters)) da \($0.name)", "\(convMeters($0.meters)) from \($0.name)") }]).compactMap { $0 }.joined(separator: " · ")
}

// MARK: maps

/// A small non-interactive map with the circle of a place's radius.
struct PlaceMap: View {
    let lat: Double
    let lon: Double
    let radius: Int
    var name: String?

    var body: some View {
        let c = CLLocationCoordinate2D(latitude: lat, longitude: lon)
        Map(initialPosition: .region(MKCoordinateRegion(center: c, latitudinalMeters: max(Double(radius) * 3.6, 260), longitudinalMeters: max(Double(radius) * 3.6, 260))), interactionModes: []) {
            MapCircle(center: c, radius: CLLocationDistance(radius)).foregroundStyle(Color.accentColor.opacity(0.22)).stroke(Color.accentColor, lineWidth: 1.5)
            Annotation("", coordinate: c) { Circle().fill(Color.accentColor).frame(width: 9, height: 9).overlay(Circle().stroke(.white, lineWidth: 2)) }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// The address of a spot, gray while it loads.
struct PlaceAddress: View {
    let lat: Double
    let lon: Double
    var fallback = ""
    @State private var text: String?
    @State private var done = false

    var body: some View {
        Text(text ?? (done ? fallback : tr("Cerco l'indirizzo…", "Looking up the address…"))).lineLimit(2)
            .task(id: "\(lat),\(lon)") {
                text = await PlaceGeocoder.shared.address(lat: lat, lon: lon)
                done = true
            }
    }
}

/// "Via Roma 12, Milano" in Italian, one request at a time (Apple throttles), cached in memory.
actor PlaceGeocoder {
    static let shared = PlaceGeocoder()
    private var cache: [String: String?] = [:]
    private var tail: Task<Void, Never>?

    func address(lat: Double, lon: Double) async -> String? {
        let key = String(format: "%.5f,%.5f", lat, lon)
        if let hit = cache[key] { return hit }
        let prev = tail
        let t = Task<String??, Never> {
            _ = await prev?.value
            return await Self.reverse(lat: lat, lon: lon)
        }
        tail = Task { _ = await t.value }
        let r = await t.value
        // A failure (offline, throttled) is not cached: it is asked again later.
        if let r { cache[key] = .some(r); return r }
        return nil
    }

    /// `.some(nil)`: Apple has no address; `nil`: the request failed.
    private static func reverse(lat: Double, lon: Double) async -> String?? {
        guard let request = MKReverseGeocodingRequest(location: CLLocation(latitude: lat, longitude: lon)) else { return nil }
        request.preferredLocale = Locale(identifier: "it_IT")
        do {
            let items = try await request.mapItems
            let m = items.first
            let street = m?.address?.shortAddress ?? m?.name
            var city = m?.addressRepresentations?.cityName
            if let c = city, let st = street, st.localizedCaseInsensitiveContains(c) { city = nil }
            let parts = [street, city].compactMap { $0 }.filter { !$0.isEmpty }
            return .some(parts.isEmpty ? nil : parts.joined(separator: ", "))
        } catch let e as MKError where e.code == .placemarkNotFound {
            return .some(nil)
        } catch {
            return nil
        }
    }
}

/// Name and radius for a spot, with the circle previewed on the map.
struct PlaceNameSheet: View {
    let spot: Spot
    let onSave: (String, Int) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var radius = 150

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    PlaceMap(lat: spot.lat, lon: spot.lon, radius: radius).frame(height: 200)
                        .listRowInsets(.init())
                    VStack(alignment: .leading, spacing: 2) {
                        PlaceAddress(lat: spot.lat, lon: spot.lon).font(.headline)
                        Text(placeDetails(spot)).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                Section { TextField(tr("Casa, Ufficio, Palestra…", "Home, Office, Gym…"), text: $name).textInputAutocapitalization(.sentences) }
                Section {
                    Picker(tr("Raggio", "Radius"), selection: $radius) { ForEach(placeRadii.dropLast(), id: \.self) { Text("\($0) m").tag($0) } }
                        .pickerStyle(.segmented)
                } header: { Text(tr("Raggio", "Radius").uppercased()) } footer: {
                    Text(tr("Le registrazioni entro questa distanza dal punto prendono il nome del luogo.", "Recordings within this distance of the spot take the name of the place."))
                }
            }
            .navigationTitle(tr("Nuovo luogo", "New place")).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button(tr("Annulla", "Cancel")) { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(tr("Salva", "Save")) { onSave(name.trimmingCharacters(in: .whitespaces), radius); dismiss() }
                        .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }
}
