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
    # A companion: lives next to "chat" (about 0.7 GB), never next to the organizer or during diarize.
    "embed": {
        "path": ARCHIVE / "models" / "brain" / "Qwen3-Embedding-0.6B-Q8_0.gguf",
        "port": 8092,
        "args": ["-ngl", "all", "-c", "2048", "-ub", "1024", "-b", "1024", "--embedding", "--pooling", "last",
                 "-np", "2"],
        "companion": True,
    },
}
COMPANIONS = ("embed",)
COMP_STATE = STATE.with_name("models-embed.json")
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
# What abort() acts on without the mutex: the server being started or running
# (role, pid), and when each role was last aborted (monotonic).
_active: tuple[str, int] | None = None
_comp_procs: dict[str, subprocess.Popen] = {}
_aborted: dict[str, float] = {}


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
    comps = companions()
    try:
        st = json.loads(STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {"loaded": None, "pid": None, "port": None, "companions": comps}
    if not _alive(st.get("pid")):
        return {"loaded": None, "pid": None, "port": None, "companions": comps}
    return {"loaded": st.get("role"), "pid": st["pid"], "port": st.get("port"), "since": st.get("since"),
            "starting": st.get("starting"), "companions": comps}


def _comp_state() -> dict:
    """role -> {"pid", "port", "since"} of the companions recorded alive."""
    try:
        data = json.loads(COMP_STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return {r: v for r, v in data.items() if isinstance(v, dict) and _alive(v.get("pid"))}


def _write_comp_state(data: dict) -> None:
    COMP_STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = COMP_STATE.with_name(f".{COMP_STATE.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data) + "\n")
    os.replace(tmp, COMP_STATE)


def companions() -> list[str]:
    """The companion roles (e.g. "embed") whose server is running now."""
    return sorted(_comp_state())


def _stop_companions(only: str | None = None) -> None:
    for role, v in _comp_state().items():
        if only is not None and role != only:
            continue
        proc = _comp_procs.pop(role, None)
        _stop_pid(proc.pid if proc is not None else v["pid"], proc)
        log(f"stopped llama-server {role} (pid {v['pid']})")
    left = {r: v for r, v in _comp_state().items() if only is not None and r != only}
    _write_comp_state(left)


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
    global _proc, _active
    _active = None
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
        _write_comp_state({})
        _comp_procs.clear()
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
    global _proc, _active
    entered = time.monotonic()
    if role not in MODELS:
        raise ModelError(f"unknown role {role!r}")
    if role in COMPANIONS:
        return _ensure_companion(role)
    with _mutex, _FileLock():
        st = state()
        if st["loaded"] == role and _healthy(role):
            if role == "organizer":
                _stop_companions()
            return base_url(role)
        if st["loaded"] is not None or _proc is not None:
            _stop()
        if role == "organizer":  # the memory is all for it
            _stop_companions()
        path = model_path(role)
        if not path.is_file():
            raise ModelError(f"model for {role} not found: {path}")
        cmd = _command(role)
        log(f"starting {role}: {' '.join(cmd)}")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("ab") as out:
            _proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        _active = (role, _proc.pid)
        _write_state({"role": None, "pid": _proc.pid, "port": MODELS[role]["port"], "starting": role})
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while True:
            if _aborted.get(role, 0.0) > entered:
                _stop()
                raise Preempted(f"loading {role} aborted")
            if _proc.poll() is not None:
                code, _proc = _proc.returncode, None
                _active = None
                _write_state({"role": None, "pid": None, "port": None})
                raise ModelError(f"llama-server {role} exited with code {code}")
            if _healthy(role):
                break
            if time.monotonic() > deadline:
                _stop()
                raise ModelError(f"llama-server {role} not ready after {HEALTH_TIMEOUT:.0f} s")
            time.sleep(0.25)
        if _aborted.get(role, 0.0) > entered:
            _stop()
            raise Preempted(f"loading {role} aborted")
        _write_state({"role": role, "pid": _proc.pid, "port": MODELS[role]["port"], "since": time.time()})
        log(f"{role} ready on port {MODELS[role]['port']}")
        return base_url(role)


def _ensure_companion(role: str) -> str:
    """Start a companion next to the chat model. The organizer is stopped
    first (never together)."""
    with _mutex, _FileLock():
        st = state()
        if role in st["companions"] and _healthy(role):
            return base_url(role)
        if role in st["companions"]:
            _stop_companions(role)
        if st["loaded"] == "organizer" or (_proc is not None and not st["loaded"] and _active
                                            and _active[0] == "organizer"):
            _stop()
        path = model_path(role)
        if not path.is_file():
            raise ModelError(f"model for {role} not found: {path}")
        cmd = _command(role)
        log(f"starting {role}: {' '.join(cmd)}")
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("ab") as out:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        _comp_procs[role] = proc
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while not _healthy(role):
            if proc.poll() is not None:
                _comp_procs.pop(role, None)
                raise ModelError(f"llama-server {role} exited with code {proc.returncode}")
            if time.monotonic() > deadline:
                _stop_pid(proc.pid, proc)
                _comp_procs.pop(role, None)
                raise ModelError(f"llama-server {role} not ready after {HEALTH_TIMEOUT:.0f} s")
            time.sleep(0.25)
        data = _comp_state()
        data[role] = {"pid": proc.pid, "port": MODELS[role]["port"], "since": time.time()}
        _write_comp_state(data)
        log(f"{role} ready on port {MODELS[role]['port']}")
        return base_url(role)


def abort(role: str | None = None) -> bool:
    """Kill the llama-server of this role (any, if None), running or still
    loading, WITHOUT the mutex or the file lock: they may be held by the very
    ensure() that is loading it, and that ensure() raises Preempted. For the
    guard (a question arrived) and the SIGTERM handler. Returns whether
    there was a process to kill."""
    now = time.monotonic()
    for r in (MODELS if role is None else [role]):
        _aborted[r] = now
    killed = False
    for r, v in _comp_state().items():
        if role is None or role == r:
            try:
                os.kill(v["pid"], signal.SIGKILL)
                killed = True
            except (ProcessLookupError, PermissionError):
                pass
    if role in COMPANIONS:
        return killed
    pid = None
    active = _active
    if active is not None and (role is None or active[0] == role):
        pid = active[1]
    else:
        try:
            st = json.loads(STATE.read_text())
            if role is None or st.get("role") == role or st.get("starting") == role:
                pid = st.get("pid")
        except (OSError, ValueError):
            pass
    if not pid or not _alive(pid):
        return killed
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return killed
    return True


def release(role: str) -> None:
    """Stop the llama-server of this role, if it is the one running."""
    with _mutex, _FileLock():
        if role in COMPANIONS:
            _stop_companions(role)
            return
        st = state()
        if st["loaded"] == role or (_proc is not None and not st["loaded"]):
            _stop()


def release_all() -> None:
    with _mutex, _FileLock():
        _stop_companions()
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


def loading_or_demanded() -> bool:
    """Whether the chat/embed servers are being loaded or a question is pending: a query embedding is
    worth waiting for. Otherwise nobody is going to load the embedding companion soon."""
    return state().get("starting") in ("chat", "embed") or chat_active()


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
