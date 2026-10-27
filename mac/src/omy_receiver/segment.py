"""Parser for local-omy segment files (.bin, format v1) and packet-loss analysis.

Format (little-endian), see the repo README:
  header:  "OMIS" | u8 version | u8 codec | u64 start unix ms     (14 bytes)
  record:  u32 offset ms | u16 len | len bytes (BLE notification, 3-byte Omi header kept)
Omi header: u16 LE packet index (per notification, wraps at 65536) | u8 fragment index.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

MAGIC = b"OMIS"
VERSION = 1
HEADER = struct.Struct("<4sBBQ")
RECORD = struct.Struct("<IH")
OMI_HEADER_SIZE = 3
INDEX_MOD = 1 << 16
# Codec 21 (opusFS320): 20 ms per frame, the only one used by current firmware.
FRAME_MS = {20: 10, 21: 20}
FRAME_SAMPLES = {20: 160, 21: 320}
SAMPLE_RATE = 16000


class SegmentError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    offset_ms: int
    frame: bytes

    @property
    def packet_index(self) -> int | None:
        return self.frame[0] | (self.frame[1] << 8) if len(self.frame) >= OMI_HEADER_SIZE else None

    @property
    def fragment(self) -> int | None:
        return self.frame[2] if len(self.frame) >= OMI_HEADER_SIZE else None

    @property
    def payload(self) -> bytes:
        return self.frame[OMI_HEADER_SIZE:]


@dataclass
class Segment:
    version: int
    codec: int
    start_ms: int
    records: list[Record]
    trailing_bytes: int = 0  # bytes after the last complete record (torn write)


def parse_header(data: bytes) -> tuple[int, int, int]:
    """Return (version, codec, start_ms) or raise SegmentError."""
    if len(data) < HEADER.size:
        raise SegmentError("file too short for header")
    magic, version, codec, start_ms = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise SegmentError("bad magic")
    if version != VERSION:
        raise SegmentError(f"unsupported version {version}")
    return version, codec, start_ms


def iter_records(data: bytes) -> Iterator[Record]:
    pos = HEADER.size
    while pos + RECORD.size <= len(data):
        offset_ms, length = RECORD.unpack_from(data, pos)
        end = pos + RECORD.size + length
        if end > len(data):
            return
        yield Record(offset_ms, bytes(data[pos + RECORD.size : end]))
        pos = end


def parse(data: bytes) -> Segment:
    version, codec, start_ms = parse_header(data)
    records = list(iter_records(data))
    used = HEADER.size + sum(RECORD.size + len(r.frame) for r in records)
    return Segment(version, codec, start_ms, records, len(data) - used)


def index_step(prev: int, cur: int) -> int:
    """Notifications between prev and cur, modulo the u16 wrap (1 = consecutive)."""
    return (cur - prev) % INDEX_MOD


def plausible_missing(prev: Record, cur: Record, frame_ms: int = 20) -> int | None:
    """Missing notifications between two records, or None if the jump is not
    explainable by loss (pendant reboot, index reset, duplicate/reorder)."""
    if prev.packet_index is None or cur.packet_index is None:
        return None
    step = index_step(prev.packet_index, cur.packet_index)
    if step == 0:
        return None
    missing = step - 1
    # Lost notifications still take air time: a jump much larger than the
    # elapsed time can explain is a reset, not loss.
    elapsed = max(0, cur.offset_ms - prev.offset_ms)
    if missing > elapsed / frame_ms * 1.5 + 50:
        return None
    return missing


@dataclass
class Gap:
    after_index: int
    before_index: int
    at_ms: int
    missing: int | None  # None = discontinuity


@dataclass
class LossReport:
    records: int = 0
    missing: int = 0
    gaps: list[Gap] = field(default_factory=list)
    discontinuities: int = 0
    broken_fragments: int = 0


def analyze(seg: Segment) -> LossReport:
    rep = LossReport(records=len(seg.records))
    frame_ms = FRAME_MS.get(seg.codec, 20)
    for prev, cur in zip(seg.records, seg.records[1:]):
        if prev.packet_index is None or cur.packet_index is None:
            continue
        if index_step(prev.packet_index, cur.packet_index) == 1:
            continue
        missing = plausible_missing(prev, cur, frame_ms)
        rep.gaps.append(Gap(prev.packet_index, cur.packet_index, cur.offset_ms, missing))
        if missing is None:
            rep.discontinuities += 1
        else:
            rep.missing += missing
    rep.broken_fragments = sum(1 for f in codec_frames(seg) if f is None)
    return rep


def codec_frames(seg: Segment) -> Iterator[bytes | None]:
    """Reassemble codec frames from notifications. A frame split over several
    notifications has fragments 0, 1, 2… with consecutive packet indexes.
    Yields None once for each frame whose first fragment is missing. A frame
    that lost a later fragment can't be detected here (the total count isn't
    transmitted): the decoder will reject it."""
    buf: bytearray | None = None
    orphan = False
    last: Record | None = None
    for r in seg.records:
        if r.packet_index is None:
            continue
        if r.fragment == 0:
            if buf is not None:
                yield bytes(buf)
            buf, orphan = bytearray(r.payload), False
        elif (buf is not None and last is not None and r.fragment == last.fragment + 1
              and index_step(last.packet_index, r.packet_index) == 1):
            buf.extend(r.payload)
        else:
            if buf is not None:
                yield bytes(buf)
                buf = None
            if not orphan:
                yield None
                orphan = True
        last = r
    if buf is not None:
        yield bytes(buf)
