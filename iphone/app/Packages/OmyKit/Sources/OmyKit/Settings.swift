import Foundation
import Security

public protocol KeychainStore: Sendable {
    /// nil = definitively not there (errSecItemNotFound); any other failure throws.
    func read(service: String, account: String) throws -> String?
    func write(_ v: String, service: String, account: String) throws
    /// Removes the item (no error if missing). Default: does nothing.
    func delete(service: String, account: String) throws
}

extension KeychainStore {
    public func delete(service: String, account: String) throws {}
}

public struct KeychainError: Error, Sendable, Equatable { public let status: Int32 }

/// The real Keychain (generic passwords, value = UTF-8 bytes, as flutter_secure_storage 9.x stores them: kSecClass
/// GenericPassword, kSecAttrService = accountName option (default "flutter_secure_storage_service"), kSecAttrAccount =
/// the key, kSecValueData = utf8). New items are AfterFirstUnlockThisDeviceOnly: readable while the phone is locked,
/// when uploads run after a BLE wake-up.
public struct SystemKeychain: KeychainStore {
    public init() {}

    private func base(_ service: String, _ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }

    public func read(service: String, account: String) throws -> String? {
        var q = base(service, account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        q[kSecAttrSynchronizable as String] = kSecAttrSynchronizableAny  // flutter_secure_storage also tries synchronizable
        var out: CFTypeRef?
        let st = SecItemCopyMatching(q as CFDictionary, &out)
        if st == errSecItemNotFound { return nil }
        guard st == errSecSuccess else { throw KeychainError(status: st) }
        guard let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    public func write(_ v: String, service: String, account: String) throws {
        let data = Data(v.utf8)
        let update = SecItemUpdate(base(service, account) as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if update == errSecSuccess { return }
        var add = base(service, account)
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let s = SecItemAdd(add as CFDictionary, nil)
        if s != errSecSuccess { throw KeychainError(status: s) }
    }

    public func delete(service: String, account: String) throws {
        let s = SecItemDelete(base(service, account) as CFDictionary)
        if s != errSecSuccess && s != errSecItemNotFound { throw KeychainError(status: s) }
    }
}

/// Pendant id, Mac URL and features in UserDefaults; the upload token in the Keychain.
public final class SettingsStore: @unchecked Sendable {
    public static let service = "local-omy"
    public static let tokenAccount = "upload_token"
    public enum Key {
        public static let deviceID = "deviceID", macURL = "macURL", features = "features"
    }

    public let defaults: UserDefaults
    public let keychain: KeychainStore

    public init(defaults: UserDefaults = .standard, keychain: KeychainStore = SystemKeychain()) {
        self.defaults = defaults
        self.keychain = keychain
    }

    public var deviceID: String? {
        get { defaults.string(forKey: Key.deviceID) }
        set { if let v = newValue { defaults.set(v, forKey: Key.deviceID) } else { defaults.removeObject(forKey: Key.deviceID) } }
    }

    /// e.g. https://mymac.tailnet-name.ts.net (no trailing slash).
    public var macURL: String? {
        get { defaults.string(forKey: Key.macURL) }
        set {
            if let v = newValue, !v.isEmpty { defaults.set(v, forKey: Key.macURL) } else { defaults.removeObject(forKey: Key.macURL) }
        }
    }

    /// `{key: {on, at}}` as JSON (see `Features`).
    public var featuresJSON: String? {
        get { defaults.string(forKey: Key.features) }
        set { if let v = newValue { defaults.set(v, forKey: Key.features) } else { defaults.removeObject(forKey: Key.features) } }
    }

    public var token: String? { try? keychain.read(service: Self.service, account: Self.tokenAccount) }

    public func setToken(_ v: String?) throws {
        if let v, !v.isEmpty { try keychain.write(v, service: Self.service, account: Self.tokenAccount) } else {
            try keychain.delete(service: Self.service, account: Self.tokenAccount)
        }
    }

    /// A `MacAPI` for the current URL and token, nil if either is missing or the URL is not valid.
    public func makeAPI(session: URLSession = .shared) -> MacAPI? {
        guard let u = macURL, let url = URL(string: u), let t = token, !t.isEmpty else { return nil }
        return MacAPI(baseURL: url, token: t, session: session)
    }
}

public struct MigrationReport: Sendable, Equatable {
    public enum Item: Sendable, Equatable { case copied, alreadyPresent, missing, failed }
    public var alreadyDone = false
    /// Not run because protected data was unavailable (device locked since boot); retried at the next launch.
    public var deferred = false
    public var deviceID: Item = .missing
    public var macURL: Item = .missing
    public var features: Item = .missing
    public var token: Item = .missing
}

public enum Migration {
    public static let doneKey = "migratedFromFlutter"
    public static let flutterService = "flutter_secure_storage_service"

    /// Once, at launch. Copies (never deletes: the old values stay, so a rollback to the Flutter app works):
    /// UserDefaults `flutter.device_id` / `flutter.mac_url` / `flutter.features` -> `deviceID` / `macURL` / `features`;
    /// Keychain service `flutter_secure_storage_service` account `upload_token` -> service `local-omy`. Values
    /// already present in the new keys win. If the token could not be read or written (or protected data is unavailable) the migration is retried next launch.
    @discardableResult
    public static func fromFlutter(defaults: UserDefaults, keychain: KeychainStore, protectedDataAvailable: Bool = true) -> MigrationReport {
        var r = MigrationReport()
        // Before the first unlock (background relaunch) the Keychain and defaults cannot be read: try again later.
        guard protectedDataAvailable else {
            r.deferred = true
            return r
        }
        if defaults.bool(forKey: doneKey) {
            r.alreadyDone = true
            return r
        }
        func copy(_ old: String, _ new: String) -> MigrationReport.Item {
            if let cur = defaults.string(forKey: new), !cur.isEmpty { return .alreadyPresent }
            guard let v = defaults.string(forKey: old), !v.isEmpty else { return .missing }
            defaults.set(v, forKey: new)
            return .copied
        }
        r.deviceID = copy("flutter.device_id", SettingsStore.Key.deviceID)
        r.macURL = copy("flutter.mac_url", SettingsStore.Key.macURL)
        r.features = copy("flutter.features", SettingsStore.Key.features)
        do {
            if let t = try keychain.read(service: SettingsStore.service, account: SettingsStore.tokenAccount), !t.isEmpty {
                r.token = .alreadyPresent
            } else if let old = try keychain.read(service: flutterService, account: SettingsStore.tokenAccount), !old.isEmpty {
                try keychain.write(old, service: SettingsStore.service, account: SettingsStore.tokenAccount)
                r.token = .copied
            }
        } catch {
            r.token = .failed
        }
        if r.token != .failed { defaults.set(true, forKey: doneKey) }
        return r
    }
}
