"""Who speaks: speaker diarization with pyannote, on the Mac (Metal/MPS).

Per session, after transcription: decode the session, run
pyannote/speaker-diarization-community-1, and write
  transcripts/YYYY-MM-DD/speakers/<session>.json
with the speaker turns (UTC) and, per voice cluster, its similarity to your
voice profile. The cluster close to the profile is you (MY_NAME), a cluster
close to a named person (omy.people) is that person, the others "Persona 1",
"Persona 2"… in order of appearance. render_day labels the speech with them.

Voice data, all biometric (mode 600, never leaves the Mac), in
~/OmiArchive/voices:
  me.npy                 your profile, from `omy voice enroll` (omy.enroll)
  enroll/                the clips annotated by hand it is built from
  clusters/<session>.npz the embedding of each voice of each session, so a
                         voice can be named later without running pyannote
  people.json            the people you named, people/<id>.npz their samples

The model is downloaded once from Hugging Face with the token in
~/.config/local-omy/hf_token (its terms must be accepted on the model page);
it then runs offline from ~/OmiArchive/models/hf.
"""

from __future__ import annotations

import json
import os
import warnings
from datetime import timedelta
from pathlib import Path

from omy.audio import StreamDecoder
from omy.config import ARCHIVE, CONFIG_DIR, feature_on, load_config
from omy.sessions import Session
from omy.transcribe import CACHE_DIR, TRANSCRIPTS, TranscribeError, _iso, _write_atomic
from omy_receiver.segment import parse

MODEL = "pyannote/speaker-diarization-community-1"
HF_HOME = ARCHIVE / "models" / "hf"
TOKEN_FILE = CONFIG_DIR / "hf_token"
VOICES = ARCHIVE / "voices"
PROFILE = VOICES / "me.npy"
ENROLL_DIR = VOICES / "enroll"
# Cosine similarity to the profile above which a cluster is you. Measured on
# hand-annotated clips: your voice 0.74-0.75, every other voice (people, TV, Teams)
# 0.18 or less.
ME_THRESHOLD = 0.5
# The same for a named person, against the mean of the voices named after them.
# Lower: one person's voice varies more across sessions (distance, room) than
# within one. Measured: the same speaker in three talks 1.0, 0.92, 0.49; other
# voices 0.1 or less.
PERSON_THRESHOLD = 0.4
CLUSTERS = VOICES / "clusters"
PEOPLE = VOICES / "people.json"
PEOPLE_DIR = VOICES / "people"
# 2: cluster embeddings saved, turns carry the cluster label.
VERSION = 2

_pipeline = None


def my_name() -> str:
    return load_config().get("my_name", "Io")


def available() -> bool:
    return TOKEN_FILE.is_file() or (HF_HOME / "hub").is_dir()


def _load():
    global _pipeline
    if _pipeline is None:
        os.environ.setdefault("HF_HOME", str(HF_HOME))
        warnings.filterwarnings("ignore")
        import torch
        from pyannote.audio import Pipeline
        token = TOKEN_FILE.read_text().strip() if TOKEN_FILE.is_file() else None
        try:
            _pipeline = Pipeline.from_pretrained(MODEL, token=token)
        except Exception as e:  # noqa: BLE001 - HF errors come in many types
            raise TranscribeError(f"cannot load {MODEL}: {e}") from e
        if _pipeline is None:
            raise TranscribeError(f"cannot load {MODEL}: token missing or terms not accepted")
        if torch.backends.mps.is_available():
            _pipeline.to(torch.device("mps"))
    return _pipeline


def diarize_wav(wav: Path) -> dict:
    """{"turns": [(start_s, end_s, label)], "embeddings": {label: vector},
    "seconds": {label: speech seconds}}."""
    import numpy as np
    import torch
    import wave
    with wave.open(str(wav), "rb") as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    out = _load()({"waveform": torch.from_numpy(x)[None], "sample_rate": rate})
    d = out.speaker_diarization
    labels = d.labels()
    turns = [(seg.start, seg.end, label) for seg, _, label in d.itertracks(yield_label=True)]
    emb = {}
    if out.speaker_embeddings is not None:
        for i, label in enumerate(labels):
            v = out.speaker_embeddings[i]
            norm = float(np.linalg.norm(v))
            if np.all(np.isfinite(v)) and norm > 0:
                emb[label] = v / norm
    seconds = {label: sum(e - s for s, e, k in turns if k == label) for label in labels}
    return {"turns": turns, "embeddings": emb, "seconds": seconds}


