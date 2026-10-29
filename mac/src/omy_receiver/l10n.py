"""Italian or English.

  archive  the names omy writes in the transcripts ("Persona 1",
           "Sconosciuto", "Io", "Riunione"): language in
           ~/.config/local-omy/config.toml ("it" or "en"), else the Mac's
           (System Settings → Language & Region). Fixed per Mac, so that a
           name doesn't change with who runs omy; set it before the first
           transcription.
  shown    what omy shows (the setup checklist, the features,
           notifications): OMY_LANG, which the Mac app sets to its own
           language, else the archive's.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from functools import cache
from pathlib import Path

CONFIG_FILE = Path("~/.config/local-omy/config.toml").expanduser()
LANGUAGES = ("it", "en")


def _mac() -> str:
    """The first of Italian and English among the Mac's languages; English
    if neither."""
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleLanguages"], capture_output=True, text=True).stdout
    except OSError:
        out = os.environ.get("LANG", "")
    for item in out.replace("(", " ").replace(")", " ").replace(",", " ").replace('"', " ").split():
        lang = item.split("-")[0].split("_")[0].lower()
        if lang in LANGUAGES:
            return lang
    return "en"


@cache
def archive_language() -> str:
    try:
        with CONFIG_FILE.open("rb") as f:
            value = tomllib.load(f).get("language")
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        value = None
    return value if value in LANGUAGES else _mac()


def shown_language() -> str:
    value = os.environ.get("OMY_LANG", "")
    return value if value in LANGUAGES else archive_language()


def tr(it: str, en: str) -> str:
    """What omy shows, in its language."""
    return en if shown_language() == "en" else it


def archive_tr(it: str, en: str) -> str:
    """A name written in the archive, in the archive's language."""
    return en if archive_language() == "en" else it
