/// The Mac's review of transcripts, people and places (omy_receiver/app.py),
/// same URL and token as the uploads. Nothing fetched is stored on the
/// phone: pages keep it in memory while open.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import '../settings.dart';
import 'models.dart';
import '../l10n.dart';

/// Today in the archive's time zone (the Mac's), from the last GET
/// /v1/transcripts: the days of the conversations are the Mac's days,
/// wherever the phone is.
String? archiveToday;

/// The events of a text/event-stream: one JSON per `data: {...}` line, in
/// chunks cut anywhere. Other lines (comments, keep-alives) and events of
/// types this app doesn't know are skipped.
Stream<AskEvent> parseAskStream(Stream<List<int>> bytes) async* {
  await for (final line in bytes.transform(utf8.decoder).transform(const LineSplitter())) {
    if (!line.startsWith('data:')) continue;
    final data = line.substring(5).trim();
    if (data.isEmpty) continue;
    final AskEvent? e;
    try {
      e = AskEvent.fromJson(jsonDecode(data) as Json);
    } catch (_) {
      continue;
    }
    if (e != null) yield e;
  }
}

/// 2026-11-23T14:30:00+01:00: the phone's time with its offset.
String isoWithOffset(DateTime t) {
  String two(int n) => n.toString().padLeft(2, '0');
  final o = t.timeZoneOffset, a = o.abs();
  return '${t.year.toString().padLeft(4, '0')}-${two(t.month)}-${two(t.day)}T${two(t.hour)}:${two(t.minute)}:${two(t.second)}'
      '${o.isNegative ? '-' : '+'}${two(a.inHours)}:${two(a.inMinutes % 60)}';
}

class MacError implements Exception {
  final String message;
  const MacError(this.message);
  @override
  String toString() => message;
}

class MacApi {
  static const _read = Duration(seconds: 30);
  // A change rewrites every day with that person or place.
  static const _change = Duration(minutes: 5);
  /// No byte for this long in an answer: the Mac is stuck.
  static const askIdle = Duration(seconds: 90);
  static const _audio = MethodChannel('local_omy/audio');

  final Settings _settings;
  final http.Client _client;

  MacApi(this._settings, {http.Client? client}) : _client = client ?? http.Client();

  bool get configured => _settings.macUrl != null && _settings.token != null;

  Future<DayList> days() async {
    final d = await _get('/v1/transcripts', DayList.fromJson);
    archiveToday = d.today;
    return d;
  }

  Future<Day> day(String day) => _get('/v1/transcripts/$day', Day.fromJson);

  /// Paragraphs with [q], only in the conversations with [person] and of [type].
  Future<SearchResult> search(String q, {String? person, String? type}) async => _parse(
    (await _request('POST', '/v1/search', body: {'q': q, 'person': ?person, 'type': ?type}, timeout: _read)),
    SearchResult.fromJson,
  );

  /// The sessions with [person] (an id or 'me'), of [type] (room, meeting,
  /// call, media), of [day] or of the days [before] one (the next page),
  /// newest first: whole days, at least [limit] (Mac: 100).
  Future<ConversationList> conversations({String? person, String? type, String? day, String? before, int? limit}) =>
      _get('/v1/conversations', ConversationList.fromJson, {
        'person': ?person,
        'type': ?type,
        'day': ?day,
        'before': ?before,
        'limit': ?limit?.toString(),
      });

  /// The sessions started in the last [hours] with voices still to label.
  Future<Unlabeled> unlabeled({required int hours}) => _get('/v1/unlabeled', Unlabeled.fromJson, {'hours': '$hours'});

  /// The note of [session]; null if the Mac has none.
  Future<Note?> note(String session) async {
    final base = _settings.macUrl, token = _settings.token;
    if (base == null || token == null) throw MacError(_notConfigured);
    final http.Response res;
    try {
      res = await http.Response.fromStream(
        await _client
            .send(
              http.Request('GET', Uri.parse('$base/v1/brain/note/${Uri.encodeComponent(session)}'))
                ..headers['Authorization'] = 'Bearer $token',
            )
            .timeout(_read),
      ).timeout(_read);
    } on SocketException {
      throw MacError(_unreachable);
    } on http.ClientException {
      throw MacError(_unreachable);
    } on Exception {
      throw MacError(tr('Il Mac non ha risposto in tempo', 'The Mac did not answer in time'));
    }
    if (res.statusCode == 404) return null;
    if (res.statusCode != 200) throw MacError(_statusError(res.statusCode));
    return _parse(res, Note.fromJson);
  }

