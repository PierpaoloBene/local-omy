/// Sends closed segments to the Mac, oldest first. A segment is deleted
/// locally only after a 200 whose body reports the same sha256.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/widgets.dart';
import 'package:http/http.dart' as http;

import '../recording/segment_store.dart';
import '../settings.dart';

class Uploader extends ChangeNotifier with WidgetsBindingObserver {
  static const _minBackoff = Duration(seconds: 30);
  static const _maxBackoff = Duration(minutes: 30);
  static const _requestTimeout = Duration(seconds: 60);

  final SegmentStore _store;
  final Settings _settings;
  final http.Client _client;

  Uploader(this._store, this._settings, {http.Client? client}) : _client = client ?? http.Client();

  int queued = 0;
  bool uploading = false;
  DateTime? lastSuccessAt;
  String? lastError;
  DateTime? lastErrorAt;

  int _failures = 0;
  DateTime _nextAttempt = DateTime.fromMillisecondsSinceEpoch(0);
  bool _again = false;
  Timer? _timer;

  void start() {
    WidgetsBinding.instance.addObserver(this);
    _timer = Timer.periodic(const Duration(seconds: 30), (_) {
      if (DateTime.now().isAfter(_nextAttempt)) trigger();
    });
    trigger();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) trigger();
  }

  /// Try to drain the queue now. Called when a segment closes, when the app
  /// returns to foreground, and by the periodic timer once the backoff expired.
  Future<void> trigger() async {
    if (uploading) {
      _again = true;
      return;
    }
    uploading = true;
    _refreshQueued();
    try {
      do {
        _again = false;
        final ids = _store.queuedIds();
        for (var i = 0; i < ids.length; i++) {
          await _upload(ids[i], remaining: ids.length - i - 1);
          _refreshQueued();
        }
      } while (_again);
      _failures = 0;
      _nextAttempt = DateTime.fromMillisecondsSinceEpoch(0);
    } catch (e) {
      _failures++;
      final backoff = _minBackoff * pow(2, min(_failures - 1, 6)).toInt();
      _nextAttempt = DateTime.now().add(backoff > _maxBackoff ? _maxBackoff : backoff);
      lastError = e.toString();
      lastErrorAt = DateTime.now();
      debugPrint('Upload failed ($_failures), next try at $_nextAttempt: $e');
    } finally {
      uploading = false;
      _refreshQueued();
    }
  }

  /// remaining: segments still queued after this one, so the Mac can tell a
  /// backlog from an idle phone.
  Future<void> _upload(String id, {required int remaining}) async {
    final base = _settings.macUrl;
    final token = _settings.token;
    if (base == null || token == null) throw const UploadException('Mac URL or token not set');

    final bin = await _store.bin(id).readAsBytes();
    final metaBytes = await _store.json(id).readAsBytes();
    final meta = jsonDecode(utf8.decode(metaBytes)) as Map<String, dynamic>;
    final sha = meta['sha256'] as String;

    final res = await _client
        .post(
          Uri.parse('$base/v1/segments/$id'),
          headers: {
            'Authorization': 'Bearer $token',
            'Content-Type': 'application/octet-stream',
            'X-Segment-Id': id,
            'X-Sha256': sha,
            'X-Segment-Meta': base64Url.encode(metaBytes),
            'X-Queue-Remaining': '$remaining',
          },
          body: bin,
        )
        .timeout(_requestTimeout);

    if (res.statusCode != 200) {
      throw UploadException('$id: HTTP ${res.statusCode} ${_short(res.body)}');
    }
    final Object? body;
    try {
      body = jsonDecode(res.body);
    } catch (_) {
      throw UploadException('$id: 200 with invalid body');
    }
    if (body is! Map || body['sha256'] != sha) {
      throw UploadException('$id: 200 but sha256 mismatch, kept locally');
    }
    _store.delete(id);
    lastSuccessAt = DateTime.now();
  }

  void _refreshQueued() {
    queued = _store.queuedIds().length;
    notifyListeners();
  }

  static String _short(String s) => s.length > 200 ? '${s.substring(0, 200)}…' : s;

  @override
  void dispose() {
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _client.close();
    super.dispose();
  }
}

class UploadException implements Exception {
  final String message;
  const UploadException(this.message);
  @override
  String toString() => message;
}
