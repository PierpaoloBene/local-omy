"""The hub's side of the jobs the Mac's compute worker runs (docs/hub-migration.md, 2.6).

inputs_for(kind, key, ...) says what the Mac must download (paths relative to the archive, so that the Mac's
workspace has the same layout and the pipeline code runs unchanged) and what it will send back; commit(...)
validates the outputs and writes them into the archive, with the same locks and follow-ups the local worker
had. Nothing here runs a model.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

import numpy as np

from omy import jobs, lock, transcribe
from omy.config import ARCHIVE
from omy.sessions import Session

# One commit at a time: they all take lock.writing(), which is per process, not per thread.
COMMIT_LOCK = threading.Lock()
NOTE_PATH = re.compile(r"brain/note/[0-9]{4}/[0-9]{2}/[0-9]{2}/[^/]+-([0-9a-f]{8})\.json")
EMBED_BATCH = 256
# What the Mac may download from the archive, besides the files a job lists.
READABLE = ("raw/audio/", "transcripts/", "voices/people.json", "brain/note/")


class Stale(Exception):
    """The inputs changed while the Mac worked: the result is not used, the job is queued again."""


class Invalid(Exception):
    """The output is not what the job asked for."""


def segments_hash(session: Session) -> str:
    return hashlib.sha256(",".join(x.id for x in session.segments).encode()).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ARCHIVE).as_posix()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry(path: Path, sha: str | None = None) -> dict:
    return {"path": rel(path), "size": path.stat().st_size, "sha256": sha or sha256_file(path)}


def _segment_entries(session: Session, skip_audio: int = 0) -> list[dict]:
    """The sidecars of every segment (they define the session) and the audio of those after skip_audio. The
    audio of the first ones is an empty file on the Mac: its transcript is the prior one, never decoded."""
    out = []
    for i, seg in enumerate(session.segments):
        meta = seg.bin.with_suffix(".json")
        out.append(_entry(meta))
        if i < skip_audio:
            out.append({"path": rel(seg.bin), "empty": True})
        else:
            try:
                sha = json.loads(meta.read_text())["sha256"]  # checked by the receiver at upload
            except (OSError, ValueError, KeyError):
                sha = None
            out.append(_entry(seg.bin, sha))
    return out


def can_read(path: str) -> Path | None:
    """The file of this archive-relative path, if the Mac may download it."""
    if ".." in Path(path).parts or not any(path == p or (p.endswith("/") and path.startswith(p)) for p in READABLE):
        return None
    f = (ARCHIVE / path)
    return f if f.is_file() and not f.is_symlink() else None


# --- what the Mac downloads ------------------------------------------------------------------

def inputs_for(job: jobs.Job) -> dict | None:
    """{"inputs": [...], "outputs": [names], "params": {...}, "payload": {...}} or None if there is nothing to do
    any more (the job is then done)."""
    return {"transcribe": _in_transcribe, "diarize": _in_diarize, "organize": _in_organize,
            "embed": _in_embed}[job.kind](job)


def _in_transcribe(job: jobs.Job) -> dict | None:
    try:
        session = transcribe.find_session(job.key)
    except transcribe.TranscribeError:
        return None
    todo, _open = transcribe.plan([session.day], force=job.force)
    mine = next(((s, prior) for s, prior in todo if s.id == job.key), None)
    if mine is None:
        return None
    session, prior = mine
    done = len(prior["segments"]) if prior else 0
    inputs = _segment_entries(session, done)
    if prior:
        inputs.append(_entry(transcribe.session_path(session)))
    return {"inputs": inputs, "outputs": [rel(transcribe.session_path(session))],
            "params": {"session": session.id, "day": session.day.isoformat(), "input_hash": segments_hash(session),
                       "segments": [s.id for s in session.segments]}}


def _in_diarize(job: jobs.Job) -> dict | None:
    from omy import diarize
    try:
        session = transcribe.find_session(job.key)
    except transcribe.TranscribeError:
        return None
    doc = transcribe._load_doc(session)
    if doc is None or doc["segments"] != [s.id for s in session.segments] or not diarize.needs_diarization(session):
        return None
    return {"inputs": _segment_entries(session), "outputs": ["diarize-result.json"],
            "params": {"session": session.id, "day": session.day.isoformat(), "input_hash": segments_hash(session),
                       "context_days": sorted({d.isoformat() for d in (session.start.astimezone(transcribe.LOCAL_TZ).date(),
                                                                       session.end.astimezone(transcribe.LOCAL_TZ).date())})}}


def _in_organize(job: jobs.Job) -> dict | None:
    from omy.brain import organize
    if not job.force and not organize.needs_organize(job.key):
        return None
    try:
        s = organize.Session(job.key)
    except organize.OrganizeError:
        return None
    inputs = [_entry(ARCHIVE / "transcripts" / f"{s.day}.jsonl")]
    spk = ARCHIVE / "transcripts" / s.day / "speakers" / f"{job.key}.json"
    people = ARCHIVE / "voices" / "people.json"
    old = organize.note_path(job.key)
    inputs += [_entry(p) for p in (spk, people, old) if p is not None and p.is_file()]
    return {"inputs": inputs, "outputs": [f"brain/note/YYYY/MM/DD/*-{job.key[-8:]}.json"],
            "params": {"session": job.key, "day": s.day, "input_hash": organize.input_hash(job.key),
                       "force": job.force}}


def _in_embed(job: jobs.Job) -> dict | None:
    from omy.brain import index
    if not index.INDEX_PATH.is_file():
        return None
    from contextlib import closing
    with closing(index._connect(create=False)) as db:
        rows = db.execute("SELECT id, text FROM chunks WHERE id NOT IN (SELECT chunk_id FROM vectors) "
                          "ORDER BY id LIMIT ?", (EMBED_BATCH,)).fetchall()
    if not rows:
        return None
    return {"inputs": [], "outputs": ["embed-output.json"], "params": {},
            "payload": {"chunks": [{"id": r["id"], "text": r["text"]} for r in rows]}}


def missing_vectors() -> int:
    from contextlib import closing
    from omy.brain import index
    if not index.INDEX_PATH.is_file():
        return 0
    with closing(index._connect(create=False)) as db:
        return db.execute("SELECT COUNT(*) FROM chunks WHERE id NOT IN (SELECT chunk_id FROM vectors)").fetchone()[0]


def output_allowed(kind: str, key: str, name: str) -> bool:
    if kind == "transcribe":
        return bool(re.fullmatch(r"transcripts/[0-9]{4}-[0-9]{2}-[0-9]{2}/" + re.escape(key) + r"\.json", name))
    if kind == "diarize":
        return name == "diarize-result.json"
    if kind == "organize":
        m = NOTE_PATH.fullmatch(name)
        return bool(m) and m.group(1) == key[-8:]
    if kind == "embed":
        return name == "embed-output.json"
    return False


# --- what the Mac uploads ----------------------------------------------------------------------

def commit(job: jobs.Job, staged: dict[str, Path], body: dict) -> dict:
    """Validate and write the outputs of a finished job. Raises Stale (queue it again) or Invalid."""
    fn = {"transcribe": _commit_transcribe, "diarize": _commit_diarize, "organize": _commit_organize,
          "embed": _commit_embed}[job.kind]
    with COMMIT_LOCK:
        return fn(job, staged, body)


def _json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise Invalid(f"{path.name} is not JSON") from e
    if not isinstance(doc, dict):
        raise Invalid(f"{path.name} is not an object")
    return doc


def _only(staged: dict[str, Path]) -> tuple[str, Path]:
    if len(staged) != 1:
        raise Invalid(f"expected one output, got {sorted(staged)}")
    return next(iter(staged.items()))


def _commit_transcribe(job, staged, body) -> dict:
    name, path = _only(staged)
    doc = _json(path)
    if doc.get("pipeline") != transcribe.PIPELINE or doc.get("session") != job.key:
        raise Invalid("the transcript is of another pipeline or session")
    try:
        session = transcribe.find_session(job.key)
    except transcribe.TranscribeError as e:
        raise Stale("the session is gone") from e
    ids = [s.id for s in session.segments]
    got = doc.get("segments")
    # The session may have grown meanwhile: what was transcribed is a prefix, the rest follows as for any prior.
    if not isinstance(got, list) or not got or ids[:len(got)] != got:
        raise Stale("the session was regrouped while the Mac worked")
    if not _transcript_ok(doc):
        raise Invalid("malformed transcript")
    with lock.writing():
        dest = transcribe.session_path(session)
        if rel(dest) != name:
            raise Invalid("the transcript is for another day")
        _replace(path, dest)
    transcribe.render_day(session.day)
    jobs.enqueue("index", job.key)
    jobs.wake()  # the scan: diarize next
    return {"day": session.day.isoformat()}


def _transcript_ok(doc: dict) -> bool:
    return (isinstance(doc.get("speech"), list) and isinstance(doc.get("start"), str)
            and isinstance(doc.get("end"), str) and all(isinstance(x, dict) and "text" in x and "start" in x
                                                        for x in doc["speech"]))


def _replace(src: Path, dest: Path) -> None:
    """src (in tmp/, same filesystem) becomes dest, atomically, mode 600."""
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as f, src.open("rb") as r:
            f.write(r.read())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _commit_diarize(job, staged, body) -> dict:
    from omy import diarize
    _, path = _only(staged)
    doc = _json(path)
    if doc.get("version") != diarize.VERSION:
        raise Invalid(f"diarize version {doc.get('version')!r}, hub {diarize.VERSION}")
    try:
        session = transcribe.find_session(job.key)
    except transcribe.TranscribeError as e:
        raise Stale("the session is gone") from e
    if body.get("input_hash") != segments_hash(session):
        raise Stale("the session changed while the Mac worked")
    try:
        result = diarize.result_from_json(doc)
    except (KeyError, TypeError, ValueError) as e:
        raise Invalid(f"malformed diarization: {e}") from e
    if not result["turns"] and not result["seconds"] and doc.get("turns"):
        raise Invalid("malformed diarization")
    diarize.commit(session, result)
    transcribe.render_day(session.day)
    jobs.enqueue("index", job.key)
    jobs.wake()  # the scan: organize next
    return {"day": session.day.isoformat()}


def _commit_organize(job, staged, body) -> dict:
    from omy import worker
    from omy.brain import organize, vault
    name, path = _only(staged)
    note = _json(path)
    el = note.get("elaborazione") or {}
    if el.get("prompt_version") != organize.PROMPT_VERSION:
        raise Invalid("note of another prompt version")
    try:
        current = organize.input_hash(job.key)
    except organize.OrganizeError as e:
        raise Stale("the transcript is gone") from e
    if el.get("input_hash") != current:
        raise Stale("the transcript changed while the Mac worked")
    old = organize.note_path(job.key)
    dest = ARCHIVE / name
    if old is not None and old != dest:
        dest = old  # the note keeps its file name when the title changes
    with lock.writing():
        _replace(path, dest)
        vault.render_note(job.key)
    worker.add_to_batch(job.key)
    return {"note": rel(dest)}


def _commit_embed(job, staged, body) -> dict:
    from contextlib import closing
    from omy.brain import embed, index
    _, path = _only(staged)
    vectors = _json(path).get("vectors")
    if not isinstance(vectors, dict):
        raise Invalid("no vectors")
    n = 0
    with closing(index._connect()) as db:
        known = {r[0] for r in db.execute("SELECT id FROM chunks")}
        with db:
            for cid, b64 in vectors.items():
                try:
                    blob = base64.b64decode(b64, validate=True)
                    cid = int(cid)
                except (ValueError, TypeError) as e:
                    raise Invalid("malformed vector") from e
                if len(np.frombuffer(blob, dtype=np.float16)) != embed.EMBED_DIM:
                    raise Invalid("a vector has the wrong dimension")
                if cid in known:  # the chunk may be gone (the session was re-indexed)
                    db.execute("INSERT OR REPLACE INTO vectors(chunk_id, vec) VALUES (?,?)", (cid, blob))
                    n += 1
            if n:
                db.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='version'")
    return {"embedded": n}
