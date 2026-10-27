import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Pendant id and Mac URL in shared preferences, upload token in the Keychain.
class Settings {
  final SharedPreferences _prefs;
  Settings._(this._prefs, this._token);

  // first_unlock: the token must be readable while the phone is locked, when
  // uploads run in background after a BLE wake-up.
  static const _secure = FlutterSecureStorage(
    iOptions: IOSOptions(accessibility: KeychainAccessibility.first_unlock_this_device),
  );

  static Future<Settings> load() async {
    final prefs = await SharedPreferences.getInstance();
    String? token;
    try {
      token = await _secure.read(key: 'upload_token');
    } catch (_) {}
    return Settings._(prefs, token);
  }

  String? get deviceId => _prefs.getString('device_id');
  set deviceId(String? v) => v == null ? _prefs.remove('device_id') : _prefs.setString('device_id', v);

  /// e.g. https://mymac.tailnet-name.ts.net (no trailing slash).
  String? get macUrl => _prefs.getString('mac_url');
  set macUrl(String? v) => v == null || v.isEmpty ? _prefs.remove('mac_url') : _prefs.setString('mac_url', v);

  String? _token;
  String? get token => _token;
  Future<void> setToken(String? v) async {
    _token = v == null || v.isEmpty ? null : v;
    if (_token == null) {
      await _secure.delete(key: 'upload_token');
    } else {
      await _secure.write(key: 'upload_token', value: _token);
    }
  }
}
