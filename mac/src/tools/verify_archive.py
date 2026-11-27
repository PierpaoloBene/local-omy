#!/usr/bin/env python3
"""Count and check an archive (stdlib only: run it on the Mac and on the hub and compare).

    python3 verify_archive.py ~/OmiArchive
    ssh hub@hub.local python3 - /srv/hub/data/omy < verify_archive.py

Prints JSON: the number of segments (.bin and .json), of days, of day transcripts, of notes in the vault; a hash
of the manifest of raw/ (sorted "path sha256" lines: the same on both sides if the files are the same); and the
segments whose .bin does not match the sha256 in its sidecar, or that lack their sidecar (should be none).
Exit status 1 if there are such segments. --quick: skip hashing the audio (counts and sizes only).
"""

import hashlib
import json
import sys
from pathlib import Path


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(root: Path, quick: bool) -> int:
    raw = root / "raw" / "audio"
    bins = sorted(raw.glob("????-??-??/*.bin"))
    metas = sorted(raw.glob("????-??-??/*.json"))
    bad, lines = [], []
    for b in bins:
        side = b.with_suffix(".json")
        digest = None if quick else sha(b)
        try:
            want = json.loads(side.read_text())["sha256"]
        except (OSError, ValueError, KeyError):
            bad.append({"segment": b.stem, "problem": "no readable sidecar"})
            want = None
        if want and digest and digest != want:
            bad.append({"segment": b.stem, "problem": "sha256 differs from the sidecar"})
        lines.append(f"{b.relative_to(root)} {digest or b.stat().st_size}")
    for m in metas:
        if not m.with_suffix(".bin").exists():
            bad.append({"segment": m.stem, "problem": "sidecar without audio"})
        lines.append(f"{m.relative_to(root)} {m.stat().st_size if quick else sha(m)}")
    out = {
        "root": str(root), "bin": len(bins), "json": len(metas),
        "days": len({b.parent.name for b in bins}),
        "transcript_days": len(list((root / "transcripts").glob("????-??-??.jsonl"))),
        "sessions_json": len(list((root / "transcripts").glob("????-??-??/*.json"))),
        "speakers_json": len(list((root / "transcripts").glob("????-??-??/speakers/*.json"))),
        "notes": len(list((root / "brain" / "note").glob("*/*/*/*.json"))),
        "voices_files": len([p for p in (root / "voices").rglob("*") if p.is_file()]),
        "manifest_sha256": hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest(),
        "problems": bad,
    }
    print(json.dumps(out, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    sys.exit(main(Path(args[0]).expanduser(), "--quick" in sys.argv))
