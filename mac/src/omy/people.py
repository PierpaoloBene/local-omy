"""Naming the voices: what the "Persone" tab of the Mac app runs.

  omy speakers list --day D            the voices of each session of a day
  omy speakers play SESSION LABEL      a few seconds of one of them, as a WAV
  omy speakers rename SESSION LABEL N  that voice is N, from now on
  omy speakers unknown SESSION LABEL…  somebody you don't know: stop asking
  omy people list | rename ID N | delete ID

Naming a voice copies its embedding (from voices/clusters) into the person's
samples (voices/people/<id>.npz) and pins that voice to them; every other
session is then relabelled from the stored embeddings, without running
pyannote again: the voices close to the person's samples get their name.
Deleting a person deletes the name and their samples (copies), not the
voices' embeddings: those voices go back to "Persona N" and can be named
again.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from omy import diarize
from omy.audio import StreamDecoder, _read, _write
from omy.config import feature_on
from omy.sessions import LOCAL_TZ, sessions_for_day
from omy.transcribe import CACHE_DIR, TRANSCRIPTS, TranscribeError, _clean, _iso, _write_atomic, render_day
from omy_receiver.segment import SAMPLE_RATE, parse

# Voices with less speech than this are left out of the list: pyannote makes
# short clusters out of coughs and overlaps.
MIN_SECONDS = 3
SAMPLE_MAX = timedelta(seconds=8)
# One file per play (the Mac app and the receiver may play at once); the
# player deletes it, leftovers go after a minute.
SAMPLE_PREFIX = "voice-sample-"
SAMPLE_KEEP = 60


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _docs():
    for path in sorted(TRANSCRIPTS.glob("????-??-??/speakers/*.json")):
        try:
            yield path, json.loads(path.read_text())
        except ValueError:
            continue


def _doc(session_id: str) -> tuple[Path, dict]:
    for path in TRANSCRIPTS.glob(f"????-??-??/speakers/{session_id}.json"):
        doc = json.loads(path.read_text())
        if doc.get("version") != diarize.VERSION:
            raise TranscribeError(f"session {session_id} was diarized by an older version: transcribe it again")
        return path, doc
    raise TranscribeError(f"session {session_id} not diarized")


def _save_people(people: dict) -> None:
    _write_atomic(diarize.PEOPLE, json.dumps({"people": people}, ensure_ascii=False, indent=1) + "\n")


def relabel() -> list[str]:
    """Name again the voices of every diarized session, from the stored
    embeddings and the current people; re-render the days that changed."""
    profile = diarize.load_profile()
    people = diarize.person_profiles()
    media = diarize.media_prints() if feature_on("voice_prints") else None
    days = set()
    for path, doc in _docs():
        if doc.get("version") != diarize.VERSION:
            continue
        clusters = doc["clusters"]
        result = {
            "turns": sorted((x["start"], x["end"], x["label"]) for x in doc["turns"]),
            "embeddings": diarize.cluster_embeddings(doc["session"]),
            "seconds": {k: c["seconds"] for k, c in clusters.items()},
        }
        contexts = {k: (c["context"]["kind"], c["context"]["app"]) for k, c in clusters.items() if c["context"]}
        pins = {k: c["person"] for k, c in clusters.items() if c.get("pinned")}
        sources = {k: c["source"] for k, c in clusters.items() if c.get("source")}
        unknown = {k for k, c in clusters.items() if c.get("unknown")}
        new = diarize.name_clusters(result, profile, contexts, people, pins, sources, unknown, media)
        if new == clusters:
            continue
        doc["clusters"] = new
        for x in doc["turns"]:
            x["speaker"] = new[x["label"]]["name"]
        _write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        days.add(path.parent.parent.name)
    for d in days:
        render_day(date.fromisoformat(d))
    return sorted(days)


def list_day(day: date) -> dict:
    """Per session, its voices (longest first) with a sample of what they said."""
    day_dir = TRANSCRIPTS / day.isoformat()
    sessions = []
    for path in sorted((day_dir / "speakers").glob("*.json")):
        doc = json.loads(path.read_text())
        if doc.get("version") != diarize.VERSION:
            continue
        try:
            speech = _clean(json.loads((day_dir / f"{doc['session']}.json").read_text())["speech"])
        except (FileNotFoundError, ValueError):
            speech = []
        turns = [(_t(x["start"]), _t(x["end"]), x["label"]) for x in doc["turns"]]
        said: dict[str, list[str]] = {}
        for it in speech:
            s, e = _t(it["start"]), _t(it["end"])
            best = max(turns, key=lambda tr: min(e, tr[1]) - max(s, tr[0]), default=None)
            if best and min(e, best[1]) > max(s, best[0]):
                said.setdefault(best[2], []).append(it["text"])
        voices = []
        for label, c in sorted(doc["clusters"].items(), key=lambda kv: -kv[1]["seconds"]):
            if c["seconds"] < MIN_SECONDS and not c.get("person"):
                continue
            text = " ".join(said.get(label, []))
            ctx = diarize.via(c["context"], c.get("source"), "media" in c) or {}
            voices.append({
                "label": label, "name": c["name"], "person": c.get("person"), "pinned": c.get("pinned", False),
                "seconds": c["seconds"], "kind": ctx.get("kind"), "app": ctx.get("app"), "source": c.get("source"),
                "unknown": c.get("unknown", False),
                "sample": text[:240] + ("…" if len(text) > 240 else ""),
            })
        if voices:
            start = min((x[0] for x in turns), default=None)
            sessions.append({"session": doc["session"], "start": _iso(start) if start else None, "voices": voices})
    return {"day": day.isoformat(), "sessions": sessions}


TODO_DAYS = 14


def todo(voice: dict) -> bool:
    """Still to label: nobody yet, not a video, not marked unknown."""
    return voice["person"] is None and voice["kind"] != "media" and not voice["unknown"]


def unlabeled(days: int = TODO_DAYS, hours: int | None = None) -> dict:
    """The sessions of the last recorded days (or, with hours, that started
    in the last hours) with voices still to label, newest first, with all
    their voices."""
    from omy.sessions import recorded_days
    since = datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None
    out = []
    for day in sorted(recorded_days(), reverse=True)[:days]:
        if since and day < since.astimezone(LOCAL_TZ).date():
            break
        for s in reversed(list_day(day)["sessions"]):
            if since and (s["start"] is None or _t(s["start"]) < since):
                continue
            if any(todo(v) for v in s["voices"]):
                out.append({"day": day.isoformat(), **s})
    return {"sessions": out}


def play(session_id: str, label: str) -> dict:
    """What this voice said, up to SAMPLE_MAX (its longest turns, in order),
    into a WAV of its own in the cache; whoever plays it deletes it."""
    import numpy as np
    path, doc = _doc(session_id)
    turns = [(_t(x["start"]), _t(x["end"])) for x in doc["turns"] if x["label"] == label]
    if not turns:
        raise TranscribeError(f"no voice {label} in session {session_id}")
    session = next((s for s in sessions_for_day(date.fromisoformat(path.parent.parent.name)) if s.id == session_id), None)
    if session is None:
        raise TranscribeError(f"audio of session {session_id} not found")
    # The longest turns first, until there are SAMPLE_MAX seconds of them.
    chosen, left = [], SAMPLE_MAX
    for start, end in sorted(turns, key=lambda tr: tr[0] - tr[1]):
        if left <= timedelta(0):
            break
        end = min(end, start + left)
        chosen.append((start, end))
        left -= end - start
    chosen.sort()
    # Turns are seconds into the session's audio as diarization decoded it,
    # from its first segment: decode the same way, or the times don't match.
    last = max(e for _, e in chosen)
    n = max((k for k, seg in enumerate(session.segments) if seg.start <= last), default=0)
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CACHE_DIR / f"voice-{session_id}.wav"
    dec = StreamDecoder(session.segments[0].codec)
    dec.open(tmp)
    try:
        # Plus one segment: the audio runs shorter than the clock, never longer.
        for seg in session.segments[:n + 2]:
            dec.add(parse(seg.bin.read_bytes()))
        dec.close()
        params, pcm = _read(tmp)
    finally:
        dec.close()
        tmp.unlink(missing_ok=True)
    gap = np.zeros(SAMPLE_RATE // 4, dtype=pcm.dtype)
    parts = []
    for start, end in chosen:
        a = int((start - session.start).total_seconds() * SAMPLE_RATE)
        z = int((end - session.start).total_seconds() * SAMPLE_RATE)
        parts += [pcm[max(a, 0):z], gap]
    for old in CACHE_DIR.glob(f"{SAMPLE_PREFIX}*.wav"):
        if time.time() - old.stat().st_mtime > SAMPLE_KEEP:
            old.unlink(missing_ok=True)
    fd, name = tempfile.mkstemp(dir=CACHE_DIR, prefix=SAMPLE_PREFIX, suffix=".wav")
    os.close(fd)
    _write(Path(name), params, np.concatenate(parts))
    return {"path": name, "seconds": round(sum((e - s).total_seconds() for s, e in chosen), 1)}


def _remove_sample(people: dict, key: str) -> None:
    """Take this voice out of whoever it was named after; drop people left
    with no samples."""
    for pid in list(people):
        samples = diarize.person_samples(pid)
        if key in samples:
            del samples[key]
            diarize.save_npz(diarize.PEOPLE_DIR / f"{pid}.npz", samples)
            if not samples:
                del people[pid]


def _require_prints() -> None:
    if not feature_on("voice_prints"):
        raise TranscribeError("voice prints are turned off (Impostazioni, Funzioni)")


def rename(session_id: str, label: str, name: str) -> dict:
    """This voice is name (you, an existing person, or a new one), from now on."""
    _require_prints()
    name = " ".join(name.split())
    if not name:
        raise TranscribeError("empty name")
    path, doc = _doc(session_id)
    clusters = doc["clusters"]
    if label not in clusters:
        raise TranscribeError(f"no voice {label} in session {session_id}")
    key = f"{session_id}/{label}"
    people = diarize.load_people()
    _remove_sample(people, key)
    if name.casefold() == diarize.my_name().casefold():
        pin = "me"
    else:
        emb = diarize.cluster_embeddings(session_id).get(label)
        if emb is None:
            raise TranscribeError("this voice has no voice print (too short or deleted)")
        pin = next((pid for pid, p in people.items() if p["name"].casefold() == name.casefold()), None)
        if pin is None:
            pin = uuid.uuid4().hex[:12]
            people[pin] = {"name": name, "created": _iso(datetime.now(timezone.utc))}
        samples = diarize.person_samples(pin)
        samples[key] = emb
        diarize.save_npz(diarize.PEOPLE_DIR / f"{pin}.npz", samples)
    _save_people(people)
    for k, c in clusters.items():
        if k != label and c.get("pinned") and c.get("person") == pin:
            c["pinned"] = False
    clusters[label].update(person=pin, pinned=True)
    clusters[label].pop("unknown", None)
    # A voice with a name is somebody, not a video.
    if clusters[label].get("source") == "media":
        del clusters[label]["source"]
    _write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return {"person": pin, "name": name, "days": relabel()}


def set_source(session_id: str, labels: list[str], source: str) -> dict:
    """Where these voices come from (diarize.SOURCES), or "auto" for what the
    Mac and the iPhone found. A "media" voice is nobody: its name and its
    sample in a person's voice print go."""
    if source not in (*diarize.SOURCES, "auto"):
        raise TranscribeError(f"invalid source {source!r}")
    path, doc = _doc(session_id)
    clusters = doc["clusters"]
    missing = [label for label in labels if label not in clusters]
    if missing or not labels:
        raise TranscribeError(f"no voice {', '.join(missing)} in session {session_id}")
    people = diarize.load_people()
    for label in labels:
        c = clusters[label]
        if source == "auto":
            c.pop("source", None)
        else:
            c["source"] = source
        if source == "media":
            _remove_sample(people, f"{session_id}/{label}")
            c.update(person=None, pinned=False)
            c.pop("unknown", None)
    _save_people(people)
    _write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return {"source": source, "labels": labels, "days": relabel()}


