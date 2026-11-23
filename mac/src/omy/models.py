"""The llama-server processes of the second brain: at most ONE at a time (the
Mac's memory), the organizer or the chat model.

The worker owns the process (ensure / release). Its state goes to
ARCHIVE/cache/models.json so the receiver, another process, can read it
(state) and ask for the chat model (request_chat, answered by the worker via
the "demand" table of omy.jobs).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from omy import jobs
from omy.config import ARCHIVE, CONFIG_FILE

HOST = "127.0.0.1"
STATE = ARCHIVE / "cache" / "models.json"
LOCK = ARCHIVE / "cache" / ".models.lock"
LOG = Path("~/Library/Logs/local-omy-models.log").expanduser()
BREW_BIN = "/opt/homebrew/bin/llama-server"

# Where the GGUF files live; config.toml chat_model / organizer_model replace the path.
MODELS: dict[str, dict] = {
    "organizer": {
        "path": ARCHIVE / "models" / "brain" / "qwen3.8-27b" / "Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf",
        "port": 8090,
        "args": ["-ngl", "all", "-fa", "on", "-c", "16384", "-np", "1", "--no-mmproj", "--jinja",
                 "--cache-ram", "1024", "--spec-type", "draft-mtp", "--spec-draft-n-max", "4"],
    },
    "chat": {
        "path": ARCHIVE / "models" / "brain" / "small" / "Qwen3.5-4B-Q4_0.gguf",
        "port": 8091,
        "args": ["-ngl", "all", "-fa", "on", "-c", "8192", "-np", "1", "--no-mmproj", "--jinja"],
    },
}
HEALTH_TIMEOUT = 180.0
CHAT_WAIT = 60.0
CHAT_POLL = 0.2
STOP_WAIT = 15.0
DEFAULT_CHAT_IDLE_MIN = 5.0


class ModelError(Exception):
    pass


class Preempted(Exception):
    """A question arrived while the organizer or the diarization was working."""


_proc: subprocess.Popen | None = None
_mutex = threading.RLock()
_heavy_started: float = 0.0


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def config_value(key: str, default):
    """config.toml, with values of any type (load_config keeps only strings)."""
    try:
        with CONFIG_FILE.open("rb") as f:
            return tomllib.load(f).get(key, default)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return default


def chat_idle() -> float:
    """Seconds of silence after a question before the heavy work resumes."""
    try:
        return float(config_value("chat_idle_min", DEFAULT_CHAT_IDLE_MIN)) * 60
    except (TypeError, ValueError):
        return DEFAULT_CHAT_IDLE_MIN * 60


def model_path(role: str) -> Path:
    if role not in MODELS:
        raise ModelError(f"unknown role {role!r}")
    override = config_value(f"{role}_model", None)
    return Path(override).expanduser() if isinstance(override, str) and override else Path(MODELS[role]["path"])


def available(role: str) -> bool:
    return model_path(role).is_file()


def base_url(role: str) -> str:
    return f"http://{HOST}:{MODELS[role]['port']}"


def _binary() -> str:
    path = os.environ.get("OMY_LLAMA_SERVER") or shutil.which("llama-server") or BREW_BIN
    if not Path(path).is_file():
        raise ModelError("llama-server not found: brew install llama.cpp")
    return path


def _command(role: str) -> list[str]:
    spec = MODELS[role]
    return [_binary(), "-m", str(model_path(role)), "--host", HOST, "--port", str(spec["port"]), *spec["args"]]


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_state(state: dict) -> None:
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.{os.getpid()}")
    tmp.write_text(json.dumps(state) + "\n")
    os.replace(tmp, STATE)


def state() -> dict:
    """{"loaded": role|None, "pid", "port", "since"}: what is serving now."""
    try:
        st = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {"loaded": None, "pid": None, "port": None}
    if not _alive(st.get("pid")):
        return {"loaded": None, "pid": None, "port": None}
    return {"loaded": st.get("role"), "pid": st["pid"], "port": st.get("port"), "since": st.get("since")}


def _healthy(role: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url(role)}/health", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _stop_pid(pid: int, proc: subprocess.Popen | None = None) -> None:
    if not _alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_WAIT
    while time.monotonic() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                return
        elif not _alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if proc is not None:
        proc.wait()


def _stop() -> None:
    global _proc
    st = state()
    proc, _proc = _proc, None
    pid = proc.pid if proc is not None else st["pid"]
    if pid:
        _stop_pid(pid, proc)
        log(f"stopped llama-server {st['loaded']} (pid {pid})")
    _write_state({"role": None, "pid": None, "port": None})


def _listening_pids(port: int) -> list[int]:
    r = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def _is_llama(pid: int) -> bool:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return "llama-server" in r.stdout


def kill_orphans() -> list[int]:
    """At worker start: llama-servers still on our ports (from a worker that
    died) are killed, so ours can bind them."""
    killed = []
    with _mutex:
        for role, spec in MODELS.items():
            for pid in _listening_pids(spec["port"]):
                if pid != os.getpid() and _is_llama(pid):
                    log(f"killing orphan llama-server pid {pid} on port {spec['port']}")
                    _stop_pid(pid)
                    killed.append(pid)
        _write_state({"role": None, "pid": None, "port": None})
    return killed


class _FileLock:
    def __enter__(self):
        LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.f = LOCK.open("w")
        fcntl.flock(self.f, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        self.f.close()


def ensure(role: str) -> str:
    """The base URL of the llama-server of this role, ready: any other role is
    stopped first, then it is started (if needed) and waited for."""
    global _proc
    if role not in MODELS:
        raise ModelError(f"unknown role {role!r}")
    with _mutex, _FileLock():
        st = state()
        if st["loaded"] == role and _healthy(role):
            return base_url(role)
        if st["loaded"] is not None or _proc is not None:
            _stop()
        path = model_path(role)
        if not path.is_file():
            raise ModelError(f"model for {role} not found: {path}")
        cmd = _command(role)
        log(f"starting {role}: {' '.join(cmd)}")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("ab") as out:
            _proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        _write_state({"role": None, "pid": _proc.pid, "port": MODELS[role]["port"], "starting": role})
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while True:
            if _proc.poll() is not None:
                code, _proc = _proc.returncode, None
                _write_state({"role": None, "pid": None, "port": None})
                raise ModelError(f"llama-server {role} exited with code {code}")
            if _healthy(role):
                break
            if time.monotonic() > deadline:
                _stop()
                raise ModelError(f"llama-server {role} not ready after {HEALTH_TIMEOUT:.0f} s")
            time.sleep(0.25)
        _write_state({"role": role, "pid": _proc.pid, "port": MODELS[role]["port"], "since": time.time()})
        log(f"{role} ready on port {MODELS[role]['port']}")
        return base_url(role)


def release(role: str) -> None:
    """Stop the llama-server of this role, if it is the one running."""
    with _mutex, _FileLock():
        st = state()
        if st["loaded"] == role or (_proc is not None and not st["loaded"]):
            _stop()


def release_all() -> None:
    with _mutex, _FileLock():
        if state()["loaded"] is not None or _proc is not None:
            _stop()


# --- questions during heavy work ---------------------------------------------------

def request_chat() -> str:
    """For the receiver: ask for the chat model and wait until the worker has
    it loaded. Each question renews the demand. Returns its base URL."""
    jobs.touch_demand("chat")
    deadline = time.monotonic() + CHAT_WAIT
    while state()["loaded"] != "chat":
        if time.monotonic() > deadline:
            raise ModelError("the chat model is not ready")
        time.sleep(CHAT_POLL)
        jobs.touch_demand("chat")
    return base_url("chat")


def chat_active(now: float | None = None) -> bool:
    """Whether somebody asked a question in the last CHAT_IDLE."""
    at = jobs.demand_at("chat")
    return at is not None and (time.time() if now is None else now) - at < chat_idle()


def mark_heavy_start(at: float | None = None) -> None:
    """The worker started (or restarted) a job that questions can preempt."""
    global _heavy_started
    _heavy_started = time.time() if at is None else at


def check_preempt() -> None:
    """Raises Preempted if a question came, within CHAT_IDLE, after the
    current heavy job started."""
    at = jobs.demand_at("chat")
    if at is not None and at > _heavy_started and time.time() - at < chat_idle():
        raise Preempted("a chat question is waiting")
