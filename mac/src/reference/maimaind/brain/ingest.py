"""Sync the index with transcripts/<day>.jsonl.

local-omy rewrites a day's file in full (atomically) whenever the day changes,
and the file is always the whole truth about that day. So: read the days whose
file changed since the last read, and make that day's paragraphs exactly the
file's, by id. Paragraphs gone from the file are deleted, and every session with
an added, changed or deleted paragraph becomes stale (its summary is redone).
An empty file is a day of silence. Running it twice changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DAY_FILE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
FORMAT_VERSION = 1
# Paragraphs this short are mostly fragments ("Sì.", "Ok.").
SHORT = 20

COLUMNS = ("id", "day", "session", "ms", "start", "end", "start_ts", "speaker", "person", "via_kind", "via_app",
           "place_name", "lat", "lon", "language", "text", "channel", "weight", "row_hash")


@dataclass
class SyncReport:
    days_read: list[str] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped_lines: int = 0
    sessions_touched: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {"days_read": self.days_read, "added": self.added, "updated": self.updated, "removed": self.removed,
                "skipped_lines": self.skipped_lines, "sessions_touched": len(self.sessions_touched)}


def channel_of(p: dict) -> str:
    # A media voice is something heard, never something said, whoever it resembles.
    if (p.get("via") or {}).get("kind") == "media":
        return "heard"
    return "me" if p.get("person") == "me" else "said"


def weight_of(p: dict, channel: str) -> float:
    """How much to trust a paragraph: yours most, named people next; unnamed
    voices ("Persona N", unknown, not diarized) and media less."""
    if channel == "me":
        w = 1.0
    elif channel == "heard":
        w = 0.5
    elif p.get("person"):
        w = 0.8
    else:
        w = 0.5
    return w * (0.5 if len(p.get("text") or "") < SHORT else 1.0)


def row_of(p: dict, day: str) -> dict:
    if p.get("v") != FORMAT_VERSION:
        raise ValueError(f"format version {p.get('v')!r}")
    for key in ("id", "session", "start", "end", "text"):
        if not isinstance(p.get(key), str):
            raise ValueError(f"missing {key}")
    session, _, ms = p["id"].rpartition("/")
    if session != p["session"] or not ms.isdigit():
        raise ValueError(f"bad id {p['id']!r}")
    via, place = p.get("via") or {}, p.get("place") or {}
    channel = channel_of(p)
    return {
        "id": p["id"], "day": day, "session": session, "ms": int(ms),
        "start": p["start"], "end": p["end"], "start_ts": int(datetime.fromisoformat(p["start"]).timestamp()),
        "speaker": p.get("speaker"), "person": p.get("person"),
        "via_kind": via.get("kind"), "via_app": via.get("app"),
        "place_name": place.get("name"), "lat": place.get("lat"), "lon": place.get("lon"),
        "language": p.get("language"), "text": p["text"].strip(),
        "channel": channel, "weight": weight_of(p, channel),
        "row_hash": hashlib.sha256(json.dumps(p, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
    }


def parse_day(data: bytes, day: str, report: SyncReport) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for n, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = row_of(json.loads(line), day)
        except (ValueError, TypeError, AttributeError) as e:
            print(f"{day}.jsonl:{n}: skipped ({e})", file=sys.stderr)
            report.skipped_lines += 1
            continue
        rows[row["id"]] = row
    return rows


def apply_day(conn: sqlite3.Connection, day: str, rows: dict[str, dict], report: SyncReport) -> None:
    existing = {r["id"]: (r["row_hash"], r["session"])
                for r in conn.execute("SELECT id, row_hash, session FROM paragraphs WHERE day = ?", (day,))}
    for pid, (_, session) in existing.items():
        if pid not in rows:
            conn.execute("DELETE FROM paragraphs WHERE id = ?", (pid,))
            report.removed += 1
            report.sessions_touched.add(session)
    placeholders = ", ".join("?" for _ in COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "id")
    for pid, row in rows.items():
        old = existing.get(pid)
        if old and old[0] == row["row_hash"]:
            continue
        conn.execute(f"INSERT INTO paragraphs({', '.join(COLUMNS)}) VALUES ({placeholders}) "
                     f"ON CONFLICT(id) DO UPDATE SET {updates}", [row[c] for c in COLUMNS])
        if old:
            report.updated += 1
            report.sessions_touched.add(old[1])
        else:
            report.added += 1
        report.sessions_touched.add(row["session"])


def forget_session(conn: sqlite3.Connection, sid: str) -> None:
    """A session gone from the transcripts (re-transcribed under new ids, or
    deleted): its summary goes too. Its vault note stays, marked orphan (vault.py)."""
    for table, col in (("sessions", "id"), ("summaries", "session"), ("summaries_fts", "session"),
                       ("commitments", "session"), ("facts", "session"), ("topics", "session")):
        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (sid,))


def refresh_sessions(conn: sqlite3.Connection, sessions: set[str]) -> None:
    """Recompute the touched sessions from their paragraphs; a session left
    without paragraphs is deleted. A changed session becomes stale."""
    for sid in sessions:
        rows = conn.execute("SELECT * FROM paragraphs WHERE session = ? ORDER BY ms", (sid,)).fetchall()
        if not rows:
            forget_session(conn, sid)
            continue
        content_hash = hashlib.sha256("".join(r["row_hash"] for r in rows).encode()).hexdigest()
        place = next((r["place_name"] for r in rows if r["place_name"]), None)
        kinds = sorted({r["via_kind"] or "room" for r in rows})
        speakers = sorted({r["speaker"] for r in rows if r["speaker"]})
        persons = sorted({r["person"] for r in rows if r["person"]})
        conn.execute(
            """INSERT INTO sessions(id, day, start, end, start_ts, place, kinds, speakers, persons, n_paragraphs,
                                    content_hash, summary_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stale')
               ON CONFLICT(id) DO UPDATE SET day = excluded.day, start = excluded.start, end = excluded.end,
                 start_ts = excluded.start_ts, place = excluded.place, kinds = excluded.kinds,
                 speakers = excluded.speakers, persons = excluded.persons, n_paragraphs = excluded.n_paragraphs,
                 content_hash = excluded.content_hash,
                 summary_status = CASE WHEN sessions.content_hash = excluded.content_hash
                                       THEN sessions.summary_status ELSE 'stale' END""",
            (sid, rows[0]["day"], rows[0]["start"], max(r["end"] for r in rows), rows[0]["start_ts"], place,
             json.dumps(kinds), json.dumps(speakers, ensure_ascii=False), json.dumps(persons), len(rows),
             content_hash))


def sync(conn: sqlite3.Connection, transcripts: Path, force: bool = False) -> SyncReport:
    report = SyncReport()
    files = {p.stem: p for p in transcripts.glob("*.jsonl") if DAY_FILE.fullmatch(p.stem)} \
        if transcripts.is_dir() else {}
    known = {r["day"]: r for r in conn.execute("SELECT * FROM day_files")}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for day in sorted(files):
        try:
            fd = os.open(files[day], os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue  # replaced or removed while listing: next sync
        with os.fdopen(fd, "rb") as f:
            # fstat of the open file: mtime and content belong to the same version.
            st = os.fstat(f.fileno())
            old = known.get(day)
            if not force and old and (old["mtime_ns"], old["size"]) == (st.st_mtime_ns, st.st_size):
                continue
            data = f.read()
        sha = hashlib.sha256(data).hexdigest()
        with conn:
            if force or not old or old["sha256"] != sha:
                report.days_read.append(day)
                before = set(report.sessions_touched)
                apply_day(conn, day, parse_day(data, day, report), report)
                refresh_sessions(conn, report.sessions_touched - before)
            conn.execute("""INSERT INTO day_files(day, mtime_ns, size, sha256, synced_at) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(day) DO UPDATE SET mtime_ns = excluded.mtime_ns, size = excluded.size,
                              sha256 = excluded.sha256, synced_at = excluded.synced_at""",
                         (day, st.st_mtime_ns, st.st_size, sha, now))

    # A day whose file disappeared has no paragraphs any more.
    for day in sorted(set(known) - set(files)):
        with conn:
            report.days_read.append(day)
            before = set(report.sessions_touched)
            apply_day(conn, day, {}, report)
            refresh_sessions(conn, report.sessions_touched - before)
            conn.execute("DELETE FROM day_files WHERE day = ?", (day,))
    return report


def sync_people(conn: sqlite3.Connection, people: dict) -> int:
    """Replace the people table with omy people list (you as "me")."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    me = people.get("me") or {}
    rows = [("me", me.get("name") or "Io", me.get("minutes"), me.get("sessions"), me.get("last_seen"))]
    rows += [(p["id"], p["name"], p.get("minutes"), p.get("sessions"), p.get("last_seen"))
             for p in people.get("people") or [] if p.get("id") and p.get("name")]
    with conn:
        conn.execute("DELETE FROM people")
        conn.executemany("INSERT INTO people(id, name, minutes, sessions, last_seen, synced_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", [(*r, now) for r in rows])
    return len(rows)
