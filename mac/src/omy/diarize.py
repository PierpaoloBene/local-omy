"""Who speaks: speaker diarization with pyannote, on the Mac (Metal/MPS).

Per session, after transcription: decode the session, run
pyannote/speaker-diarization-community-1, and write
  transcripts/YYYY-MM-DD/speakers/<session>.json
with the speaker turns (UTC) and, per voice cluster, its similarity to your
voice profile. The cluster close to the profile is you (MY_NAME), the others
"Persona 1", "Persona 2"… in order of appearance. render_day labels the
speech with them.

Voice profile: ~/OmiArchive/voices/me.npy, a speaker embedding (biometric
data: mode 600, never leaves the Mac), made by `omy voice enroll` from the
hand-annotated eval clips.

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
from omy.config import ARCHIVE, CONFIG_DIR, load_config
from omy.sessions import Session
from omy.transcribe import CACHE_DIR, TRANSCRIPTS, TranscribeError, _iso, _write_atomic
from omy_receiver.segment import parse

MODEL = "pyannote/speaker-diarization-community-1"
HF_HOME = ARCHIVE / "models" / "hf"
TOKEN_FILE = CONFIG_DIR / "hf_token"
VOICES = ARCHIVE / "voices"
PROFILE = VOICES / "me.npy"
# Cosine similarity to the profile above which a cluster is you. Measured on
# the eval clips: your voice 0.74-0.75, every other voice (people, TV, Teams)
# 0.18 or less.
ME_THRESHOLD = 0.5
VERSION = 1

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


def name_clusters(result: dict, profile) -> dict[str, dict]:
    """label -> {"name", "similarity", "seconds"}; the best match above
    ME_THRESHOLD is you, the rest numbered by first appearance."""
    sims = {k: float(v @ profile) for k, v in result["embeddings"].items()} if profile is not None else {}
    me = max(sims, key=sims.get, default=None)
    if me is not None and sims[me] < ME_THRESHOLD:
        me = None
    names, n = {}, 0
    for _, _, label in sorted(result["turns"]):
        if label in names:
            continue
        if label == me:
            names[label] = my_name()
        else:
            n += 1
            names[label] = f"Persona {n}"
    return {k: {"name": names.get(k, k), "similarity": round(sims[k], 3) if k in sims else None,
                "seconds": round(result["seconds"].get(k, 0), 1)} for k in result["seconds"]}


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
    clusters = name_clusters(result, load_profile())
    doc = {
        "version": VERSION,
        "session": session.id,
        "segments": [s.id for s in session.segments],
        "model": MODEL,
        "profile": PROFILE.is_file(),
        "clusters": clusters,
        "turns": [
            {"start": _iso(session.start + timedelta(seconds=s)), "end": _iso(session.start + timedelta(seconds=e)),
             "speaker": clusters[k]["name"]}
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
    return doc.get("segments") != [s.id for s in session.segments] or doc.get("profile") != PROFILE.is_file()


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
