"""How long the pendant battery lasts, from the level the iPhone puts in each
segment sidecar ("battery", 0-100, when the pendant reported it).

Drain is measured on wall-clock time between readings, recording or not
(the pendant runs either way), and a reading higher than the previous one
ends a stretch: it was charged. Over a few days the levels give a rate in
%/hour, so a full charge lasts 100 / rate hours.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from omy.config import ARCHIVE
from omy.sessions import recorded_days

DAYS = 14
# Readings further apart than this are not joined: too little is known.
MAX_GAP = timedelta(hours=12)
# A rise this large is a charge; smaller ones are the level wobbling.
CHARGE_RISE = 3


def readings(days: int = DAYS) -> list[tuple[datetime, int]]:
    out = []
    for day in recorded_days()[-days:]:
        for meta_path in (ARCHIVE / "raw" / "audio" / day.isoformat()).glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text())
                out.append((datetime.fromisoformat(meta["end"].replace("Z", "+00:00")), int(meta["battery"])))
            except (ValueError, KeyError, TypeError):
                continue
    return sorted(out)


def battery(days: int = DAYS) -> dict:
    r = readings(days)
    if not r:
        return {"level": None}
    drop, hours, charges = 0, 0.0, 0
    # Drops are counted from the last counted level; rises below CHARGE_RISE
    # are ignored, so a level wobbling by a point doesn't add up to drain.
    top_t, top = r[0]
    prev_t = r[0][0]
    for t, level in r[1:]:
        if level >= top + CHARGE_RISE or t - prev_t > MAX_GAP:
            charges += level >= top + CHARGE_RISE
            top_t, top = t, level
        prev_t = t
        stretch_drop, stretch_hours = top - level, (t - top_t).total_seconds() / 3600
        if stretch_drop > 0 and stretch_hours > 0:
            drop += stretch_drop
            hours += stretch_hours
            top_t, top = t, level
    rate = drop / hours if hours >= 1 and drop > 0 else None
    level_t, level = r[-1]
    return {
        "level": level,
        "at": level_t.isoformat().replace("+00:00", "Z"),
        "readings": len(r),
        "hours_measured": round(hours, 1),
        "drain_per_hour": round(rate, 2) if rate else None,
        "full_charge_hours": round(100 / rate, 1) if rate else None,
        "remaining_hours": round(level / rate, 1) if rate else None,
        "charges": charges,
    }


def firmware(days: int = DAYS) -> dict:
    """The pendant firmware revision in the newest sidecar that has one
    ("firmware", read by the iPhone app at each connection)."""
    for day in reversed(recorded_days()[-days:]):
        metas = []
        for meta_path in (ARCHIVE / "raw" / "audio" / day.isoformat()).glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text())
                if isinstance(meta.get("firmware"), str):
                    metas.append((meta["end"], meta["firmware"]))
            except (ValueError, KeyError, TypeError):
                continue
        if metas:
            at, version = max(metas)
            return {"version": version, "at": at}
    return {"version": None}
