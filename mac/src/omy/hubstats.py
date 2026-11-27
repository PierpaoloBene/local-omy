"""What the hub dashboard's Omi widget and the Mac app show (GET /v1/widget, /v1/status): recorded today, the
queue, whether the Mac is there, the pendant battery. Plain reads of the archive and the queue, no models."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, time as dtime, timedelta

from omy import battery, hubstate, jobs, status
from omy.config import ARCHIVE
from omy.sessions import LOCAL_TZ

_cache: dict = {}


def _cached(key: str, ttl: float, fn):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def organize_end() -> dtime:
    """When the night window closes (default 01:00-07:00)."""
    from omy import models, worker
    spec = str(models.config_value("organize_window", worker.DEFAULT_WINDOW))
    try:
        return datetime.strptime(spec.split("-")[1].strip(), "%H:%M").time()
    except (IndexError, ValueError):
        return dtime(7, 0)


def queue(now: datetime | None = None) -> dict:
    """The jobs by state, and the organize jobs that waited through the night: queued since before the window
    closed this morning, and the window is closed now."""
    now = now or datetime.now(LOCAL_TZ)
    s = jobs.summary()
    by_kind: dict[str, dict[str, int]] = {}
    for kind, states in s["by_kind"].items():
        n = {k: states.get(k, 0) for k in ("queued", "leased", "running")}
        if any(n.values()):
            by_kind[kind] = {"queued": n["queued"], "active": n["leased"] + n["running"]}
    closed = datetime.combine(now.date(), organize_end(), tzinfo=LOCAL_TZ)
    missed = 0
    if now >= closed:
        from contextlib import closing
        with closing(jobs._connect()) as db:
            missed = db.execute("SELECT COUNT(*) FROM jobs WHERE kind='organize' AND state='queued' AND created<?",
                                (closed.timestamp(),)).fetchone()[0]
    oldest = s.get("oldest_queued")
    return {"queued": s["queued"], "active": len(s["running"]), "failed": len(s["failed"]),
            "oldest_age_min": round((time.time() - oldest) / 60) if oldest else None, "by_kind": by_kind,
            "running": s["running"], "failed_jobs": s["failed"], "organize_missed": missed}


def mac() -> dict:
    from omy import worker
    seen = hubstate.read().get("last_seen")
    st = worker.read_status()
    caps = hubstate.read().get("capabilities") or {}
    problems = hubstate.version_problems()
    return {"online": hubstate.online(), "last_seen_s": round(time.time() - seen) if seen else None,
            "state": st.get("state"), "job": st.get("job"), "progress": st.get("progress"),
            "kinds": caps.get("kinds"), "version_ok": not problems, "version_problems": problems,
            "on_ac": st.get("on_ac"), "in_window": st.get("in_window"), "last_error": st.get("last_error")}


def pendant() -> dict:
    def read() -> dict:
        b, fw = battery.battery(), battery.firmware()
        at = b.get("at")
        age = round((datetime.now(LOCAL_TZ) - datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds() / 60) \
            if at else None
        return {"battery": b.get("level"), "drain_pct_h": b.get("drain_per_hour"), "hours_left": b.get("remaining_hours"),
                "firmware": fw.get("version"), "last_reading_min": age}
    return _cached("pendant", 60.0, read)


def disk() -> dict:
    def read() -> dict:
        size = sum(f.stat().st_size for f in ARCHIVE.rglob("*") if f.is_file()) if ARCHIVE.is_dir() else 0
        return {"omy_gb": round(size / 1e9, 2), "free_gb": round(shutil.disk_usage(ARCHIVE).free / 1e9, 1)}
    return _cached("disk", 600.0, read)


def widget() -> dict:
    arch = _cached("archive", 15.0, status._archive)
    up = status._upload()
    return {
        "today": {"minutes": arch["today_minutes"], "segments": arch["today_segments"],
                  "sessions": arch["today_sessions"], "last_end": arch["last_segment_end"],
                  "minutes_since_last": arch["minutes_since_last"]},
        "queue": queue(), "mac": mac(), "pendant": pendant(),
        "iphone_upload": {"last_upload_at": up["last_upload_at"], "queue_remaining": up["queue_remaining"],
                          "minutes_since": up["minutes_since"]},
        "disk": disk(),
        "asks": ask_summary(),
        "now": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
    }


def ask_summary() -> dict:
    from omy import asks
    return asks.summary()


def last_segment_start() -> str | None:
    from omy import iphone
    t = iphone._last_segment_start()
    return t.isoformat().replace("+00:00", "Z") if t else None


def status_doc() -> dict:
    arch, up = status._archive(), status._upload()
    return {"archive": arch, "upload": up, "last_segment_start": last_segment_start(), "mac": mac(),
            "queue": queue(), "battery": battery.battery(), "firmware": battery.firmware(),
            "transcripts": {"pending_sessions": status._transcripts()["pending_sessions"]}}
