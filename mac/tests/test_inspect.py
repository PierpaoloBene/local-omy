"""End-to-end check of tools/inspect_segment.py with real Opus frames.
Skipped when opuslib/libopus are not installed."""

import json
import math
import os
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib:/usr/local/lib:/usr/lib")
opuslib = pytest.importorskip("opuslib")

TOOL = Path(__file__).parents[1] / "tools" / "inspect_segment.py"


def opus_segment(start_ms: int, first_index: int, n: int, drop: set[int]) -> bytes:
    """n frames of a 440 Hz tone, codec 21, frames whose position is in drop omitted."""
    enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_RESTRICTED_LOWDELAY)
    out = bytearray(struct.pack("<4sBBQ", b"OMIS", 1, 21, start_ms))
    for i in range(n):
        pcm = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * (i * 320 + k) / 16000)))
            for k in range(320)
        )
        if i in drop:
            continue
        idx = (first_index + i) % 65536
        frame = bytes([idx & 0xFF, idx >> 8, 0]) + enc.encode(pcm, 320)
        out += struct.pack("<IH", i * 20, len(frame)) + frame
    return bytes(out)


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *map(str, args)], capture_output=True, text=True, check=True)


def test_single_segment_report_and_wav(tmp_path):
    seg = tmp_path / "20261027T080000Z-00000000.bin"
    seg.write_bytes(opus_segment(1793088000000, 65530, 100, drop={10, 11, 12}))
    res = run(seg)
    assert "missing         3 (3.00%)" in res.stdout
    with wave.open(str(seg.with_suffix(".wav"))) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 100 * 320  # 97 decoded + 3 concealed: timeline preserved


def test_day_report(tmp_path):
    day = tmp_path / "raw" / "audio" / "2026-10-27"
    day.mkdir(parents=True)
    t0 = 1793088000000  # 2026-10-27T08:00:00Z
    segs = [
        ("20261027T080000Z-00000001", t0, 0, 100, {50}),
        # rotation right after: index continues, first frame of next segment lost
        ("20261027T080002Z-00000002", t0 + 2000, 101, 100, set()),
        # 10 min later after a disconnection: index continued (no loss counted)
        ("20261027T081002Z-00000003", t0 + 602000, 200, 100, set()),
    ]
    for sid, start, first, n, drop in segs:
        (day / f"{sid}.bin").write_bytes(opus_segment(start, first, n, drop))
        (day / f"{sid}.json").write_text(json.dumps({"device_id": "DEV-1"}))
    res = run("--day", "2026-10-27", "--archive", tmp_path)
    assert "MISSING PACKETS 2 " in res.stdout
    assert "[inside segments 1, at rotations 1]" in res.stdout
    assert "in 1 holes" in res.stdout
