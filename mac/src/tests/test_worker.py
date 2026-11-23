"""The worker: order, organize gating, preemption, chat model, render hook.
Models, subprocesses and the brain modules are fakes."""

import json
import sys
import threading
import time
import types
from datetime import datetime

import pytest

from omy import jobs, models, worker


class FakeProc:
    """A job subprocess that ends by itself (exit_after seconds) or when terminated."""

    def __init__(self, exit_after=0.0, code=0, ignore_term=False):
        self.t_end = time.monotonic() + exit_after
        self.code, self.returncode, self.terminated = code, None, False
        self.ignore_term, self.killed = ignore_term, False

    def poll(self):
        if self.returncode is None and self.killed:
            self.returncode = -9
        elif self.returncode is None and ((self.terminated and not self.ignore_term)
                                          or time.monotonic() >= self.t_end):
            self.returncode = -15 if self.terminated else self.code
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class FakeModels:
    def __init__(self):
        self.calls = []
        self.loaded = None
        self.chat_file = True
        self.on_release = None
        self.companions = []

    def ensure(self, role):
        self.calls.append(("ensure", role))
        if role == "embed":  # a companion: next to whatever is loaded
            self.companions = ["embed"]
            return f"http://fake/{role}"
        if role == "organizer":
            self.companions = []
        self.loaded = role
        return f"http://fake/{role}"

    def release(self, role):
        self.calls.append(("release", role))
        if self.loaded == role:
            self.loaded = None
        if self.on_release:
            self.on_release(role)

    def release_all(self):
        self.calls.append(("release_all",))
        self.loaded = None
        self.companions = []

    def abort(self, role=None):
        self.calls.append(("abort", role))
        if self.loaded == role or role is None:
            self.loaded = None
        if self.on_release:
            self.on_release(role)
        return True


@pytest.fixture
def env(archive, monkeypatch):
    fm = FakeModels()
    monkeypatch.setattr(models, "ensure", fm.ensure)
    monkeypatch.setattr(models, "release", fm.release)
    monkeypatch.setattr(models, "release_all", fm.release_all)
    monkeypatch.setattr(models, "abort", fm.abort)
    monkeypatch.setattr(models, "available", lambda role: role == "chat" and fm.chat_file)
    monkeypatch.setattr(models, "state", lambda: {"loaded": fm.loaded, "pid": None, "port": None,
                                                       "companions": list(fm.companions)})
    monkeypatch.setattr(models, "chat_idle", lambda: 300.0)
    monkeypatch.setattr(worker, "in_window", lambda *a, **k: True)
    monkeypatch.setattr(worker, "on_ac", lambda: True)
    monkeypatch.setattr(worker, "BATCH", archive / "cache" / "organize-batch.json")
    monkeypatch.setattr(worker, "STATUS", archive / "cache" / "worker.json")
    ran = []

    def spawn(kind, key):
        ran.append((kind, key))
        return FakeProc()

    monkeypatch.setattr(worker, "spawn_job", spawn)
    brain = sys.modules["omy.brain"] if "omy.brain" in sys.modules else __import__("omy.brain").brain
    organized, vault_calls = [], []
    organize = types.ModuleType("omy.brain.organize")
    organize.needs_organize = lambda sid: True
    organize.organize_session = lambda sid, base: organized.append((sid, base))
    vault = types.ModuleType("omy.brain.vault")
    vault.render_note = lambda sid: vault_calls.append(("note", sid))
    vault.update_links = lambda ids: vault_calls.append(("links", list(ids)))
    vault.render_entities = lambda ids: vault_calls.append(("entities", list(ids)))
    vault.render_indexes = lambda: vault_calls.append(("indexes",))
    vault.rerender_people = lambda ids: vault_calls.append(("people", ids))
    for name, mod in (("organize", organize), ("vault", vault)):
        monkeypatch.setitem(sys.modules, f"omy.brain.{name}", mod)
        monkeypatch.setattr(brain, name, mod, raising=False)
    index = types.ModuleType("omy.brain.index")
    index.update = lambda sessions=None: vault_calls.append(("index", sessions))
    monkeypatch.setitem(sys.modules, "omy.brain.index", index)
    monkeypatch.setattr(brain, "index", index, raising=False)

    w = worker.Worker()
    w.scan_due, w.last_scan, w.last_iphone = False, time.time(), time.time()
    return types.SimpleNamespace(w=w, fm=fm, ran=ran, organized=organized, vault=vault_calls, organize=organize)


