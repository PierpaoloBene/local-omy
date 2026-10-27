/// Writes every audio notification from the pendant into the current segment
/// and rotates segments: every 5 minutes, when the pendant disconnects, and
/// when the app is being terminated.
library;

import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';

import '../services/ble_service.dart';
import 'segment_store.dart';

class Recorder extends ChangeNotifier with WidgetsBindingObserver {
  static const segmentLength = Duration(minutes: 5);

  final BleService _ble;
  final SegmentStore store;

  /// Called after a segment has been closed into the upload queue.
  void Function()? onSegmentClosed;

  Recorder(this._ble, this.store);

  SegmentWriter? _writer;
  bool get recording => _writer != null;

  String? lastError;
  DateTime? lastErrorAt;

  final _subs = <StreamSubscription>[];
  Timer? _timer;

  void start() {
    store.recoverOrphans();
    _subs.add(_ble.audioStream.listen(_onFrame));
    _subs.add(
      _ble.stateStream.listen((s) {
        if (s != PendantState.streaming) _rotate();
        notifyListeners();
      }),
    );
    _subs.add(_ble.batteryStream.listen((_) => notifyListeners()));
    // Closes a segment that stopped receiving frames while still connected.
    _timer = Timer.periodic(const Duration(seconds: 15), (_) {
      final w = _writer;
      if (w != null && w.age >= segmentLength + const Duration(seconds: 10)) _rotate();
    });
    WidgetsBinding.instance.addObserver(this);
  }

  void _onFrame(Uint8List frame) {
    try {
      var w = _writer;
      // Rotate only on the first fragment of a codec frame, so fragments of
      // one frame never end up in different segments.
      if (w != null && w.age >= segmentLength && frame.length >= 3 && frame[2] == 0) {
        _rotate();
        w = null;
      }
      if (w == null) {
        w = store.create(deviceId: _ble.deviceId ?? '', codecId: _ble.codecId ?? 0, start: DateTime.now());
        _writer = w;
        notifyListeners();
      }
      w.add(frame);
    } catch (e) {
      _setError('Write failed: $e');
      _writer = null;
    }
  }

  void _rotate() {
    final w = _writer;
    if (w == null) return;
    _writer = null;
    try {
      w.close();
    } catch (e) {
      _setError('Closing segment ${w.id} failed: $e');
    }
    notifyListeners();
    onSegmentClosed?.call();
  }

  /// iOS gives no reliable termination callback: detached is best effort, and
  /// anything left as .part is finalized by recoverOrphans() at next launch.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.detached) _rotate();
  }

  void _setError(String message) {
    debugPrint(message);
    lastError = message;
    lastErrorAt = DateTime.now();
    notifyListeners();
  }

  @override
  void dispose() {
    _rotate();
    _timer?.cancel();
    for (final s in _subs) {
      s.cancel();
    }
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }
}
