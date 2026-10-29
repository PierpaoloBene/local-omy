"""One writer at a time on what omy derives from the audio: the speakers of
each session, people and places, the day transcripts. The Mac app, the
receiver (for the iPhone) and the hourly job are separate processes, and a
rename rewrites files that a transcription may be writing.

Held only while writing, never while whisper or pyannote run. Reentrant
within a process: a rename holds it and re-renders days, which take it too.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager

from omy.config import ARCHIVE

LOCK_FILE = ARCHIVE / "cache" / ".write.lock"

_depth = 0
_file = None


@contextmanager
def writing() -> Iterator[None]:
    global _depth, _file
    if _depth == 0:
        LOCK_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _file = LOCK_FILE.open("w")
        fcntl.flock(_file, fcntl.LOCK_EX)
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1
        if _depth == 0:
            _file.close()
            _file = None