def drain(w, limit=20):
    n = 0
    while w.step() and n < limit:
        n += 1
    return n


def test_runs_by_priority(env):
    for kind, key in [("index", "all"), ("organize", "o1"), ("render", "people"), ("diarize", "d1"),
                      ("transcribe", "t1")]:
        jobs.enqueue(kind, key)
    order = []
    orig = env.w.run_job
    env.w.run_job = lambda job: (order.append(job.kind), orig(job))
    drain(env.w)
    # transcribe and diarize queue an index job of their session, render one for 'all'
    assert order == ["transcribe", "diarize", "render", "organize", "index", "index", "index", "index"]
    assert env.ran == [("transcribe", "t1"), ("diarize", "d1")]
    assert jobs.summary()["queued"] == 0
    assert ("people", None) in env.vault and ("index", None) in env.vault
    assert env.organized == [("o1", "http://fake/organizer")]


def test_subprocess_failure_is_retried_then_fails(env, monkeypatch):
    monkeypatch.setattr(worker, "spawn_job", lambda k, key: FakeProc(code=2))
    jid = jobs.enqueue("transcribe", "t1")
    assert env.w.step()
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 1
    assert env.w.last_error and "exit code 2" in env.w.last_error


@pytest.mark.parametrize("window,ac,busy,chat,runs", [
    (True, True, False, False, True),
    (False, True, False, False, False),   # outside the night window
    (True, False, False, False, False),   # on battery
    (True, True, True, False, False),     # a transcription is waiting
    (True, True, False, True, False),     # somebody is asking questions
])
def test_organize_gating(env, monkeypatch, window, ac, busy, chat, runs):
    monkeypatch.setattr(worker, "in_window", lambda *a, **k: window)
    monkeypatch.setattr(worker, "on_ac", lambda: ac)
    monkeypatch.setattr(models, "chat_active", lambda now=None: chat)
    if busy:
        jobs.enqueue("diarize", "d1", )
        jobs.next_job(["diarize"], now=time.time())  # running: still counts
    jobs.enqueue("organize", "o1")
    env.w.step()
    assert bool(env.organized) is runs
    assert jobs.pending(["organize"]) == (0 if runs else 1)


def test_forced_organize_ignores_window_ac_and_pending(env, monkeypatch):
    monkeypatch.setattr(worker, "in_window", lambda *a, **k: False)
    monkeypatch.setattr(worker, "on_ac", lambda: False)
    jobs.enqueue("transcribe", "t1", )
    jobs.enqueue("organize", "o1", force=True)
    monkeypatch.setattr(worker, "spawn_job", lambda k, key: FakeProc(exit_after=0))
    drain(env.w)
    assert env.organized == [("o1", "http://fake/organizer")]
    # but never while a question is being asked
    monkeypatch.setattr(models, "chat_active", lambda now=None: True)
    jobs.enqueue("organize", "o2", force=True)
    env.w.step()
    assert len(env.organized) == 1


def test_up_to_date_note_is_skipped_unless_forced(env):
    env.organize.needs_organize = lambda sid: False
    jobs.enqueue("organize", "o1")
    env.w.step()
    assert env.organized == [] and ("ensure", "organizer") not in env.fm.calls
    jobs.enqueue("organize", "o2", force=True)
    env.w.step()
    assert env.organized == [("o2", "http://fake/organizer")]


