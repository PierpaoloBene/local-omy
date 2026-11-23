"""Worker robustness: the transcribe lock shared with the old periodic agent,
no infinite retries, the guard while a model loads, SIGTERM, the receiver
fallback, the batch backoff. No real model, server or subprocess of ours."""

import fcntl
import json
import stat
import subprocess
import sys
import threading
import time
import types
from contextlib import closing
from datetime import datetime

import pytest

from omy import cli, jobs, macsetup, models, transcribe, worker
from test_models import FAKE, free_port
from test_worker import FakeProc, env  # noqa: F401  (the fixture)


def hold_lock():
    (worker.ARCHIVE / "cache").mkdir(parents=True, exist_ok=True)
    f = (worker.ARCHIVE / "cache" / ".transcribe.lock").open("w")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return f


def heartbeat(age=0.0):
    (worker.ARCHIVE / "cache").mkdir(parents=True, exist_ok=True)
    (worker.ARCHIVE / "cache" / "worker.json").write_text(json.dumps({"heartbeat": time.time() - age}))


# --- the old periodic agent and the worker ---------------------------------------

def test_periodic_does_not_transcribe_while_the_worker_is_alive(archive, monkeypatch):
    monkeypatch.setattr(cli, "feature_on", lambda k: True)
    monkeypatch.setattr(cli.iphone, "renew", lambda: {"renewed": False})
    called = []
    monkeypatch.setattr(transcribe, "transcribe", lambda days: called.append(days))
    heartbeat(10)
    assert cli.periodic()["transcribe"] == {"skipped": "the worker is running"}
    assert called == []
    heartbeat(16 * 60)  # stale: the worker is gone, periodic works as before
    monkeypatch.setattr(transcribe, "pending_days", lambda: [])
    monkeypatch.setattr(transcribe, "transcribe", lambda days: {"failed": {}})
    assert cli.periodic()["transcribe"] == {"failed": {}}


def test_transcribe_and_periodic_take_the_same_lock_as_the_worker_jobs(archive, monkeypatch):
    monkeypatch.setattr(transcribe, "models_status", lambda: {"x": True})
    monkeypatch.setattr(cli, "feature_on", lambda k: True)
    monkeypatch.setattr(cli.iphone, "renew", lambda: {})
    monkeypatch.setattr(transcribe, "pending_days", lambda: [])
    f = hold_lock()  # the worker's diarize job is running
    try:
        assert "another transcription" in cli.periodic()["transcribe"]["skipped"]
        monkeypatch.setattr(transcribe, "find_session", lambda sid: types.SimpleNamespace(day=None))
        with pytest.raises(transcribe.Busy):
            transcribe.transcribe_job("x")
        monkeypatch.setattr(transcribe, "_load_doc", lambda s: {})
        with pytest.raises(transcribe.Busy):
            transcribe.diarize_job("x")
    finally:
        f.close()
    transcribe.take_lock().close()  # released


