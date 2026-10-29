"""What the iPhone app shows of the transcripts (omy transcripts): the days
with a summary, one day, a search. Read only from transcripts/DAY.jsonl
(written by render_day), so it is exactly what other apps see."""

from __future__ import annotations

import json
from datetime import date, datetime

from omy import transcribe
from omy.transcribe import TRANSCRIPTS

SEARCH_LIMIT = 200


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
    return {"days": [{"day": d, **_summary(_records(d))} for d in _days()], "pending": pending()}


def day(d: date) -> dict:
    records = _records(d.isoformat())
    return {"day": d.isoformat(), "summary": _summary(records), "records": records}


def search(query: str) -> dict:
    """Paragraphs whose text or speaker contains the query (any case), newest
    day first, in order within a day."""
    q = query.casefold().strip()
    if not q:
        raise transcribe.TranscribeError("empty query")
    results: list[dict] = []
    for d in _days():
        for r in _records(d):
            if q in r["text"].casefold() or q in (r["speaker"] or "").casefold():
                if len(results) == SEARCH_LIMIT:
                    return {"query": query, "results": results, "more": True}
                results.append({"day": d, **r})
    return {"query": query, "results": results, "more": False}
