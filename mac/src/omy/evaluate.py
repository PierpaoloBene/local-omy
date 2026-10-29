"""Transcription accuracy on a hand-corrected sample (omy eval).

  omy eval make   cuts the CLIPS out of the archive into ~/OmiArchive/eval:
                  <clip>.wav (16 kHz mono) and <clip>.txt, the current
                  transcript of that stretch, to be corrected by hand.
                  <clip>.orig.txt keeps it unchanged: the baseline.
  omy eval run    transcribes every clip with each CONFIG and prints the word
                  error rate (WER) against the corrected <clip>.txt.

Reference format: one line per utterance, optionally "[mm:ss]" and a speaker
tag ("P:" you, "A:", "B:"… other people, "TV:" recorded audio). Lines starting
with "#" are comments. Tags and times are ignored by the WER and kept for
diarization.

Everything here is private like the archive (mode 700/600) and derived: make
never overwrites a .txt that was already corrected.
"""

from __future__ import annotations

import json
import re
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from omy.audio import StreamDecoder
from omy.config import ARCHIVE
from omy.sessions import LOCAL_TZ, sessions_for_day
from omy.transcribe import (
    CACHE_DIR, MODELS_DIR, OPTIONAL_MODELS, WHISPER_MODEL, TranscribeError, _iso, _load_doc,
    log, transcribe_wav,
)
from omy_receiver.segment import SAMPLE_RATE, parse

EVAL_DIR = ARCHIVE / "eval"
CLIP_SECONDS = 120


@dataclass(frozen=True)
class Clip:
    name: str
    session: str      # session id
    offset_s: int     # from the session start
    what: str


CLIPS = [
    Clip("A", "20261027T091229Z-696dba54", 226, "riunione di lavoro in inglese"),
    Clip("B", "20261027T105208Z-51a71aa0", 3900, "call di lavoro: italiano trascritto come inglese dal minuto 66"),
    Clip("C", "20261027T135708Z-a009e6cf", 178, "lavoro, in italiano"),
    Clip("D", "20261028T172504Z-f3e50f8d", 1452, "conversazione in famiglia, più voci e rumore"),
    Clip("E", "20261028T201126Z-dea8a7db", 1406, "TV o documentario?"),
    Clip("F", "20261029T055509Z-4a7fcd28", 1556, "mattina, italiano con un po' di inglese"),
    Clip("G", "20261029T064530Z-52a96f58", 755, "quasi silenzio: whisper ha scritto 'Thank you.'"),
]


@dataclass(frozen=True)
class Config:
    name: str
    model: str = WHISPER_MODEL
    language: str = "auto"
    # Split each clip at pauses into windows of about this length, each with
    # its own language detection (0: the whole clip at once).
    window_s: int = 0
    extra: list[str] = field(default_factory=list)
    vad: bool = True
    # Dynamic gain before whisper: quiet stretches raised up to +MAX_GAIN_DB.
    normalize: bool = False


CONFIGS = {c.name: c for c in [
    Config("turbo"),
    Config("turbo-30s", window_s=30),
    Config("large", model="ggml-large-v3.bin"),
    Config("large-30s", model="ggml-large-v3.bin", window_s=30),
    # Silero VAD sensitivity: the default 0.5 drops quiet or distant speech.
    Config("turbo-30s-vt35", window_s=30, extra=["-vt", "0.35"]),
    Config("turbo-30s-vt20", window_s=30, extra=["-vt", "0.2"]),
    Config("turbo-30s-vt35-pad", window_s=30, extra=["-vt", "0.35", "-vp", "200"]),
    Config("turbo-30s-novad", window_s=30, vad=False),
    Config("turbo-30s-norm", window_s=30, normalize=True),
    Config("turbo-30s-norm-vt35-pad", window_s=30, normalize=True, extra=["-vt", "0.35", "-vp", "200"]),
    Config("large-30s-norm-vt35-pad", model="ggml-large-v3.bin", window_s=30, normalize=True,
           extra=["-vt", "0.35", "-vp", "200"]),
]}

HEADER = """\
# {name} · {when} · {what}
# Ascolta {name}.wav e correggi il testo: scrivi quello che viene detto davvero,
# nella lingua in cui viene detto. Se una frase manca aggiungila, se è inventata cancellala.
# A inizio riga indica chi parla: P: tu, A: B: C: altre persone, TV: video/TV/audio registrato.
# Gli orari [mm:ss] sono dall'inizio dello spezzone e servono solo a orientarti.
# Le righe che iniziano con # vengono ignorate.

"""


