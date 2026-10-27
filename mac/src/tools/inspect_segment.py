"""Inspect local-omy segments: missing-packet report and Opus -> WAV decoding.

  uv run tools/inspect_segment.py SEGMENT.bin [--wav OUT.wav | --no-wav]
  uv run tools/inspect_segment.py --day 2026-10-27 [--archive ~/OmiArchive]

Decoding needs libopus (`brew install opus`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from omy_receiver.segment import (
    FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, Record, Segment, SegmentError, analyze, codec_frames,
    index_step, parse, plausible_missing,
)

LOCAL_TZ = ZoneInfo("Europe/Rome")
# Below this, the gap between two segments is a rotation, not a disconnection.
ROTATION_GAP = timedelta(seconds=2)


def load_opus_decoder(codec: int):
    if codec not in FRAME_SAMPLES:
        raise SystemExit(f"codec {codec} is not Opus, cannot decode")
    # Homebrew on Apple Silicon installs outside the default dyld search path,
    # where ctypes.util.find_library (used by opuslib) doesn't look.
    os.environ.setdefault("DYLD_FALLBACK_LIBRARY_PATH", "/opt/homebrew/lib:/usr/local/lib:/usr/lib")
    try:
        import opuslib
    except Exception as e:  # noqa: BLE001 - opuslib raises plain Exception if libopus is missing
        raise SystemExit(f"cannot load opuslib/libopus ({e}); run: brew install opus")
    return opuslib.Decoder(SAMPLE_RATE, 1)


def decode_to_wav(seg: Segment, out: Path, fill_gaps: bool = True) -> tuple[int, int]:
    """Decode to 16 kHz mono PCM16 WAV. Missing notifications (counted gaps) and
    broken frames are filled with Opus packet-loss concealment so the timeline
    stays aligned. Returns (decoded frames, concealed frames)."""
    dec = load_opus_decoder(seg.codec)
    samples = FRAME_SAMPLES[seg.codec]
    frame_ms = FRAME_MS[seg.codec]
    decoded = concealed = 0

    # Loss before each frame start, keyed by the record that starts it.
    loss_before: dict[int, int] = {}
    if fill_gaps:
        for prev, cur in zip(seg.records, seg.records[1:]):
            m = plausible_missing(prev, cur, frame_ms)
            if m:
                loss_before[id(cur)] = m
    starts = [r for r in seg.records if r.fragment == 0]

    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)

        def conceal(n: int) -> None:
            nonlocal concealed
            for _ in range(n):
                w.writeframes(dec.decode(b"", samples))
                concealed += 1

        start_iter = iter(starts)
        for frame in codec_frames(seg):
            if frame is None:
                conceal(1)
                continue
            r: Record | None = next(start_iter, None)
            if r is not None:
                conceal(loss_before.get(id(r), 0))
            try:
                w.writeframes(dec.decode(frame, samples * 6))
                decoded += 1
            except Exception:  # noqa: BLE001 - opuslib.OpusError on corrupt/truncated frame
                conceal(1)
    return decoded, concealed


def utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def local(t: datetime) -> str:
    return t.astimezone(LOCAL_TZ).strftime("%H:%M:%S")


def fmt_dur(td: timedelta) -> str:
    s = int(td.total_seconds())
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def report_segment(path: Path, seg: Segment) -> None:
    rep = analyze(seg)
    start = utc(seg.start_ms)
    dur = timedelta(milliseconds=seg.records[-1].offset_ms if seg.records else 0)
    expected = rep.records + rep.missing
    print(f"{path.name}")
    print(f"  codec           {seg.codec}")
    print(f"  start           {start.isoformat()} ({local(start)} Europe/Rome)")
    print(f"  duration        {fmt_dur(dur)}")
    print(f"  notifications   {rep.records}")
    if seg.records:
        print(f"  packet index    {seg.records[0].packet_index} -> {seg.records[-1].packet_index}")
    print(f"  missing         {rep.missing} ({rep.missing / expected * 100 if expected else 0:.2f}%)")
    print(f"  discontinuities {rep.discontinuities} (index reset/duplicate, not counted as loss)")
    print(f"  broken frames   {rep.broken_fragments} (first fragment lost)")
    if seg.trailing_bytes:
        print(f"  WARNING         {seg.trailing_bytes} trailing bytes after last record")
    for g in rep.gaps[:50]:
        what = f"{g.missing} missing" if g.missing is not None else "discontinuity"
        print(f"    +{g.at_ms / 1000:8.2f}s  {g.after_index} -> {g.before_index}  {what}")
    if len(rep.gaps) > 50:
        print(f"    … {len(rep.gaps) - 50} more gaps")


def report_day(archive: Path, day: str) -> int:
    day_dir = archive / "raw" / "audio" / day
    if not day_dir.is_dir():
        print(f"no segments for {day} in {day_dir}", file=sys.stderr)
        return 1

    items = []
    for bin_path in sorted(day_dir.glob("*.bin")):
        try:
            seg = parse(bin_path.read_bytes())
        except SegmentError as e:
            print(f"{bin_path.name}: unreadable ({e})", file=sys.stderr)
            continue
        meta_path = bin_path.with_suffix(".json")
        device = json.loads(meta_path.read_text()).get("device_id", "?") if meta_path.exists() else "?"
        items.append((seg.start_ms, bin_path, seg, device))
    items.sort(key=lambda x: x[0])
    if not items:
        print(f"no readable segments in {day_dir}", file=sys.stderr)
        return 1

    print(f"Day {day} (Europe/Rome) — {len(items)} segments in {day_dir}\n")
    print(f"{'start':>8} {'end':>8} {'dur':>8} {'notif':>7} {'miss':>6} {'disc':>4}  segment")
    tot_records = tot_missing = tot_disc = tot_broken = 0
    recorded = timedelta()
    holes: list[tuple[datetime, datetime]] = []
    boundary_missing = 0
    prev: tuple[Segment, datetime, str] | None = None

    for start_ms, path, seg, device in items:
        rep = analyze(seg)
        start = utc(start_ms)
        end = start + timedelta(milliseconds=seg.records[-1].offset_ms if seg.records else 0)
        recorded += end - start
        tot_records += rep.records
        tot_missing += rep.missing
        tot_disc += rep.discontinuities
        tot_broken += rep.broken_fragments

        if prev is not None:
            pseg, pend, pdevice = prev
            gap = start - pend
            if gap > ROTATION_GAP or pdevice != device:
                holes.append((pend, start))
                print(f"{'':>8} {'':>8} {fmt_dur(gap):>8}  -- not recorded (pendant disconnected?) --")
            elif pseg.records and seg.records:
                # Rotation: index should continue across the boundary.
                a, b = pseg.records[-1], seg.records[0]
                bridged = Record(0, a.frame), Record(int(gap.total_seconds() * 1000), b.frame)
                if a.packet_index is not None and b.packet_index is not None \
                        and index_step(a.packet_index, b.packet_index) != 1:
                    m = plausible_missing(*bridged, FRAME_MS.get(seg.codec, 20))
                    if m is None:
                        tot_disc += 1
                    else:
                        boundary_missing += m

        print(f"{local(start):>8} {local(end):>8} {fmt_dur(end - start):>8} {rep.records:>7} "
              f"{rep.missing:>6} {rep.discontinuities:>4}  {path.name}")
        prev = (seg, end, device)

    missing = tot_missing + boundary_missing
    expected = tot_records + missing
    span = prev[1] - utc(items[0][0])
    print()
    print(f"span            {local(utc(items[0][0]))} -> {local(prev[1])} ({fmt_dur(span)})")
    print(f"recorded        {fmt_dur(recorded)}")
    print(f"not recorded    {fmt_dur(sum((b - a for a, b in holes), timedelta()))} in {len(holes)} holes")
    print(f"notifications   {tot_records}")
    print(f"MISSING PACKETS {missing} ({missing / expected * 100 if expected else 0:.3f}%)"
          f"  [inside segments {tot_missing}, at rotations {boundary_missing}]")
    print(f"discontinuities {tot_disc}")
    print(f"broken frames   {tot_broken}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("segment", nargs="?", type=Path, help=".bin segment file")
    p.add_argument("--wav", type=Path, help="output WAV (default: next to the .bin)")
    p.add_argument("--no-wav", action="store_true", help="report only, do not decode")
    p.add_argument("--no-fill", action="store_true", help="do not conceal missing packets in the WAV")
    p.add_argument("--day", help="report for a whole day, YYYY-MM-DD (Europe/Rome)")
    p.add_argument("--archive", type=Path, default=Path("~/OmiArchive").expanduser())
    args = p.parse_args()

    if args.day:
        return report_day(args.archive, args.day)
    if not args.segment:
        p.error("give a segment file or --day")

    try:
        seg = parse(args.segment.read_bytes())
    except SegmentError as e:
        print(f"{args.segment}: {e}", file=sys.stderr)
        return 1
    report_segment(args.segment, seg)
    if not args.no_wav:
        out = args.wav or args.segment.with_suffix(".wav")
        decoded, concealed = decode_to_wav(seg, out, fill_gaps=not args.no_fill)
        print(f"  wav             {out} ({decoded} frames decoded, {concealed} concealed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
