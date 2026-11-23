"""Everything the Mac app's status tab shows, in one call (omy status)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from omy import battery, iphone, macsetup, transcribe
from omy.config import ARCHIVE
from omy.sessions import LOCAL_TZ, sessions_for_day
from omy.transcribe import TRANSCRIPTS

LOGS = {
    name: str(Path(f"~/Library/Logs/local-omy-{name}.log").expanduser()) for name in (*macsetup.AGENTS, "models")
}


def _archive() -> dict:
    today = datetime.now(LOCAL_TZ).date()
    sessions = sessions_for_day(today)
    segments = [s for sess in sessions for s in sess.segments]
    last = max(segments, key=lambda s: s.end, default=None)
    if last is None:
        # Nothing today: look for the newest segment of earlier days.
        days = sorted((ARCHIVE / "raw" / "audio").glob("????-??-??"), reverse=True)
        for day in days[:7]:
            ids = sorted(p.stem for p in day.glob("*.bin"))
            if ids:
                last_id = ids[-1]
                break
        else:
            last_id = None
    return {
        "path": str(ARCHIVE),
        "today_segments": len(segments),
        "today_minutes": round(sum((s.end - s.start).total_seconds() for s in segments) / 60, 1),
        "today_sessions": len(sessions),
        "last_segment_end": last.end.isoformat() if last else None,
        "last_segment_id": last.id if last else last_id,
        "minutes_since_last": round((datetime.now(timezone.utc) - last.end).total_seconds() / 60, 1)
        if last else None,
    }


def _upload() -> dict:
    """Last upload accepted by the receiver. queue_remaining is what the phone
    still had queued then (None from builds that don't say)."""
    try:
        state = json.loads((ARCHIVE / "receiver.json").read_text())
    except (FileNotFoundError, ValueError):
        # Receiver older than receiver.json: the newest segment file's mtime.
        days = sorted((ARCHIVE / "raw" / "audio").glob("????-??-??"), reverse=True)[:7]
        newest = max((p.stat().st_mtime for d in days for p in d.glob("*.bin")), default=None)
        state = {"last_upload_at": datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds")
                 if newest else None}
    if not state.get("last_upload_at"):
        return {"last_upload_at": None, "minutes_since": None, "queue_remaining": None}
    at = datetime.fromisoformat(state["last_upload_at"])
    return {
        "last_upload_at": state["last_upload_at"],
        "minutes_since": round((datetime.now(timezone.utc) - at).total_seconds() / 60, 1),
        "queue_remaining": state.get("queue_remaining"),
    }


def _transcripts() -> dict:
    days = sorted((p.stem for p in TRANSCRIPTS.glob("????-??-??.md")), reverse=True) \
        if TRANSCRIPTS.is_dir() else []
    todo, still_open = transcribe.plan(transcribe.pending_days())
    return {
        "path": str(TRANSCRIPTS),
        "days": days,
        "pending_sessions": len(todo),
        "pending_minutes": round(sum(transcribe._minutes(transcribe.remaining(*t)) for t in todo), 1),
        "open_sessions": len(still_open),
        "progress_file": str(transcribe.PROGRESS),
        "progress": transcribe.progress(),
    }


def _worker() -> dict:
    """The worker's LaunchAgent plus what it reports in cache/worker.json."""
    from omy import worker
    info = macsetup.agent_info("worker")
    st = worker.read_status()
    queue = st.get("queue") or {}
    hb = st.get("heartbeat")
    info.update({
        "state": st.get("state"),
        "job": st.get("job"),
        "loaded_model": st.get("loaded"),
        "chat_active": st.get("chat_active"),
        "last_error": st.get("last_error"),
        "queued": queue.get("queued", 0),
        "by_kind": queue.get("by_kind", {}),
        "failed": queue.get("failed", []),
        "heartbeat_age": round(time.time() - hb, 1) if isinstance(hb, (int, float)) else None,
    })
    return info


def brain_status() -> dict:
    """The second brain at a glance: notes in the vault, the index, the night window."""
    import sqlite3
    from contextlib import closing
    from omy import models, worker
    from omy.brain import BRAIN, NOTES, index
    sidecars = sorted(NOTES.glob("*/*/*/*.json"))
    last = None
    if sidecars:
        try:
            sc = json.loads(sidecars[-1].read_text())
            last = {"id": sc.get("id"), "titolo": sc.get("titolo"), "data": sc.get("data")}
        except (OSError, ValueError):
            pass
    chunks, version = 0, 0
    if index.INDEX_PATH.exists():
        try:
            with closing(sqlite3.connect(f"file:{index.INDEX_PATH}?mode=ro", uri=True, timeout=5)) as db:
                chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                version = int(row[0]) if row else 0
        except (sqlite3.Error, ValueError):
            pass
    window = str(models.config_value("organize_window", worker.DEFAULT_WINDOW))
    return {
        "notes": len(sidecars),
        "last_note": last,
        "index": {"chunks": chunks, "version": version},
        "vault": str(BRAIN),
        "window": window,
        "in_window": worker.in_window(window),
        "on_ac": worker.on_ac(),
    }


def status() -> dict:
    info = {
        "receiver": macsetup.agent_info("receiver"),
        "worker": _worker(),
    }
    if macsetup._plist_path("periodic").exists():  # retired agent still installed
        info["periodic"] = macsetup.agent_info("periodic")
    return {
        **info,
        "tailscale": macsetup.tailscale_info(),
        "archive": _archive(),
        "upload": _upload(),
        "iphone": iphone.status(),
        "transcripts": _transcripts(),
        "battery": battery.battery(),
        "firmware": battery.firmware(),
        "logs": LOGS,
    }
