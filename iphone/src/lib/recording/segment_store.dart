/// Segment files in the app's Documents/segments directory.
///
///   {id}.part        segment being written
///   {id}.part.json   device id of a .part, needed to finalize it after a crash
///   {id}.bin         closed and fsynced segment
///   {id}.json        sidecar metadata of a .bin
///
/// Documents/calls.json (written by CallLog in AppDelegate.swift) lists when
/// calls were connected; the calls overlapping a segment go into its sidecar.
/// Documents/locations.json (LocationLog, same file) lists iOS visits and
/// significant location changes; the best fix for a segment goes into its
/// sidecar too, with the pendant battery level when known.
///
/// The closed .bin/.json pairs ARE the upload queue: there is no database.
library;

import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

import 'segment_format.dart';

final _random = Random.secure();

/// Sortable id: UTC start time + 32 random bits, e.g. 20261027T081530Z-3fa2c91b.
/// The Mac validates it with ^\d{8}T\d{6}Z-[0-9a-f]{8}$.
String newSegmentId(DateTime start) {
  final t = start.toUtc();
  String two(int v) => v.toString().padLeft(2, '0');
  final ts = '${t.year}${two(t.month)}${two(t.day)}T${two(t.hour)}${two(t.minute)}${two(t.second)}Z';
  final rnd = List.generate(4, (_) => _random.nextInt(256).toRadixString(16).padLeft(2, '0')).join();
  return '$ts-$rnd';
}

class SegmentStore {
  final Directory dir;
  SegmentStore(this.dir);

  /// Pendant battery level (%), if connected: set by main.dart.
  int? Function() battery = () => null;

  /// Whether a feature is on (features.dart): calls, location, battery.
  bool Function(String) feature = (_) => true;

  static Future<SegmentStore> open() async {
    final docs = await getApplicationDocumentsDirectory();
    final dir = Directory('${docs.path}/segments');
    await dir.create(recursive: true);
    return SegmentStore(dir);
  }

  File bin(String id) => File('${dir.path}/$id.bin');
  File json(String id) => File('${dir.path}/$id.json');

  /// Closed segments waiting for upload, oldest first.
  List<String> queuedIds() {
    final ids =
        dir
            .listSync()
            .whereType<File>()
            .map((f) => f.uri.pathSegments.last)
            .where((n) => n.endsWith('.bin'))
            .map((n) => n.substring(0, n.length - 4))
            .where((id) => json(id).existsSync())
            .toList()
          ..sort();
    return ids;
  }

  SegmentWriter create({required String deviceId, required int codecId, required DateTime start}) {
    final id = newSegmentId(start);
    File('${dir.path}/$id.part.json').writeAsStringSync(jsonEncode({'device_id': deviceId}), flush: true);
    return SegmentWriter._(this, id, deviceId, codecId, start.millisecondsSinceEpoch);
  }

  /// Finalize every .part left over by a crash or a kill. Call at startup,
  /// before opening a new segment.
  void recoverOrphans() {
    for (final f in dir.listSync().whereType<File>()) {
      final name = f.uri.pathSegments.last;
      if (!name.endsWith('.part')) continue;
      final id = name.substring(0, name.length - 5);
      String deviceId = '';
      try {
        deviceId = (jsonDecode(File('${f.path}.json').readAsStringSync()) as Map)['device_id'] as String;
      } catch (_) {}
      debugPrint('Recovering orphan segment $id');
      finalize(id, deviceId, recovering: true);
    }
    // Leftovers of a finalize() interrupted between its last steps.
    for (final f in dir.listSync().whereType<File>()) {
      final p = f.path;
      if (p.endsWith('.json.tmp') || (p.endsWith('.part.json') && !File(p.substring(0, p.length - 5)).existsSync())) {
        f.deleteSync();
      }
    }
  }

  /// Turn {id}.part into {id}.bin + {id}.json. Truncates a torn last record,
  /// fsyncs, writes the sidecar, then renames. Empty or invalid parts are
  /// deleted. Safe to re-run if interrupted at any step.
  void finalize(String id, String deviceId, {bool recovering = false}) {
    final part = File('${dir.path}/$id.part');
    final partMeta = File('${dir.path}/$id.part.json');
    if (!part.existsSync()) {
      if (partMeta.existsSync()) partMeta.deleteSync();
      return;
    }

    var data = part.readAsBytesSync();
    final seg = parseSegment(data);
    if (seg == null || seg.records.isEmpty) {
      part.deleteSync();
      if (partMeta.existsSync()) partMeta.deleteSync();
      return;
    }
    if (seg.validLength < data.length) {
      final raf = part.openSync(mode: FileMode.append);
      raf.truncateSync(seg.validLength);
      raf.flushSync();
      raf.closeSync();
      data = Uint8List.sublistView(data, 0, seg.validLength);
    }

    final meta = buildSidecar(
      id: id,
      deviceId: deviceId,
      data: data,
      seg: seg,
      calls: feature('calls') ? _calls() : const [],
      locations: feature('location') ? _readList('locations.json') : const [],
      // A .part recovered at launch was closed long ago: its level is unknown.
      battery: recovering || !feature('battery') ? null : battery(),
    );
    final tmp = File('${dir.path}/$id.json.tmp');
    final raf = tmp.openSync(mode: FileMode.write);
    raf.writeStringSync(const JsonEncoder.withIndent('  ').convert(meta));
    raf.flushSync();
    raf.closeSync();
    tmp.renameSync(json(id).path);
    part.renameSync(bin(id).path);
    if (partMeta.existsSync()) partMeta.deleteSync();
  }

