"""The Mac's compute worker (LaunchAgent `compute`, docs/hub-migration.md, 2 and 6): it works for the hub.

Same Worker as before (omy.worker: the conditions, the preemption by chat questions, the models) with the
source of the jobs replaced: instead of its own queue it asks the hub for a job with a lease (long-poll),
downloads the audio and files the job needs into ~/OmiCompute (the same layout as an archive), runs the
pipeline UNCHANGED (whisper-cli, pyannote, llama-server), uploads the result and completes the job. The hub
validates and commits. If the Mac is off, jobs wait on the hub; nothing is lost and nothing needs a port open
on the Mac.

  ~/OmiCompute              OMY_ARCHIVE of this process: raw/, transcripts/, voices/, brain/ are the current
                            job's inputs and are wiped between jobs; cache/ (checkpoints, wav) and
                            models -> ~/OmiArchive/models stay
  ~/.config/local-omy/compute_token   mode 600, the token of /compute/v1/* (OMY_COMPUTE_TOKEN_FILE)
  hub_url in config.toml (OMY_HUB_URL)  https://hub.tailXXXX.ts.net:10000
  context_dir in config.toml (OMY_CONTEXT_DIR)  where the Mac app writes audio-DAY.jsonl (default
                            ~/OmiArchive/context); uploaded to the hub before a diarization
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from omy import hubstate, jobs, models
from omy.config import ARCHIVE, CONFIG_DIR, load_config
from omy.worker import Worker, log

PROTOCOL = 1
MARKER = ".omy-compute-workspace"
WIPE = ("raw", "transcripts", "voices", "brain")   # a job's inputs; never cache/ or models
HEARTBEAT_EVERY = 30.0
STATUS_EVERY = 20.0
CONTEXT_EVERY = 300.0
LEASE_WAIT = 20.0
DOWNLOAD_THREADS = 4
RETRY_START, RETRY_MAX = 5.0, 120.0
COMPLETE_GIVE_UP = 1800.0   # the hub unreachable for this long: the result is thrown away (it has requeued the job)
EMBED_BATCH = 32
CONTEXT_DAYS = 3


class HubDown(Exception):
    """The hub did not answer (the network, the hub restarting)."""


class Rejected(Exception):
    """The hub answered, and not with what we needed."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"hub answered {status}: {detail}")
        self.status, self.detail = status, detail


class Lost(Exception):
    """The lease is gone (it expired and the job went to somebody else, or the hub cancelled it)."""


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self) -> dict:
        return json.loads(self.body or b"{}")


