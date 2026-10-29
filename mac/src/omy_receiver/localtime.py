"""The archive's time zone: days (audio folders, transcripts) are local days
in it, and the times in the transcripts are local times in it.

  OMY_TZ                                   e.g. Europe/Rome, if set
  timezone in ~/.config/local-omy/config.toml   otherwise
  the Mac's time zone                      otherwise (System Settings)

Set it once: changing it later moves the boundary between days for new
recordings only.
"""

from __future__ import annotations

import os
import tomllib
from functools import cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_FILE = Path("~/.config/local-omy/config.toml").expanduser()


def _configured() -> str | None:
    try:
        with CONFIG_FILE.open("rb") as f:
            value = tomllib.load(f).get("timezone")
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return None
    return value if isinstance(value, str) else None


def _system() -> str:
    # /etc/localtime -> /var/db/timezone/zoneinfo/Europe/Rome
    target = os.path.realpath("/etc/localtime")
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"


@cache
def local_tz() -> ZoneInfo:
    name = os.environ.get("OMY_TZ") or _configured() or _system()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise SystemExit(f"omy: unknown time zone {name!r} (OMY_TZ or timezone in {CONFIG_FILE})") from e