def test_busy_job_exits_75_and_the_worker_requeues_it_soon(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(worker, "spawn_job", lambda k, key: FakeProc(code=transcribe.BUSY_EXIT))
    jid = jobs.enqueue("diarize", "d1")
    assert env.w.step()
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 0
    assert env.w.step() is False  # not due yet
    with closing(jobs._connect()) as db:
        nb = db.execute("SELECT not_before FROM jobs WHERE id=?", (jid,)).fetchone()[0]
    assert 0 < nb - time.time() <= worker.BUSY_RETRY_S


def test_cli_job_busy_exit_code(archive, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["omy", "job", "diarize", "x"])

    def busy(sid):
        raise transcribe.Busy("busy")

    monkeypatch.setattr(transcribe, "diarize_job", busy)
    assert cli.main() == transcribe.BUSY_EXIT


# --- no infinite retry -------------------------------------------------------------

def test_scan_does_not_requeue_a_permanent_failure_until_the_input_changes(env, monkeypatch):  # noqa: F811
    from omy import sessions
    segs = [types.SimpleNamespace(id="a")]
    sess = types.SimpleNamespace(id="t1", closed=lambda: True, segments=segs)
    monkeypatch.setattr(worker, "feature_on", lambda k: True)
    monkeypatch.setattr(transcribe, "pending_days", lambda: [datetime(2026, 11, 21).date()])
    monkeypatch.setattr(transcribe, "plan", lambda days: ([(sess, None)], []))
    monkeypatch.setattr(transcribe, "_to_diarize", lambda days, still_open: [])
    monkeypatch.setattr(transcribe, "_load_doc", lambda s: None)
    monkeypatch.setattr(sessions, "sessions_for_day", lambda day: [sess])
    monkeypatch.setattr(worker, "COOLDOWN", {"failed": 0.0, "done": 0.0})
    assert env.w.scan()["transcribe"] == 1
    for _ in range(jobs.MAX_ATTEMPTS):
        j = jobs.next_job(["transcribe"], now=time.time() + 1e6)
        jobs.fail(j.id, "boom", retry_after=0)
    assert env.w.scan()["transcribe"] == 0 and jobs.pending(["transcribe"]) == 0
    assert env.w.scan()["transcribe"] == 0
    segs.append(types.SimpleNamespace(id="b"))  # the session grew
    assert env.w.scan()["transcribe"] == 1


def test_organize_permanent_failure_waits_for_a_new_input_hash(env, monkeypatch):  # noqa: F811
    from omy import sessions
    sess = types.SimpleNamespace(id="o1", closed=lambda: True, segments=[])
    monkeypatch.setattr(worker, "feature_on", lambda k: True)
    monkeypatch.setattr(transcribe, "pending_days", lambda: [datetime(2026, 11, 21).date()])
    monkeypatch.setattr(transcribe, "plan", lambda days: ([], []))
    monkeypatch.setattr(transcribe, "_to_diarize", lambda days, still_open: [])
    monkeypatch.setattr(transcribe, "_load_doc", lambda s: {"x": 1})
    monkeypatch.setattr(sessions, "sessions_for_day", lambda day: [sess])
    monkeypatch.setattr(worker, "COOLDOWN", {"failed": 0.0, "done": 0.0})
    hashes = ["h1"]
    env.organize.input_hash = lambda sid: hashes[-1]
    assert env.w.scan()["organize"] == 1
    for _ in range(jobs.MAX_ATTEMPTS):
        j = jobs.next_job(["organize"], now=time.time() + 1e6)
        jobs.fail(j.id, "boom", retry_after=0)
    assert env.w.scan()["organize"] == 0
    hashes.append("h2")
    assert env.w.scan()["organize"] == 1


def test_failed_transcribe_does_not_hold_organize_back(env):  # noqa: F811
    jid = jobs.enqueue("transcribe", "t1")
    for _ in range(jobs.MAX_ATTEMPTS):
        j = jobs.next_job(["transcribe"], now=time.time() + 1e6)
        jobs.fail(j.id, "boom", retry_after=0)
    assert jobs.state_of(jid) == "failed"
    assert env.w.organize_ok()


# --- preemption --------------------------------------------------------------------

def test_diarize_that_ignores_terminate_is_killed_before_the_chat_model_loads(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(worker, "PROC_STOP_WAIT", 0.3)
    proc = FakeProc(exit_after=60, ignore_term=True)
    monkeypatch.setattr(worker, "spawn_job", lambda k, key: proc)
    order = []
    real_ensure = env.fm.ensure
    monkeypatch.setattr(models, "ensure", lambda role: (order.append((role, proc.killed)), real_ensure(role))[1])
    jid = jobs.enqueue("diarize", "d1")
    t = threading.Thread(target=env.w.step)
    t.start()
    deadline = time.time() + 5
    while env.w.proc is None and time.time() < deadline:
        time.sleep(0.01)
    jobs.touch_demand("chat")
    env.w.guard_once()
    t.join(5)
    assert proc.terminated and proc.killed
    assert order == [("chat", True)]  # chat loaded only after the kill
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 0


def test_guard_aborts_without_waiting_when_ensure_organizer_is_loading(env, monkeypatch):  # noqa: F811
    """The guard calls abort(), which needs no lock: it works while ensure() is stuck."""
    release = threading.Event()
    in_ensure = threading.Event()

    def slow_ensure(role):
        if role == "organizer":
            in_ensure.set()
            if release.wait(5):
                raise models.Preempted("aborted")
        return f"http://fake/{role}"

    aborted = []
    monkeypatch.setattr(models, "ensure", slow_ensure)
    monkeypatch.setattr(models, "abort", lambda role=None: (aborted.append(role), release.set())[0])
    jid = jobs.enqueue("organize", "o1")
    t = threading.Thread(target=env.w.step)
    t.start()
    assert in_ensure.wait(5)
    jobs.touch_demand("chat")
    env.w.guard_once()
    t.join(5)
    assert not t.is_alive() and aborted == ["organizer"]
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 0
    assert env.organized == []


def test_question_before_ensure_never_loads_the_organizer(env, monkeypatch):  # noqa: F811
    jid = jobs.enqueue("organize", "o1")
    calls = iter([False, False])  # allowed() and organize_ok() see no chat; the check before ensure does
    monkeypatch.setattr(models, "chat_active", lambda now=None: next(calls, True))
    assert env.w.step()
    assert ("ensure", "organizer") not in env.fm.calls
    assert jobs.state_of(jid) == "queued" and jobs.get(jid).attempts == 0


@pytest.fixture
def fake_models(tmp_path, monkeypatch, archive):
    script = tmp_path / "fake_llama.py"
    script.write_text(FAKE)
    binary = tmp_path / "llama-server"
    binary.write_text(f'#!/bin/sh\nsleep ${{SLOW:-0}}\nexec {sys.executable} {script} "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("OMY_LLAMA_SERVER", str(binary))
    specs = {}
    for role in ("organizer", "chat"):
        gguf = tmp_path / f"{role}.gguf"
        gguf.write_text("x")
        specs[role] = {"path": gguf, "port": free_port(), "args": []}
    monkeypatch.setattr(models, "MODELS", specs)
    monkeypatch.setattr(models, "LOG", tmp_path / "models.log")
    yield
    models._aborted.clear()
    models.release_all()


def test_models_abort_interrupts_a_load_in_progress_without_the_mutex(fake_models, monkeypatch):
    monkeypatch.setenv("SLOW", "30")
    result = []

    def load():
        try:
            models.ensure("organizer")
        except BaseException as e:
            result.append(e)

    t = threading.Thread(target=load)
    t.start()
    deadline = time.time() + 5
    while models._active is None and time.time() < deadline:
        time.sleep(0.01)
    t0 = time.time()
    assert models.abort("organizer")  # the thread holds the mutex: abort does not take it
    t.join(5)
    assert not t.is_alive() and time.time() - t0 < 4
    assert isinstance(result[0], models.Preempted)
    monkeypatch.setenv("SLOW", "0")
    assert models.ensure("chat").endswith(str(models.MODELS["chat"]["port"]))  # and chat loads at once


def test_abort_with_nothing_running(fake_models):
    assert models.abort("organizer") is False


# --- SIGTERM ------------------------------------------------------------------------

def test_stop_kills_the_llama_server_recorded_in_models_json(archive):
    victim = subprocess.Popen(["sleep", "60"])
    try:
        models._write_state({"role": "organizer", "pid": victim.pid, "port": 1, "since": time.time()})
        w = worker.Worker()
        w.stop()
        assert victim.wait(5) == -9
        assert w.stopping and w.preempted
    finally:
        victim.kill()
        victim.wait()


# --- the receiver without a worker -------------------------------------------------------

def test_receiver_starts_transcribe_itself_if_the_heartbeat_is_stale(archive, monkeypatch):
    from fastapi.testclient import TestClient

    from omy_receiver import app as app_mod
    spawned = []

    class P:
        def poll(self):
            return None

        def wait(self):
            time.sleep(0.01)

    def popen(cmd, **kw):
        spawned.append(cmd)
        return P()

    monkeypatch.setattr(app_mod.subprocess, "Popen", popen)
    token = "t" * 40
    c = TestClient(app_mod.create_app(app_mod.Config(token=token.encode(), archive=archive)))
    auth = {"Authorization": f"Bearer {token}"}
    assert c.post("/v1/transcribe", headers=auth).json() == {"started": True, "running": True}  # no worker.json
    assert spawned and spawned[0][-2:] == ["transcribe", "--pending"]
    heartbeat(20 * 60)  # stale
    assert c.post("/v1/transcribe", headers=auth).json() == {"started": False, "running": True}
    assert len(spawned) == 1  # the first is still running
    heartbeat(5)
    jobs.enqueue("transcribe", "t1")  # the worker is alive: it is only woken
    assert c.post("/v1/transcribe", headers=auth).json() == {"started": False, "running": True}
    assert len(spawned) == 1


# --- the batch -----------------------------------------------------------------------------

def test_batch_failure_backs_off_exponentially_and_logs_once_per_attempt(env, monkeypatch, capsys):  # noqa: F811
    env.w.add_batch("a")
    fails = []

    def boom(ids):
        fails.append(ids)
        raise RuntimeError("vault down")

    monkeypatch.setattr(sys.modules["omy.brain.vault"], "update_links", boom)
    env.w.finalize_batch()
    assert len(fails) == 1 and env.w.batch_failures == 1
    assert 290 < env.w.batch_retry_at - time.time() <= 300
    env.w.finalize_batch()
    env.w.finalize_batch()
    assert len(fails) == 1  # waiting
    env.w.batch_retry_at = 0
    env.w.finalize_batch()
    assert len(fails) == 2 and 590 < env.w.batch_retry_at - time.time() <= 600
    env.w.batch_failures = 20
    env.w.batch_retry_at = 0
    env.w.finalize_batch()
    assert env.w.batch_retry_at - time.time() <= 6 * 3600
    assert capsys.readouterr().err.count("vault batch failed") == 3
    assert env.w.batch() == ["a"]  # kept for the retry


def test_unreadable_sidecar_is_dropped_from_the_batch(env, tmp_path):  # noqa: F811
    good = tmp_path / "good.json"
    good.write_text("{}")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    env.organize.note_path = lambda sid: {"good": good, "bad": bad}.get(sid)
    for sid in ("good", "bad", "missing"):
        env.w.add_batch(sid)
    env.w.finalize_batch()
    assert [c for c in env.vault if c[0] == "links"] == [("links", ["good"])]
    assert env.w.batch() == []


# --- setup -----------------------------------------------------------------------------------

def test_installing_the_worker_retires_periodic(tmp_path, monkeypatch):
    monkeypatch.setattr(macsetup, "AGENTS_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(macsetup, "_run", lambda cmd: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    (tmp_path / f"{macsetup._label('periodic')}.plist").write_text("old")
    assert macsetup.AGENTS == ("receiver", "worker")
    macsetup.install_agent("worker")
    assert not (tmp_path / f"{macsetup._label('periodic')}.plist").exists()
    assert any(c[:2] == ["launchctl", "bootout"] and c[2].endswith(".periodic") for c in calls)
    assert (tmp_path / f"{macsetup._label('worker')}.plist").exists()
    assert (macsetup.MAC_SRC / "launchd" / "periodic.plist").exists()  # template kept for a rollback


def test_installing_the_receiver_leaves_periodic_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(macsetup, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(macsetup, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""))
    p = tmp_path / f"{macsetup._label('periodic')}.plist"
    p.write_text("old")
    macsetup.install_agent("receiver")
    assert p.exists()


# --- omy organize --since ---------------------------------------------------------------------

def test_organize_since_runs_now_but_skips_up_to_date_notes(env, monkeypatch):  # noqa: F811
    from omy import sessions
    sess = types.SimpleNamespace(id="o1", closed=lambda: True)
    monkeypatch.setattr(sessions, "recorded_days", lambda: [datetime(2026, 11, 21).date()])
    monkeypatch.setattr(sessions, "sessions_for_day", lambda day: [sess])
    monkeypatch.setattr(transcribe, "_load_doc", lambda s: {})
    monkeypatch.setattr(worker, "in_window", lambda *a, **k: False)  # daytime
    monkeypatch.setattr(worker, "on_ac", lambda: False)              # on battery
    env.organize.needs_organize = lambda sid: False
    assert cli.organize(datetime(2026, 11, 1).date())["queued"] == 1
    with closing(jobs._connect()) as db:
        row = db.execute("SELECT force, ignore_window FROM jobs").fetchone()
    assert (row["force"], row["ignore_window"]) == (0, 1)
    assert env.w.step()
    assert env.organized == [] and ("ensure", "organizer") not in env.fm.calls  # up to date: skipped
    cli.organize(datetime(2026, 11, 1).date(), force=True)
    assert env.w.step()
    assert env.organized == [("o1", "http://fake/organizer")]


def test_model_path_respects_config_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "config_value", lambda k, d: str(tmp_path / "x.gguf") if k == "organizer_model" else d)
    assert models.model_path("organizer") == tmp_path / "x.gguf"
    assert models.model_path("chat") == models.MODELS["chat"]["path"]
