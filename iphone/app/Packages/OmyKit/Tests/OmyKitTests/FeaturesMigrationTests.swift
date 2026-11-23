import Foundation
import Testing
@testable import OmyKit

@Suite struct FeaturesTests {
    @Test func defaultsOnAndKeys() {
        let f = Features()
        #expect(Features.keys.allSatisfy { f.on($0) })
        #expect(Features.info.map(\.key) == Features.keys)
        #expect(Features.info.filter { $0.device == "iphone" }.map(\.key) == Features.phoneKeys)
        #expect(Features.info.allSatisfy { !$0.name.isEmpty && !$0.description.isEmpty })
    }

    @Test func lastWriterWins() {
        var f = Features()
        var changed = false
        f.set("calls", false, at: ISO8601UTC.parse("2026-11-23T10:00:00Z")!)
        // older remote value loses
        changed = f.merge(["calls": FeatureValue(on: true, at: "2026-11-23T09:59:59.000Z")])
        #expect(!changed)
        #expect(!f.on("calls"))
        // same timestamp: ours stays
        changed = f.merge(["calls": FeatureValue(on: true, at: "2026-11-23T10:00:00.000Z")])
        #expect(!changed)
        // newer wins, also with a different ISO format
        changed = f.merge(["calls": FeatureValue(on: true, at: "2026-11-23T11:00:00+01:00"), "diarization": FeatureValue(on: false, at: "2026-01-01T00:00:00Z")])
        #expect(changed)
        #expect(f.on("calls") == false)  // 11:00+01:00 = 10:00Z, equal: not newer
        changed = f.merge(["calls": FeatureValue(on: true, at: "2026-11-23T12:00:01+01:00")])
        #expect(changed)
        #expect(f.on("calls") && !f.on("diarization"))
        // unknown keys and bad dates ignored
        changed = f.merge(["nope": FeatureValue(on: false, at: "2030-01-01T00:00:00Z"), "battery": FeatureValue(on: false, at: "garbage")])
        #expect(!changed)
        #expect(f.values.keys.sorted() == ["calls", "diarization"])
    }

    @Test func persistenceRoundTripAndLenientLoad() throws {
        var f = Features()
        f.set("location", false, at: t0)
        f.set("auto_transcribe", true, at: t0)
        let g = Features(json: f.jsonString())
        #expect(g == f)
        let o = try #require(JSONSerialization.jsonObject(with: Data(f.jsonString().utf8)) as? [String: [String: Any]])
        #expect(o["location"]?["on"] as? Bool == false && o["location"]?["at"] as? String == "2023-11-14T22:13:20.123Z")
        let weird = Features(json: #"{"location":{"on":false,"at":"x"},"bogus":{"on":true,"at":"y"},"calls":{"on":1,"at":"z"},"battery":5}"#)
        #expect(weird.values.keys.sorted() == ["location"])
        #expect(Features(json: nil).values.isEmpty && Features(json: "not json").values.isEmpty)
        #expect(Features(values: ["bogus": FeatureValue(on: false, at: "a")]).values.isEmpty)
    }

    @Test func phoneFileHasOnlyPhoneKeys() throws {
        var f = Features()
        f.set("battery", false)
        f.set("mac_audio", false)
        let o = try #require(JSONSerialization.jsonObject(with: f.phoneJSON()) as? [String: Bool])
        #expect(o == ["location": true, "calls": true, "battery": false])
    }

    @Test func storeSetsSyncsAndWritesFeaturesJSON() async throws {
        let tmp = TempDir()
        let settings = SettingsStore(defaults: fakeDefaults(), keychain: FakeKeychain())
        let store = FeaturesStore(settings: settings, documents: tmp.url)
        #expect(ls(tmp.url) == ["features.json"])  // written at start, all on
        let (api, reqs) = FakeNet.make { _ in .json(#"{"features":{"battery":{"on":true,"at":"2099-01-01T00:00:00.000Z"},"mac_audio":{"on":false,"at":"2099-01-01T00:00:00.000Z"}}}"#) }
        await store.set("battery", false, api: api)  // remote has a newer value: it wins
        let f = await store.features
        #expect(f.on("battery") && !f.on("mac_audio"))
        #expect(reqs().count == 1 && settings.featuresJSON == f.jsonString())
        let syncedAt = await store.lastSyncAt
        let syncErr = await store.lastSyncError
        #expect(syncedAt != nil && syncErr == nil)
        let file = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: tmp.url.appendingPathComponent("features.json"))) as? [String: Bool])
        #expect(file == ["location": true, "calls": true, "battery": true])

        let (bad, _) = FakeNet.make { _ in FakeResponse(status: 500) }
        await store.sync(api: bad)
        let failedErr = await store.lastSyncError
        #expect(failedErr != nil)
        await store.sync(api: nil)  // no Mac: nothing happens
        // a new store reads what was saved
        let reloaded = await FeaturesStore(settings: settings, documents: tmp.url).features
        #expect(reloaded == f)
    }
}

@Suite struct MigrationTests {
    private static let flutter = (service: "flutter_secure_storage_service", account: "upload_token", value: "old-token-xyz")

