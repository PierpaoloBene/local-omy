"""The worker: everything that runs on its own, one tick at a time.

Every tick (TICK seconds):
1. Without the model, always: sync the changed transcripts and, every few
   minutes, the people; read back the ticks in commitments.md; refresh the
   vault notes of the sessions that changed; jobs registered by other parts
   (documents: scan for new and moved files).
2. Questions first. If the model cannot be loaded because local-omy is
   transcribing, and interrupt_omy is on: when the transcription will end
   within omy_wait_seconds, wait for it; otherwise pause it (SIGINT: its own
   cleanup runs, everything it writes is atomic, the session in progress is
   simply redone later). Then load the model and answer. Without interrupt_omy
   the question stays queued and gets the plain text search meanwhile.
3. Otherwise, a round of model work when one is due (every round_minutes, or
   as soon as local-omy finishes a transcription) and the gate is open:
   embeddings, then summaries one session at a time, then profiles, then the
   jobs registered by other parts (documents). Between jobs: a question that
   came in goes first, and if local-omy started the round stops and the model
   is unloaded. A question arriving during a model call cuts the call off
   (the job stays to do).
4. The model is unloaded after idle_minutes without use; if brain paused a
   transcription, it is then resumed: `omy transcribe --pending --render`
   (brings the day files up to date with what was done before the pause),
   then `omy transcribe --pending` if auto_transcribe is on.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from brain import ask as ask_mod
from brain import db, embed, gate, ingest, omy, summarize, vault
from brain.config import Settings
from brain.llm import Aborted, EmbedServer, LLMError, Reranker, RerankServer, Server

TICK = 15
PEOPLE_EVERY = 600
# whisper.cpp runs at about 15 times real time on this Mac (local-omy README).
TRANSCRIBE_SPEED = 15
EMBED_BATCH = 256
# How long a question waits for memory to free up before it is queued.
MEMORY_WAIT = 60


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", file=sys.stderr, flush=True)


def _cmdline(pid: int) -> str:
    r = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _notify(message: str) -> None:
    # Message passed as an argument, never interpolated into the script.
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    'display notification (item 1 of argv) with title "maimaind"', "-e", "end run", message],
                   check=False, capture_output=True)


@dataclass
class Deps:
    """What the worker touches outside itself; replaced in the tests."""
    settings: Settings
    gate: Callable[..., gate.Gate] = None
    progress: Callable[[], dict] = None
    features: Callable[[], dict] = None
    omy_run: Callable[..., dict] = None
    omy_start: Callable[..., object] = None
    people: Callable[[], dict] = None
    chat: Server = None
    embedder: EmbedServer = None
    reranker: RerankServer = None
    kill: Callable[[int, int], None] = None
    alive: Callable[[int], bool] = _alive
    cmdline: Callable[[int], str] = _cmdline
    notify: Callable[[str], None] = _notify
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self):
        s = self.settings
        self.gate = self.gate or (lambda loaded=False: gate.check(s, model_loaded=loaded))
        self.progress = self.progress or (lambda: omy.transcription(s))
        self.features = self.features or (lambda: omy.features(s))
        self.omy_run = self.omy_run or (lambda *a: omy.run(s, *a, timeout=900))
        self.omy_start = self.omy_start or (lambda *a: omy.start(s, *a))
        self.people = self.people or (lambda: omy.people(s))
        self.chat = self.chat or Server(s)
        self.embedder = self.embedder or EmbedServer(s)
        self.reranker = self.reranker or RerankServer(s)
        self.kill = self.kill or os.kill


@dataclass
class Question:
    text: str
    chat_id: str | None = None
    history: list[dict] = field(default_factory=list)
    on_event: Callable[[str, object], None] | None = None
    # Called once answered, whether or not anyone is still listening (the chat is saved there).
    on_done: Callable[["Question"], None] | None = None
    answer: ask_mod.Answer | None = None
    queued_reason: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


class Worker:
    def __init__(self, deps: Deps, conn=None):
        self.d = deps
        self.s = deps.settings
        self.conn = conn or db.connect(self.s.db_path)
        self.questions: queue.Queue[Question] = queue.Queue()
        self.waiting: list[Question] = []      # questions that could not be answered yet
        self.llm = None                        # the chat client, when the model is loaded
        self.embed_client = None
        self.rerank_client = None
        self.last_use = 0.0
        self.last_round = None                 # never: the first round is due at once
        self.last_people = None
        self.round_due = True
        self.omy_was_running = False
        self.paused_omy = False
        self.busy = threading.Lock()           # held while a model call of a job runs
        self.status = "avvio"
        self.started = False
        # Other parts plug in here (documents): cheap scans every tick, model jobs in a round.
        self.scans: list[Callable[[], object]] = []
        self.jobs: list[Callable[["Worker"], bool]] = []
        self.ask_setup = None
        self.unload_requested = False

    # --- commands from the settings page (API threads) ------------------------------

    def request_round(self) -> None:
        """Work now (summaries, embeddings, documents) instead of at the next round."""
        self.round_due = True

    def request_unload(self) -> None:
        """Free the memory now: the round in progress stops, the next one is a full
        round_minutes away."""
        self.unload_requested = True
        if self.llm is not None and self.busy.locked():
            self.llm.abort()

    # --- questions (called from the API threads) -----------------------------------

    def submit(self, q: Question) -> Question:
        self.questions.put(q)
        # A job's model call in progress is cut off: the question goes first.
        if self.llm is not None and self.busy.locked():
            self.llm.abort()
        return q

    def _pending_questions(self) -> list[Question]:
        out = list(self.waiting)
        self.waiting = []
        while True:
            try:
                out.append(self.questions.get_nowait())
            except queue.Empty:
                return out

    # --- model -----------------------------------------------------------------------

    def load(self):
        if self.llm is None:
            log("loading the model")
            self.llm = self.d.chat.start(self.s.profile())
        self.last_use = self.d.clock()
        return self.llm

    def embedder(self):
        if self.embed_client is None:
            self.embed_client = embed.Embedder(self.d.embedder.start())
        return self.embed_client

    def reranker(self):
        """The reranker, when its model is here: questions only (~0.7 GB)."""
        if self.rerank_client is None:
            srv = self.d.reranker
            if not self.s.model_path(srv.profile()).is_file():
                return None
            self.rerank_client = Reranker(srv.start())
        return self.rerank_client

    def unload(self, why: str) -> None:
        if self.llm is not None or self.embed_client is not None or self.rerank_client is not None:
            log(f"unloading the model: {why}")
        self.d.chat.stop()
        self.d.embedder.stop()
        self.d.reranker.stop()
        self.llm = self.embed_client = self.rerank_client = None

    # --- the tick ----------------------------------------------------------------------

    def tick(self) -> None:
        self.cheap()
        if self.unload_requested:
            self.unload_requested = False
            self.unload("richiesto")
            self.resume_omy()
            self.round_due = False
            self.last_round = self.d.clock()
        try:
            progress = self.d.progress()
        except omy.OmyError as e:
            progress = {"running": True, "unknown": str(e)}  # not knowing counts as busy
        running = bool(progress.get("running"))
        questions = self._pending_questions()
        if questions:
            self.answer(questions, progress)
            return
        if self.llm is not None and self.d.clock() - self.last_use > self.s.idle_minutes * 60:
            self.unload("idle")
            self.resume_omy()
        if running:
            if self.llm is not None and not self.paused_omy:
                self.unload("local-omy is transcribing")
            self.omy_was_running = True
            self.status = "local-omy sta trascrivendo"
            return
        if self.omy_was_running:
            self.omy_was_running = False
            self.round_due = True  # new transcripts just arrived
        due = self.round_due or self.last_round is None or \
            self.d.clock() - self.last_round > self.s.round_minutes * 60
        if due:
            g = self.d.gate(loaded=self.llm is not None)
            if not g.open:
                self.status = f"in attesa: {g.reason}"
                return
            self.round()

    def cheap(self) -> None:
        """Everything that needs no model."""
        report = ingest.sync(self.conn, self.s.transcripts)
        if not self.started:
            # The whole vault once at start (cheap: unchanged notes are not rewritten).
            self.started = True
            vault.render_all(self.conn, self.s.vault)
        now = self.d.clock()
        if self.last_people is None or now - self.last_people > PEOPLE_EVERY:
            try:
                ingest.sync_people(self.conn, self.d.people())
                self.last_people = now
            except omy.OmyError as e:
                log(f"people: {e}")
        if report.sessions_touched:
            log(f"sync: {report.as_dict()}")
            self.round_due = True
            vault.render_all(self.conn, self.s.vault, report.sessions_touched)
        else:
            vault.sync_commitment_ticks(self.conn, self.s.vault)
        for scan in self.scans:
            try:
                if scan():
                    self.round_due = True
            except Exception as e:  # a scan must never stop the worker
                log(f"scan failed: {e!r}")

    def interrupted(self) -> bool:
        """Should a round stop here? A question waits, or local-omy started."""
        if not self.questions.empty() or self.unload_requested:
            return True
        try:
            if self.d.progress().get("running"):
                self.unload("local-omy started")
                return True
        except omy.OmyError:
            return True
        return False

    def round(self) -> None:
        self.round_due = False
        self.status = "lavoro in corso"
        log("round: start")
        try:
            # Embeddings: small model, fast.
            r = embed.sync(self.conn, None)
            while r.pending and not self.interrupted():
                r = embed.sync(self.conn, self.embedder(), limit=EMBED_BATCH)
            done = set()
            for sid in summarize.pending(self.conn):
                if self.interrupted():
                    self.round_due = True
                    return
                try:
                    with self.busy:
                        summarize.summarize(self.conn, self.load(), sid, self.s.model)
                    done.add(sid)
                except Aborted:
                    log(f"summary of {sid} cut off by a question: later")
                    self.round_due = True
                    return
                except (summarize.SummarizeError, LLMError) as e:
                    log(f"summary of {sid} failed: {e}")
                finally:
                    self.last_use = self.d.clock()
            if done:
                vault.render_all(self.conn, self.s.vault, done)
            for person, _ in summarize.profiles_due(self.conn):
                if self.interrupted():
                    self.round_due = True
                    return
                try:
                    with self.busy:
                        summarize.write_profile(self.conn, self.load(), person)
                    vault.render_all(self.conn, self.s.vault, set())
                except (summarize.SummarizeError, LLMError) as e:
                    log(f"profile of {person} failed: {e}")
            for job in self.jobs:
                # A job does a piece of work and says whether there is more.
                while not self.interrupted():
                    try:
                        if not job(self):
                            break
                    except Aborted:
                        self.round_due = True
                        return
            if embed.sync(self.conn, None).pending and not self.interrupted():
                embed.sync(self.conn, self.embedder())  # summaries and documents just written
            self.last_round = self.d.clock()
        finally:
            self.status = "pronto"
            log("round: end")

    # --- answering ---------------------------------------------------------------------

    def answer(self, questions: list[Question], progress: dict) -> None:
        if self.llm is None and progress.get("running") and not self.paused_omy:
            if not self.make_room(progress):
                for q in questions:
                    self.queue_with_fallback(q, "local-omy sta trascrivendo")
                return
        if self.llm is None:
            # Memory takes a moment to come back after local-omy stops.
            g = self.d.gate(loaded=False)
            for _ in range(MEMORY_WAIT // 5):
                if g.open or "local-omy" in g.reason:
                    break
                self.d.sleep(5)
                g = self.d.gate(loaded=False)
            if not g.open and "local-omy" not in g.reason and not self.paused_omy:
                for q in questions:
                    self.queue_with_fallback(q, g.reason)
                return
        try:
            llm = self.load()
        except LLMError as e:
            for q in questions:
                self.queue_with_fallback(q, f"modello non disponibile: {e}")
            return
        embedder = None
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_map'").fetchone() and \
                self.conn.execute("SELECT 1 FROM vec_map LIMIT 1").fetchone():
            try:
                embedder = self.embedder()
            except LLMError as e:
                log(f"semantic search unavailable: {e}")
        reranker = None
        try:
            reranker = self.reranker()
        except LLMError as e:
            log(f"reranker unavailable: {e}")
        for q in questions:
            self.status = "rispondo"
            try:
                q.answer = ask_mod.ask(q.text, self.conn, llm, embedder=embedder, setup=self.ask_setup,
                                       on_event=q.on_event, history=q.history, reranker=reranker)
                q.queued_reason = None
            except LLMError as e:
                self.queue_with_fallback(q, f"errore del modello: {e}")
                continue
            finally:
                self.last_use = self.d.clock()
            if q.on_done:
                try:
                    q.on_done(q)
                except Exception as e:  # saving the chat must not lose the next questions
                    log(f"saving the answer failed: {e!r}")
            q.done.set()
        self.status = "pronto"

    def queue_with_fallback(self, q: Question, reason: str) -> None:
        """Not now: plain text search meanwhile, the question stays queued."""
        if q.queued_reason is None:
            from brain.tools import Tools
            hits = Tools(self.conn, ask_mod.constraints_for(q.text)).search_text(q.text, mode="qualsiasi", limit=10)
            q.queued_reason = reason
            if q.on_event:
                q.on_event("queued", {"reason": reason, "text_search": hits})
        self.waiting.append(q)

    def make_room(self, progress: dict) -> bool:
        """local-omy is transcribing and a question came in: wait for it, or pause it."""
        if not self.s.interrupt_omy:
            return False
        left = self.seconds_left(progress)
        if left is not None and left <= self.s.omy_wait_seconds:
            log(f"local-omy ends in ~{left:.0f} s: waiting")
            deadline = self.d.clock() + left + 60
            while self.d.clock() < deadline:
                self.d.sleep(5)
                try:
                    if not self.d.progress().get("running"):
                        return True
                except omy.OmyError:
                    pass
            return False
        pid = progress.get("pid")
        # Only a pid that is really omy's: pids are reused.
        if not pid or "omy" not in self.d.cmdline(pid):
            log(f"not pausing local-omy: pid {pid} is not an omy process")
            return False
        log(f"pausing local-omy (pid {pid}, session {progress.get('session')}) to answer a question")
        self.d.kill(pid, signal.SIGINT)
        for _ in range(60):
            if not self.d.alive(pid):
                break
            self.d.sleep(1)
        else:
            log("local-omy did not stop within 60 s")
            return False
        self.paused_omy = True
        self.d.notify("Trascrizione in pausa per rispondere a una domanda: riprende da sola dopo.")
        return True

    @staticmethod
    def seconds_left(progress: dict) -> float | None:
        if progress.get("phase") == "diarize" or "minutes_total" not in progress:
            return None
        left_audio = max(0.0, float(progress.get("minutes_total", 0)) - float(progress.get("minutes_done", 0)))
        return left_audio * 60 / TRANSCRIBE_SPEED

    def resume_omy(self) -> None:
        if not self.paused_omy:
            return
        self.paused_omy = False
        try:
            # The day files the paused run would have rewritten at its end.
            self.d.omy_run("transcribe", "--pending", "--render")
            if self.d.features().get("auto_transcribe", True):
                self.d.omy_start("transcribe", "--pending")
                log("local-omy resumed")
        except omy.OmyError as e:
            log(f"resuming local-omy failed: {e}")
            self.d.notify(f"Non sono riuscito a far ripartire la trascrizione: {e}")

    # --- running -----------------------------------------------------------------------

    def clear_leftovers(self) -> None:
        """Servers a previous run left behind (killed before it could unload: a crash,
        launchd restarting it) hold GPU memory, and with that memory taken the gate
        stays closed for good. Stopped here; the model loads again when needed."""
        left = [srv.name for srv in (self.d.chat, self.d.embedder, self.d.reranker) if srv.pid() is not None]
        if left:
            log(f"stopping servers left by a previous run: {', '.join(left)}")
            self.unload("left by a previous run")

    def run_forever(self, stop: threading.Event) -> None:
        log("worker: start")
        self.clear_leftovers()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # the worker must survive anything a tick does
                log(f"tick failed: {e!r}")
            # Wake up early for a question.
            deadline = time.monotonic() + TICK
            while not stop.is_set() and time.monotonic() < deadline and self.questions.empty():
                stop.wait(0.5)
        self.unload("stopping")
        self.resume_omy()