class Hub:
    """The compute API of the hub, over HTTPS (urllib: no dependency)."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token

    def _headers(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.token}", "X-Omy-Compute-Protocol": str(PROTOCOL), **(extra or {})}

    def request(self, method: str, path: str, *, body: dict | None = None, data: bytes | None = None,
                headers: dict | None = None, timeout: float = 60.0) -> Response:
        h = self._headers(headers)
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise HubDown(str(e)) from e

    def download(self, path: str, dest: Path, headers: dict, start: int = 0) -> tuple[int, dict]:
        """GET into dest, appended from byte `start` (a Range request). Returns (status, headers)."""
        h = self._headers(headers)
        if start:
            h["Range"] = f"bytes={start}-"
        req = urllib.request.Request(self.url + path, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                mode = "ab" if r.status == 206 else "wb"
                with dest.open(mode) as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                return r.status, {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, {k.lower(): v for k, v in e.headers.items()}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise HubDown(str(e)) from e


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hub_from_config() -> Hub:
    cfg = load_config()
    url = os.environ.get("OMY_HUB_URL") or cfg.get("hub_url")
    if not url:
        raise SystemExit("compute worker: set hub_url in ~/.config/local-omy/config.toml (or OMY_HUB_URL)")
    path = Path(os.environ.get("OMY_COMPUTE_TOKEN_FILE", CONFIG_DIR / "compute_token")).expanduser()
    try:
        st = path.stat()
        if st.st_mode & 0o077:
            raise SystemExit(f"compute worker: {path} must be mode 600")
        token = path.read_text().strip()
    except OSError as e:
        raise SystemExit(f"compute worker: cannot read the token {path}: {e}")
    return Hub(url, token)


def default_workspace() -> Path:
    return ARCHIVE


class ComputeWorker(Worker):
    def __init__(self, hub: Hub, workspace: Path | None = None, context_dir: Path | None = None,
                 worker_id: str | None = None) -> None:
        super().__init__()
        self.hub = hub
        self.ws = (workspace or default_workspace()).expanduser()
        cfg = load_config()
        self.context_dir = Path(context_dir or os.environ.get("OMY_CONTEXT_DIR") or cfg.get("context_dir")
                                or "~/OmiArchive/context").expanduser()
        self.worker_id = worker_id or "mac"
        self.boot_id = uuid.uuid4().hex
        self.lease: dict | None = None
        self.lost = False
        self.hub_down_since: float | None = None
        self.last_status = 0.0
        self.last_status_key = None
        self.last_context = 0.0
        self.caffeinate: subprocess.Popen | None = None
        self.progress: dict = {}
        self.cached_kinds: list[str] | None = None

    # --- safety: this directory is wiped between jobs ------------------------------------------

    def check_workspace(self) -> None:
        """Only a directory made for this (marker file) or an empty one is ever wiped: never an archive."""
        self.ws.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = self.ws / MARKER
        real = Path("~/OmiArchive").expanduser()
        if self.ws.resolve() == real.resolve():
            raise SystemExit(f"compute worker: {self.ws} is the archive of this Mac, not a workspace")
        if not marker.exists():
            if any(p.name not in ("cache", "models") for p in self.ws.iterdir()):
                raise SystemExit(f"compute worker: {self.ws} is not empty and not a workspace (no {MARKER})")
            marker.write_text("wiped between jobs: raw/ transcripts/ voices/ brain/\n")

    def wipe(self) -> None:
        for name in WIPE:
            shutil.rmtree(self.ws / name, ignore_errors=True)

    # --- what this Mac can do --------------------------------------------------------------------

    def capabilities(self) -> dict:
        from omy import diarize, transcribe
        kinds = []
        if all(transcribe.models_status().values()) and (shutil.which("whisper-cli")
                                                         or Path("/opt/homebrew/bin/whisper-cli").is_file()):
            kinds.append("transcribe")
        if diarize.available():
            kinds.append("diarize")
        if models.available("organizer"):
            kinds.append("organize")
        if models.available("embed"):
            kinds.append("embed")
        if models.available("chat"):
            kinds.append("ask")
        v = hubstate.versions()
        v["organizer_model"] = models.model_path("organizer").stem
        git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             cwd=Path(__file__).parent).stdout.strip()
        return {"protocol": PROTOCOL, "kinds": kinds, "versions": v, "git": git or None, "host": socket.gethostname()}

    def organize_ok(self) -> bool:
        # The hub only hands out organize while no transcription or diarization is queued or leased.
        from omy.worker import in_window, on_ac
        return not models.chat_active() and in_window() and on_ac()

    def allowed(self) -> tuple[list[str], list[str]]:
        kinds, forced = ["transcribe", "embed"], []
        if not models.chat_active():
            kinds.append("diarize")
            forced.append("organize")
            if self.organize_ok():
                kinds.append("organize")
        return kinds, forced

    # --- talking to the hub ---------------------------------------------------------------------

    def state_doc(self) -> dict:
        from omy.worker import in_window, on_ac
        job = self.current
        return {"pid": os.getpid(), "state": self.state, "job": job and {"id": job.id, "kind": job.kind, "key": job.key,
                                                                         "started": job.started},
                "loaded": models.state()["loaded"], "chat_active": models.chat_active(), "on_ac": on_ac(),
                "in_window": in_window(), "last_error": self.last_error, "last_scan": None,
                "progress": self.progress or None, "hub_down_since": self.hub_down_since}

    def write_status(self) -> None:
        """Tell the hub (at most every STATUS_EVERY seconds, at once when the state or the job changes)."""
        key = (self.state, self.current and self.current.key)
        if time.time() - self.last_status < STATUS_EVERY and key == self.last_status_key:
            return
        try:
            r = self.hub.request("POST", "/compute/v1/status", timeout=15, body={
                "worker_id": self.worker_id, "boot_id": self.boot_id, "capabilities": self.capabilities(),
                "kinds": self.allowed()[0], "state": self.state_doc()})
            self.hub_down_since = None
            if r.status == 426:
                self.last_error = "the hub speaks another compute protocol: update this Mac"
        except HubDown as e:
            self.hub_down_since = self.hub_down_since or time.time()
            log(f"hub unreachable: {e}")
        except Exception as e:  # noqa: BLE001 - a courtesy
            log(f"status: {e}")
        self.last_status, self.last_status_key = time.time(), key

    def get_lease(self, kinds: list[str], forced: list[str], wait: float) -> dict | None:
        r = self.hub.request("POST", "/compute/v1/lease", timeout=wait + 30, body={
            "worker_id": self.worker_id, "boot_id": self.boot_id, "kinds": kinds, "forced_kinds": forced,
            "wait_s": wait, "capabilities": self.capabilities(), "state": self.state_doc()})
        self.hub_down_since = None
        if r.status == 204:
            return None
        if r.status == 426:
            raise SystemExit("compute worker: the hub speaks another compute protocol (update this Mac)")
        if r.status != 200:
            raise Rejected(r.status, r.body[:200].decode(errors="replace"))
        return r.json()

    # --- the main loop -----------------------------------------------------------------------------

    def step(self, wait: float = LEASE_WAIT, only: list[str] | None = None, dry_run: bool = False,
             load_models: bool = True) -> bool:
        """One round: renew the iPhone app and sync the context if due, then ask the hub for a job (waiting up
        to `wait` seconds) and run it. True if a job ran."""
        now = time.time()
        if now - self.last_iphone >= 3600.0 and not dry_run:
            self.renew_iphone()
        if now - self.last_context >= CONTEXT_EVERY and not dry_run:
            self.sync_context()
        kinds, forced = self.allowed()
        if only:
            kinds, forced = [k for k in kinds if k in only], [k for k in forced if k in only]
        try:
            lease = self.get_lease(kinds, forced, wait) if (kinds or forced) else None
        except HubDown as e:
            self.hub_down_since = self.hub_down_since or time.time()
            log(f"hub unreachable: {e}")
            time.sleep(min(RETRY_MAX, RETRY_START * 4))
            return False
        except Rejected as e:
            log(f"lease: {e}")
            time.sleep(RETRY_START * 4)
            return False
        if lease is None:
            if load_models:
                self.idle()
            return False
        self.run_lease(lease, dry_run=dry_run)
        return True

    def run(self) -> None:
        import signal
        self.check_workspace()
        signal.signal(signal.SIGUSR1, lambda *_: self.wake_event.set())
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        models.kill_orphans()
        log(f"compute worker started (workspace {self.ws}, hub {self.hub.url})")
        self.guard_thread = threading.Thread(target=self._guard_loop, daemon=True)
        self.guard_thread.start()
        self.cached_kinds = self.capabilities()["kinds"]
        threading.Thread(target=self.ask_loop, daemon=True).start()
        while not self.stopping:
            try:
                if self.step():
                    continue
            except Exception as e:  # noqa: BLE001
                log(f"step failed: {e}")
                self.last_error = str(e)
                time.sleep(5.0)
            self.write_status()
        models.release_all()
        self.state = "stopped"
        self.write_status()
        log("compute worker stopped")

    def stop(self, *_) -> None:
        super().stop()
        lease = self.lease
        if lease is not None:  # give the job back at once, the hub need not wait for the lease to run out
            try:
                self.hub.request("POST", f"/compute/v1/jobs/{lease['job']['id']}/release", timeout=5,
                                 body={"lease_id": lease["job"]["lease_id"], "reason": "shutdown"})
            except Exception:  # noqa: BLE001
                pass

    # --- one job ------------------------------------------------------------------------------------

    def run_lease(self, lease: dict, dry_run: bool = False) -> None:
        j = lease["job"]
        job = jobs.Job(j["id"], j["kind"], j["key"], jobs.PRIORITY[j["kind"]], j.get("attempt", 1), True,
                       started=time.time(), lease_id=j["lease_id"])
        log(f"lease {job.kind} {job.key} (job {job.id}, {len(lease['inputs'])} files)")
        if dry_run:
            size = sum(i.get("size", 0) for i in lease["inputs"])
            print(json.dumps({"kind": job.kind, "key": job.key, "files": len(lease["inputs"]), "bytes": size,
                              "outputs": lease["outputs"], "params": lease["params"]}))
            self.release(lease, "dry-run")
            return
        with self.job_lock:
            self.current, self.preempted, self.lease, self.lost = job, False, lease, False
        self.state, self.progress = "running", {}
        if job.kind in ("organize", "diarize"):
            models.mark_heavy_start()
        self.start_caffeinate()
        beat = threading.Thread(target=self._heartbeat_loop, args=(lease,), daemon=True)
        beat_stop = self._beat_stop = threading.Event()
        beat.start()
        self.write_status()
        try:
            self.check_workspace()
            self.wipe()
            self.stage_in(lease)
            outputs, extra = self.execute(job, lease)
            beat_stop.set()
            if self.preempted:
                raise models.Preempted("interrupted")
            self.stage_out_and_complete(lease, outputs, extra)
        except (models.Preempted, Lost) as e:
            self.release(lease, "preempted" if self.preempted and not self.lost else "lost")
            log(f"{job.kind} {job.key}: {e}")
        except Rejected as e:
            log(f"{job.kind} {job.key}: {e}")
            if e.status == 409:  # stale: the hub has queued it again with the inputs as they are now
                pass
            else:
                self.fail(lease, str(e))
        except HubDown as e:
            log(f"{job.kind} {job.key}: the hub went away ({e}); the lease will run out and the job go back in the queue")
        except Exception as e:  # noqa: BLE001
            if self.preempted:
                self.release(lease, "preempted")
            else:
                self.last_error = f"{job.kind} {job.key}: {e}"
                log(f"{job.kind} {job.key} failed: {e}")
                self.fail(lease, str(e)[-900:], retry_after=jobs_retry(job.kind))
        finally:
            beat_stop.set()
            self.stop_caffeinate()
            self.wipe()
            with self.job_lock:
                self.current, self.proc, self.lease, self.state, self.progress = None, None, None, "idle", {}
            self.write_status()

    def _heartbeat_loop(self, lease: dict) -> None:
        j = lease["job"]
        stop = self._beat_stop
        while not stop.wait(HEARTBEAT_EVERY):
            try:
                r = self.hub.request("POST", f"/compute/v1/jobs/{j['id']}/heartbeat", timeout=20,
                                     body={"lease_id": j["lease_id"], "progress": self.progress})
                if r.status == 409:
                    self.lost = True
                    self.preempted = True  # stop what is running: somebody else has the job now
                    proc = self.proc
                    if proc is not None:
                        proc.terminate()
                    return
            except HubDown:
                pass  # the job in hand goes on; complete() retries

    def release(self, lease: dict, reason: str) -> None:
        j = lease["job"]
        try:
            self.hub.request("POST", f"/compute/v1/jobs/{j['id']}/release", timeout=15,
                             body={"lease_id": j["lease_id"], "reason": reason})
        except HubDown:
            pass

    def fail(self, lease: dict, error: str, retry_after: float | None = None, permanent: bool = False) -> None:
        j = lease["job"]
        try:
            self.hub.request("POST", f"/compute/v1/jobs/{j['id']}/fail", timeout=15,
                             body={"lease_id": j["lease_id"], "error": error, "retry_after": retry_after,
                                   "permanent": permanent})
        except HubDown:
            pass

    def start_caffeinate(self) -> None:
        """A job must not be cut short by the Mac going to sleep (-i: no idle sleep, while this process lives)."""
        if sys.platform != "darwin" or self.caffeinate is not None:
            return
        try:
            self.caffeinate = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.caffeinate = None

    def stop_caffeinate(self) -> None:
        if self.caffeinate is not None:
            self.caffeinate.terminate()
            self.caffeinate = None

    # --- staging --------------------------------------------------------------------------------------

    def stage_in(self, lease: dict) -> None:
        j = lease["job"]
        files = lease["inputs"]
        for f in files:
            if f.get("empty"):  # the audio of segments already transcribed: only its sidecar matters
                p = self._ws_path(f["path"])
                p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                p.write_bytes(b"")
        todo = [f for f in files if not f.get("empty")]
        total = sum(f.get("size", 0) for f in todo) or 1
        done = [0]
        lock = threading.Lock()

        def get(f: dict) -> None:
            self.download(j["id"], j["lease_id"], f)
            with lock:
                done[0] += f.get("size", 0)
                self.progress = {"phase": "download", "pct": round(100 * done[0] / total)}

        with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
            for fut in [pool.submit(get, f) for f in todo]:
                fut.result()
        self.progress = {}

    def _ws_path(self, rel: str) -> Path:
        p = (self.ws / rel).resolve()
        if self.ws.resolve() not in p.parents:
            raise Rejected(0, f"path outside the workspace: {rel}")
        return p

    def download(self, job_id: int, lease_id: str, f: dict) -> None:
        """One input, resumed from a partial file, checked against its sha256 (a mismatch: start again, once)."""
        dest = self._ws_path(f["path"])
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        headers = {"X-Lease-Id": lease_id}
        url = f"/compute/v1/jobs/{job_id}/inputs/{f['path']}"
        for attempt in range(3):
            start = part.stat().st_size if part.exists() else 0
            status, h = self.hub.download(url, part, headers, start)
            if status == 416:  # asked for a range past the end: the file is whole (or wrong)
                start, status = 0, 0
                part.unlink(missing_ok=True)
                continue
            if status == 409:
                raise Lost("the lease is gone")
            if status not in (200, 206):
                raise Rejected(status, f"download of {f['path']}")
            want = f.get("sha256") or h.get("x-sha256")
            if want is None or sha256_file(part) == want:
                os.replace(part, dest)
                return
            log(f"{f['path']}: hash mismatch, downloading again")
            part.unlink(missing_ok=True)
        raise Rejected(0, f"{f['path']}: the hash never matched")

    def stage_out_and_complete(self, lease: dict, outputs: list[Path], extra: dict) -> None:
        j = lease["job"]
        sent = []
        for path in outputs:
            try:
                name = path.resolve().relative_to(self.ws.resolve()).as_posix()
            except ValueError:
                name = path.name
            data = path.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            self.retry(lambda: self._put(j, name, data, sha))
            sent.append({"name": name, "sha256": sha})
        body = {"lease_id": j["lease_id"], "outputs": sent, "versions": hubstate.versions(),
                "metrics": {"seconds": round(time.time() - (self.current.started or time.time()), 1)},
                "input_hash": lease["params"].get("input_hash"), **extra}
        r = self.retry(lambda: self.hub.request("POST", f"/compute/v1/jobs/{j['id']}/complete", timeout=300, body=body))
        if r.status == 200:
            log(f"{j['kind']} {j['key']}: {r.json().get('status')}")
        elif r.status == 409:
            raise Rejected(409, r.json().get("detail", "stale"))
        else:
            raise Rejected(r.status, r.body[:300].decode(errors="replace"))

    def _put(self, j: dict, name: str, data: bytes, sha: str) -> Response:
        r = self.hub.request("PUT", f"/compute/v1/jobs/{j['id']}/outputs/{name}", data=data, timeout=600,
                             headers={"X-Lease-Id": j["lease_id"], "X-Sha256": sha})
        if r.status == 409:
            raise Lost("the lease is gone")
        if r.status != 200:
            raise Rejected(r.status, r.body[:300].decode(errors="replace"))
        return r

    def retry(self, fn):
        """The hub unreachable: try again with a backoff (5 s to 2 min) for up to COMPLETE_GIVE_UP; the job done
        stays in the workspace meanwhile."""
        wait, since = RETRY_START, time.monotonic()
        while True:
            try:
                return fn()
            except HubDown as e:
                if time.monotonic() - since > COMPLETE_GIVE_UP or self.stopping:
                    raise
                log(f"hub unreachable ({e}), again in {wait:.0f} s")
                time.sleep(wait)
                wait = min(wait * 2, RETRY_MAX)

    # --- running the pipeline (what the tests replace) ---------------------------------------------------

    def execute(self, job: jobs.Job, lease: dict) -> tuple[list[Path], dict]:
        """Run the job in the workspace: the output files to upload and extra fields for `complete`."""
        return {"transcribe": self.exec_transcribe, "diarize": self.exec_diarize, "organize": self.exec_organize,
                "embed": self.exec_embed}[job.kind](job, lease)

    def spawn_kind(self, job: jobs.Job) -> str:
        return "diarize-compute" if job.kind == "diarize" else job.kind

    def exec_transcribe(self, job: jobs.Job, lease: dict) -> tuple[list[Path], dict]:
        self.run_subprocess(job)   # `omy job transcribe KEY` with OMY_ARCHIVE=workspace: pipeline 5, untouched
        return [self.ws / lease["outputs"][0]], {}

    def exec_diarize(self, job: jobs.Job, lease: dict) -> tuple[list[Path], dict]:
        self.sync_context(days=lease["params"].get("context_days"))  # the hub names the voices with it
        self.run_subprocess(job)   # `omy job diarize-compute KEY`: audio + pyannote
        out = self.ws / "cache" / f"diarize-result-{job.key}.json"
        dest = self.ws / "diarize-result.json"
        os.replace(out, dest)
        return [dest], {}

    def exec_organize(self, job: jobs.Job, lease: dict) -> tuple[list[Path], dict]:
        ran, path = self.organize_note(job)   # the 27B, checkpoints in cache/organize; the hub renders the vault
        if not ran or path is None:
            raise RuntimeError("the organizer did not run")
        return [Path(path)], {}

    def exec_embed(self, job: jobs.Job, lease: dict) -> tuple[list[Path], dict]:
        import numpy as np
        from omy.brain import embed
        chunks = lease["payload"]["chunks"]
        vectors = {}
        for i in range(0, len(chunks), EMBED_BATCH):
            if self.preempted:
                raise models.Preempted("interrupted")
            batch = chunks[i:i + EMBED_BATCH]
            vecs = embed.embed([c["text"] for c in batch], query=False)
            for c, v in zip(batch, vecs):
                vectors[str(c["id"])] = base64.b64encode(v.astype(np.float16).tobytes()).decode()
            self.progress = {"phase": "embed", "pct": round(100 * (i + len(batch)) / len(chunks))}
        out = self.ws / "embed-output.json"
        out.write_text(json.dumps({"vectors": vectors}))
        return [out], {}

    # --- "Chiedi": questions queued on the hub ----------------------------------------------------------------

    def ask_loop(self) -> None:
        """A thread of its own: a question must not wait for a transcription to end. It long-polls the hub for
        the oldest question nobody is answering."""
        while not self.stopping:
            if "ask" not in (self.cached_kinds or ["ask"]):
                time.sleep(30.0)
                continue
            try:
                r = self.hub.request("GET", f"/compute/v1/ask/next?worker_id={self.worker_id}&wait=20", timeout=50)
            except HubDown:
                time.sleep(10.0)
                continue
            if r.status != 200:
                time.sleep(10.0 if r.status != 204 else 0.0)
                continue
            try:
                self.answer_ask(r.json()["ask"])
            except Exception as e:  # noqa: BLE001
                log(f"ask: {e}")

    def answer_ask(self, ask: dict) -> None:
        """Embed the question, let the hub search, write the answer with the chat model and stream it back."""
        import queue
        import re
        from omy.brain import ask as ask_mod, embed
        base = f"/compute/v1/ask/{ask['id']}"
        t0 = time.monotonic()
        jobs.touch_demand("chat")      # a heavy job in hand gives way (guard_once), and the models are loaded
        try:
            qvec = embed.embed([ask["embed_text"]], query=True)[0].tolist()
        except Exception as e:  # noqa: BLE001 - BM25 only
            log(f"ask: no query embedding ({e})")
            qvec = None
        r = self.hub.request("POST", base + "/retrieve", timeout=120, body={"lease_id": ask["lease_id"], "qvec": qvec})
        if r.status != 200 or r.json().get("not_found"):
            return

        def post(events: list[dict]) -> bool:
            try:
                return self.hub.request("POST", base + "/events", timeout=30,
                                        body={"lease_id": ask["lease_id"], "events": events}).status == 200
            except HubDown:
                return False

        doc = r.json()
        try:
            url = models.request_chat()
        except Exception as e:  # noqa: BLE001
            post([{"type": "error", "message": f"{type(e).__name__}: {e}"[:200]}])
            return
        q: queue.Queue = queue.Queue()
        stop, holder = threading.Event(), {}
        threading.Thread(target=ask_mod._stream_llama, daemon=True,
                         args=(url, doc["messages"], q.put, stop, holder)).start()
        think, full, first = ask_mod.ThinkFilter(), "", None
        batch: list[dict] = []
        last = time.monotonic()
        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    item = None
                if item is ask_mod._DONE:
                    break
                if isinstance(item, Exception):
                    raise item
                if item:
                    text = think.feed(item)
                    if text:
                        first = first or time.monotonic()
                        full += text
                        batch.append({"type": "delta", "text": text})
                if batch and time.monotonic() - last >= 0.15:
                    if not post(batch):
                        return  # the hub does not know this lease any more
                    batch, last = [], time.monotonic()
            if tail := think.flush():
                full += tail
                batch.append({"type": "delta", "text": tail})
            valid = {c["n"] for c in doc["citations"]}
            used = sorted({int(n) for n in re.findall(r"\[(\d+)\]", full)} & valid)
            batch.append({"type": "done", "used": used, "timings": {
                "ttft_ms": int((first - t0) * 1000) if first else 0, "total_ms": int((time.monotonic() - t0) * 1000)}})
            post(batch)
        except Exception as e:  # noqa: BLE001
            post([*batch, {"type": "error", "message": f"{type(e).__name__}: {e}"[:200]}])
        finally:
            stop.set()
            resp = holder.get("resp")
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass

    # --- the Mac app's audio context ------------------------------------------------------------------------

    def sync_context(self, days: list[str] | None = None) -> None:
        """Upload the recent context/audio-DAY.jsonl files (written by the Mac app) to the hub."""
        self.last_context = time.time()
        if not self.context_dir.is_dir():
            return
        files = sorted(self.context_dir.glob("audio-????-??-??.jsonl"))[-CONTEXT_DAYS:]
        if days:
            files = [f for f in self.context_dir.glob("audio-????-??-??.jsonl") if f.stem[6:] in days] or files
        for f in files:
            try:
                data = f.read_bytes()
                self.hub.request("PUT", f"/compute/v1/context/{f.stem[6:]}", data=data, timeout=60,
                                 headers={"X-Sha256": hashlib.sha256(data).hexdigest()})
            except (OSError, HubDown) as e:
                log(f"context {f.name}: {e}")
                return


def jobs_retry(kind: str) -> float | None:
    from omy.worker import JOB_RETRY_S
    return JOB_RETRY_S if kind in ("transcribe", "diarize") else None


def main(once: bool = False, dry_run: bool = False, kinds: list[str] | None = None) -> dict:
    """`omy compute-worker`: forever (LaunchAgent). --once: take one job and exit; with --dry-run only report it
    and give it back; --kind limits what it takes (a test must not run the 27B)."""
    worker = ComputeWorker(hub_from_config())
    if not once:
        worker.run()
        return {}
    worker.check_workspace()
    ran = worker.step(wait=10.0, only=kinds, dry_run=dry_run, load_models=False)
    return {"ran": ran, "dry_run": dry_run}