def _find_session(session_id: str):
    day = datetime.strptime(session_id[:8], "%Y%m%d").date()
    for d in (day, day - timedelta(days=1)):
        for s in sessions_for_day(d):
            if s.id == session_id:
                return s
    raise TranscribeError(f"session {session_id} not in the archive")


def _cut(session, start_ms: int, end_ms: int, out: Path) -> None:
    """Decode the session and keep [start_ms, end_ms) of its timeline (the
    same timeline omy transcribe maps to wall-clock time)."""
    full = CACHE_DIR / f"eval-{session.id}.wav"
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    dec = StreamDecoder(session.segments[0].codec)
    dec.open(full)
    try:
        for seg in session.segments:
            dec.add(parse(seg.bin.read_bytes()))
            if dec.position_ms >= end_ms:
                break
    finally:
        dec.close()
    try:
        with wave.open(str(full), "rb") as src, wave.open(str(out), "wb") as dst:
            dst.setparams(src.getparams())
            src.setpos(start_ms * SAMPLE_RATE // 1000)
            dst.writeframes(src.readframes((end_ms - start_ms) * SAMPLE_RATE // 1000))
    finally:
        full.unlink(missing_ok=True)


def _hyp_lines(doc: dict, clip_start: datetime, clip_end: datetime) -> list[str]:
    lines = []
    for it in doc["speech"]:
        t = datetime.fromisoformat(it["start"].replace("Z", "+00:00"))
        if clip_start <= t < clip_end:
            s = int((t - clip_start).total_seconds())
            lines.append(f"[{s // 60:02d}:{s % 60:02d}] {it['text']}")
    return lines


def make() -> dict:
    EVAL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    made = {}
    for clip in CLIPS:
        session = _find_session(clip.session)
        doc = _load_doc(session)
        if doc is None:
            raise TranscribeError(f"session {clip.session} is not transcribed yet")
        start = session.start + timedelta(seconds=clip.offset_s)
        end = start + timedelta(seconds=CLIP_SECONDS)
        wav = EVAL_DIR / f"{clip.name}.wav"
        if not wav.exists():
            log(f"{clip.name}: cutting {start.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M:%S}")
            _cut(session, clip.offset_s * 1000, (clip.offset_s + CLIP_SECONDS) * 1000, wav)
        text = HEADER.format(name=clip.name, what=clip.what,
                             when=f"{start.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M:%S}")
        text += "\n".join(_hyp_lines(doc, start, end)) + "\n"
        orig = EVAL_DIR / f"{clip.name}.orig.txt"
        ref = EVAL_DIR / f"{clip.name}.txt"
        orig.write_text(text)
        if not ref.exists():
            ref.write_text(text)
            made[clip.name] = "created"
        else:
            made[clip.name] = "kept (already there)"
        (EVAL_DIR / f"{clip.name}.json").write_text(json.dumps({
            "session": clip.session, "start": _iso(start), "end": _iso(end), "what": clip.what,
        }, indent=1) + "\n")
    return {"dir": str(EVAL_DIR), "clips": made}


# --- scoring ---

# "[00:12] ", then a speaker tag with an optional description:
# "P:", "TV (Linus tech tip):", "TV [video di mkbhd]:".
_TAG = re.compile(r"^\s*(?:\[\d+:\d+\]\s*)?(?:([A-Za-z]{1,3})\s*(?:\([^)]*\)|\[[^\]]*\])?\s*:)?")
_DEFAULT = re.compile(r"^\s*#\s*default:\s*([A-Za-z]{1,3})\b")
# Non-speech: [noises, comments], (incomprensibili).
_NOTE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# Hesitations whisper leaves out: not scored on either side.
_FILLER = re.compile(r"^(?:m+h*|h?m+|a+h+|e+h+m*|u+h+|u+m+|o+h+)$")
PEOPLE = {"P", "A", "B", "C"}


def tagged_words(text: str, default: str = "?") -> list[tuple[str, str]]:
    """(word, speaker) of a reference or hypothesis text."""
    out = []
    for line in text.splitlines():
        if m := _DEFAULT.match(line):
            default = m.group(1).upper()
            continue
        if line.lstrip().startswith("#"):
            continue
        m = _TAG.match(line)
        speaker = (m.group(1) or default).upper()
        body = _NOTE.sub(" ", line[m.end():]).lower()
        body = re.sub(r"(?<=\d)[.,](?=\d{3}\b)", "", body)  # 2,900 -> 2900
        out += [(w, speaker) for w in re.sub(r"[^\w]+", " ", body).split() if not _FILLER.match(w)]
    return out


def words(text: str) -> list[str]:
    return [w for w, _ in tagged_words(text)]


def partial(text: str) -> bool:
    """The reference has stretches the annotator could not make out."""
    return bool(re.search(r"incomprensibil|inaudible", text, re.I))


def align(ref: list[str], hyp: list[str]) -> tuple[int, list[bool]]:
    """(edit distance, for each reference word whether hyp has it)."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    hit = [False] * n
    i, j = n, m
    while i > 0 and j > 0:
        if d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            hit[i - 1] = ref[i - 1] == hyp[j - 1]
            i, j = i - 1, j - 1
        elif d[i][j] == d[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return d[n][m], hit


def score(ref_text: str, hyp_text: str) -> dict:
    ref = tagged_words(ref_text)
    hyp = words(hyp_text)
    dist, hit = align([w for w, _ in ref], hyp)
    out: dict = {"ref_words": len(ref), "hyp_words": len(hyp)}
    if ref:
        out["wer"] = dist / len(ref)
    for group, members in (("people", PEOPLE), ("tv", {"TV"})):
        idx = [k for k, (_, sp) in enumerate(ref) if sp in members]
        if idx:
            out[f"{group}_recall"] = sum(hit[k] for k in idx) / len(idx)
            out[f"{group}_words"] = len(idx)
    return out


# --- transcribing clips with a config ---

def transcribe_clip(wav: Path, config: Config) -> str:
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = CACHE_DIR / f"eval-{wav.name}"
    work.write_bytes(wav.read_bytes())
    try:
        items = transcribe_wav(work, config.model, config.language, config.window_s,
                               config.normalize, config.extra, config.vad)
    finally:
        work.unlink(missing_ok=True)
    lines = [f"[{it['from'] // 60000:02d}:{it['from'] // 1000 % 60:02d}] {it['text']}" for it in items]
    return "\n".join(lines) + "\n"


def run(config_names: list[str] | None = None, fresh: bool = False) -> dict:
    """Scores per clip and config. WER only where the reference is complete;
    on partial clips (unintelligible TV) the share of people's words found;
    on clips with no speech the words made up. Transcripts are cached in
    eval/runs/<config>/ (fresh: transcribe again)."""
    configs = [CONFIGS[n] for n in (config_names or list(CONFIGS))]
    for c in configs:
        if not (MODELS_DIR / c.model).is_file():
            hint = " (omy eval models)" if c.model in OPTIONAL_MODELS else ""
            raise TranscribeError(f"model {c.model} missing{hint}")
    refs = {c.name: (EVAL_DIR / f"{c.name}.txt").read_text()
            for c in CLIPS if (EVAL_DIR / f"{c.name}.txt").exists()}
    if not refs:
        raise TranscribeError("no clips: run omy eval make first")

    hyps: dict[str, dict[str, str]] = {"current": {n: (EVAL_DIR / f"{n}.orig.txt").read_text() for n in refs}}
    for config in configs:
        out_dir = EVAL_DIR / "runs" / config.name
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        hyps[config.name] = {}
        for name in refs:
            cached = out_dir / f"{name}.txt"
            if fresh or not cached.exists():
                log(f"{config.name}: {name}")
                cached.write_text(transcribe_clip(EVAL_DIR / f"{name}.wav", config))
            hyps[config.name][name] = cached.read_text()

    result: dict = {"clips": {}, "summary": {}}
    for cfg, texts in hyps.items():
        per = {n: score(refs[n], texts[n]) for n in refs}
        complete = [n for n in refs if not partial(refs[n]) and per[n]["ref_words"]]
        errors = sum(per[n]["wer"] * per[n]["ref_words"] for n in complete)
        total = sum(per[n]["ref_words"] for n in complete)
        people = [(per[n]["people_recall"], per[n]["people_words"]) for n in refs if "people_recall" in per[n]]
        result["summary"][cfg] = {
            "wer_complete_clips": round(errors / total, 3) if total else None,
            "people_words_found": round(sum(r * w for r, w in people) / sum(w for _, w in people), 3)
            if people else None,
            "made_up_words_in_silence": sum(per[n]["hyp_words"] for n in refs if not per[n]["ref_words"]),
        }
        for n, v in per.items():
            result["clips"].setdefault(n, {})[cfg] = {k: round(x, 3) if isinstance(x, float) else x
                                                      for k, x in v.items()}
    result["complete_clips"] = [n for n in refs if not partial(refs[n]) and tagged_words(refs[n])]
    (EVAL_DIR / "results.json").write_text(json.dumps(result, indent=1) + "\n")
    return result


# --- diarization ---

def _clip_diarization(name: str) -> dict:
    from omy import diarize
    return diarize.diarize_wav(EVAL_DIR / f"{name}.wav")


def enroll() -> dict:
    """Your voice profile from the eval clips: seeded with the main speaker of
    the clip with the largest share of P: lines, then averaged (weighted by
    speech time) with every cluster close to it in clips that have P: lines."""
    import numpy as np
    from omy import diarize
    refs = {c.name: (EVAL_DIR / f"{c.name}.txt").read_text() for c in CLIPS if (EVAL_DIR / f"{c.name}.txt").exists()}
    share = {}
    for n, text in refs.items():
        tw = tagged_words(text)
        if tw:
            share[n] = sum(sp == "P" for _, sp in tw) / len(tw)
    with_me = [n for n, s in share.items() if s > 0]
    if not with_me:
        raise TranscribeError("no clip has P: lines")
    seed_clip = max(with_me, key=share.get)
    results = {n: _clip_diarization(n) for n in with_me}
    seed = results[seed_clip]
    main = max(seed["embeddings"], key=lambda k: seed["seconds"][k])
    centre = seed["embeddings"][main]
    total = np.zeros_like(centre)
    used = {}
    for n, r in results.items():
        for k, v in r["embeddings"].items():
            sim = float(v @ centre)
            if sim >= diarize.ME_THRESHOLD:
                total += v * r["seconds"][k]
                used[f"{n}/{k}"] = {"similarity": round(sim, 3), "seconds": round(r["seconds"][k], 1)}
    path = diarize.save_profile(total)
    return {"profile": str(path), "seed": f"{seed_clip}/{main}", "clusters": used}


def _align_pairs(ref: list[str], hyp: list[str]) -> list[tuple[int, int]]:
    """(ref index, hyp index) of the words the two texts share."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    pairs = []
    i, j = n, m
    while i > 0 and j > 0:
        if d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            if ref[i - 1] == hyp[j - 1]:
                pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif d[i][j] == d[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


BEST = "turbo-30s-norm-vt35-pad"


def diarization() -> dict:
    """Per clip, with the BEST transcription: of your words (P: in the
    reference) found by whisper, the share labelled as you (recall); of the
    words labelled as you, the share that are yours (precision)."""
    from omy import diarize
    profile = diarize.load_profile()
    if profile is None:
        raise TranscribeError("no voice profile: run omy voice enroll")
    config = CONFIGS[BEST]
    out: dict = {"clips": {}}
    tp = fn = fp = 0
    for clip in CLIPS:
        ref_path = EVAL_DIR / f"{clip.name}.txt"
        if not ref_path.exists():
            continue
        wav = EVAL_DIR / f"{clip.name}.wav"
        work = CACHE_DIR / f"eval-{wav.name}"
        work.write_bytes(wav.read_bytes())
        try:
            items = transcribe_wav(work, config.model, config.language, config.window_s,
                                   config.normalize, config.extra, config.vad)
        finally:
            work.unlink(missing_ok=True)
        r = _clip_diarization(clip.name)
        names = diarize.name_clusters(r, profile)
        me = diarize.my_name()

        def speaker_at(a: float, b: float) -> str:
            best, best_overlap = None, 0.0
            for s, e, k in r["turns"]:
                ov = min(b, e) - max(a, s)
                if ov > best_overlap:
                    best, best_overlap = names[k]["name"], ov
            return best or "?"

        hyp: list[tuple[str, str]] = []
        for it in items:
            sp = speaker_at(it["from"] / 1000, it["to"] / 1000)
            hyp += [(w, sp) for w in words(it["text"])]
        ref = tagged_words(ref_path.read_text())
        pairs = _align_pairs([w for w, _ in ref], [w for w, _ in hyp])
        c = {"tp": 0, "fn": 0, "fp": 0}
        for i, j in pairs:
            mine_ref, mine_hyp = ref[i][1] == "P", hyp[j][1] == me
            c["tp"] += mine_ref and mine_hyp
            c["fn"] += mine_ref and not mine_hyp
            c["fp"] += mine_hyp and not mine_ref
        tp, fn, fp = tp + c["tp"], fn + c["fn"], fp + c["fp"]
        out["clips"][clip.name] = {**c, "clusters": {v["name"]: v["similarity"] for v in names.values()}}
    out["my_words_labelled_me"] = round(tp / (tp + fn), 3) if tp + fn else None
    out["labelled_me_that_are_mine"] = round(tp / (tp + fp), 3) if tp + fp else None
    (EVAL_DIR / "diarization.json").write_text(json.dumps(out, indent=1) + "\n")
    return out