def set_unknown(session_id: str, labels: list[str], unknown: bool = True) -> dict:
    """These voices are somebody you don't know (diarize.UNKNOWN_NAME): out
    of the voices to label, out of anyone's voice print, never recognized as
    anyone. With unknown False they are just unnamed again."""
    path, doc = _doc(session_id)
    clusters = doc["clusters"]
    missing = [label for label in labels if label not in clusters]
    if missing or not labels:
        raise TranscribeError(f"no voice {', '.join(missing)} in session {session_id}")
    people = diarize.load_people()
    for label in labels:
        c = clusters[label]
        if not unknown:
            c.pop("unknown", None)
            continue
        _remove_sample(people, f"{session_id}/{label}")
        c.update(person=None, pinned=False, unknown=True)
        # Somebody, so not a video.
        if c.get("source") == "media":
            del c["source"]
    _save_people(people)
    _write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return {"unknown": unknown, "labels": labels, "days": relabel()}


def set_person_source(person_id: str, source: str) -> dict:
    """Where all the voices of this person come from (diarize.SOURCES, or
    "auto"). "media": it was a video, so its voices become video and the
    name and its samples go."""
    if source not in (*diarize.SOURCES, "auto"):
        raise TranscribeError(f"invalid source {source!r}")
    people = diarize.load_people()
    if person_id not in people:
        raise TranscribeError(f"no person {person_id}")
    voices = 0
    for path, doc in _docs():
        changed = False
        for c in doc.get("clusters", {}).values():
            if c.get("person") != person_id:
                continue
            if source == "media":
                c.update(person=None, pinned=False)
            if source == "auto":
                c.pop("source", None)
            else:
                c["source"] = source
            changed = True
            voices += 1
        if changed:
            _write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    name = people[person_id]["name"]
    if source == "media":
        (diarize.PEOPLE_DIR / f"{person_id}.npz").unlink(missing_ok=True)
        del people[person_id]
        _save_people(people)
    return {"person": person_id, "name": name, "source": source, "voices": voices, "days": relabel()}


