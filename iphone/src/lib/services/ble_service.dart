/// BLE link to the Omi pendant: scan, connect to the saved device, automatic
/// reconnection, codec, audio stream, battery level, firmware version.
/// Nothing else.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_blue_plus/flutter_blue_plus.dart';

// Omi GATT UUIDs (BasedHardware/omi firmware, transport.c)
final Guid omiServiceUuid = Guid('19b10000-e8f2-537e-4f6c-d104768a1214');
final Guid audioDataUuid = Guid('19b10001-e8f2-537e-4f6c-d104768a1214');
final Guid audioCodecUuid = Guid('19b10002-e8f2-537e-4f6c-d104768a1214');
final Guid batteryServiceUuid = Guid('180f');
final Guid batteryLevelUuid = Guid('2a19');
// Device Information Service: firmware revision string, e.g. "3.0.21".
final Guid deviceInfoServiceUuid = Guid('180a');
final Guid firmwareRevisionUuid = Guid('2a26');

/// Codec ids as reported by the codec characteristic.
const Map<int, String> codecNames = {1: 'pcm8', 20: 'opus', 21: 'opusFS320'};

enum PendantState { disconnected, connecting, streaming }

/// What the app shows: [PendantState] seen from the outside.
enum PendantStatus {
  /// No pendant chosen, or not seen for a while: out of range, or flat.
  notFound,

  /// Was connected a moment ago; reconnecting by itself.
  disconnected,

  /// Connected, but no audio arriving.
  idle,

  /// Connected and sending audio.
  connected,
}

class BleService {
  static final BleService _instance = BleService._internal();
  factory BleService() => _instance;
  BleService._internal();

  /// Must run before any other FlutterBluePlus call. With restoreState iOS
  /// relaunches the app in background for BLE events after the system killed
  /// it (not after a user force-quit).
  static Future<void> init() async {
    await FlutterBluePlus.setOptions(restoreState: true);
    FlutterBluePlus.adapterState.listen((s) {
      _instance.bluetoothOn = s == BluetoothAdapterState.on || s == BluetoothAdapterState.unknown;
      _instance._stateController.add(_instance._state);
    });
  }

  BluetoothDevice? _device;
  StreamSubscription<BluetoothConnectionState>? _connSub;

  PendantState _state = PendantState.disconnected;
  PendantState get state => _state;
  final _stateController = StreamController<PendantState>.broadcast();
  Stream<PendantState> get stateStream => _stateController.stream;

  /// Codec id read at the start of the current streaming session
  /// (null = could not be read; never guessed).
  int? codecId;

  int? _battery;
  int? get battery => _battery;
  final _batteryController = StreamController<int>.broadcast();
  Stream<int> get batteryStream => _batteryController.stream;

  /// Raw audio notifications, 3-byte header included.
  final _audioController = StreamController<Uint8List>.broadcast();
  Stream<Uint8List> get audioStream => _audioController.stream;

  /// Firmware revision read at the last connection; kept after a
  /// disconnection (it is still the same pendant).
  String? firmware;

  String? get deviceId => _device?.remoteId.str;

  static const _audioQuiet = Duration(seconds: 10);
  static const _lostFor = Duration(minutes: 10);
  DateTime? _lastAudioAt;
  DateTime? _lastConnectedAt;

  bool bluetoothOn = true;

  PendantStatus get status {
    final now = DateTime.now();
    if (_device == null) return PendantStatus.notFound;
    if (_state == PendantState.streaming) {
      final a = _lastAudioAt;
      return a != null && now.difference(a) < _audioQuiet ? PendantStatus.connected : PendantStatus.idle;
    }
    final c = _lastConnectedAt;
    return c != null && now.difference(c) < _lostFor ? PendantStatus.disconnected : PendantStatus.notFound;
  }

  /// When the link dropped, while [PendantStatus.disconnected] or
  /// [PendantStatus.notFound] after a connection.
  DateTime? get lastConnectedAt => _state == PendantState.streaming ? null : _lastConnectedAt;

