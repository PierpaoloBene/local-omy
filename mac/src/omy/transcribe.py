"""Local transcription of closed sessions with whisper.cpp (whisper-cli, Metal).

Per session: decode to WAV in chunks of about CHUNK (cut at segment
boundaries, so whisper never loads hours of audio), run whisper-cli with the
Silero VAD (skips silence, avoids text hallucinated over noise), map the
timestamps back to wall-clock time.

Output, derived and regenerable (the raw archive is only read):
  transcripts/YYYY-MM-DD/<session>.json   speech items with UTC start/end
  transcripts/YYYY-MM-DD.md               the readable day, Europe/Rome times
A session JSON exists only once complete (temp file + rename).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from omy.audio import StreamDecoder
from omy.config import ARCHIVE
from omy.sessions import LOCAL_TZ, Session, recorded_days, sessions_for_day
from omy_receiver.segment import SegmentError, parse

MODELS_DIR = ARCHIVE / "models"
CACHE_DIR = ARCHIVE / "cache"
TRANSCRIPTS = ARCHIVE / "transcripts"

# Auto-detected once per chunk (on its first 30 s of speech). Forcing "it"
# made whisper translate English speech into broken Italian.
LANGUAGE = "auto"
# name -> (url, sha256, size). Hashes from the Hugging Face LFS metadata.
MODELS = {
    "ggml-large-v3-turbo.bin": (
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin",
        "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69", 1624555275),
    "ggml-silero-v6.2.0.bin": (
        "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin",
        "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987", 885098),
}
WHISPER_MODEL = "ggml-large-v3-turbo.bin"
VAD_MODEL = "ggml-silero-v6.2.0.bin"

CHUNK = timedelta(minutes=30)
# Silence longer than this starts a new paragraph in the day markdown.
BLOCK_GAP = timedelta(minutes=2)
# How far back --pending looks.
PENDING_DAYS = 7


class TranscribeError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def models_status() -> dict[str, bool]:
    return {name: (MODELS_DIR / name).is_file() for name in MODELS}


def download_models() -> dict[str, str]:
    MODELS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = {}
    for name, (url, sha, size) in MODELS.items():
        dest = MODELS_DIR / name
        if dest.is_file():
            result[name] = "present"
            continue
        fd, tmp = tempfile.mkstemp(dir=MODELS_DIR, prefix=f".{name}.")
        os.close(fd)
        try:
            log(f"downloading {name} ({size / 1e6:.0f} MB)")
            # curl rather than urllib: system CA store, progress bar, retries.
            r = subprocess.run(["curl", "-fL", "--retry", "3", "--progress-bar", "-o", tmp, url],
                               stdout=sys.stderr, stderr=sys.stderr)
            if r.returncode != 0:
                raise TranscribeError(f"download of {name} failed (curl exit {r.returncode})")
            got_size, got_sha = os.path.getsize(tmp), _sha256(Path(tmp))
            if got_size != size or got_sha != sha:
                raise TranscribeError(f"{name}: checksum mismatch ({got_size} bytes, sha256 {got_sha})")
            os.replace(tmp, dest)
            result[name] = "downloaded"
        finally:
            Path(tmp).unlink(missing_ok=True)
    return result


def _whisper_cli() -> str:
    path = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
    if not Path(path).is_file():
        raise TranscribeError("whisper-cli not found: brew install whisper-cpp")
    return path


def _run_whisper(wav: Path) -> list[dict]:
    """Speech items of one WAV, with offsets in ms from its start and the
    detected language."""
    base = wav.with_suffix("")
    r = subprocess.run([
        _whisper_cli(), "-m", str(MODELS_DIR / WHISPER_MODEL), "-l", LANGUAGE,
        # No text context between windows: stops the repetition loops
        # ("Grazie a tutti." x18) that whisper produces on noise.
        "-mc", "0",
        "--vad", "-vm", str(MODELS_DIR / VAD_MODEL),
        "-np", "-oj", "-of", str(base), "-f", str(wav),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise TranscribeError(f"whisper-cli failed (exit {r.returncode}): {r.stderr.strip()[-500:]}")
    out = base.with_suffix(".json")
    try:
        data = json.loads(out.read_text())
    finally:
        out.unlink(missing_ok=True)
    lang = data.get("result", {}).get("language")
    return [
        {"from": it["offsets"]["from"], "to": it["offsets"]["to"], "text": it["text"].strip(), "language": lang}
        for it in data["transcription"] if it["text"].strip()
    ]


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def session_path(session: Session) -> Path:
    return TRANSCRIPTS / session.day.isoformat() / f"{session.id}.json"


def transcribe_session(session: Session) -> dict:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    decoder = StreamDecoder(session.segments[0].codec)
    speech: list[dict] = []
    chunk_start_ms = 0
    wav = CACHE_DIR / f"{session.id}.wav"

    def flush() -> None:
        decoder.close()
        if decoder.position_ms > chunk_start_ms:
            log(f"  whisper {session.id} +{chunk_start_ms // 60000} min")
            for it in _run_whisper(wav):
                speech.append({
                    "start": _iso(session.start + timedelta(milliseconds=chunk_start_ms + it["from"])),
                    "end": _iso(session.start + timedelta(milliseconds=chunk_start_ms + it["to"])),
                    "language": it["language"],
                    "text": it["text"],
                })
        wav.unlink(missing_ok=True)

    try:
        decoder.open(wav)
        for seg in session.segments:
            try:
                decoder.add(parse(seg.bin.read_bytes()))
            except SegmentError as e:
                raise TranscribeError(f"{seg.bin.name}: {e}")
            if decoder.position_ms - chunk_start_ms >= CHUNK.total_seconds() * 1000:
                flush()
                chunk_start_ms = decoder.position_ms
                decoder.open(wav)
        flush()
    finally:
        decoder.close()
        wav.unlink(missing_ok=True)

    doc = {
        "version": 1,
        "session": session.id,
        "device_id": session.segments[0].device_id,
        "start": _iso(session.start),
        "end": _iso(session.end),
        "segments": [s.id for s in session.segments],
        "model": WHISPER_MODEL,
        "vad": VAD_MODEL,
        "language": LANGUAGE,
        "frames_decoded": decoder.decoded,
        "frames_concealed": decoder.concealed,
        "speech": speech,
    }
    _write_atomic(session_path(session), json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return doc


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ).strftime("%H:%M")


def render_day(day: date) -> Path | None:
    """Rebuild transcripts/DAY.md from the session JSONs of that day."""
    day_dir = TRANSCRIPTS / day.isoformat()
    docs = sorted((json.loads(p.read_text()) for p in day_dir.glob("*.json")), key=lambda d: d["start"]) \
        if day_dir.is_dir() else []
    if not docs:
        return None
    lines = [f"# {day.isoformat()}", ""]
    for doc in docs:
        lines += [f"## {_hm(doc['start'])}–{_hm(doc['end'])}", ""]
        if not doc["speech"]:
            lines += ["_(nessun parlato)_", ""]
            continue
        block: list[str] = []
        prev_end: datetime | None = None
        for it in doc["speech"]:
            start = datetime.fromisoformat(it["start"].replace("Z", "+00:00"))
            if prev_end is None or start - prev_end > BLOCK_GAP:
                if block:
                    lines += [" ".join(block), ""]
                block = [f"**{_hm(it['start'])}**"]
            block.append(it["text"])
            prev_end = datetime.fromisoformat(it["end"].replace("Z", "+00:00"))
        lines += [" ".join(block), ""]
    path = TRANSCRIPTS / f"{day.isoformat()}.md"
    _write_atomic(path, "\n".join(lines))
    return path


def transcribe(days: list[date], force: bool = False) -> dict:
    """Transcribe the closed sessions of these days that have no transcript yet
    (all closed sessions with force), then rebuild those days' markdown."""
    missing = [n for n, ok in models_status().items() if not ok]
    if missing:
        raise TranscribeError(f"missing models {missing}: run omy setup models")
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (CACHE_DIR / ".transcribe.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise TranscribeError("another transcription is running")

    result: dict = {"transcribed": [], "failed": {}, "open": [], "days": []}
    with lock:
        for day in days:
            touched = False
            for session in sessions_for_day(day):
                if not session.closed():
                    result["open"].append(session.id)
                    continue
                if session_path(session).exists() and not force:
                    continue
                log(f"session {session.id}: {len(session.segments)} segments, "
                    f"{(session.end - session.start).total_seconds() / 60:.0f} min")
                try:
                    transcribe_session(session)
                    result["transcribed"].append(session.id)
                    touched = True
                except TranscribeError as e:
                    log(f"  failed: {e}")
                    result["failed"][session.id] = str(e)
            if touched or force:
                if render_day(day):
                    result["days"].append(day.isoformat())
    return result


def pending_days() -> list[date]:
    return recorded_days()[-PENDING_DAYS:]
