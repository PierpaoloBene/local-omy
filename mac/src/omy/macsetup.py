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
from omy.config import CONFIG_DIR, REPO, label_prefix
from omy_receiver.l10n import tr

MAC_SRC = REPO / "mac" / "src"
TOKEN_FILE = Path(os.environ.get("OMY_TOKEN_FILE", CONFIG_DIR / "token"))
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
# Installed by `omy setup`. launchd/periodic.plist stays in the repo for a rollback, not installed.
AGENTS = ("receiver", "worker")
# The compute worker (docs/hub-migration.md) is installed apart: disabled until the cutover.
COMPUTE = "compute"
# Pulls a read-only copy of the vault from the hub into ~/OmiBrain (for Obsidian), every 10 minutes.
VAULT_MIRROR = "vault-mirror"
VAULT_DIR = Path("~/OmiBrain").expanduser()
WORKSPACE = Path("~/OmiCompute").expanduser()
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
    return f"{label_prefix()}.{name}"


def _plist_path(name: str) -> Path:
    return AGENTS_DIR / f"{_label(name)}.plist"


def _render_agent(name: str) -> str:
    uv = shutil.which("uv") or str(Path("~/.local/bin/uv").expanduser())
    template_path = MAC_SRC / "launchd" / f"{name}.plist"
    if not template_path.exists():
        template_path = MAC_SRC / "launchd" / f"com.pierpaolobene.local-omy.{name}.plist"
    template = template_path.read_text()
    return (template.replace("__LABEL__", _label(name)).replace("__UV__", uv).replace("__MAC_DIR__", str(MAC_SRC))
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
            if name == "worker":
                retire_agent("periodic")  # the worker replaces it; two would fight over pyannote
            return
        time.sleep(0.5)
    raise SetupError(f"launchctl bootstrap {name} failed: {r.stderr.strip()}")


def install_compute(load: bool = False) -> dict:
    """The compute worker's plist and workspace (~/OmiCompute, models -> ~/OmiArchive/models). Without load
    it is left NOT loaded and disabled (launchctl disable), so that not even the next login starts it:
    the original worker is still the one at work. `compute-on` enables and loads it (the cutover)."""
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(mode=0o700, parents=True, exist_ok=True)
    (WORKSPACE / "cache").mkdir(mode=0o700, exist_ok=True)
    marker = WORKSPACE / ".omy-compute-workspace"
    if not marker.exists():
        marker.write_text("wiped between jobs: raw/ transcripts/ voices/ brain/\n")
    models_link = WORKSPACE / "models"
    if not models_link.exists() and not models_link.is_symlink():
        models_link.symlink_to(Path("~/OmiArchive/models").expanduser())
    path = _plist_path(COMPUTE)
    path.write_text(_render_agent(COMPUTE))
    os.chmod(path, 0o644)
    domain, label = f"gui/{os.getuid()}", _label(COMPUTE)
    if not load:
        _run(["launchctl", "disable", f"{domain}/{label}"])
        return {"installed": str(path), "loaded": agent_info(COMPUTE)["loaded"], "workspace": str(WORKSPACE)}
    _run(["launchctl", "enable", f"{domain}/{label}"])
    _run(["launchctl", "bootout", f"{domain}/{label}"])
    for _ in range(10):
        r = _run(["launchctl", "bootstrap", domain, str(path)])
        if r.returncode == 0:
            return {"installed": str(path), "loaded": True, "workspace": str(WORKSPACE)}
        time.sleep(0.5)
    raise SetupError(f"launchctl bootstrap {COMPUTE} failed: {r.stderr.strip()}")


def install_vault_mirror(load: bool = False) -> dict:
    """The mirror's plist (not loaded and disabled unless load: it needs the hub to hold the vault)."""
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _plist_path(VAULT_MIRROR)
    path.write_text(_render_agent(VAULT_MIRROR))
    os.chmod(path, 0o644)
    domain, label = f"gui/{os.getuid()}", _label(VAULT_MIRROR)
    if not load:
        _run(["launchctl", "disable", f"{domain}/{label}"])
        return {"installed": str(path), "loaded": agent_info(VAULT_MIRROR)["loaded"]}
    _run(["launchctl", "enable", f"{domain}/{label}"])
    _run(["launchctl", "bootout", f"{domain}/{label}"])
    for _ in range(10):
        r = _run(["launchctl", "bootstrap", domain, str(path)])
        if r.returncode == 0:
            return {"installed": str(path), "loaded": True}
        time.sleep(0.5)
    raise SetupError(f"launchctl bootstrap {VAULT_MIRROR} failed: {r.stderr.strip()}")


def stop_vault_mirror() -> dict:
    domain, label = f"gui/{os.getuid()}", _label(VAULT_MIRROR)
    _run(["launchctl", "bootout", f"{domain}/{label}"])
    _run(["launchctl", "disable", f"{domain}/{label}"])
    return {"loaded": agent_info(VAULT_MIRROR)["loaded"]}


def set_hub_url(url: str | None) -> dict:
    """hub_url in config.toml turns omy into a client of the hub (omy.remote); None turns it off."""
    from omy.config import load_config, save_config
    cfg = load_config()
    if url:
        if not url.startswith("https://"):
            raise SetupError("the hub's URL must be https://… (tailscale serve)")
        cfg["hub_url"] = url.rstrip("/")
    else:
        cfg.pop("hub_url", None)
    save_config(cfg)
    return {"hub_url": cfg.get("hub_url")}


def stop_compute() -> dict:
    """The compute worker stops (its job goes back to the hub's queue) and stays disabled."""
    domain, label = f"gui/{os.getuid()}", _label(COMPUTE)
    _run(["launchctl", "bootout", f"{domain}/{label}"])
    _run(["launchctl", "disable", f"{domain}/{label}"])
    return {"loaded": agent_info(COMPUTE)["loaded"]}


def retire_agent(name: str = "periodic") -> bool:
    """Stop a LaunchAgent and delete its plist (the worker replaces periodic).
    Returns whether there was a plist. Run by install_agent("worker")."""
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{_label(name)}"])
    path = _plist_path(name)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


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
        return False, tr("manca", "missing")
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return False, tr(f"{TOKEN_FILE} deve essere un file tuo, normale, con mode 600",
                     f"{TOKEN_FILE} must be a regular file of yours, mode 600")
    return True, str(TOKEN_FILE)


def checks() -> list[dict]:
    items = []

    def add(id_: str, label: str, ok: bool, detail: str) -> None:
        items.append({"id": id_, "label": label, "ok": ok, "detail": detail})

    whisper = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
    has_opus = any(Path(p).exists() for p in ("/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib"))
    has_whisper = Path(whisper).exists()
    add("brew", tr("Homebrew: opus e whisper.cpp", "Homebrew: opus and whisper.cpp"), has_opus and has_whisper,
        "ok" if has_opus and has_whisper else "brew install opus whisper.cpp")

    ok, detail = _token_state()
    add("token", tr("Token del ricevitore", "Receiver token"), ok, detail)

    for name, label in (("receiver", tr("Ricevitore (LaunchAgent)", "Receiver (LaunchAgent)")),
                        ("worker", tr("Worker (LaunchAgent)", "Worker (LaunchAgent)")),
                        ("periodic", tr("Job orario (LaunchAgent, sostituito dal worker)",
                                        "Hourly job (LaunchAgent, replaced by the worker)"))):
        if name == "periodic" and not _plist_path(name).exists():
            continue  # retired: the worker replaces it
        a = agent_info(name)
        ok = a["loaded"] and a["up_to_date"] and (a["running"] or name == "periodic")
        detail = tr("non installato", "not installed") if not a["loaded"] \
            else tr("plist non aggiornato", "plist out of date") if not a["up_to_date"] \
            else tr("non in esecuzione", "not running") if not ok else "ok"
        add(name, label, ok, detail)

    if _plist_path(COMPUTE).exists():  # installed for the move to the hub
        a = agent_info(COMPUTE)
        add(COMPUTE, tr("Compute worker (per l'hub)", "Compute worker (for the hub)"), a["loaded"] and a["running"],
            tr("installato, non attivo (si attiva al passaggio all'hub)", "installed, not active (enabled at the move to the hub)")
            if not a["loaded"] else "ok")

    if _plist_path(VAULT_MIRROR).exists():
        a = agent_info(VAULT_MIRROR)
        add(VAULT_MIRROR, tr("Copia del vault per Obsidian (solo lettura)", "Vault copy for Obsidian (read-only)"), a["loaded"],
            "ok" if a["loaded"] else tr("installata, non attiva", "installed, not active"))

    ts = tailscale_info()
    add("tailscale", "tailscale serve → 127.0.0.1:8787", ts["serving"],
        ts["url"] or (tr("Tailscale non installato (tailscale.com/download)", "Tailscale not installed (tailscale.com/download)")
                      if not ts["installed"] else tr("tailscale serve non attivo", "tailscale serve not running")))

    models = transcribe.models_status()
    missing = ", ".join(n for n, v in models.items() if not v)
    add("models", tr("Modelli di trascrizione", "Transcription models"), all(models.values()),
        "ok" if all(models.values()) else tr(f"mancano: {missing}", f"missing: {missing}"))

    from omy import diarize
    model = any((diarize.HF_HOME / "hub").glob("models--pyannote--speaker-diarization-community-1"))
    if model and diarize.PROFILE.is_file():
        detail = "ok"
    elif not model and not diarize.TOKEN_FILE.is_file():
        detail = tr("manca il token Hugging Face (mac/README.md, Chi parla)",
                    "Hugging Face token missing (mac/README.md, Chi parla)")
    elif not model:
        detail = tr("modello non scaricato", "model not downloaded")
    else:
        detail = tr("manca la tua impronta vocale: omy voice enroll", "your voice profile is missing: omy voice enroll")
    add("diarize", tr("Chi parla (pyannote e impronta vocale)", "Who speaks (pyannote and voice profile)"), detail == "ok", detail)

    cfg = iphone.load_config()
    ok = "team_id" in cfg and "device_udid" in cfg
    add("iphone", tr("Team Apple e iPhone", "Apple team and iPhone"), ok,
        f"team {cfg['team_id']}, iPhone {cfg['device_udid']}" if ok
        else tr("non rilevati: Apple ID in Xcode, iPhone collegato e sbloccato",
                "not detected: Apple ID in Xcode, iPhone connected and unlocked"))
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
    elif id_ == "compute":
        install_compute(load=False)
    elif id_ == "compute-on":
        install_compute(load=True)
        id_ = "compute"
    elif id_ == "compute-off":
        stop_compute()
        id_ = "compute"
    elif id_ == "vault-mirror":
        install_vault_mirror(load=False)
    elif id_ == "vault-mirror-on":
        install_vault_mirror(load=True)
        id_ = "vault-mirror"
    elif id_ == "vault-mirror-off":
        stop_vault_mirror()
        id_ = "vault-mirror"
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
    elif id_ == "diarize":
        from omy import diarize, enroll
        if not diarize.TOKEN_FILE.is_file() and not (diarize.HF_HOME / "hub").is_dir():
            raise SetupError("save a Hugging Face token in ~/.config/local-omy/hf_token first (README, Chi parla)")
        diarize._load()
        if not diarize.PROFILE.is_file():
            enroll.enroll()
    else:
        raise SetupError(f"unknown item {id_}")
    return next(c for c in checks() if c["id"] == id_)


def read_token() -> str:
    ok, detail = _token_state()
    if not ok:
        raise SetupError(f"token: {detail}")
    return TOKEN_FILE.read_text().strip()