  /// Scan for devices whose name contains "omi".
  Stream<List<ScanResult>> scan({Duration timeout = const Duration(seconds: 15)}) async* {
    await FlutterBluePlus.adapterState
        .where((s) => s == BluetoothAdapterState.on)
        .first
        .timeout(const Duration(seconds: 10));
    await FlutterBluePlus.startScan(timeout: timeout);
    yield* FlutterBluePlus.scanResults.map(
      (results) =>
          results.where((r) => r.device.platformName.toLowerCase().contains('omi')).toList()
            ..sort((a, b) => b.rssi.compareTo(a.rssi)),
    );
  }

  Future<void> stopScan() => FlutterBluePlus.stopScan();

  /// Connect to [remoteId] with autoConnect: the connection request never
  /// times out, and FlutterBluePlus re-issues it after every disconnection
  /// and whenever the Bluetooth adapter turns back on.
  Future<void> connect(String remoteId) async {
    if (_device?.remoteId.str == remoteId) return;
    await forget();

    final device = BluetoothDevice.fromId(remoteId);
    _device = device;
    _setState(PendantState.connecting);

    _connSub = device.connectionState.listen((s) {
      if (s == BluetoothConnectionState.connected) {
        _onConnected(device);
      } else if (s == BluetoothConnectionState.disconnected) {
        codecId = null;
        if (_state == PendantState.streaming) _lastConnectedAt = DateTime.now();
        if (_device == device) _setState(PendantState.connecting);
      }
    });

    try {
      await device.connect(autoConnect: true, mtu: null);
    } catch (e) {
      debugPrint('BLE connect error: $e');
    }
  }

  /// Drop the current device and stop auto-reconnecting to it.
  Future<void> forget() async {
    final device = _device;
    _device = null;
    firmware = null;
    _lastConnectedAt = null;
    await _connSub?.cancel();
    _connSub = null;
    codecId = null;
    if (device != null) {
      try {
        await device.disconnect();
      } catch (e) {
        debugPrint('BLE disconnect error: $e');
      }
    }
    _setState(PendantState.disconnected);
  }

  Future<void> _onConnected(BluetoothDevice device) async {
    try {
      final services = await device.discoverServices();

      BluetoothCharacteristic? find(Guid service, Guid char) {
        for (final s in services.where((s) => s.uuid == service)) {
          for (final c in s.characteristics.where((c) => c.uuid == char)) {
            return c;
          }
        }
        return null;
      }

      final audio = find(omiServiceUuid, audioDataUuid);
      if (audio == null) {
        debugPrint('BLE: audio characteristic not found, not an Omi device');
        if (_device == device) await forget();
        return;
      }

      final codecChar = find(omiServiceUuid, audioCodecUuid);
      if (codecChar != null) {
        final v = await codecChar.read();
        codecId = v.isNotEmpty ? v[0] : null;
      }

      final fwChar = find(deviceInfoServiceUuid, firmwareRevisionUuid);
      if (fwChar != null) {
        try {
          final v = utf8.decode(await fwChar.read(), allowMalformed: true).replaceAll('\x00', '').trim();
          firmware = v.isEmpty ? null : v;
        } catch (e) {
          debugPrint('BLE firmware revision: $e');
        }
      }

      final batteryChar = find(batteryServiceUuid, batteryLevelUuid);
      if (batteryChar != null) {
        final v = await batteryChar.read();
        if (v.isNotEmpty) _setBattery(v[0]);
        if (batteryChar.properties.notify) {
          final sub = batteryChar.onValueReceived.listen((v) {
            if (v.isNotEmpty) _setBattery(v[0]);
          });
          device.cancelWhenDisconnected(sub);
          await batteryChar.setNotifyValue(true);
        }
      }

      final sub = audio.onValueReceived.listen((v) {
        if (v.isNotEmpty) {
          _lastAudioAt = DateTime.now();
          _audioController.add(Uint8List.fromList(v));
        }
      });
      device.cancelWhenDisconnected(sub);
      await audio.setNotifyValue(true);

      if (_device == device) _setState(PendantState.streaming);
    } catch (e) {
      debugPrint('BLE setup error: $e');
      // Force a clean disconnect; autoConnect will retry.
      try {
        await device.disconnect();
        if (_device == device) await device.connect(autoConnect: true, mtu: null);
      } catch (_) {}
    }
  }

  void _setBattery(int level) {
    _battery = level;
    _batteryController.add(level);
  }

  void _setState(PendantState s) {
    if (_state == s) return;
    _state = s;
    _stateController.add(s);
  }
}
