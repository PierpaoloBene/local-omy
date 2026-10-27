import 'package:shared_preferences/shared_preferences.dart';

/// Non-secret settings.
class Settings {
  final SharedPreferences _prefs;
  Settings._(this._prefs);

  static Future<Settings> load() async => Settings._(await SharedPreferences.getInstance());

  String? get deviceId => _prefs.getString('device_id');
  set deviceId(String? v) => v == null ? _prefs.remove('device_id') : _prefs.setString('device_id', v);
}