def test_organize_force_is_passed_if_supported(env):
    seen = []
    env.organize.organize_session = lambda sid, base, force=False: seen.append((sid, force))
    jobs.enqueue("organize", "o1", force=True)
    env.w.step()
    assert seen == [("o1", True)]


def test_batch_is_finalized_when_no_organize_is_left(env):
    jobs.enqueue("organize", "a")
    jobs.enqueue("organize", "b")
    env.w.step()
    assert [c for c in env.vault if c[0] != "note"] == []  # one more to go
    env.w.step()
    assert ("note", "a") in env.vault and ("note", "b") in env.vault
    env.w.step()  # finalize, then the index job it queued
    after = [c for c in env.vault if c[0] not in ("note",)]
    assert after[:3] == [("links", ["a", "b"]), ("entities", ["a", "b"]), ("indexes",)]
    assert ("index", ["a", "b"]) in env.vault
    assert env.w.batch() == []


def test_organize_failure_retries_with_backoff(env):
    def boom(sid, base):
        raise RuntimeError("model said no")
    env.organize.organize_session = boom
    jid = jobs.enqueue("organize", "o1")
    env.w.step()
    j = jobs.get(jid)
    assert jobs.state_of(jid) == "queued" and j.attempts == 1
    assert env.w.step() is False  # backing off


def test_render_job_keys(env):
    jobs.enqueue("render", "p1,p2")
    env.w.step()
    assert env.vault == [("people", ["p1", "p2"])]


def test_organize_preempted_by_a_question(env, monkeypatch):
    """The guard closes the organizer's server: the call fails, the job goes
    back with attempts as before, and the chat model is loaded."""
    gate = threading.Event()

    def long_organize(sid, base):
        assert gate.wait(5)
        raise ConnectionError("server closed")

    env.organize.organize_session = long_organize
    env.fm.on_release = lambda role: gate.set() if role == "organizer" else None
    jid = jobs.enqueue("organize", "o1")
    t = threading.Thread(target=env.w.step)
    t.start()
    deadline = time.time() + 5
    while env.w.current is None and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    jobs.touch_demand("chat")  # a question, after the job started
    env.w.guard_once()
    t.join(5)
    assert not t.is_alive()
    assert ("abort", "organizer") in env.fm.calls
    assert env.fm.calls[-1] == ("ensure", "chat") and env.fm.loaded == "chat"
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 0
    # no heavy work while the questions last; it resumes after CHAT_IDLE
    assert env.w.step() is False
    monkeypatch.setattr(models, "chat_idle", lambda: 0.0)
    env.organize.organize_session = lambda sid, base: env.organized.append((sid, base))
    assert env.w.step() is True
    assert env.organized == [("o1", "http://fake/organizer")]
    assert jobs.state_of(jid) == "done"
    assert env.fm.loaded == "organizer"  # the chat model made room


def test_diarize_preempted_terminates_subprocess(env, monkeypatch):
    procs = []
    monkeypatch.setattr(worker, "spawn_job", lambda k, key: procs.append(FakeProc(exit_after=30)) or procs[-1])
    jid = jobs.enqueue("diarize", "d1")
    t = threading.Thread(target=env.w.step)
    t.start()
    deadline = time.time() + 5
    while env.w.proc is None and time.time() < deadline:
        time.sleep(0.01)
    jobs.touch_demand("chat")
    env.w.guard_once()
    t.join(5)
    assert procs[0].terminated and not t.is_alive()
    assert ("release_all",) in env.fm.calls  # chat was unloaded for pyannote, then back
    assert env.fm.loaded == "chat"
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 0


def test_question_without_heavy_job_just_loads_chat(env):
    jobs.touch_demand("chat")
    env.w.guard_once()
    assert env.fm.loaded == "chat"
    env.fm.calls.clear()
    env.w.guard_once()  # already loaded
    assert env.fm.calls == []


def test_idle_keeps_chat_loaded_only_if_the_model_exists(env):
    env.w.step()
    assert env.fm.loaded == "chat"
    env.fm.loaded, env.fm.chat_file = None, False
    env.w.step()
    assert env.fm.loaded is None


