"""Where the recordings were made: the phone's location in the sidecars,
named with your own list of places, offline.

The iPhone puts in each segment sidecar its best fix ("location": {"lat",
"lon", "acc", "source", "at"}): an iOS visit (accurate to tens of meters) or
a significant location change (hundreds of meters). Nothing is geocoded.

~/OmiArchive/places.json, mode 600, written by `omy places add` (or by hand):
  {"places": [{"name": "Casa", "lat": 45.4642, "lon": 9.19, "radius": 150}]}
A fix is a place when it is within its radius (plus up to MAX_SLACK of the
fix's own inaccuracy); otherwise it is shown as coordinates. `omy places
list` also shows where you spent time that has no name yet.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from omy.config import ARCHIVE
from omy.sessions import LOCAL_TZ, recorded_days

PLACES = ARCHIVE / "places.json"
DEFAULT_RADIUS = 150
MAX_SLACK = 100
# Unnamed fixes closer than this are one spot in `places list`.
SPOT_RADIUS = 150


@dataclass(frozen=True)
class Fix:
    start: datetime  # of the segment
    end: datetime
    lat: float
    lon: float
    acc: float
    source: str


def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Meters, haversine."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def load_places() -> list[dict]:
    try:
        return json.loads(PLACES.read_text())["places"]
    except (FileNotFoundError, ValueError, KeyError):
        return []


def _save(places: list[dict]) -> None:
    from omy.transcribe import _write_atomic
    _write_atomic(PLACES, json.dumps({"places": places}, ensure_ascii=False, indent=1) + "\n")


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def segment_fix(segment_id: str) -> Fix | None:
    """The fix in a segment's sidecar (raw/audio/<local day>/<id>.json)."""
    start = datetime.strptime(segment_id[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    day = start.astimezone(LOCAL_TZ).date()
    try:
        meta = json.loads((ARCHIVE / "raw" / "audio" / day.isoformat() / f"{segment_id}.json").read_text())
        loc = meta["location"]
        return Fix(_t(meta["start"]), _t(meta["end"]), loc["lat"], loc["lon"], loc["acc"], loc["source"])
    except (FileNotFoundError, ValueError, KeyError):
        return None


def name_of(fix: Fix, places: list[dict]) -> str | None:
    best, best_d = None, None
    for p in places:
        d = distance(fix.lat, fix.lon, p["lat"], p["lon"])
        if d <= p.get("radius", DEFAULT_RADIUS) + min(fix.acc, MAX_SLACK) and (best_d is None or d < best_d):
            best, best_d = p["name"], d
    return best


def label(fix: Fix, places: list[dict]) -> str:
    return name_of(fix, places) or f"{fix.lat:.4f}, {fix.lon:.4f} (±{round(fix.acc)} m)"


def session_places(segment_ids: list[str], places: list[dict] | None = None) -> list[str]:
    """The places of a session in order, consecutive repeats merged."""
    places = load_places() if places is None else places
    out: list[str] = []
    for sid in segment_ids:
        fix = segment_fix(sid)
        if fix:
            name = label(fix, places)
            if not out or out[-1] != name:
                out.append(name)
    return out


def _all_fixes() -> list[Fix]:
    fixes = []
    for day in recorded_days():
        for meta_path in (ARCHIVE / "raw" / "audio" / day.isoformat()).glob("*.json"):
            try:
                meta = json.loads(meta_path.read_text())
                loc = meta["location"]
                fixes.append(Fix(_t(meta["start"]), _t(meta["end"]), loc["lat"], loc["lon"], loc["acc"], loc["source"]))
            except (ValueError, KeyError):
                continue
    return sorted(fixes, key=lambda f: f.start)


def _sample(spot: dict) -> str:
    """Something said at the spot (the longest paragraph of its last day in
    transcripts/DAY.jsonl), to recognize it without a map."""
    from omy.transcribe import TRANSCRIPTS
    best = ""
    try:
        lines = (TRANSCRIPTS / f"{spot['last']}.jsonl").read_text().splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        r = json.loads(line)
        p = r.get("place")
        if p and distance(p["lat"], p["lon"], spot["lat"], spot["lon"]) <= SPOT_RADIUS and len(r["text"]) > len(best):
            best = r["text"]
    return best[:200]


def list_places() -> dict:
    """Named places with the time recorded there, and the unnamed spots where
    the most was recorded (to name them), each with what helps to recognize
    it offline: days, time of day, the nearest named place, a sample."""
    places = load_places()
    named = {p["name"]: {"seconds": 0.0, "last": None} for p in places}
    spots: list[dict] = []
    for f in _all_fixes():
        local = f.start.astimezone(LOCAL_TZ)
        n = name_of(f, places)
        if n:
            named[n]["seconds"] += (f.end - f.start).total_seconds()
            named[n]["last"] = local.date().isoformat()
            continue
        if f.acc > SPOT_RADIUS * 2:
            continue
        spot = next((s for s in spots if distance(f.lat, f.lon, s["lat"], s["lon"]) <= SPOT_RADIUS), None)
        if spot is None:
            spot = {"lat": f.lat, "lon": f.lon, "seconds": 0.0, "last": None, "days": set(), "times": []}
            spots.append(spot)
        spot["seconds"] += (f.end - f.start).total_seconds()
        spot["last"] = local.date().isoformat()
        spot["days"].add(spot["last"])
        spot["times"].append(local.strftime("%H:%M"))
    spots.sort(key=lambda s: -s["seconds"])

    def nearest(s: dict) -> dict | None:
        d = [(distance(s["lat"], s["lon"], p["lat"], p["lon"]), p["name"]) for p in places]
        return {"name": min(d)[1], "meters": round(min(d)[0])} if d else None

    return {
        "places": [{**p, "radius": p.get("radius", DEFAULT_RADIUS),
                    "hours": round(named[p["name"]]["seconds"] / 3600, 1),
                    "minutes": round(named[p["name"]]["seconds"] / 60),
                    "last_seen": named[p["name"]]["last"]} for p in places],
        "unnamed": [{"lat": round(s["lat"], 5), "lon": round(s["lon"], 5),
                     "hours": round(s["seconds"] / 3600, 1), "minutes": round(s["seconds"] / 60), "last_seen": s["last"], "days": len(s["days"]),
                     "from": min(s["times"]), "to": max(s["times"]), "nearest": nearest(s),
                     "sample": _sample(s)} for s in spots[:10]],
    }


def add(name: str, lat: float, lon: float, radius: int = DEFAULT_RADIUS) -> dict:
    from omy.transcribe import TranscribeError
    name = " ".join(name.split())
    if not name or not -90 <= lat <= 90 or not -180 <= lon <= 180 or not 10 <= radius <= 5000:
        raise TranscribeError("invalid place")
    places = [p for p in load_places() if p["name"].casefold() != name.casefold()]
    places.append({"name": name, "lat": lat, "lon": lon, "radius": radius})
    _save(places)
    return {"added": name, "days": _rerender()}


def rename(name: str, new: str) -> dict:
    from omy.transcribe import TranscribeError
    new = " ".join(new.split())
    places = load_places()
    p = next((p for p in places if p["name"].casefold() == name.casefold()), None)
    if p is None:
        raise TranscribeError(f"no place {name!r}")
    if not new or any(q is not p and q["name"].casefold() == new.casefold() for q in places):
        raise TranscribeError(f"invalid or already used name {new!r}")
    p["name"] = new
    _save(places)
    return {"renamed": name, "name": new, "days": _rerender()}


def remove(name: str) -> dict:
    places = load_places()
    kept = [p for p in places if p["name"].casefold() != name.casefold()]
    if len(kept) == len(places):
        from omy.transcribe import TranscribeError
        raise TranscribeError(f"no place {name!r}")
    _save(kept)
    return {"removed": name, "days": _rerender()}


def _rerender() -> list[str]:
    """Rebuild the markdown of the days with locations."""
    from omy.transcribe import TRANSCRIPTS, render_day
    days = sorted({f.start.astimezone(LOCAL_TZ).date() for f in _all_fixes()})
    return [d.isoformat() for d in days if (TRANSCRIPTS / d.isoformat()).is_dir() and render_day(d)]
