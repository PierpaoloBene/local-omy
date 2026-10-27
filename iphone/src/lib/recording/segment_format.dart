/// On-disk segment format (.bin). All integers little-endian.
///
///   file header (14 bytes)
///     0  4  magic "OMIS"
///     4  1  format version (1)
///     5  1  codec id (0 = unknown)
///     6  8  start time, unix ms UTC (u64)
///   record (repeated)
///     0  4  offset ms from start (u32)
///     4  2  frame length N (u16)
///     6  N  BLE notification as received, 3-byte Omi header included
library;

import 'dart:typed_data';

const List<int> segmentMagic = [0x4F, 0x4D, 0x49, 0x53]; // "OMIS"
const int segmentVersion = 1;
const int segmentHeaderSize = 14;
const int recordHeaderSize = 6;

Uint8List encodeSegmentHeader({required int codecId, required int startMs}) {
  final b = ByteData(segmentHeaderSize);
  for (var i = 0; i < 4; i++) {
    b.setUint8(i, segmentMagic[i]);
  }
  b.setUint8(4, segmentVersion);
  b.setUint8(5, codecId);
  b.setUint64(6, startMs, Endian.little);
  return b.buffer.asUint8List();
}

Uint8List encodeRecord(int offsetMs, List<int> frame) {
  final out = Uint8List(recordHeaderSize + frame.length);
  final b = ByteData.sublistView(out);
  b.setUint32(0, offsetMs, Endian.little);
  b.setUint16(4, frame.length, Endian.little);
  out.setRange(recordHeaderSize, out.length, frame);
  return out;
}

class SegmentRecord {
  final int offsetMs;
  final Uint8List frame;
  SegmentRecord(this.offsetMs, this.frame);

  /// Omi packet index (bytes 0-1 of the frame), null if the frame is too short.
  int? get packetIndex => frame.length >= 3 ? frame[0] | (frame[1] << 8) : null;

  /// Fragment index inside one codec frame (byte 2).
  int? get fragmentIndex => frame.length >= 3 ? frame[2] : null;
}

class ParsedSegment {
  final int version;
  final int codecId;
  final int startMs;
  final List<SegmentRecord> records;

  /// Bytes up to the end of the last complete record. Anything after it is a
  /// torn write (app killed mid-record) and must be truncated.
  final int validLength;

  ParsedSegment(this.version, this.codecId, this.startMs, this.records, this.validLength);
}

/// Parses [data]; returns null if the file header is missing or invalid.
ParsedSegment? parseSegment(Uint8List data) {
  if (data.length < segmentHeaderSize) return null;
  for (var i = 0; i < 4; i++) {
    if (data[i] != segmentMagic[i]) return null;
  }
  final b = ByteData.sublistView(data);
  final version = b.getUint8(4);
  if (version != segmentVersion) return null;

  final records = <SegmentRecord>[];
  var pos = segmentHeaderSize;
  while (pos + recordHeaderSize <= data.length) {
    final len = b.getUint16(pos + 4, Endian.little);
    final end = pos + recordHeaderSize + len;
    if (end > data.length) break;
    records.add(
      SegmentRecord(
        b.getUint32(pos, Endian.little),
        Uint8List.sublistView(data, pos + recordHeaderSize, end),
      ),
    );
    pos = end;
  }
  return ParsedSegment(version, b.getUint8(5), b.getUint64(6, Endian.little), records, pos);
}