def test_chat_unloaded_before_diarize_and_reloaded_after(env):
    env.w.step()
    assert env.fm.loaded == "chat"
    jobs.enqueue("diarize", "d1")
    env.w.step()
    assert env.fm.calls.index(("release_all",)) > env.fm.calls.index(("ensure", "chat"))
    assert env.fm.loaded is None
    env.w.step()  # the index job of d1
    env.w.step()  # nothing left: chat again
    assert env.fm.loaded == "chat"


def test_status_file(env):
    jobs.enqueue("render", "people")
    env.w.step()
    st = worker.read_status() if worker.STATUS.exists() else {}
    assert st["state"] in ("running", "idle") and "queue" in st and "heartbeat" in st


def test_wake_file_triggers_scan(env):
    assert not env.w.scan_due
    time.sleep(0.01)
    jobs.wake()
    env.w.guard_once()
    assert env.w.scan_due and env.w.wake_event.is_set()


def test_scan_enqueues_transcribe_diarize_and_organize(env, monkeypatch):
    from omy import transcribe
    from omy import sessions
    sess = lambda i: types.SimpleNamespace(id=i, closed=lambda: True, segments=[])  # noqa: E731
    monkeypatch.setattr(worker, "feature_on", lambda k: True)
    monkeypatch.setattr(transcribe, "pending_days", lambda: [datetime(2026, 11, 21).date()])
    monkeypatch.setattr(transcribe, "plan", lambda days: ([(sess("t1"), None)], ["open1"]))
    monkeypatch.setattr(transcribe, "_to_diarize", lambda days, still_open: [sess("d1")])
    monkeypatch.setattr(transcribe, "_load_doc", lambda s: {"x": 1})
    monkeypatch.setattr(sessions, "sessions_for_day", lambda day: [sess("t1"), sess("d1"), sess("open1"), sess("o1")])
    assert env.w.scan() == {"transcribe": 1, "diarize": 1, "organize": 1}
    kinds = {(j["kind"], j["key"]) for j in _queued()}
    assert kinds == {("transcribe", "t1"), ("diarize", "d1"), ("organize", "o1")}
    env.w.scan()  # idempotent
    assert jobs.summary()["queued"] == 3
    # a job that just failed for good is not queued again at once
    monkeypatch.setattr(worker, "COOLDOWN", {"failed": 3600.0, "done": 600.0})
    j = jobs.next_job(["transcribe"], now=time.time())
    for _ in range(jobs.MAX_ATTEMPTS):
        jobs.fail(j.id, "x", retry_after=0)
        j = jobs.next_job(["transcribe"], now=time.time() + 5) or j
    assert env.w.scan()["transcribe"] == 0


def _queued():
    from contextlib import closing
    with closing(jobs._connect()) as db:
        return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE state='queued'")]


def test_scan_respects_auto_transcribe_off(env, monkeypatch):
    monkeypatch.setattr(worker, "feature_on", lambda k: False)
    assert env.w.scan() == {"transcribe": 0, "diarize": 0, "organize": 0}


def test_in_window():
    t = lambda h, m: datetime(2026, 11, 21, h, m)  # noqa: E731
    assert worker.in_window("01:00-07:00", t(1, 0)) and worker.in_window("01:00-07:00", t(6, 59))
    assert not worker.in_window("01:00-07:00", t(7, 0)) and not worker.in_window("01:00-07:00", t(12, 0))
    assert worker.in_window("22:00-06:00", t(23, 30)) and worker.in_window("22:00-06:00", t(5, 0))
    assert not worker.in_window("22:00-06:00", t(12, 0))
    assert worker.in_window("garbage", t(3, 0))  # falls back to the default window


def test_on_ac(monkeypatch):
    import subprocess

    def fake(out):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out)
    monkeypatch.setattr(subprocess, "run", fake("Now drawing from 'AC Power'\n -InternalBattery-0 98%"))
    assert worker.on_ac()
    monkeypatch.setattr(subprocess, "run", fake("Now drawing from 'Battery Power'\n"))
    assert not worker.on_ac()


