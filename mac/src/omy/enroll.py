"""Your voice profile (omy voice enroll), from a few clips annotated by hand.

~/OmiArchive/voices/enroll holds <clip>.wav (16 kHz mono, 2 min) and
<clip>.txt: what is said, one line per utterance, with who says it ("P:"
you, "A:", "B:"… other people, "TV:" recorded audio, optionally "TV (Teams):").
Lines starting with "#" are comments; "# default: P" applies to the lines
without a tag. [...] and (...) are notes. Biometric like the profile: mode
700/600, never leaves the Mac.
"""

from __future__ import annotations

import re

from omy.transcribe import TranscribeError

# "[00:12] ", then a speaker tag with an optional description:
# "P:", "TV (Linus tech tip):", "TV [video di mkbhd]:".
_TAG = re.compile(r"^\s*(?:\[\d+:\d+\]\s*)?(?:([A-Za-z]{1,3})\s*(?:\([^)]*\)|\[[^\]]*\])?\s*:)?")
_DEFAULT = re.compile(r"^\s*#\s*default:\s*([A-Za-z]{1,3})\b")
_NOTE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_FILLER = re.compile(r"^(?:m+h*|h?m+|a+h+|e+h+m*|u+h+|u+m+|o+h+)$")


def tagged_words(text: str, default: str = "?") -> list[tuple[str, str]]:
    """(word, speaker) of an annotated text."""
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


def enroll() -> dict:
    """Seeded with the main speaker of the clip with the largest share of P:
    words, then averaged (weighted by speech time) with every cluster close to
    it in the clips that have P: words."""
    import numpy as np
    from omy import diarize
    clips = sorted(diarize.ENROLL_DIR.glob("*.txt"))
    share = {}
    for txt in clips:
        tw = tagged_words(txt.read_text())
        if tw and (mine := sum(sp == "P" for _, sp in tw)):
            share[txt.stem] = mine / len(tw)
    if not share:
        raise TranscribeError(f"no clip with P: lines in {diarize.ENROLL_DIR}")
    seed_clip = max(share, key=share.get)
    results = {n: diarize.diarize_wav(diarize.ENROLL_DIR / f"{n}.wav") for n in share}
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
