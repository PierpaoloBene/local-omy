import struct
from pathlib import Path

import pytest

from omy_receiver.segment import (
    HEADER, Record, Segment, SegmentError, analyze, codec_frames, parse, plausible_missing,
)

# Written by the Dart writer (test/segment_test.dart): pins the format on both sides.
FIXTURE = (Path(__file__).parent / "fixtures" / "sample.bin").read_bytes()


def rec(offset, index, frag=0, payload=b"\xab\xab"):
    return Record(offset, bytes([index & 0xFF, index >> 8, frag]) + payload)


def test_parses_dart_fixture():
    seg = parse(FIXTURE)
    assert seg.codec == 21
    assert seg.start_ms == 1793141880000  # 2026-10-27T22:58:00Z
    assert [r.packet_index for r in seg.records] == [65534, 65535, 1, 2, 3, 4]
    assert [r.fragment for r in seg.records] == [0, 0, 0, 0, 0, 1]
    assert [r.offset_ms for r in seg.records] == [0, 20, 60, 80, 81, 81]
    assert seg.records[0].payload == b"\xab" * 4
    assert seg.trailing_bytes == 0


def test_fixture_loss_report():
    rep = analyze(parse(FIXTURE))
    assert rep.records == 6
    assert rep.missing == 1                     # index 0 lost across the u16 wrap
    assert [(g.after_index, g.before_index, g.missing) for g in rep.gaps] == [(65535, 1, 1)]
    assert rep.discontinuities == 0
    assert rep.broken_fragments == 0


def test_fixture_reassembles_fragments():
    frames = list(codec_frames(parse(FIXTURE)))
    assert frames == [b"\xab" * 4] * 4 + [b"\xab" * 4]  # last two records -> one frame


def test_truncated_record_reported_as_trailing():
    seg = parse(FIXTURE[:-1])
    assert len(seg.records) == 5
    assert seg.trailing_bytes == 6 + 3 + 2 - 1


@pytest.mark.parametrize("data,msg", [
    (b"OMIS", "too short"),
    (b"XXXX" + FIXTURE[4:], "magic"),
    (FIXTURE[:4] + b"\x02" + FIXTURE[5:], "version"),
])
def test_bad_header(data, msg):
    with pytest.raises(SegmentError, match=msg):
        parse(data)


def test_reset_is_discontinuity_not_loss():
    # Pendant rebooted: index jumps from 30000 to 0 within 20 ms.
    assert plausible_missing(rec(0, 30000), rec(20, 0)) is None
    # Same jump over a long silence-free interval would be loss.
    assert plausible_missing(rec(0, 100), rec(2000, 150)) == 49


def test_duplicate_index_is_discontinuity():
    seg = Segment(1, 21, 0, [rec(0, 5), rec(20, 5)])
    rep = analyze(seg)
    assert rep.discontinuities == 1 and rep.missing == 0


def test_lost_first_fragment_counts_broken_frame():
    seg = Segment(1, 21, 0, [rec(0, 1, 0), rec(20, 3, 1), rec(40, 4, 2), rec(60, 5, 0)])
    frames = list(codec_frames(seg))
    assert frames == [b"\xab\xab", None, b"\xab\xab"]
    assert analyze(seg).broken_fragments == 1


def test_header_layout_is_14_bytes():
    assert HEADER.size == 14
    assert struct.unpack_from("<4sBBQ", FIXTURE) == (b"OMIS", 1, 21, 1793141880000)