# --- hooks ------------------------------------------------------------------------------

def test_people_changes_queue_a_render_and_wake(archive, monkeypatch):
    from omy import diarize, people
    monkeypatch.setattr(diarize, "load_people", lambda: {"p1": {"name": "Anna"}})
    monkeypatch.setattr(diarize, "my_name", lambda: "Io")
    monkeypatch.setattr(people, "_save_people", lambda p: None)
    monkeypatch.setattr(people, "relabel", lambda: ["2026-11-21"])
    before = jobs.wake_time()
    time.sleep(0.01)
    assert people.rename_person("p1", "Anna Rossi")["name"] == "Anna Rossi"
    assert people.delete_person("p1")["deleted"] == "Anna"
    assert jobs.summary()["by_kind"]["render"] == {"queued": 1}  # deduplicated
    assert jobs.wake_time() > before


def test_every_name_change_has_the_hook():
    import inspect
    from omy import people
    for fn in ("rename", "set_source", "set_unknown", "set_person_source", "rename_person", "delete_person"):
        assert "_renamed(" in inspect.getsource(getattr(people, fn)), fn


def test_receiver_transcribe_wakes_the_worker(archive):
    from fastapi.testclient import TestClient

    from omy_receiver.app import Config, create_app
    token = "t" * 40
    c = TestClient(create_app(Config(token=token.encode(), archive=archive)))
    auth = {"Authorization": f"Bearer {token}"}
    (archive / "cache").mkdir(exist_ok=True)
    (archive / "cache" / "worker.json").write_text(json.dumps({"heartbeat": time.time()}))
    assert c.post("/v1/transcribe").status_code == 401
    before = jobs.wake_time()
    time.sleep(0.01)
    r = c.post("/v1/transcribe", headers=auth)
    assert r.status_code == 200 and r.json() == {"started": True, "running": True}
    assert jobs.wake_time() > before
    jobs.enqueue("transcribe", "t1")
    assert c.post("/v1/transcribe", headers=auth).json() == {"started": False, "running": True}
    assert c.get("/v1/brain/status").status_code == 401
    st = c.get("/v1/brain/status", headers=auth).json()
    assert set(st) == {"worker", "jobs", "models"} and st["jobs"]["queued"] == 1
    assert st["models"]["loaded"] is None


def test_guard_loads_embed_before_chat(env, monkeypatch):
    monkeypatch.setattr(models, "available", lambda role: True)
    jobs.touch_demand("chat")
    env.w.guard_once()
    assert [c for c in env.fm.calls if c[0] == "ensure"] == [("ensure", "embed"), ("ensure", "chat")]


def test_retry_timestamps_are_per_role(env, monkeypatch):
    monkeypatch.setattr(models, "available", lambda role: True)
    real = env.fm.ensure

    def ensure(role):
        if role == "embed":
            raise models.ModelError("no embed")
        return real(role)

    monkeypatch.setattr(models, "ensure", ensure)
    jobs.touch_demand("chat")
    env.w.guard_once()
    assert env.fm.loaded == "chat" and "embed" in env.w.retry_at and "chat" not in env.w.retry_at
    env.fm.loaded = None  # chat lost: the embed failure must not block its reload
    env.w.guard_once()
    assert env.fm.loaded == "chat"


def test_companions_never_loaded_during_unpreempted_heavy_job(env, monkeypatch):
    monkeypatch.setattr(models, "available", lambda role: True)
    for kind in ("diarize", "organize"):
        env.w.current, env.w.preempted = types.SimpleNamespace(kind=kind), False
        env.w.load_roles(("embed", "chat"))
        env.w.idle()
        assert env.fm.calls == []
    env.w.preempted = True  # a question preempted it: embed and chat may come
    env.w.load_roles(("embed", "chat"))
    assert ("ensure", "embed") in env.fm.calls
