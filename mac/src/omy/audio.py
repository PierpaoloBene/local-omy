"""Opus -> 16 kHz mono PCM16 WAV, streamed over consecutive segments.

Missing notifications (counted gaps, also across segment boundaries) and
broken frames are filled with Opus packet-loss concealment, so every codec
frame is exactly FRAME_MS of output: frames written x FRAME_MS is the timeline.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

from omy_receiver.segment import (
    FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, Record, Segment, codec_frames, plausible_missing,
)


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


class StreamDecoder:
    """Decode segments in order into one or more WAV files (open() starts a
    new file), keeping the Opus decoder state and gap detection across both
    segment and file boundaries."""

    def __init__(self, codec: int, fill_gaps: bool = True):
        self.dec = load_opus_decoder(codec)
        self.samples = FRAME_SAMPLES[codec]
        self.frame_ms = FRAME_MS[codec]
        self.fill_gaps = fill_gaps
        self.frames = 0  # all frames written, decoded or concealed
        self.decoded = self.concealed = 0
        self._wav: wave.Wave_write | None = None
        self._last: Record | None = None  # with offset relative to the first segment
        self._origin_ms: int | None = None

    @property
    def position_ms(self) -> int:
        return self.frames * self.frame_ms

    def open(self, path: Path) -> None:
        self.close()
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)

    def close(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    def _write(self, pcm: bytes) -> None:
        self._wav.writeframes(pcm)
        self.frames += 1

    def _conceal(self, n: int) -> None:
        for _ in range(n):
            self._write(self.dec.decode(b"", self.samples))
            self.concealed += 1

    def add(self, seg: Segment) -> None:
        if self._origin_ms is None:
            self._origin_ms = seg.start_ms
        shift = seg.start_ms - self._origin_ms
        records = [Record(r.offset_ms + shift, r.frame) for r in seg.records]

        # Loss before each frame start, keyed by the record that starts it.
        loss_before: dict[int, int] = {}
        if self.fill_gaps:
            pairs = zip([self._last, *records], records)
            for prev, cur in pairs:
                if prev is None:
                    continue
                m = plausible_missing(prev, cur, self.frame_ms)
                if m:
                    loss_before[id(cur)] = m
        if records:
            self._last = records[-1]

        start_iter = iter(r for r in records if r.fragment == 0)
        for frame in codec_frames(Segment(seg.version, seg.codec, 0, records)):
            if frame is None:
                self._conceal(1)
                continue
            r = next(start_iter, None)
            if r is not None:
                self._conceal(loss_before.get(id(r), 0))
            try:
                self._write(self.dec.decode(frame, self.samples * 6))
                self.decoded += 1
            except Exception:  # noqa: BLE001 - opuslib.OpusError on corrupt/truncated frame
                self._conceal(1)


def decode_to_wav(seg: Segment, out: Path, fill_gaps: bool = True) -> tuple[int, int]:
    """Decode one segment. Returns (decoded frames, concealed frames)."""
    d = StreamDecoder(seg.codec, fill_gaps)
    d.open(out)
    d.add(seg)
    d.close()
    return d.decoded, d.concealed


# --- preparing audio for whisper ---

# Quiet stretches (people away from the pendant, a TV across the room) are
# raised up to this much: the default VAD and whisper miss them otherwise.
MAX_GAIN_DB = 20


def _read(wav: Path):
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        return w.getparams(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def _write(path: Path, params, pcm) -> None:
    with wave.open(str(path), "wb") as w:
        w.setparams(params)
        w.writeframes(pcm.tobytes())


def normalize_wav(src: Path, dst: Path) -> None:
    """Speech-level gain: per 100 ms towards -20 dBFS RMS, capped at
    +MAX_GAIN_DB, smoothed over ~1 s so noise isn't pumped up abruptly."""
    import numpy as np
    params, pcm = _read(src)
    x = pcm.astype(np.float32) / 32768
    hop = SAMPLE_RATE // 10
    n = len(x) // hop + 1
    padded = np.pad(x, (0, n * hop - len(x)))
    rms = np.sqrt(np.mean(padded.reshape(n, hop) ** 2, axis=1) + 1e-10)
    gain = np.log(np.clip(10 ** (-20 / 20) / rms, 1, 10 ** (MAX_GAIN_DB / 20)))
    gain = np.convolve(gain, np.ones(10) / 10, mode="same")
    g = np.exp(np.interp(np.arange(len(x)), np.arange(n) * hop + hop // 2, gain))
    _write(dst, params, (np.clip(x * g, -1, 1) * 32767).astype(np.int16))


def split_at_pauses(wav: Path, window_s: int, out_dir: Path) -> list[tuple[Path, int]]:
    """Cut wav into pieces of about window_s, each cut at the quietest 100 ms
    within 5 s of the target, so whisper detects the language per piece.
    Returns (piece, offset ms)."""
    import numpy as np
    params, pcm = _read(wav)
    hop = SAMPLE_RATE // 10
    n = len(pcm) // hop
    energy = np.abs(pcm[:n * hop].astype(np.int32)).reshape(n, hop).sum(axis=1) if n else np.zeros(0)
    cuts = [0]
    target = window_s * 10
    while cuts[-1] + target + 50 < n:
        lo = cuts[-1] + target - 50
        cuts.append(lo + int(np.argmin(energy[lo:lo + 100])))
    pieces = []
    for k, (a, b) in enumerate(zip(cuts, cuts[1:] + [None])):
        piece = out_dir / f"{wav.stem}-{k}.wav"
        _write(piece, params, pcm[a * hop:None if b is None else b * hop])
        pieces.append((piece, a * 100))
    return pieces