def load_profile():
    import numpy as np
    return np.load(PROFILE) if PROFILE.is_file() else None


def load_people() -> dict[str, dict]:
    """id -> {"name", "created"}, the people named in the app or with
    `omy speakers rename`."""
    try:
        return json.loads(PEOPLE.read_text())["people"]
    except (FileNotFoundError, ValueError, KeyError):
        return {}


def person_samples(person_id: str) -> dict:
    """"session/label" -> embedding of the voices named after this person."""
    import numpy as np
    path = PEOPLE_DIR / f"{person_id}.npz"
    if not path.is_file():
        return {}
    with np.load(path) as z:
        return {k.replace("~", "/"): z[k] for k in z.files}


def person_profiles() -> dict[str, tuple[str, object]]:
    """id -> (name, normalized mean embedding)."""
    import numpy as np
    out = {}
    for pid, p in load_people().items():
        samples = list(person_samples(pid).values())
        if samples:
            v = np.mean(samples, axis=0)
            out[pid] = (p["name"], v / np.linalg.norm(v))
    return out


def save_npz(path: Path, arrays: dict) -> None:
    """Atomic, mode 600; keys may contain "/" (stored as "~")."""
    import io
    import numpy as np
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not arrays:
        path.unlink(missing_ok=True)
        return
    buf = io.BytesIO()
    np.savez(buf, **{k.replace("/", "~"): np.asarray(v, dtype=np.float32) for k, v in arrays.items()})
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)


def cluster_embeddings(session_id: str) -> dict:
    import numpy as np
    path = CLUSTERS / f"{session_id}.npz"
    if not path.is_file():
        return {}
    with np.load(path) as z:
        return {k: z[k] for k in z.files}


# Where a voice comes from, set by hand (omy speakers kind): it overrides the
# context found on the Mac and the iPhone (omy.context). "media" voices (a TV
# in the room, a video) are nobody: never recognized, never named.
SOURCES = ("room", "meeting", "media")
MEETING_APP = "Riunione"
MEDIA_APP = "TV"


def via(context: dict | None, source: str | None) -> dict | None:
    """The context of a voice ({"kind": "call"|"media", "app"}), after its
    source set by hand."""
    if source == "room":
        return None
    if source == "meeting":
        return context if context and context["kind"] == "call" else {"kind": "call", "app": MEETING_APP}
    if source == "media":
        return {"kind": "media", "app": MEDIA_APP}
    return context


