"""Paths, user config (config.toml) and runtime state (state.json).

Both live in ~/.config/local-omy (mode 700) next to the receiver token, and are
written with mode 600 via a temp file + rename.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from pathlib import Path

CONFIG_DIR = Path("~/.config/local-omy").expanduser()
CONFIG_FILE = CONFIG_DIR / "config.toml"
STATE_FILE = CONFIG_DIR / "state.json"
ARCHIVE = Path(os.environ.get("OMY_ARCHIVE", "~/OmiArchive")).expanduser()

# mac/src/omy/config.py -> repo root. The project is installed editable by uv.
REPO = Path(__file__).resolve().parents[3]
IPHONE_SRC = REPO / "iphone" / "src"
IPHONE_BUILD = REPO / "iphone" / "build"


def _write_private(path: Path, text: str) -> None:
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:  # mkstemp creates the file with mode 600
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_config() -> dict[str, str]:
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open("rb") as f:
        return {k: v for k, v in tomllib.load(f).items() if isinstance(v, str)}


def save_config(config: dict[str, str]) -> None:
    # Flat string keys only; a JSON string is a valid TOML basic string.
    _write_private(CONFIG_FILE, "".join(f"{k} = {json.dumps(v)}\n" for k, v in sorted(config.items())))


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}


def save_state(state: dict) -> None:
    _write_private(STATE_FILE, json.dumps(state, indent=2) + "\n")


def label_prefix() -> str:
    """Of the LaunchAgents and the Mac app: bundle_id in config.toml, else
    local-omy. Set once: a new prefix installs new agents next to the old."""
    return load_config().get("bundle_id", "local-omy")


def iphone_bundle_id(team: str) -> str:
    """bundle_id in config.toml, else one of this Apple team (a bundle id is
    unique across all of Apple: a fork can't reuse someone else's)."""
    return load_config().get("bundle_id") or f"com.{team.lower()}.local-omy"


def feature_on(key: str) -> bool:
    """A feature turned on or off in the app (Mac or iPhone, synced):
    omy_receiver/features.py."""
    from omy_receiver import features
    return features.enabled(ARCHIVE, key)
