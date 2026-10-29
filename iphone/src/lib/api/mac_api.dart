/// The Mac's review of transcripts, people and places (omy_receiver/app.py),
/// same URL and token as the uploads. Nothing fetched is stored on the
/// phone: pages keep it in memory while open.
library;

import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import '../settings.dart';
import 'models.dart';

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
  static const _audio = MethodChannel('local_omy/audio');

  final Settings _settings;
  final http.Client _client = http.Client();

  MacApi(this._settings);

  bool get configured => _settings.macUrl != null && _settings.token != null;

  Future<DayList> days() async => DayList.fromJson(await _get('/v1/transcripts'));
  Future<Day> day(String day) async => Day.fromJson(await _get('/v1/transcripts/$day'));

  /// Paragraphs with [q], only in the conversations with [person] and of [type].
  Future<SearchResult> search(String q, {String? person, String? type}) async => SearchResult.fromJson(
    jsonDecode(
      utf8.decode(
        (await _request(
          'POST',
          '/v1/search',
          body: {'q': q, 'person': ?person, 'type': ?type},
          timeout: _read,
        )).bodyBytes,
      ),
    ),
  );

  /// The sessions with [person] (an id or 'me'), of [type] (room, meeting,
  /// call, media) and of [day], newest first; at most [limit] (Mac: 100).
  Future<ConversationList> conversations({String? person, String? type, String? day, int? limit}) async =>
      ConversationList.fromJson(
        await _get('/v1/conversations', {'person': ?person, 'type': ?type, 'day': ?day, 'limit': ?limit?.toString()}),
      );

  /// The sessions of the last days with voices still to label.
  Future<Unlabeled> unlabeled() async => Unlabeled.fromJson(await _get('/v1/unlabeled'));

  Future<void> transcribeNow() => _send('POST', '/v1/transcribe');

  Future<People> people() async => People.fromJson(await _get('/v1/people'));
  Future<void> renamePerson(String id, String name) => _send('PUT', '/v1/people/$id', {'name': name});
  Future<void> deletePerson(String id) => _send('DELETE', '/v1/people/$id');

  /// Where all the voices of a person come from: room, meeting, auto, or
  /// media (it was a video: the name and voice print go).
  Future<void> setPersonKind(String id, String source) => _send('POST', '/v1/people/$id/kind', {'source': source});

  Future<DayVoices> voices(String day) async => DayVoices.fromJson(await _get('/v1/voices/$day'));
  Future<void> nameVoice(String session, String label, String name) =>
      _send('PUT', '/v1/voices/$session/$label', {'name': name});

  /// Where these voices come from: room, meeting, media, or auto.
  Future<void> setVoiceKind(String session, List<String> labels, String source) =>
      _send('POST', '/v1/voices/$session/kind', {'source': source, 'labels': labels});

  /// Plays a few seconds of the voice; returns how many.
  Future<double> playVoice(String session, String label) async {
    final res = await _request('GET', '/v1/voices/$session/$label/sample', timeout: _read);
    return await _audio.invokeMethod<double>('play', res.bodyBytes) ?? 0;
  }

  Future<void> stopVoice() => _audio.invokeMethod('stop');

  Future<Places> places() async => Places.fromJson(await _get('/v1/places'));
  Future<void> addPlace(String name, double lat, double lon, int radius) =>
      _send('POST', '/v1/places', {'name': name, 'lat': lat, 'lon': lon, 'radius': radius});
  Future<void> renamePlace(String name, String to) =>
      _send('PUT', '/v1/places/${Uri.encodeComponent(name)}', {'name': to});
  Future<void> removePlace(String name) => _send('DELETE', '/v1/places/${Uri.encodeComponent(name)}');

  Future<Map<String, dynamic>> _get(String path, [Map<String, String>? query]) async =>
      jsonDecode(utf8.decode((await _request('GET', path, query: query, timeout: _read)).bodyBytes))
          as Map<String, dynamic>;

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
    if (base == null || token == null) throw const MacError('Mac non configurato: URL e token in Impostazioni');
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
      throw const MacError('Mac non raggiungibile: è acceso e su Tailscale?');
    } on http.ClientException {
      throw const MacError('Mac non raggiungibile: è acceso e su Tailscale?');
    } on Exception {
      throw const MacError('Il Mac non ha risposto in tempo');
    }
    if (res.statusCode == 401) throw const MacError('Token non valido (Impostazioni)');
    if (res.statusCode == 404) throw const MacError('Il Mac va aggiornato: non conosce questa richiesta');
    if (res.statusCode != 200) {
      String? detail;
      try {
        detail = (jsonDecode(res.body) as Map)['error'] as String?;
      } catch (_) {}
      throw MacError(_italian(detail) ?? 'Errore dal Mac (HTTP ${res.statusCode})');
    }
    return res;
  }

  /// omy speaks English; the messages the phone can trigger, in Italian.
  static String? _italian(String? e) {
    if (e == null) return null;
    if (e.contains('voice prints are turned off')) return 'Impronte vocali disattivate (Impostazioni, Funzioni)';
    if (e.contains('no voice print')) return 'Questa voce non ha un\'impronta (troppo breve o cancellata)';
    if (e.contains('already used')) return 'Nome già usato da un altro luogo';
    if (e.contains('invalid name')) return 'Nome non valido';
    if (e.startsWith('no place')) return 'Luogo non trovato: forse è stato cambiato dal Mac';
    if (e.startsWith('no person')) return 'Persona non trovata: forse è stata cambiata dal Mac';
    return e;
  }
}
