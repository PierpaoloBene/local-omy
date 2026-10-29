"""Local transcription of closed sessions with whisper.cpp (whisper-cli, Metal).

Per session: decode to WAV in chunks of about CHUNK (cut at segment
boundaries, so whisper never loads hours of audio), run whisper-cli with the
Silero VAD (skips silence, avoids text hallucinated over noise), map the
timestamps back to wall-clock time.

Output, derived and regenerable (the raw archive is only read):
  transcripts/YYYY-MM-DD/<session>.json   speech items with UTC start/end
  transcripts/YYYY-MM-DD.md               the readable day, Europe/Rome times
A session JSON exists only once complete (temp file + rename). Segments can
reach the Mac hours late (Mac asleep, phone queue): a session that grew after
being transcribed gets only its new segments transcribed and appended.

While running, cache/transcribe-progress.json says how far it got (read by
omy status and, directly, by the Mac app).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

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
# Silence longer than this starts a new paragraph in the day markdown, and so
# does a paragraph that has run this long without one.
BLOCK_GAP = timedelta(minutes=2)
BLOCK_MAX = timedelta(minutes=5)
# Whisper's stock phrases on noise. Dropped from the markdown when nothing else
# is said within ISOLATED of them; the JSON keeps them.
HALLUCINATIONS = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching",
    "grazie", "grazie a tutti", "grazie per la visione", "gracias", "merci", "you", "",
}
ISOLATED = timedelta(seconds=30)
# How far back --pending looks.
PENDING_DAYS = 7
PROGRESS = CACHE_DIR / "transcribe-progress.json"


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


def _load_doc(session: Session) -> dict | None:
    try:
        return json.loads(session_path(session).read_text())
    except (FileNotFoundError, ValueError):
        return None


def transcribe_session(session: Session, prior: dict | None = None,
                       on_progress: Callable[[int], None] | None = None) -> dict:
    """Transcribe a session, or with prior (its transcript when it had fewer
    segments) only the segments added since. on_progress gets the ms of audio
    decoded so far."""
    done = len(prior["segments"]) if prior else 0
    work = Session(session.segments[done:])
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    decoder = StreamDecoder(work.segments[0].codec)
    speech: list[dict] = []
    chunk_start_ms = 0
    wav = CACHE_DIR / f"{session.id}.wav"

    def flush() -> None:
        decoder.close()
        if decoder.position_ms > chunk_start_ms:
            log(f"  whisper {work.id} +{chunk_start_ms // 60000} min")
            for it in _run_whisper(wav):
                speech.append({
                    "start": _iso(work.start + timedelta(milliseconds=chunk_start_ms + it["from"])),
                    "end": _iso(work.start + timedelta(milliseconds=chunk_start_ms + it["to"])),
                    "language": it["language"],
                    "text": it["text"],
                })
            if on_progress:
                on_progress(decoder.position_ms)
        wav.unlink(missing_ok=True)

    try:
        decoder.open(wav)
        for seg in work.segments:
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
        "frames_decoded": decoder.decoded + (prior["frames_decoded"] if prior else 0),
        "frames_concealed": decoder.concealed + (prior["frames_concealed"] if prior else 0),
        "speech": (prior["speech"] if prior else []) + speech,
    }
    _write_atomic(session_path(session), json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return doc


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ).strftime("%H:%M")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def _clean(speech: list[dict]) -> list[dict]:
    """Speech fit to read: whisper's repetition loops (the same line over and
    over) collapsed to one line, stock phrases alone in silence dropped."""
    out: list[dict] = []
    for it in speech:
        if out and _norm(it["text"]) == _norm(out[-1]["text"]):
            out[-1] = {**out[-1], "end": it["end"]}
            continue
        out.append(it)

    def t(iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    kept = []
    for i, it in enumerate(out):
        if _norm(it["text"]) in HALLUCINATIONS:
            before = i > 0 and t(it["start"]) - t(out[i - 1]["end"]) < ISOLATED
            after = i + 1 < len(out) and t(out[i + 1]["start"]) - t(it["end"]) < ISOLATED
            if not (before or after):
                continue
        kept.append(it)
    return kept


def render_day(day: date) -> Path | None:
    """Rebuild transcripts/DAY.md from the session JSONs of that day. Sessions
    with no speech are left out."""
    day_dir = TRANSCRIPTS / day.isoformat()
    docs = sorted((json.loads(p.read_text()) for p in day_dir.glob("*.json")), key=lambda d: d["start"]) \
        if day_dir.is_dir() else []
    if not docs:
        return None
    lines = [f"# {day.isoformat()}", ""]
    for doc in docs:
        speech = _clean(doc["speech"])
        if not speech:
            continue
        lines += [f"## {_hm(doc['start'])}–{_hm(doc['end'])}", ""]
        block: list[str] = []
        block_start: datetime | None = None
        prev_end: datetime | None = None
        for it in speech:
            start = datetime.fromisoformat(it["start"].replace("Z", "+00:00"))
            if prev_end is None or start - prev_end > BLOCK_GAP or start - block_start > BLOCK_MAX:
                if block:
                    lines += [" ".join(block), ""]
                block = [f"**{_hm(it['start'])}**"]
                block_start = start
            block.append(it["text"])
            prev_end = datetime.fromisoformat(it["end"].replace("Z", "+00:00"))
        lines += [" ".join(block), ""]
    if len(lines) == 2:
        lines += ["_(nessun parlato)_", ""]
    path = TRANSCRIPTS / f"{day.isoformat()}.md"
    _write_atomic(path, "\n".join(lines))
    return path


def _minutes(segments: list) -> float:
    return sum((s.end - s.start).total_seconds() for s in segments) / 60


def remaining(session: Session, prior: dict | None) -> list:
    """The segments of session still to transcribe."""
    return session.segments[len(prior["segments"]) if prior else 0:]


def plan(days: list[date], force: bool = False) -> tuple[list[tuple[Session, dict | None]], list[str]]:
    """(closed sessions to transcribe, each with its prior transcript if it
    only grew since; ids of sessions still open)."""
    todo: list[tuple[Session, dict | None]] = []
    still_open: list[str] = []
    for day in days:
        for session in sessions_for_day(day):
            if not session.closed():
                still_open.append(session.id)
                continue
            doc = None if force else _load_doc(session)
            ids = [s.id for s in session.segments]
            if doc is not None:
                if doc["segments"] == ids:
                    continue
                if ids[:len(doc["segments"])] != doc["segments"]:
                    doc = None  # regrouped differently: start over
            todo.append((session, doc))
    return todo, still_open


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def progress() -> dict:
    """The running transcription, by whichever process (app, terminal, hourly
    job), or {"running": False}."""
    try:
        p = json.loads(PROGRESS.read_text())
    except (FileNotFoundError, ValueError):
        return {"running": False}
    if not _pid_alive(p.get("pid", 0)):
        return {"running": False}
    return {"running": True, **p}


def transcribe(days: list[date], force: bool = False) -> dict:
    """Transcribe the closed sessions of these days that have no transcript yet
    or grew since (all closed sessions with force), then rebuild those days'
    markdown."""
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
        todo, result["open"] = plan(days, force)
        work = [remaining(session, prior) for session, prior in todo]
        state = {
            "pid": os.getpid(),
            "started": _iso(datetime.now(timezone.utc)),
            "sessions_total": len(todo),
            "sessions_done": 0,
            "minutes_total": round(sum(_minutes(w) for w in work), 1),
            "minutes_done": 0.0,
            "session": None,
            "day": None,
        }

        def save(**changes) -> None:
            state.update(changes)
            _write_atomic(PROGRESS, json.dumps(state) + "\n")

        log(f"{len(todo)} sessions to transcribe, {state['minutes_total']:.0f} min of audio"
            + (f", {len(result['open'])} still recording" if result["open"] else ""))
        touched: set[date] = set()
        minutes_before = 0.0
        try:
            for i, ((session, prior), segs) in enumerate(zip(todo, work), 1):
                save(session=session.id, day=session.day.isoformat(), minutes_done=round(minutes_before, 1))
                log(f"[{i}/{len(todo)}] {session.day} session {session.id}: "
                    + (f"{len(segs)} new segments" if prior else f"{len(segs)} segments")
                    + f", {_minutes(segs):.0f} min")
                try:
                    transcribe_session(session, prior, lambda ms, base=minutes_before:
                                       save(minutes_done=round(base + ms / 60000, 1)))
                    result["transcribed"].append(session.id)
                    touched.add(session.day)
                except TranscribeError as e:
                    log(f"  failed: {e}")
                    result["failed"][session.id] = str(e)
                minutes_before += _minutes(segs)
                save(sessions_done=i, minutes_done=round(minutes_before, 1))
                # Rebuild a day's markdown as soon as its last session is done.
                nxt = todo[i][0].day if i < len(todo) else None
                if session.day in touched and nxt != session.day and render_day(session.day):
                    result["days"].append(session.day.isoformat())
            if force:
                for day in days:
                    if day not in touched and render_day(day):
                        result["days"].append(day.isoformat())
        finally:
            PROGRESS.unlink(missing_ok=True)
    return result


def pending_days() -> list[date]:
    return recorded_days()[-PENDING_DAYS:]
