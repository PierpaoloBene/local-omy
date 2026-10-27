import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:local_omy/recording/segment_format.dart';
import 'package:local_omy/recording/segment_store.dart';

Uint8List omiFrame(int index, int fragment, int payloadLen) =>
    Uint8List.fromList([index & 0xFF, (index >> 8) & 0xFF, fragment, ...List.filled(payloadLen, 0xAB)]);

void main() {
  late Directory tmp;
  late SegmentStore store;

  setUp(() {
    tmp = Directory.systemTemp.createTempSync('segtest');
    store = SegmentStore(tmp);
  });
  tearDown(() => tmp.deleteSync(recursive: true));

  test('header and record byte layout', () {
    final h = encodeSegmentHeader(codecId: 21, startMs: 0x0102030405);
    expect(h, [0x4F, 0x4D, 0x49, 0x53, 1, 21, 0x05, 0x04, 0x03, 0x02, 0x01, 0, 0, 0]);
    final r = encodeRecord(0x01020304, [0xAA, 0xBB]);
    expect(r, [0x04, 0x03, 0x02, 0x01, 2, 0, 0xAA, 0xBB]);
  });

  test('write, close, parse back with sidecar', () {
    final start = DateTime.utc(2026, 10, 27, 8, 15, 30, 123);
    final w = store.create(deviceId: 'DEV-1', codecId: 21, start: start);
    final frames = [omiFrame(65534, 0, 80), omiFrame(65535, 0, 77), omiFrame(0, 0, 90)];
    for (final f in frames) {
      w.add(f);
    }
    w.close();

    expect(store.queuedIds(), [w.id]);
    expect(RegExp(r'^\d{8}T\d{6}Z-[0-9a-f]{8}$').hasMatch(w.id), isTrue);
    expect(w.id.startsWith('20261027T081530Z-'), isTrue);
    expect(tmp.listSync().length, 2); // only .bin + .json left

    final data = store.bin(w.id).readAsBytesSync();
    final seg = parseSegment(data)!;
    expect(seg.codecId, 21);
    expect(seg.startMs, start.millisecondsSinceEpoch);
    expect(seg.validLength, data.length);
    expect(seg.records.map((r) => r.frame), frames);
    expect(seg.records.map((r) => r.packetIndex), [65534, 65535, 0]);

    final meta = jsonDecode(store.json(w.id).readAsStringSync()) as Map;
    expect(meta['id'], w.id);
    expect(meta['device_id'], 'DEV-1');
    expect(meta['codec'], 21);
    expect(meta['start'], '2026-10-27T08:15:30.123Z');
    expect(meta['frame_count'], 3);
    expect(meta['first_packet_index'], 65534);
    expect(meta['last_packet_index'], 0);
    expect(meta['sha256'], sha256.convert(data).toString());
  });

  test('orphan .part with torn last record is truncated and finalized', () {
    final w = store.create(deviceId: 'DEV-1', codecId: 21, start: DateTime.now());
    w.add(omiFrame(7, 0, 80));
    w.add(omiFrame(8, 0, 80));
    // Simulate a kill in the middle of the third record.
    final part = File('${tmp.path}/${w.id}.part');
    part.writeAsBytesSync(encodeRecord(40, omiFrame(9, 0, 80)).sublist(0, 20), mode: FileMode.append);

    SegmentStore(tmp).recoverOrphans();

    expect(store.queuedIds(), [w.id]);
    final seg = parseSegment(store.bin(w.id).readAsBytesSync())!;
    expect(seg.records.map((r) => r.packetIndex), [7, 8]);
    final meta = jsonDecode(store.json(w.id).readAsStringSync()) as Map;
    expect(meta['device_id'], 'DEV-1');
    expect(meta['frame_count'], 2);
  });

  test('empty orphan .part is discarded', () {
    final w = store.create(deviceId: 'DEV-1', codecId: 21, start: DateTime.now());
    w.add(omiFrame(1, 0, 10));
    File('${tmp.path}/${w.id}.part').writeAsBytesSync(encodeSegmentHeader(codecId: 21, startMs: 0));
    store.recoverOrphans();
    expect(tmp.listSync(), isEmpty);
  });

  // mac/tests/fixtures/sample.bin is parsed by the Python tests: this pins the
  // format on both sides. Regenerate with: UPDATE_FIXTURE=1 flutter test
  test(
    'matches the fixture shared with the Mac tests',
    () {
      final fixture = File('mac/tests/fixtures/sample.bin');
      final data = BytesBuilder()
        ..add(
          encodeSegmentHeader(
            codecId: 21,
            startMs: DateTime.utc(2026, 10, 27, 22, 58).millisecondsSinceEpoch,
          ),
        )
        ..add(encodeRecord(0, omiFrame(65534, 0, 4)))
        ..add(encodeRecord(20, omiFrame(65535, 0, 4)))
        ..add(encodeRecord(60, omiFrame(1, 0, 4))) // index 0 missing (wraps)
        ..add(encodeRecord(80, omiFrame(2, 0, 4)))
        ..add(encodeRecord(81, omiFrame(3, 0, 2)))
        ..add(encodeRecord(81, omiFrame(4, 1, 2))); // frame 3 split in 2 fragments
      final bytes = data.toBytes();
      if (Platform.environment['UPDATE_FIXTURE'] == '1') {
        fixture.parent.createSync(recursive: true);
        fixture.writeAsBytesSync(bytes);
      }
      expect(fixture.readAsBytesSync(), bytes);
    },
    skip:
        !File('mac/tests/fixtures/sample.bin').existsSync() && Platform.environment['UPDATE_FIXTURE'] != '1',
  );
}
