"""Small fake archives: day transcripts and speakers documents."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SESSION = "20261029T080000Z-0000abcd"


def record(session: str, day: str, hhmm: str, speaker: str | None, person: str | None, text: str,
           via: dict | None = None, place: dict | None = None) -> dict:
    return {"v": 1, "id": f"{session}/{hhmm}", "session": session, "start": f"{day}T{hhmm}:00+01:00",
            "end": f"{day}T{hhmm}:30+01:00", "speaker": speaker, "person": person, "via": via,
            "place": place, "language": "it", "text": text}


def write_day(archive: Path, day: str, records: list[dict]) -> None:
    t = archive / "transcripts"
    t.mkdir(parents=True, exist_ok=True)
    (t / f"{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))


def vec(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def write_speakers(archive: Path, day: str, session: str, voices: dict[str, np.ndarray],
                   start: str = "2026-10-29T08:00:00.000Z") -> Path:
    """A diarized session with these voices (label -> embedding), 10 s each."""
    from omy import diarize
    labels = sorted(voices)
    turns = [(i * 10.0, i * 10.0 + 10, k) for i, k in enumerate(labels)]
    result = {"turns": turns, "embeddings": voices, "seconds": {k: 10.0 for k in labels}}
    clusters = diarize.name_clusters(result, None)
    diarize.save_npz(diarize.CLUSTERS / f"{session}.npz", voices)
    path = archive / "transcripts" / day / "speakers" / f"{session}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": diarize.VERSION, "session": session, "segments": [], "start": start,
        "context": [], "clusters": clusters,
        "turns": [{"start": start, "end": start, "speaker": clusters[k]["name"], "label": k} for _, _, k in turns],
    }))
    return path