  /// Asks the archive: the answer streams as events (status, citations,
  /// text, done). [history]: the last turns of the chat (at most 8 are sent).
  Stream<AskEvent> ask(String question, List<AskTurn> history, {Duration idle = askIdle}) async* {
    final base = _settings.macUrl, token = _settings.token;
    if (base == null || token == null) throw MacError(_notConfigured);
    final req = http.Request('POST', Uri.parse('$base/v1/ask'))
      ..headers['Authorization'] = 'Bearer $token'
      ..headers['Content-Type'] = 'application/json'
      ..headers['Accept'] = 'text/event-stream'
      ..body = jsonEncode({
        'question': question,
        'history': [for (final t in history.length > 8 ? history.sublist(history.length - 8) : history) t.toJson()],
        'now': isoWithOffset(DateTime.now()),
      });
    final http.StreamedResponse res;
    try {
      res = await _client.send(req).timeout(_read);
    } on SocketException {
      throw MacError(_unreachable);
    } on http.ClientException {
      throw MacError(_unreachable);
    } on Exception {
      throw MacError(tr('Il Mac non ha risposto in tempo', 'The Mac did not answer in time'));
    }
    if (res.statusCode != 200) throw MacError(_statusError(res.statusCode));
    try {
      yield* parseAskStream(res.stream.timeout(idle));
    } on TimeoutException {
      throw MacError(tr('Il Mac ha smesso di rispondere', 'The Mac stopped answering'));
    } on http.ClientException {
      throw MacError(_unreachable);
    }
  }

  Future<void> transcribeNow() => _send('POST', '/v1/transcribe');

  Future<People> people() => _get('/v1/people', People.fromJson);
  Future<void> renamePerson(String id, String name) => _send('PUT', '/v1/people/$id', {'name': name});
  Future<void> deletePerson(String id) => _send('DELETE', '/v1/people/$id');

  /// Where all the voices of a person come from: room, meeting, auto, or
  /// media (it was a video: the name and voice print go).
  Future<void> setPersonKind(String id, String source) => _send('POST', '/v1/people/$id/kind', {'source': source});

  Future<DayVoices> voices(String day) => _get('/v1/voices/$day', DayVoices.fromJson);
  Future<void> nameVoice(String session, String label, String name) =>
      _send('PUT', '/v1/voices/$session/$label', {'name': name});

  /// Where these voices come from: room, meeting, media, or auto.
  Future<void> setVoiceKind(String session, List<String> labels, String source) =>
      _send('POST', '/v1/voices/$session/kind', {'source': source, 'labels': labels});

  /// Somebody you don't know: no name, no voice print, not asked again.
  Future<void> setVoiceUnknown(String session, List<String> labels) =>
      _send('POST', '/v1/voices/$session/unknown', {'labels': labels, 'unknown': true});

  /// Plays a few seconds of the voice; returns how many.
  Future<double> playVoice(String session, String label) async {
    final res = await _request('GET', '/v1/voices/$session/$label/sample', timeout: _read);
    return await _audio.invokeMethod<double>('play', res.bodyBytes) ?? 0;
  }

  Future<void> stopVoice() => _audio.invokeMethod('stop');

  Future<Places> places() => _get('/v1/places', Places.fromJson);
  Future<void> addPlace(String name, double lat, double lon, int radius) =>
      _send('POST', '/v1/places', {'name': name, 'lat': lat, 'lon': lon, 'radius': radius});
  Future<void> renamePlace(String name, String to) =>
      _send('PUT', '/v1/places/${Uri.encodeComponent(name)}', {'name': to});
  Future<void> removePlace(String name) => _send('DELETE', '/v1/places/${Uri.encodeComponent(name)}');

  Future<T> _get<T>(String path, T Function(Json) parse, [Map<String, String>? query]) async =>
      _parse(await _request('GET', path, query: query, timeout: _read), parse);

  /// An answer the phone doesn't understand (the two apps at different
  /// versions) is an error to show, not a page loading forever.
  static T _parse<T>(http.Response res, T Function(Json) parse) {
    try {
      return parse(jsonDecode(utf8.decode(res.bodyBytes)) as Json);
    } catch (_) {
      throw MacError(
        tr(
          'Risposta del Mac non valida: aggiorna l\'app sul Mac e sull\'iPhone',
          'The Mac\'s answer is not valid: update the app on the Mac and on the iPhone',
        ),
      );
    }
  }

