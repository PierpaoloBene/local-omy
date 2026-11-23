"""The worker (LaunchAgent, KeepAlive): runs the jobs of omy.jobs one at a time,
by priority, and keeps the right llama-server loaded (omy.models).

Every SCAN_EVERY (or when woken: touch cache/worker.wake, SIGUSR1) it looks
for work: closed sessions to transcribe or diarize, transcribed ones to
organize. Transcribe and diarize run as subprocesses (`omy job ...`), so the
guard thread can stop a diarization when a chat question arrives; the
organizer is stopped by closing its llama-server. Heavy work resumes after
models.chat_idle() without questions. See brain/INTERFACES.md, 6 and 7.

Its state goes to cache/worker.json (read by omy jobs and the receiver).
"""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from omy import jobs, models
from omy.config import ARCHIVE, feature_on

STATUS = ARCHIVE / "cache" / "worker.json"
BATCH = ARCHIVE / "cache" / "organize-batch.json"
SCAN_EVERY = 300.0
IPHONE_EVERY = 3600.0
GUARD_EVERY = 0.5
IDLE_WAIT = 2.0
CHAT_RETRY = 300.0
# A job that just failed or finished is not queued again by the scan for this long.
COOLDOWN = {"failed": 3600.0, "done": 600.0}
ORGANIZE_COOLDOWN = 12 * 3600.0
HEAVY = ("organize", "diarize")
DEFAULT_WINDOW = "01:00-07:00"
JOB_RETRY_S = 300.0


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} worker: {msg}", file=sys.stderr, flush=True)


# --- the night window and the power ------------------------------------------------

def in_window(spec: str | None = None, now: datetime | None = None) -> bool:
    """Whether now is inside "HH:MM-HH:MM" (config organize_window), midnight crossing allowed."""
    spec = spec or str(models.config_value("organize_window", DEFAULT_WINDOW))
    try:
        a, b = (datetime.strptime(x.strip(), "%H:%M").time() for x in spec.split("-"))
    except ValueError:
        a, b = (datetime.strptime(x, "%H:%M").time() for x in DEFAULT_WINDOW.split("-"))
    t = (now or datetime.now()).time().replace(second=0, microsecond=0)
    return a <= t < b if a <= b else t >= a or t < b


def on_ac() -> bool:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "AC Power" in out


def spawn_job(kind: str, key: str) -> subprocess.Popen:
    """`python -m omy.cli job KIND KEY`, with stderr kept (tail) for the error."""
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen([sys.executable, "-m", "omy.cli", "job", kind, key], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=err, env={**os.environ, "OMY_ARCHIVE": str(ARCHIVE)})
    proc.errfile = err  # type: ignore[attr-defined]
    return proc


def _tail(proc) -> str:
    f = getattr(proc, "errfile", None)
    if f is None:
        return ""
    try:
        f.seek(0)
        text = f.read().decode(errors="replace")
        f.close()
    except (OSError, ValueError):
        return ""
    sys.stderr.write(text[-4000:])
    return text.strip().splitlines()[-1] if text.strip() else ""