  /// Calls from Documents/calls.json as (start, end) in UTC ms; end is null
  /// while a call is on (or if the app was killed during it).
  List<(int, int?)> _calls() {
    try {
      return [
        for (final c in _readList('calls.json'))
          (
            DateTime.parse(c['start'] as String).millisecondsSinceEpoch,
            c['end'] == null ? null : DateTime.parse(c['end'] as String).millisecondsSinceEpoch,
          ),
      ];
    } catch (_) {
      return const [];
    }
  }

  /// A JSON list of objects in Documents, empty if missing or broken.
  List<Map> _readList(String name) {
    try {
      return (jsonDecode(File('${dir.parent.path}/$name').readAsStringSync()) as List).cast<Map>();
    } catch (_) {
      return const [];
    }
  }

  void delete(String id) {
    for (final f in [bin(id), json(id)]) {
      if (f.existsSync()) f.deleteSync();
    }
  }
}

/// A call still open this long after its start is taken as ended (the app
/// was killed before CallKit said so).
const _maxCall = Duration(hours: 4);

/// A significant change older than this says little about where the phone is.
const _maxMoveAge = Duration(hours: 2);

/// The best location fix for [startMs, endMs] from locations.json entries: a
/// visit covering the middle of the segment, else the latest significant
/// change up to the end of the segment, if not older than _maxMoveAge and
/// not before leaving a visit.
Map<String, Object?>? bestLocation(List<Map> entries, int startMs, int endMs) {
  int? ms(Object? v) => v is String ? DateTime.tryParse(v)?.millisecondsSinceEpoch : null;
  final mid = (startMs + endMs) ~/ 2;
  Map? best;
  int? bestT;
  var lastDepart = -1;
  for (final e in entries) {
    if (e['lat'] is! num || e['lon'] is! num || e['acc'] is! num) continue;
    if (e['kind'] == 'visit') {
      final a = ms(e['arrive']), d = ms(e['depart']);
      if ((a ?? 0) <= mid && (d == null || d >= mid) && (bestT == null || (a ?? 0) > bestT)) {
        best = e;
        bestT = a ?? 0;
      } else if (d != null && d < mid && d > lastDepart) {
        lastDepart = d;
      }
    }
  }
  if (best == null) {
    for (final e in entries) {
      final t = ms(e['t']);
      if (e['kind'] != 'move' || e['lat'] is! num || e['lon'] is! num || e['acc'] is! num || t == null) continue;
      if (t <= endMs && mid - t <= _maxMoveAge.inMilliseconds && t > lastDepart && (bestT == null || t > bestT)) {
        best = e;
        bestT = t;
      }
    }
  }
  if (best == null) return null;
  return {
    'lat': (best['lat'] as num).toDouble(),
    'lon': (best['lon'] as num).toDouble(),
    'acc': (best['acc'] as num).round(),
    'source': best['kind'],
    'at': DateTime.fromMillisecondsSinceEpoch(bestT!, isUtc: true).toIso8601String(),
  };
}

Map<String, Object?> buildSidecar({
  required String id,
  required String deviceId,
  required Uint8List data,
  required ParsedSegment seg,
  List<(int, int?)> calls = const [],
  List<Map> locations = const [],
  int? battery,
}) {
  String iso(int ms) => DateTime.fromMillisecondsSinceEpoch(ms, isUtc: true).toIso8601String();
  final startMs = seg.startMs, endMs = seg.startMs + seg.records.last.offsetMs;
  final overlapping = [
    for (final (s, e) in calls)
      if (s < endMs && (e ?? s + _maxCall.inMilliseconds) > startMs) {'start': iso(s), if (e != null) 'end': iso(e)},
  ];
  final location = bestLocation(locations, startMs, endMs);
  return {
    'id': id,
    'device_id': deviceId,
    'codec': seg.codecId,
    'start': iso(seg.startMs),
    'end': iso(seg.startMs + seg.records.last.offsetMs),
    'frame_count': seg.records.length,
    'first_packet_index': seg.records.first.packetIndex,
    'last_packet_index': seg.records.last.packetIndex,
    'sha256': sha256.convert(data).toString(),
    // Only when there are some: older Macs reject unknown fields.
    if (overlapping.isNotEmpty) 'calls': overlapping.take(20).toList(),
    if (location != null) 'location': location,
    if (battery != null && battery >= 0 && battery <= 100) 'battery': battery,
  };
}

/// Appends records to {id}.part. Writes are synchronous so frames arriving
/// from the BLE stream are never reordered.
class SegmentWriter {
  final SegmentStore _store;
  final String id;
  final String deviceId;
  final int startMs;
  final RandomAccessFile _raf;
  // Offsets come from a monotonic clock: wall-clock jumps can't make them negative.
  final Stopwatch _clock = Stopwatch()..start();
  int frameCount = 0;

  SegmentWriter._(this._store, this.id, this.deviceId, int codecId, this.startMs)
    : _raf = File('${_store.dir.path}/$id.part').openSync(mode: FileMode.write) {
    _raf.writeFromSync(encodeSegmentHeader(codecId: codecId, startMs: startMs));
  }

  Duration get age => _clock.elapsed;

  void add(Uint8List frame) {
    _raf.writeFromSync(encodeRecord(_clock.elapsedMilliseconds, frame));
    frameCount++;
  }

  /// fsync, close and finalize into the upload queue.
  void close() {
    _raf.flushSync();
    _raf.closeSync();
    _store.finalize(id, deviceId);
  }
}
