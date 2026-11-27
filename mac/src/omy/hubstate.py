"""What the hub knows about the Mac's compute worker: cache/compute.json, written at each lease and status call.

Its capabilities (docs/hub-migration.md, 2.2) say which kinds of job it can run and with which versions of the
pipeline. The hub compares them with its own constants (same repo, same imports) and does not hand out a kind
whose versions differ, so that a half-updated pair never mixes transcripts of two pipelines.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from omy.config import ARCHIVE

PATH = ARCHIVE / "cache" / "compute.json"
PROTOCOL = 1
ONLINE_S = 90.0   # the Mac long-polls every ~25 s: silent for this long, it is off or asleep
ALL_KINDS = ("transcribe", "diarize", "organize", "embed", "ask")


def versions() -> dict:
    """The constants of this checkout. The Mac sends the same ones from its own."""
    from omy import diarize, transcribe
    from omy.brain import SCHEMA_VERSION, embed, organize
    return {"pipeline": transcribe.PIPELINE, "whisper_model": transcribe.WHISPER_MODEL, "diarize": diarize.VERSION,
            "prompt": organize.PROMPT_VERSION, "schema": SCHEMA_VERSION, "embed_dim": embed.EMBED_DIM}


# The versions each kind depends on.
DEPENDS = {"transcribe": ("pipeline", "whisper_model"), "diarize": ("diarize",), "organize": ("prompt", "schema"),
           "embed": ("embed_dim",), "ask": ()}


def read() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def record(worker_id: str, boot_id: str, capabilities: dict | None, state: dict | None = None,
           kinds: list[str] | None = None) -> dict:
    """Note that the Mac is there (now), with what it can do."""
    cur = read()
    doc = {**cur, "worker_id": worker_id, "boot_id": boot_id, "last_seen": time.time()}
    if capabilities is not None:
        doc["capabilities"] = capabilities
    if state is not None:
        doc["state"] = state
    if kinds is not None:
        doc["kinds"] = kinds
    PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PATH.parent, prefix=".compute.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, PATH)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return doc


def online(max_age: float = ONLINE_S, now: float | None = None) -> bool:
    seen = read().get("last_seen")
    return isinstance(seen, (int, float)) and 0 <= (now or time.time()) - seen < max_age


def capable(capabilities: dict | None, kind: str) -> str | None:
    """None if a worker with these capabilities may run this kind, else why not."""
    caps = capabilities or {}
    if kind not in caps.get("kinds", []):
        return f"the Mac does not do {kind}"
    mine, theirs = versions(), caps.get("versions") or {}
    for key in DEPENDS.get(kind, ()):
        if theirs.get(key) != mine.get(key):
            return f"{key}: Mac {theirs.get(key)!r}, hub {mine.get(key)!r}"
    return None


def can(kind: str) -> bool:
    """Whether the compute worker can run this kind: unknown (never seen) counts as yes, the jobs wait for it."""
    caps = read().get("capabilities")
    return True if caps is None else capable(caps, kind) is None


def version_problems() -> dict[str, str]:
    caps = read().get("capabilities")
    if caps is None:
        return {}
    return {k: why for k in DEPENDS if k != "ask" and (why := capable(caps, k)) and "does not do" not in why}


def organizer_model() -> str | None:
    return ((read().get("capabilities") or {}).get("versions") or {}).get("organizer_model")