def name_clusters(result: dict, profile, contexts: dict[str, tuple[str, str]] | None = None,
                  people: dict[str, tuple[str, object]] | None = None,
                  pins: dict[str, str] | None = None,
                  sources: dict[str, str] | None = None) -> dict[str, dict]:
    """label -> {"name", "person", "pinned", "similarity", "seconds", "context"}.

    The best match above ME_THRESHOLD is you. pins (label -> person id or
    "me") are the voices named by hand; each other named person (people, from
    person_profiles) is the remaining voice closest to them above
    PERSON_THRESHOLD, at most one per session. The rest are numbered by first
    appearance. contexts (label -> (kind, app), from omy.context) marks call
    participants, "Persona 2 (Teams)", "Marco (iPhone)", and media,
    "Media (Chrome)". sources (label -> SOURCES) are set by hand: "media"
    voices are "TV" and nobody."""
    contexts, people, pins, sources = contexts or {}, people or {}, pins or {}, sources or {}
    pins = {k: v for k, v in pins.items() if (v == "me" or v in people) and sources.get(k) != "media"}
    emb = {k: v for k, v in result["embeddings"].items() if sources.get(k) != "media"}
    sims = {k: float(v @ profile) for k, v in emb.items()} if profile is not None else {}
    who = dict(pins)
    if "me" not in who.values():
        free = {k: s for k, s in sims.items() if k not in who}
        me = max(free, key=free.get, default=None)
        if me is not None and free[me] >= ME_THRESHOLD:
            who[me] = "me"
    pairs = sorted(((float(v @ pv), k, pid) for k, v in emb.items() if k not in who
                    for pid, (_, pv) in people.items() if pid not in who.values()), reverse=True)
    for sim, k, pid in pairs:
        if sim >= PERSON_THRESHOLD and k not in who and pid not in who.values():
            who[k] = pid
    names, n = {}, 0
    for _, _, label in sorted(result["turns"]):
        if label in names:
            continue
        ctx = via(dict(zip(("kind", "app"), contexts[label])) if label in contexts else None, sources.get(label))
        kind, app = (ctx["kind"], ctx["app"]) if ctx else (None, None)
        call = f" ({app})" if kind == "call" else ""
        if who.get(label) == "me":
            names[label] = my_name()
        elif label in who:
            names[label] = people[who[label]][0] + call
        elif kind == "media":
            names[label] = MEDIA_APP if app == MEDIA_APP else f"Media ({app})"
        else:
            n += 1
            names[label] = f"Persona {n}{call}"
    return {k: {"name": names.get(k, k), "person": who.get(k), "pinned": k in pins,
                "similarity": round(sims[k], 3) if k in sims else None,
                "seconds": round(result["seconds"].get(k, 0), 1),
                "context": dict(zip(("kind", "app"), contexts[k])) if k in contexts else None,
                **({"source": sources[k]} if k in sources else {})}
            for k in result["seconds"]}


def speakers_path(session: Session) -> Path:
    return TRANSCRIPTS / session.day.isoformat() / "speakers" / f"{session.id}.json"


def diarize_session(session: Session) -> dict:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    wav = CACHE_DIR / f"diarize-{session.id}.wav"
    dec = StreamDecoder(session.segments[0].codec)
    dec.open(wav)
    try:
        for seg in session.segments:
            dec.add(parse(seg.bin.read_bytes()))
        dec.close()
        result = diarize_wav(wav)
    finally:
        dec.close()
        wav.unlink(missing_ok=True)
    from omy import context
    at = lambda sec: session.start + timedelta(seconds=sec)  # noqa: E731
    ctx = context.intervals(session.start, session.end)
    contexts = {}
    for label in result["seconds"]:
        c = context.classify([(at(s), at(e)) for s, e, k in result["turns"] if k == label], ctx)
        if c:
            contexts[label] = c
    # With voice prints off, nothing of the other voices is kept and nobody
    # but you is recognized: your profile is the only one you consented to.
    prints = feature_on("voice_prints")
    if prints:
        save_npz(CLUSTERS / f"{session.id}.npz", result["embeddings"])
    clusters = name_clusters(result, load_profile(), contexts, person_profiles() if prints else {})
    doc = {
        "version": VERSION,
        "session": session.id,
        "segments": [s.id for s in session.segments],
        "model": MODEL,
        "profile": PROFILE.is_file(),
        "context": [{"start": _iso(i.start), "end": _iso(i.end), "app": i.app, "kind": i.kind} for i in ctx],
        "clusters": clusters,
        "turns": [
            {"start": _iso(session.start + timedelta(seconds=s)), "end": _iso(session.start + timedelta(seconds=e)),
             "speaker": clusters[k]["name"], "label": k}
            for s, e, k in result["turns"]
        ],
    }
    _write_atomic(speakers_path(session), json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    return doc


def needs_diarization(session: Session) -> bool:
    try:
        doc = json.loads(speakers_path(session).read_text())
    except (FileNotFoundError, ValueError):
        return True
    return (doc.get("version") != VERSION or doc.get("segments") != [s.id for s in session.segments]
            or doc.get("profile") != PROFILE.is_file())


def save_profile(vector) -> Path:
    import io
    import numpy as np
    VOICES.mkdir(mode=0o700, parents=True, exist_ok=True)
    buf = io.BytesIO()
    np.save(buf, np.asarray(vector, dtype=np.float32) / np.linalg.norm(vector))
    fd = os.open(PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(buf.getvalue())
    return PROFILE
