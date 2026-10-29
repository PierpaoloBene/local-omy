/// Features turned on and off here or in the Mac app, kept in sync through
/// the Mac (PUT /v1/settings: our values in, the merged ones out). Each value
/// carries when it was changed; the most recent wins.
///
/// The ones applied on the phone are also written to Documents/features.json
/// ({"location": true, …}) for the native side (AppDelegate.swift), which
/// is told to reload it through the local_omy/features channel.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'settings.dart';
import 'l10n.dart';

/// Same keys, names and descriptions as omy_receiver/features.py on the Mac.
final featureInfo = <String, (String device, String name, String description)>{
  'location': (
    'iphone',
    tr('Posizione', 'Location'),
    tr(
      'Salva dove è stata fatta ogni registrazione, con le visite e i cambi di posizione di iOS, senza tenere acceso il GPS.',
      'Saves where each recording was made, from iOS visits and location changes, without keeping the GPS on.',
    ),
  ),
  'calls': (
    'iphone',
    tr('Chiamate', 'Calls'),
    tr(
      'Segna quando sei al telefono, su FaceTime o WhatsApp: solo inizio e fine, non chi chiama.',
      'Marks when you are on the phone, FaceTime or WhatsApp: only start and end, not who is calling.',
    ),
  ),
  'battery': (
    'iphone',
    tr('Batteria del ciondolo', 'Pendant battery'),
    tr(
      'Salva il livello della batteria a ogni segmento, per stimare quanto dura una carica.',
      'Saves the battery level with each segment, to estimate how long a charge lasts.',
    ),
  ),
  'mac_audio': (
    'mac',
    tr('Chiamate e media sul Mac', 'Calls and media on the Mac'),
    tr(
      'Segna quali app usano microfono e altoparlanti, per distinguere una chiamata da un video o dalla musica.',
      'Marks which apps use the microphone and speakers, to tell a call from a video or music.',
    ),
  ),
  'diarization': (
    'mac',
    tr('Chi parla', 'Who speaks'),
    tr(
      'Separa le voci di ogni sessione e riconosce la tua.',
      'Separates the voices of each session and recognizes yours.',
    ),
  ),
  'voice_prints': (
    'mac',
    tr('Impronte vocali', 'Voice prints'),
    tr(
      'Salva l\'impronta delle altre voci, per dar loro un nome e riconoscerle nelle sessioni successive.',
      'Saves a print of the other voices, to name them and recognize them in later sessions.',
    ),
  ),
  'auto_transcribe': (
    'mac',
    tr('Trascrizione automatica', 'Automatic transcription'),
    tr('Trascrive ogni ora le sessioni concluse.', 'Transcribes the finished sessions every hour.'),
  ),
};

class Features extends ChangeNotifier with WidgetsBindingObserver {
  static const _prefsKey = 'features';
  static const _channel = MethodChannel('local_omy/features');
  static const _every = Duration(minutes: 5);

  final SharedPreferences _prefs;
  final Settings _settings;
  final http.Client _client = http.Client();
  final Map<String, ({bool on, String at})> _values;
  Timer? _timer;
  bool _syncing = false;

  DateTime? lastSyncAt;
  String? lastSyncError;

  Features._(this._prefs, this._settings, this._values);

  static Future<Features> load(Settings settings) async {
    final prefs = await SharedPreferences.getInstance();
    final values = <String, ({bool on, String at})>{};
    try {
      final raw = jsonDecode(prefs.getString(_prefsKey) ?? '{}') as Map;
      for (final MapEntry(:key, :value) in raw.entries) {
        if (featureInfo.containsKey(key) && value is Map && value['on'] is bool && value['at'] is String) {
          values[key as String] = (on: value['on'] as bool, at: value['at'] as String);
        }
      }
    } catch (_) {}
    final f = Features._(prefs, settings, values);
    await f._apply();
    return f;
  }

  /// Never changed anywhere: on.
  bool on(String key) => _values[key]?.on ?? true;

  void start() {
    WidgetsBinding.instance.addObserver(this);
    _timer = Timer.periodic(_every, (_) => sync());
    sync();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) sync();
  }

  Future<void> set(String key, bool on) async {
    _values[key] = (on: on, at: DateTime.now().toUtc().toIso8601String());
    await _save();
    await sync();
  }

  /// Send ours, adopt the newer ones from the Mac.
  Future<void> sync() async {
    final base = _settings.macUrl, token = _settings.token;
    if (base == null || token == null || _syncing) return;
    _syncing = true;
    try {
      final res = await _client
          .put(
            Uri.parse('$base/v1/settings'),
            headers: {'Authorization': 'Bearer $token', 'Content-Type': 'application/json'},
            body: jsonEncode({
              'features': {
                for (final MapEntry(:key, :value) in _values.entries) key: {'on': value.on, 'at': value.at},
              },
            }),
          )
          .timeout(const Duration(seconds: 20));
      if (res.statusCode != 200) throw HttpException('HTTP ${res.statusCode}');
      final merged = (jsonDecode(res.body) as Map)['features'] as Map;
      var changed = false;
      for (final MapEntry(:key, :value) in merged.entries) {
        if (!featureInfo.containsKey(key) || value is! Map || value['at'] is! String || value['on'] is! bool) continue;
        final mine = _values[key];
        final at = value['at'] as String;
        if (mine == null || DateTime.parse(at).isAfter(DateTime.parse(mine.at))) {
          _values[key as String] = (on: value['on'] as bool, at: at);
          changed = true;
        }
      }
      if (changed) await _save();
      lastSyncAt = DateTime.now();
      lastSyncError = null;
    } catch (e) {
      lastSyncError = e.toString();
    } finally {
      _syncing = false;
      notifyListeners();
    }
  }

  /// iOS location permission: (auth: always|when_in_use|denied|not_determined,
  /// precise), null if the native side can't say.
  Future<({String auth, bool precise})?> locationStatus() async {
    try {
      final s = await _channel.invokeMapMethod<String, Object>('locationStatus');
      if (s == null) return null;
      return (auth: s['auth'] as String, precise: s['precise'] as bool);
    } catch (e) {
      debugPrint('features: $e');
      return null;
    }
  }

  Future<void> openSettings() => _channel.invokeMethod('openSettings');

  Future<void> _save() async {
    await _prefs.setString(
      _prefsKey,
      jsonEncode({
        for (final MapEntry(:key, :value) in _values.entries) key: {'on': value.on, 'at': value.at},
      }),
    );
    await _apply();
    notifyListeners();
  }

  Future<void> _apply() async {
    try {
      final docs = await getApplicationDocumentsDirectory();
      final tmp = File('${docs.path}/features.json.tmp');
      await tmp.writeAsString(
        jsonEncode({
          for (final e in featureInfo.entries)
            if (e.value.$1 == 'iphone') e.key: on(e.key),
        }),
        flush: true,
      );
      await tmp.rename('${docs.path}/features.json');
      await _channel.invokeMethod('reload');
    } catch (e) {
      debugPrint('features: $e');
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _client.close();
    super.dispose();
  }
}
