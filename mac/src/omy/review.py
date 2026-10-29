"""What the iPhone app shows of the transcripts (omy transcripts): the days
with a summary, one day, a search. Read only from transcripts/DAY.jsonl
(written by render_day), so it is exactly what other apps see."""

from __future__ import annotations

import json
from datetime import date, datetime

from omy import transcribe
from omy.sessions import LOCAL_TZ
from omy.transcribe import TRANSCRIPTS

SEARCH_LIMIT = 200
CONVERSATIONS_LIMIT = 100
CONVERSATIONS_MAX = 5000
# What kind of voice a paragraph is, from its via: in the room, an online
# meeting, a phone call (iPhone), a video (or a TV).
TYPES = ("room", "meeting", "call", "media")


def _records(day: str) -> list[dict]:
    try:
        return [json.loads(line) for line in (TRANSCRIPTS / f"{day}.jsonl").read_text().splitlines() if line]
    except FileNotFoundError:
        return []


def _summary(records: list[dict]) -> dict:
    """How long, who (longest first) and where (in order) for these records."""
    seconds = 0.0
    speakers: dict[str, dict] = {}
    places: list[str] = []
    unnamed = False
    for r in records:
        s = (datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds()
        seconds += s
        if r["speaker"]:
            sp = speakers.setdefault(r["speaker"], {"name": r["speaker"], "person": r["person"], "seconds": 0.0})
            sp["seconds"] += s
        p = r["place"]
        if p and p["name"]:
            if p["name"] not in places:
                places.append(p["name"])
        elif p:
            unnamed = True
    return {
        "minutes": round(seconds / 60),
        "sessions": len({r["session"] for r in records}),
        "paragraphs": len(records),
        "from": records[0]["start"][11:16] if records else None,
        "to": records[-1]["end"][11:16] if records else None,
        "speakers": [{**sp, "minutes": round(sp.pop("seconds") / 60, 1)}
                     for sp in sorted(speakers.values(), key=lambda sp: -sp["seconds"])],
        "places": places,
        "unnamed_places": unnamed,
    }


def _days() -> list[str]:
    return sorted((p.stem for p in TRANSCRIPTS.glob("????-??-??.jsonl")), reverse=True) \
        if TRANSCRIPTS.is_dir() else []


def pending() -> dict:
    """Sessions waiting for a transcript, and the transcription running now."""
    todo, still_open = transcribe.plan(transcribe.pending_days())
    return {
        "sessions": len(todo),
        "minutes": round(sum(transcribe._minutes(transcribe.remaining(*t)) for t in todo), 1),
        "open_sessions": len(still_open),
        "progress": transcribe.progress(),
    }


def list_days() -> dict:
    """Every day with a summary; today is the archive's local day (the
    phone may be in another time zone)."""
    return {"days": [{"day": d, **_summary(_records(d))} for d in _days()], "pending": pending(),
            "today": datetime.now(LOCAL_TZ).date().isoformat()}


def day(d: date) -> dict:
    records = _records(d.isoformat())
    return {"day": d.isoformat(), "summary": _summary(records), "records": records}


def _type(r: dict) -> str:
    via = r["via"]
    if not via:
        return "room"
    if via["kind"] == "media":
        return "media"
    return "call" if via["app"] == "iPhone" else "meeting"


def _sessions(records: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in records:
        out.setdefault(r["session"], []).append(r)
    return out


def _matches(records: list[dict], person: str | None, kind: str | None) -> bool:
    """A conversation is with this person if they spoke in it, and of this
    type if somebody other than you spoke that way."""
    if person and not any(r["person"] == person for r in records):
        return False
    return not kind or any(_type(r) == kind and r["person"] != "me" and r["speaker"] for r in records)


def _conversation(day: str, records: list[dict]) -> dict:
    who: dict[tuple, dict] = {}
    for r in records:
        if not r["speaker"]:
            continue
        t = _type(r)
        p = who.setdefault((r["speaker"], r["person"]), {"name": r["speaker"], "person": r["person"], "type": t,
                                                        "seconds": 0.0})
        p["seconds"] += (datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds()
    others = [r for r in records if r["person"] != "me"] or records
    place = next((r["place"] for r in records if r["place"]), None)
    via = next((r["via"] for r in records if r["via"] and r["via"]["kind"] == "call"), None)
    return {
        "day": day,
        "session": records[0]["session"],
        "focus": records[0]["id"],
        "start": records[0]["start"],
        "end": records[-1]["end"],
        "minutes": round((datetime.fromisoformat(records[-1]["end"])
                          - datetime.fromisoformat(records[0]["start"])).total_seconds() / 60),
        "place": place and (place["name"] or f"{place['lat']:.4f}, {place['lon']:.4f}"),
        "app": via and via["app"],
        "types": sorted({p["type"] for p in who.values() if p["person"] != "me"}),
        "participants": [{**p, "minutes": round(p.pop("seconds") / 60, 1)}
                         for p in sorted(who.values(), key=lambda p: (p["person"] != "me", -p["seconds"]))],
        "snippet": max((r["text"] for r in others), key=len)[:200],
    }


def conversations(person: str | None = None, kind: str | None = None, limit: int = CONVERSATIONS_LIMIT,
                  day: date | None = None, before: date | None = None) -> dict:
    """The sessions with this person, of this type and on this day, newest
    first: whole days, until at least limit conversations; then more is
    true and next is the before of the following page. Only the days needed
    are read."""
    if kind and kind not in TYPES:
        raise transcribe.TranscribeError(f"invalid type {kind!r}")
    if not 0 < limit <= CONVERSATIONS_MAX:
        raise transcribe.TranscribeError(f"limit must be 1-{CONVERSATIONS_MAX}")
    days = [d for d in _days() if (day is None or d == day.isoformat())
            and (before is None or d < before.isoformat())]
    out: list[dict] = []
    for i, d in enumerate(days):
        if len(out) >= limit:
            return {"conversations": out, "more": True, "next": days[i - 1]}
        for records in reversed(list(_sessions(_records(d)).values())):
            if _matches(records, person, kind):
                out.append(_conversation(d, records))
    return {"conversations": out, "more": False, "next": None}


def people_index() -> dict:
    """For every named person, what kinds of conversation, how many, and
    where (the three places seen most)."""
    people: dict[str, dict] = {}
    for d in _days():
        for r in _records(d):
            if r["person"] and r["person"] != "me":
                p = people.setdefault(r["person"], {"types": set(), "places": {}, "sessions": set()})
                p["types"].add(_type(r))
                p["sessions"].add(r["session"])
                if r["place"] and r["place"]["name"]:
                    p["places"][r["place"]["name"]] = p["places"].get(r["place"]["name"], 0) + 1
    return {pid: {"types": sorted(p["types"]), "conversations": len(p["sessions"]),
                  "places": sorted(p["places"], key=lambda n: -p["places"][n])[:3]}
            for pid, p in people.items()}


def search(query: str, person: str | None = None, kind: str | None = None) -> dict:
    """Paragraphs whose text or speaker contains the query (any case), newest
    day first, in order within a day; only in the conversations with this
    person and of this type."""
    q = query.casefold().strip()
    if not q:
        raise transcribe.TranscribeError("empty query")
    if kind and kind not in TYPES:
        raise transcribe.TranscribeError(f"invalid type {kind!r}")
    results: list[dict] = []
    for d in _days():
        records = _records(d)
        if person or kind:
            keep = {sid for sid, rs in _sessions(records).items() if _matches(rs, person, kind)}
            records = [r for r in records if r["session"] in keep]
        for r in records:
            if q in r["text"].casefold() or q in (r["speaker"] or "").casefold():
                if len(results) == SEARCH_LIMIT:
                    return {"query": query, "results": results, "more": True}
                results.append({"day": d, **r})
    return {"query": query, "results": results, "more": False}