  Future<void> _send(String method, String path, [Map<String, Object>? body]) =>
      _request(method, path, body: body, timeout: _change);

  Future<http.Response> _request(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, Object>? body,
    required Duration timeout,
  }) async {
    final base = _settings.macUrl, token = _settings.token;
    if (base == null || token == null)
      throw MacError(
        tr('Mac non configurato: URL e token in Impostazioni', 'Mac not set up: URL and token in Settings'),
      );
    final req = http.Request(method, Uri.parse('$base$path').replace(queryParameters: query))
      ..headers['Authorization'] = 'Bearer $token';
    if (body != null) {
      req.headers['Content-Type'] = 'application/json';
      req.body = jsonEncode(body);
    }
    final http.Response res;
    try {
      res = await http.Response.fromStream(await _client.send(req).timeout(timeout)).timeout(timeout);
    } on SocketException {
      throw MacError(_unreachable);
    } on http.ClientException {
      throw MacError(_unreachable);
    } on Exception {
      throw MacError(tr('Il Mac non ha risposto in tempo', 'The Mac did not answer in time'));
    }
    if (res.statusCode == 401) throw MacError(tr('Token non valido (Impostazioni)', 'Invalid token (Settings)'));
    // From tailscale serve, when nothing answers on the Mac behind it.
    if (res.statusCode >= 502 && res.statusCode <= 504) {
      throw MacError(
        tr(
          'Il ricevitore sul Mac è fermo: riavvialo dall\'app Mac (Impostazioni)',
          'The receiver on the Mac is stopped: restart it from the Mac app (Settings)',
        ),
      );
    }
    if (res.statusCode == 404)
      throw MacError(
        tr(
          'Il Mac va aggiornato: non conosce questa richiesta',
          'The Mac needs an update: it does not know this request',
        ),
      );
    if (res.statusCode != 200) {
      String? detail;
      try {
        detail = (jsonDecode(res.body) as Map)['error'] as String?;
      } catch (_) {}
      throw MacError(
        _translated(detail) ??
            tr('Errore dal Mac (HTTP ${res.statusCode})', 'Error from the Mac (HTTP ${res.statusCode})'),
      );
    }
    return res;
  }

  static final _notConfigured = tr(
    'Mac non configurato: URL e token in Impostazioni',
    'Mac not set up: URL and token in Settings',
  );

  static String _statusError(int code) {
    if (code == 401) return tr('Token non valido (Impostazioni)', 'Invalid token (Settings)');
    if (code >= 502 && code <= 504) {
      return tr(
        'Il ricevitore sul Mac è fermo: riavvialo dall\'app Mac (Impostazioni)',
        'The receiver on the Mac is stopped: restart it from the Mac app (Settings)',
      );
    }
    if (code == 404) {
      return tr(
        'Il Mac va aggiornato: non conosce questa richiesta',
        'The Mac needs an update: it does not know this request',
      );
    }
    return tr('Errore dal Mac (HTTP $code)', 'Error from the Mac (HTTP $code)');
  }

  static final _unreachable = tr(
    'Mac non raggiungibile: è acceso e su Tailscale?',
    'Mac not reachable: is it on, and on Tailscale?',
  );

  /// omy speaks English; the messages the phone can trigger, in the app's
  /// language.
  static String? _translated(String? e) {
    if (e == null) return null;
    if (e.contains('voice prints are turned off')) {
      return tr('Impronte vocali disattivate (Impostazioni, Funzioni)', 'Voice prints are off (Settings, Features)');
    }
    if (e.contains('no voice print')) {
      return tr(
        'Questa voce non ha un\'impronta (troppo breve o cancellata)',
        'This voice has no voice print (too short, or deleted)',
      );
    }
    if (e.contains('already used')) return tr('Nome già usato da un altro luogo', 'Name already used by another place');
    if (e.contains('invalid name')) return tr('Nome non valido', 'Invalid name');
    if (e.startsWith('no place')) {
      return tr(
        'Luogo non trovato: forse è stato cambiato dal Mac',
        'Place not found: maybe it was changed on the Mac',
      );
    }
    if (e.startsWith('no person')) {
      return tr(
        'Persona non trovata: forse è stata cambiata dal Mac',
        'Person not found: maybe it was changed on the Mac',
      );
    }
    return e;
  }
}
