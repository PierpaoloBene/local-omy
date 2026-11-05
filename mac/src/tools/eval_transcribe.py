"""Measure the transcription on the hand-corrected clips in ~/OmiArchive/voices/enroll.

    uv run python tools/eval_transcribe.py [--sweep]

Each clip X.wav has its reference X.txt (lines "P: text", "[notes]" and "#"
comments ignored). Per variant and clip it reports, against the reference:
  found   reference words the transcript contains (aligned matches)
  wrong   transcript words that are not in the reference (substituted or invented)
  WER     (substitutions + deletions + insertions) / reference words
"filtered" is the same without the lines below CONF_MIN (shown as
[incomprensibile] in the markdown); --sweep prints found/wrong per threshold.
Texts land in ~/OmiArchive/cache/eval/ to read them side by side.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from omy import transcribe as T
from omy.config import ARCHIVE

ENROLL = ARCHIVE / "voices" / "enroll"
OUT = T.CACHE_DIR / "eval"


def words(text: str) -> list[str]:
    return re.sub(r"[^\w']+", " ", text.lower()).split()


def reference(txt: Path) -> list[str]:
    lines = [ln for ln in txt.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return words(" ".join(re.sub(r"^\s*[A-Z]{1,2}:", "", re.sub(r"\[[^\]]*\]", " ", ln)) for ln in lines))


def align(ref: list[str], hyp: list[str]) -> list[bool]:
    """Per hyp word: True if aligned to an equal reference word (min edit distance)."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    ok = [False] * m
    i, j = n, m
    while i > 0 and j > 0:
        if d[i][j] == d[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            ok[j - 1] = ref[i - 1] == hyp[j - 1]
            i, j = i - 1, j - 1
        elif d[i][j] == d[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return ok


def score(ref: list[str], hyp: list[str]) -> dict:
    ok = align(ref, hyp)
    found = sum(ok)
    wrong = len(hyp) - found
    # edit distance = S + D + I; S + I = wrong (aligned non-matches + inserted), D = ref words not found minus S
    from_dist = _dist(ref, hyp)
    return {"ref": len(ref), "found": found, "wrong": wrong, "errors": from_dist}


def _dist(ref: list[str], hyp: list[str]) -> int:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1]


def run_variant(name: str, clip: Path) -> list[dict]:
    """Speech items of the clip with the pipeline variant `name`."""
    tmp = T.CACHE_DIR / f"eval-{clip.stem}.wav"
    shutil.copy(clip, tmp)
    try:
        return VARIANTS[name](tmp)
    finally:
        tmp.unlink(missing_ok=True)


VARIANTS = {
    "windows": lambda wav: T.transcribe_wav(wav),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", nargs="*", default=["windows"])
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    clips = sorted(ENROLL.glob("*.txt"))
    for name in a.variant:
        tot = {"ref": 0, "found": 0, "wrong": 0, "errors": 0}
        tot_f = dict(tot)
        items_all = []
        for txt in clips:
            ref = reference(txt)
            items = run_variant(name, txt.with_suffix(".wav"))
            (OUT / f"{txt.stem}-{name}.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
            s = score(ref, words(" ".join(it["text"] for it in items)))
            kept = [it for it in items if it.get("conf", 1) >= T.CONF_MIN]
            sf = score(ref, words(" ".join(it["text"] for it in kept)))
            for k in tot:
                tot[k] += s[k]
                tot_f[k] += sf[k]
            print(f"{txt.stem} {name:8} found {s['found'] / s['ref']:5.0%} wrong {s['wrong']:4} WER {s['errors'] / s['ref']:5.0%}"
                  f" | filtered: found {sf['found'] / sf['ref']:5.0%} wrong {sf['wrong']:4} WER {sf['errors'] / sf['ref']:5.0%}")
            items_all.append((ref, items))
        print(f"ALL {name:8} found {tot['found'] / tot['ref']:5.0%} wrong {tot['wrong']:4} WER {tot['errors'] / tot['ref']:5.0%}"
              f" | filtered: found {tot_f['found'] / tot_f['ref']:5.0%} wrong {tot_f['wrong']:4} WER {tot_f['errors'] / tot_f['ref']:5.0%}")
        if a.sweep:
            for th in (0.0, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8):
                f = w = 0
                for ref, items in items_all:
                    s = score(ref, words(" ".join(it["text"] for it in items if it.get("conf", 1) >= th)))
                    f += s["found"]
                    w += s["wrong"]
                print(f"   conf >= {th:.2f}: found {f:4} wrong {w:4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
