"""Mac setup checklist: every item can be checked and fixed (omy setup check /
omy setup fix ID). Fixes never overwrite user data: the token is created only
if missing."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path

from omy import iphone, transcribe
from omy.config import CONFIG_DIR, REPO

MAC_SRC = REPO / "mac" / "src"
TOKEN_FILE = Path(os.environ.get("OMY_TOKEN_FILE", CONFIG_DIR / "token"))
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
LABEL_PREFIX = "com.example.local-omy"
AGENTS = ("receiver", "periodic")
RECEIVER_URL = "http://127.0.0.1:8787"
# PATH for the LaunchAgents: Homebrew (flutter, pod, whisper-cli), uv, Xcode tools.
AGENT_PATH = ":".join([
    "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", str(Path("~/.local/bin").expanduser()),
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
])


class SetupError(Exception):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


# --- LaunchAgents -----------------------------------------------------------

def _label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def _plist_path(name: str) -> Path:
    return AGENTS_DIR / f"{_label(name)}.plist"


def _render_agent(name: str) -> str:
    uv = shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser())
    template = (MAC_SRC / "launchd" / f"{_label(name)}.plist").read_text()
    return (template.replace("__UV__", uv).replace("__MAC_DIR__", str(MAC_SRC))
            .replace("__HOME__", str(Path.home())).replace("__PATH__", AGENT_PATH))


def agent_info(name: str) -> dict:
    """loaded/running from launchctl; up_to_date if the installed plist is the
    one this repo would install (same paths)."""
    out = _run(["launchctl", "print", f"gui/{os.getuid()}/{_label(name)}"])
    info: dict = {"loaded": out.returncode == 0, "running": False, "pid": None, "last_exit": None}
    for line in out.stdout.splitlines():
        line = line.strip()
        if line == "state = running":
            info["running"] = True
        elif line.startswith("pid = "):
            info["pid"] = int(line.split("=")[1])
        elif line.startswith("last exit code = "):
            value = line.split("=", 1)[1].strip()
            info["last_exit"] = int(value) if value.lstrip("-").isdigit() else value
    path = _plist_path(name)
    info["up_to_date"] = path.exists() and path.read_text() == _render_agent(name)
    return info


def install_agent(name: str) -> None:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{domain}/{_label(name)}"])
    path = _plist_path(name)
    path.write_text(_render_agent(name))
    os.chmod(path, 0o644)
    # bootout completes asynchronously: bootstrap fails with EIO until it has.
    for _ in range(10):
        r = _run(["launchctl", "bootstrap", domain, str(path)])
        if r.returncode == 0:
            return
        time.sleep(0.5)
    raise SetupError(f"launchctl bootstrap {name} failed: {r.stderr.strip()}")


# --- Tailscale ----------------------------------------------------------------

def _tailscale() -> str | None:
    return shutil.which("tailscale") or next(
        (p for p in ("/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
         if Path(p).exists()), None)


def tailscale_info() -> dict:
    info: dict = {"installed": False, "serving": False, "url": None, "iphones": []}
    ts = _tailscale()
    if ts is None:
        return info
    info["installed"] = True
    try:
        serve = json.loads(_run([ts, "serve", "status", "--json"]).stdout or "{}")
        st = json.loads(_run([ts, "status", "--json"]).stdout or "{}")
    except json.JSONDecodeError:
        return info
    for host, web in (serve.get("Web") or {}).items():
        if any(h.get("Proxy") == RECEIVER_URL for h in (web.get("Handlers") or {}).values()):
            info["serving"] = True
            info["url"] = "https://" + host.removesuffix(":443")
    info["iphones"] = [
        {"name": p.get("DNSName", "").split(".")[0], "online": bool(p.get("Online"))}
        for p in (st.get("Peer") or {}).values() if p.get("OS") == "iOS"
    ]
    return info


# --- Checklist ------------------------------------------------------------------

def _token_state() -> tuple[bool, str]:
    try:
        st = TOKEN_FILE.lstat()
    except FileNotFoundError:
        return False, "missing"
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return False, f"{TOKEN_FILE} must be a regular file of yours with mode 600"
    return True, str(TOKEN_FILE)


def checks() -> list[dict]:
    items = []

    def add(id_: str, label: str, ok: bool, detail: str) -> None:
        items.append({"id": id_, "label": label, "ok": ok, "detail": detail})

    whisper = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
    has_opus = any(Path(p).exists() for p in ("/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"))
    has_whisper = Path(whisper).exists()
    add("brew", "Homebrew: opus e whisper.cpp", has_opus and has_whisper,
        "ok" if has_opus and has_whisper else "brew install opus whisper.cpp")

    ok, detail = _token_state()
    add("token", "Token del ricevitore", ok, detail)

    for name, label in (("receiver", "Ricevitore (LaunchAgent)"), ("periodic", "Job orario (LaunchAgent)")):
        a = agent_info(name)
        ok = a["loaded"] and a["up_to_date"] and (a["running"] or name == "periodic")
        detail = "not installed" if not a["loaded"] else "outdated plist" if not a["up_to_date"] \
            else "not running" if not ok else "ok"
        add(name, label, ok, detail)

    ts = tailscale_info()
    add("tailscale", "tailscale serve → 127.0.0.1:8787", ts["serving"],
        ts["url"] or ("Tailscale not installed" if not ts["installed"] else "not serving"))

    models = transcribe.models_status()
    add("models", "Modelli di trascrizione", all(models.values()),
        "ok" if all(models.values()) else f"missing: {[n for n, v in models.items() if not v]}")

    cfg = iphone.load_config()
    ok = "team_id" in cfg and "device_udid" in cfg
    add("iphone", "Team Apple e iPhone", ok,
        f"team {cfg['team_id']}, device {cfg['device_udid']}" if ok else "not detected")
    return items


def fix(id_: str) -> dict:
    if id_ == "brew":
        r = subprocess.run(["brew", "install", "opus", "whisper.cpp"])
        if r.returncode != 0:
            raise SetupError("brew install failed")
    elif id_ == "token":
        ok, detail = _token_state()
        if TOKEN_FILE.exists() or TOKEN_FILE.is_symlink():
            # Never overwrite: the phone has a copy.
            raise SetupError(f"token exists but is not valid ({detail}); fix it by hand")
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    elif id_ in AGENTS:
        install_agent(id_)
    elif id_ == "tailscale":
        ts = _tailscale()
        if ts is None:
            raise SetupError("install Tailscale first")
        # serve, never funnel: reachable only from the tailnet.
        r = _run([ts, "serve", "--bg", RECEIVER_URL])
        if r.returncode != 0:
            raise SetupError(f"tailscale serve failed: {r.stderr.strip()}")
    elif id_ == "models":
        transcribe.download_models()
    elif id_ == "iphone":
        iphone.ensure_config()
    else:
        raise SetupError(f"unknown item {id_}")
    return next(c for c in checks() if c["id"] == id_)


def read_token() -> str:
    ok, detail = _token_state()
    if not ok:
        raise SetupError(f"token: {detail}")
    return TOKEN_FILE.read_text().strip()