def list_people() -> dict:
    """You and the named people, with how much they were heard."""
    people = diarize.load_people()
    stats: dict[str, dict] = {pid: {"seconds": 0.0, "sessions": 0, "last_seen": None} for pid in [*people, "me"]}
    for path, doc in _docs():
        day = path.parent.parent.name
        for c in doc.get("clusters", {}).values():
            st = stats.get(c.get("person") or "")
            if st:
                st["seconds"] += c["seconds"]
                st["sessions"] += 1
                st["last_seen"] = max(st["last_seen"] or day, day)

    def row(st: dict) -> dict:
        return {"minutes": round(st["seconds"] / 60, 1), "sessions": st["sessions"], "last_seen": st["last_seen"]}

    out = [{"id": pid, "name": p["name"], "samples": len(diarize.person_samples(pid)), **row(stats[pid])}
           for pid, p in people.items()]
    out.sort(key=lambda r: (r["last_seen"] or "", r["minutes"]), reverse=True)
    from omy import review
    return {"me": {"name": diarize.my_name(), "enrolled": diarize.PROFILE.is_file(), **row(stats["me"])},
            "people": out, "index": review.people_index()}


def rename_person(person_id: str, name: str) -> dict:
    """New name; if another person already has it, the two become one."""
    name = " ".join(name.split())
    people = diarize.load_people()
    if person_id not in people:
        raise TranscribeError(f"no person {person_id}")
    if not name or name.casefold() == diarize.my_name().casefold():
        raise TranscribeError(f"invalid name: {name!r}")
    other = next((pid for pid, p in people.items() if pid != person_id and p["name"].casefold() == name.casefold()), None)
    if other:
        samples = diarize.person_samples(other) | diarize.person_samples(person_id)
        diarize.save_npz(diarize.PEOPLE_DIR / f"{other}.npz", samples)
        (diarize.PEOPLE_DIR / f"{person_id}.npz").unlink(missing_ok=True)
        del people[person_id]
        _repin(person_id, other)
    else:
        people[person_id]["name"] = name
    _save_people(people)
    return {"person": other or person_id, "name": name, "days": relabel()}


def delete_person(person_id: str) -> dict:
    """Forget this name: the person and their samples. The voices keep their
    embeddings (voices/clusters), go back to "Persona N" and can be named
    again."""
    people = diarize.load_people()
    if person_id not in people:
        raise TranscribeError(f"no person {person_id}")
    (diarize.PEOPLE_DIR / f"{person_id}.npz").unlink(missing_ok=True)
    name = people.pop(person_id)["name"]
    _save_people(people)
    _repin(person_id, None)
    return {"deleted": name, "days": relabel()}


def _repin(old: str, new: str | None) -> None:
    for path, doc in _docs():
        changed = False
        for c in doc.get("clusters", {}).values():
            if c.get("person") == old:
                c["person"], c["pinned"] = (new, c.get("pinned", False)) if new else (None, False)
                changed = True
        if changed:
            _write_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
