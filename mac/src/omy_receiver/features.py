"""Features you can turn on and off, from the Mac app or the iPhone app, kept
in sync between them.

~/OmiArchive/settings.json (mode 600):
  {"features": {"location": {"on": false, "at": "2026-10-29T11:02:03.000Z"}}}
"at" is when it was last changed, on whichever device: on sync each feature
takes the most recent value (merge). Features never changed are on.

The iPhone syncs with PUT /v1/settings (its values in, the merged ones out)
when it starts, comes to foreground, after a change and every few minutes.
The Mac app and omy change the file directly, under the same lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# key -> (device that applies it, name, what it does): shown by both apps.
FEATURES = {
    "location": ("iphone", "Posizione",
                 "Salva dove è stata fatta ogni registrazione, con le visite e i cambi di posizione di iOS, senza tenere acceso il GPS."),
    "calls": ("iphone", "Chiamate",
                 "Segna quando sei al telefono, su FaceTime o WhatsApp: solo inizio e fine, non chi chiama."),
    "battery": ("iphone", "Batteria del ciondolo",
                 "Salva il livello della batteria a ogni segmento, per stimare quanto dura una carica."),
    "mac_audio": ("mac", "Chiamate e media sul Mac",
                 "Segna quali app usano microfono e altoparlanti, per distinguere una chiamata da un video o dalla musica."),
    "diarization": ("mac", "Chi parla",
                 "Separa le voci di ogni sessione e riconosce la tua."),
    "voice_prints": ("mac", "Impronte vocali",
                 "Salva l'impronta delle altre voci, per dar loro un nome e riconoscerle nelle sessioni successive."),
    "auto_transcribe": ("mac", "Trascrizione automatica",
                 "Trascrive ogni ora le sessioni concluse."),
}
ISO_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
MAX_BODY = 4096


def path(archive: Path) -> Path:
    return archive / "settings.json"


def _parse(raw: object) -> dict[str, dict]:
    """Known features with valid values; unknown ones (a newer app) are
    ignored, a wrong value is an error."""
    if not isinstance(raw, dict) or not isinstance(raw.get("features"), dict):
        raise ValueError("expected {\"features\": {...}}")
    out = {}
    for key, v in raw["features"].items():
        if key not in FEATURES:
            continue
        if (not isinstance(v, dict) or set(v) != {"on", "at"} or not isinstance(v["on"], bool)
                or not isinstance(v["at"], str) or not ISO_UTC.fullmatch(v["at"])):
            raise ValueError(f"feature {key} has wrong value")
        out[key] = {"on": v["on"], "at": v["at"]}
    return out


def _t(iso: str | None) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else datetime.min.replace(tzinfo=timezone.utc)


@contextmanager
def _locked(archive: Path):
    archive.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(archive / ".settings.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(archive: Path) -> dict[str, dict]:
    try:
        return _parse(json.loads(path(archive).read_text()))
    except (FileNotFoundError, ValueError):
        return {}


def load(archive: Path) -> dict[str, dict]:
    """Every feature: {"on", "at"}; at is None if never changed."""
    stored = _read(archive)
    return {k: stored.get(k, {"on": True, "at": None}) for k in FEATURES}


def enabled(archive: Path, key: str) -> bool:
    return load(archive)[key]["on"]


def merge(archive: Path, raw: object) -> dict[str, dict]:
    """Take from raw ({"features": …}) the values newer than ours; returns all."""
    incoming = _parse(raw)
    with _locked(archive):
        stored = _read(archive)
        changed = False
        for k, v in incoming.items():
            if _t(v["at"]) > _t(stored.get(k, {}).get("at")):
                stored[k] = v
                changed = True
        if changed:
            fd, tmp = tempfile.mkstemp(dir=archive, prefix=".settings.")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"features": stored}, f, indent=1)
                os.replace(tmp, path(archive))
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
    return load(archive)


def set_feature(archive: Path, key: str, on: bool) -> dict[str, dict]:
    if key not in FEATURES:
        raise ValueError(f"unknown feature {key}")
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return merge(archive, {"features": {key: {"on": on, "at": now}}})