    private func flutterDefaults() -> UserDefaults {
        let d = fakeDefaults()
        d.set("AA:BB:CC", forKey: "flutter.device_id")
        d.set("https://mac.tailnet.ts.net", forKey: "flutter.mac_url")
        d.set(#"{"calls":{"on":false,"at":"2026-11-01T10:00:00.000Z"}}"#, forKey: "flutter.features")
        return d
    }

    @Test func copiesEverythingAndKeepsOldValues() {
        let d = flutterDefaults()
        let kc = FakeKeychain([Self.flutter])
        let r = Migration.fromFlutter(defaults: d, keychain: kc)
        #expect(r == MigrationReport(alreadyDone: false, deferred: false, deviceID: .copied, macURL: .copied, features: .copied, token: .copied))
        let s = SettingsStore(defaults: d, keychain: kc)
        #expect(s.deviceID == "AA:BB:CC" && s.macURL == "https://mac.tailnet.ts.net" && s.token == "old-token-xyz")
        #expect(Features(json: s.featuresJSON).on("calls") == false)
        // the old entries are still there (rollback)
        #expect(d.string(forKey: "flutter.device_id") == "AA:BB:CC")
        #expect((try? kc.read(service: "flutter_secure_storage_service", account: "upload_token")) == "old-token-xyz")
        #expect((try? kc.read(service: "local-omy", account: "upload_token")) == "old-token-xyz")
    }

    @Test func runsOnlyOnce() {
        let d = flutterDefaults()
        let kc = FakeKeychain([Self.flutter])
        _ = Migration.fromFlutter(defaults: d, keychain: kc)
        d.set("changed", forKey: "deviceID")
        d.set("NEW", forKey: "flutter.device_id")
        let r = Migration.fromFlutter(defaults: d, keychain: kc)
        #expect(r.alreadyDone && d.string(forKey: "deviceID") == "changed")
    }

    @Test func existingNewValuesWin() {
        let d = flutterDefaults()
        d.set("NEW-DEVICE", forKey: "deviceID")
        let kc = FakeKeychain([Self.flutter, (service: "local-omy", account: "upload_token", value: "new-token")])
        let r = Migration.fromFlutter(defaults: d, keychain: kc)
        #expect(r.deviceID == .alreadyPresent && r.token == .alreadyPresent && r.macURL == .copied)
        #expect(d.string(forKey: "deviceID") == "NEW-DEVICE" && (try? kc.read(service: "local-omy", account: "upload_token")) == "new-token")
    }

    @Test func freshInstallHasNothingToMigrate() {
        let d = fakeDefaults()
        let r = Migration.fromFlutter(defaults: d, keychain: FakeKeychain())
        #expect(r == MigrationReport(alreadyDone: false, deferred: false, deviceID: .missing, macURL: .missing, features: .missing, token: .missing))
        #expect(d.bool(forKey: Migration.doneKey))
        #expect(SettingsStore(defaults: d, keychain: FakeKeychain()).deviceID == nil)
    }

    @Test func keychainWriteFailureRetriesNextLaunch() {
        let d = flutterDefaults()
        let kc = FakeKeychain([Self.flutter])
        kc.failWrites = true
        #expect(Migration.fromFlutter(defaults: d, keychain: kc).token == .failed)
        #expect(!d.bool(forKey: Migration.doneKey))
        kc.failWrites = false
        let r = Migration.fromFlutter(defaults: d, keychain: kc)
        #expect(r.token == .copied && r.deviceID == .alreadyPresent && d.bool(forKey: Migration.doneKey))
    }

    @Test func skippedWhileProtectedDataUnavailable() {
        let d = flutterDefaults()
        let kc = FakeKeychain([Self.flutter])
        let r = Migration.fromFlutter(defaults: d, keychain: kc, protectedDataAvailable: false)
        #expect(r.deferred && !d.bool(forKey: Migration.doneKey) && d.string(forKey: "deviceID") == nil)
        let r2 = Migration.fromFlutter(defaults: d, keychain: kc, protectedDataAvailable: true)
        #expect(r2.token == .copied && d.bool(forKey: Migration.doneKey))
    }

    @Test func keychainReadFailureRetriesNextLaunch() {
        let d = flutterDefaults()
        let kc = FakeKeychain([Self.flutter])
        kc.failReads = true
        #expect(Migration.fromFlutter(defaults: d, keychain: kc).token == .failed)
        #expect(!d.bool(forKey: Migration.doneKey))
        kc.failReads = false
        let r = Migration.fromFlutter(defaults: d, keychain: kc)
        #expect(r.token == .copied && d.bool(forKey: Migration.doneKey))
    }

    @Test func settingsStoreRoundTrip() throws {
        let s = SettingsStore(defaults: fakeDefaults(), keychain: FakeKeychain())
        s.deviceID = "X"; s.macURL = "https://m.ts.net"
        try s.setToken("tok")
        #expect(s.deviceID == "X" && s.macURL == "https://m.ts.net" && s.token == "tok")
        #expect(s.makeAPI()?.baseURL.absoluteString == "https://m.ts.net" && s.makeAPI()?.token == "tok")
        s.macURL = ""
        s.deviceID = nil
        try s.setToken(nil)
        #expect(s.macURL == nil && s.deviceID == nil && s.token == nil && s.makeAPI() == nil)
        #expect(SettingsStore.service == "local-omy" && SettingsStore.Key.deviceID == "deviceID")
    }
}