class Worker:
    def __init__(self) -> None:
        self.wake_event = threading.Event()
        self.stopping = False
        self.current: jobs.Job | None = None
        self.proc = None
        self.preempted = False
        self.scan_due = True
        self.last_scan = 0.0
        self.last_iphone = 0.0
        self.chat_retry_at = 0.0
        self.wake_seen = jobs.wake_time()
        self.state = "starting"
        self.last_error: str | None = None
        self.guard_thread: threading.Thread | None = None

    # --- what may run -------------------------------------------------------------

    def organize_ok(self) -> bool:
        return (not models.chat_active() and jobs.pending(["transcribe", "diarize"]) == 0
                and in_window() and on_ac())

    def allowed(self) -> tuple[list[str], list[str]]:
        """(kinds that may run now, kinds that may run only if forced)."""
        kinds = ["transcribe", "render", "index"]
        forced: list[str] = []
        if not models.chat_active():
            kinds.append("diarize")
            forced.append("organize")
            if self.organize_ok():
                kinds.append("organize")
        return kinds, forced

    # --- scanning -----------------------------------------------------------------

    def scan(self) -> dict:
        """Queue what is missing: transcribe, diarize, organize. Cheap and
        idempotent (the queue deduplicates)."""
        from omy import transcribe
        self.last_scan = time.time()
        self.scan_due = False
        out = {"transcribe": 0, "diarize": 0, "organize": 0}

        def add(kind: str, key: str) -> bool:
            if jobs.recent(kind, key, "failed", COOLDOWN["failed"]) or jobs.recent(kind, key, "done", COOLDOWN["done"]):
                return False
            jobs.enqueue(kind, key)
            return True

        if not feature_on("auto_transcribe"):
            return out
        days = transcribe.pending_days()
        todo, still_open = transcribe.plan(days)
        for session, _prior in todo:
            out["transcribe"] += add("transcribe", session.id)
        waiting = {s.id for s, _ in todo}
        for session in transcribe._to_diarize(days, still_open):
            out["diarize"] += add("diarize", session.id)
            waiting.add(session.id)
        out["organize"] = self._scan_organize(days, set(still_open) | waiting)
        return out

    def _scan_organize(self, days, skip: set[str]) -> int:
        from omy import transcribe
        from omy.sessions import sessions_for_day
        try:
            from omy.brain import organize
        except ImportError:
            return 0
        n = 0
        for day in days:
            for session in sessions_for_day(day):
                if session.id in skip or not session.closed() or transcribe._load_doc(session) is None:
                    continue
                if (jobs.recent("organize", session.id, "done", ORGANIZE_COOLDOWN)
                        or jobs.recent("organize", session.id, "failed", COOLDOWN["failed"])):
                    continue
                try:
                    if organize.needs_organize(session.id):
                        jobs.enqueue("organize", session.id)
                        n += 1
                except Exception as e:  # one bad session must not stop the scan
                    log(f"needs_organize {session.id}: {e}")
        return n

    # --- running jobs --------------------------------------------------------------

    def step(self) -> bool:
        """One round: scan and renew if due, then run the best job allowed.
        True if a job ran."""
        now = time.time()
        if self.scan_due or now - self.last_scan >= SCAN_EVERY:
            try:
                self.scan()
            except Exception as e:
                log(f"scan failed: {e}")
        if now - self.last_iphone >= IPHONE_EVERY:
            self.renew_iphone()
        self.finalize_batch()
        kinds, forced = self.allowed()
        job = jobs.next_job(kinds, forced_kinds=forced)
        if job is None:
            self.idle()
            return False
        self.run_job(job)
        return True

    def renew_iphone(self) -> None:
        from omy import iphone
        self.last_iphone = time.time()
        self.state = "iphone"
        self.write_status()
        try:
            iphone.renew_or_notify()
        except Exception as e:
            log(f"iphone renew: {e}")

    def idle(self) -> None:
        """Nothing to run: keep the chat model loaded, if there is one."""
        self.state = "idle"
        if (not models.available("chat") or models.state()["loaded"] == "chat"
                or time.time() < self.chat_retry_at):
            return
        try:
            models.ensure("chat")
        except models.ModelError as e:
            log(f"chat model: {e}")
            self.chat_retry_at = time.time() + CHAT_RETRY

    def run_job(self, job: jobs.Job) -> None:
        self.current, self.preempted = job, False
        self.state = "running"
        heavy = job.kind in HEAVY
        if heavy:
            models.mark_heavy_start()
        self.write_status()
        log(f"{job.kind} {job.key} (attempt {job.attempts})")
        try:
            handler = {"transcribe": self.run_subprocess, "diarize": self.run_subprocess, "organize": self.run_organize,
                       "render": self.run_render, "index": self.run_index}[job.kind]
            handler(job)
            jobs.finish(job.id)
        except models.Preempted:
            log(f"{job.kind} {job.key} preempted, back in the queue")
            jobs.requeue(job.id)
        except Exception as e:
            if self.preempted:
                log(f"{job.kind} {job.key} interrupted ({e}), back in the queue")
                jobs.requeue(job.id)
            else:
                self.last_error = f"{job.kind} {job.key}: {e}"
                log(f"{job.kind} {job.key} failed: {e}")
                jobs.fail(job.id, str(e), JOB_RETRY_S if job.kind in ("transcribe", "diarize") else None)
        finally:
            self.current, self.proc = None, None
            if job.kind == "transcribe":
                self.scan_due = True  # its diarization and notes come next
            self.write_status()

    def run_subprocess(self, job: jobs.Job) -> None:
        if job.kind == "diarize":
            models.release_all()  # pyannote needs the memory
        proc = self.proc = spawn_job(job.kind, job.key)
        while proc.poll() is None:
            if self.stopping:
                proc.terminate()
            time.sleep(0.2)
        err = _tail(proc)
        if self.preempted:
            raise models.Preempted("interrupted")
        if proc.returncode != 0:
            raise RuntimeError(f"exit code {proc.returncode}" + (f": {err}" if err else ""))

    def run_organize(self, job: jobs.Job) -> None:
        from omy.brain import organize, vault
        if not job.force and hasattr(organize, "needs_organize") and not organize.needs_organize(job.key):
            return
        models.check_preempt()
        base = models.ensure("organizer")
        if self.preempted:
            raise models.Preempted("a chat question arrived")
        kwargs = {"force": True} if job.force and "force" in inspect.signature(organize.organize_session).parameters else {}
        organize.organize_session(job.key, base, **kwargs)
        vault.render_note(job.key)
        self.add_batch(job.key)

    def run_render(self, job: jobs.Job) -> None:
        from omy.brain import vault
        vault.rerender_people(None if job.key == "people" else [x for x in job.key.split(",") if x])

    def run_index(self, job: jobs.Job) -> None:
        try:
            from omy.brain import index
        except ImportError:
            return
        index.update()

    # --- the batch of organized notes ---------------------------------------------------

    def batch(self) -> list[str]:
        try:
            return json.loads(BATCH.read_text())
        except (FileNotFoundError, ValueError):
            return []

    def add_batch(self, key: str) -> None:
        b = self.batch()
        if key not in b:
            BATCH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            BATCH.write_text(json.dumps([*b, key]))

    def finalize_batch(self) -> None:
        """When no organize job is left (or none may run now): links,
        entity pages and indexes of the notes just written."""
        batch = self.batch()
        if not batch or (jobs.pending(["organize"]) > 0 and self.organize_ok()):
            return
        try:
            from omy.brain import vault
            vault.update_links(batch)
            vault.render_entities(batch)
            vault.render_indexes()
        except Exception as e:
            log(f"vault batch failed: {e}")
            self.last_error = f"vault batch: {e}"
            return
        BATCH.unlink(missing_ok=True)
        jobs.enqueue("index", "all")

    # --- the guard (questions during heavy work) ---------------------------------------------

    def guard_once(self) -> None:
        wt = jobs.wake_time()
        if wt > self.wake_seen:
            self.wake_seen, self.scan_due = wt, True
            self.wake_event.set()
        if not models.chat_active():
            return
        job = self.current
        if job is not None and job.kind in HEAVY and not self.preempted:
            self.preempted = True
            log(f"chat question: interrupting {job.kind} {job.key}")
            proc = self.proc
            if job.kind == "diarize" and proc is not None:
                proc.terminate()
            else:
                models.release("organizer")
        if models.available("chat") and models.state()["loaded"] != "chat" and time.time() >= self.chat_retry_at:
            try:
                models.ensure("chat")
            except models.ModelError as e:
                log(f"chat model: {e}")
                self.chat_retry_at = time.time() + CHAT_RETRY

    def _guard_loop(self) -> None:
        while not self.stopping:
            try:
                self.guard_once()
            except Exception as e:
                log(f"guard: {e}")
            time.sleep(GUARD_EVERY)

    # --- status --------------------------------------------------------------------------------

    def write_status(self) -> None:
        job = self.current
        try:
            status = {
                "pid": os.getpid(), "state": self.state, "heartbeat": time.time(),
                "job": job and {"id": job.id, "kind": job.kind, "key": job.key, "started": job.started},
                "loaded": models.state()["loaded"], "chat_active": models.chat_active(),
                "last_scan": self.last_scan or None, "last_error": self.last_error,
                "queue": jobs.summary(),
            }
            STATUS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = STATUS.with_name(f".{STATUS.name}.{os.getpid()}")
            tmp.write_text(json.dumps(status) + "\n")
            os.replace(tmp, STATUS)
        except Exception as e:  # the status is a courtesy
            log(f"status: {e}")

    # --- main loop ---------------------------------------------------------------------------------

    def stop(self, *_) -> None:
        self.stopping = True
        self.preempted = True  # what is running goes back in the queue, not to failed
        self.wake_event.set()
        if self.proc is not None:
            self.proc.terminate()

    def run(self) -> None:
        signal.signal(signal.SIGUSR1, lambda *_: (setattr(self, "scan_due", True), self.wake_event.set()))
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        models.kill_orphans()
        log(f"started, {jobs.reset_running()} running jobs back in the queue")
        self.guard_thread = threading.Thread(target=self._guard_loop, daemon=True)
        self.guard_thread.start()
        while not self.stopping:
            try:
                if self.step():
                    continue
            except Exception as e:
                log(f"step failed: {e}")
                self.last_error = str(e)
            self.write_status()
            self.wake_event.wait(IDLE_WAIT)
            self.wake_event.clear()
        models.release_all()
        self.state = "stopped"
        self.write_status()
        log("stopped")


def read_status() -> dict:
    try:
        return json.loads(STATUS.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def main() -> int:
    Worker().run()
    return 0
