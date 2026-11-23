"""The llama-server supervisor, against a fake llama-server (a tiny HTTP server
started as a real subprocess): never the real binary or a real model."""

import os
import socket
import stat
import sys
import textwrap
import time

import pytest

from omy import jobs, models

FAKE = textwrap.dedent('''
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        def log_message(self, *a): pass
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake(tmp_path, monkeypatch, archive):
    script = tmp_path / "fake_llama.py"
    script.write_text(FAKE)
    binary = tmp_path / "llama-server"
    binary.write_text(f'#!/bin/sh\nexec {sys.executable} {script} "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("OMY_LLAMA_SERVER", str(binary))
    specs = {}
    for role in ("organizer", "chat"):
        gguf = tmp_path / f"{role}.gguf"
        gguf.write_text("x")
        specs[role] = {"path": gguf, "port": free_port(), "args": ["-c", "128"]}
    monkeypatch.setattr(models, "MODELS", specs)
    monkeypatch.setattr(models, "LOG", tmp_path / "models.log")
    monkeypatch.setattr(models, "HEALTH_TIMEOUT", 20.0)
    yield specs
    models.release_all()
    models._proc = None


def test_default_models_per_spec():
    org, chat = models.MODELS["organizer"], models.MODELS["chat"]
    assert org["port"] == 8090 and chat["port"] == 8091
    assert org["path"].name == "Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf"
    assert chat["path"].name == "Qwen3.5-4B-Q4_0.gguf"
    assert "--spec-type draft-mtp --spec-draft-n-max 4" in " ".join(org["args"])
    assert org["args"][:4] == ["-ngl", "all", "-fa", "on"] and "16384" in org["args"]
    assert "8192" in chat["args"] and "--cache-ram" not in chat["args"]


def test_config_overrides_paths(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('chat_model = "/x/other.gguf"\nchat_idle_min = 2\n')
    monkeypatch.setattr(models, "CONFIG_FILE", cfg)
    assert str(models.model_path("chat")) == "/x/other.gguf"
    assert models.model_path("organizer").name.startswith("Qwen3.8")
    assert models.chat_idle() == 120


def test_ensure_starts_swaps_and_releases(fake):
    assert models.state()["loaded"] is None
    base = models.ensure("organizer")
    assert base == f"http://127.0.0.1:{fake['organizer']['port']}"
    st = models.state()
    assert st["loaded"] == "organizer" and st["port"] == fake["organizer"]["port"]
    pid = st["pid"]
    assert models.ensure("organizer") == base and models.state()["pid"] == pid  # already loaded
    models.ensure("chat")  # one at a time: the organizer goes
    st = models.state()
    assert st["loaded"] == "chat" and st["pid"] != pid
    time.sleep(0.1)
    with pytest.raises(OSError):
        os.kill(pid, 0)
    models.release("organizer")  # not loaded: nothing happens
    assert models.state()["loaded"] == "chat"
    models.release("chat")
    assert models.state()["loaded"] is None


def test_ensure_without_model_file(fake):
    fake["chat"]["path"].unlink()
    assert not models.available("chat")
    with pytest.raises(models.ModelError, match="not found"):
        models.ensure("chat")


def test_ensure_fails_if_server_exits(fake, monkeypatch, tmp_path):
    bad = tmp_path / "bad"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(0o755)
    monkeypatch.setenv("OMY_LLAMA_SERVER", str(bad))
    with pytest.raises(models.ModelError, match="exited with code 3"):
        models.ensure("chat")
    assert models.state()["loaded"] is None


def test_state_ignores_dead_pid(fake):
    models._write_state({"role": "chat", "pid": 2 ** 22 + 12345, "port": 1})
    assert models.state()["loaded"] is None


def test_kill_orphans(fake, monkeypatch):
    import subprocess
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    port = fake["chat"]["port"]
    monkeypatch.setattr(models, "_listening_pids", lambda p: [orphan.pid] if p == port else [])
    monkeypatch.setattr(models, "_is_llama", lambda pid: True)
    monkeypatch.setattr(models, "STOP_WAIT", 0.3)  # our own child stays a zombie until waited
    assert models.kill_orphans() == [orphan.pid]
    assert orphan.wait(5) is not None


def test_request_chat_waits_for_the_worker(fake, monkeypatch):
    import threading
    monkeypatch.setattr(models, "CHAT_POLL", 0.02)
    threading.Timer(0.3, lambda: models.ensure("chat")).start()
    t0 = time.time()
    base = models.request_chat()
    assert base.endswith(str(fake["chat"]["port"]))
    assert time.time() - t0 >= 0.25
    assert jobs.demand_at("chat") >= t0


def test_request_chat_times_out(fake, monkeypatch):
    monkeypatch.setattr(models, "CHAT_WAIT", 0.3)
    monkeypatch.setattr(models, "CHAT_POLL", 0.05)
    with pytest.raises(models.ModelError):
        models.request_chat()


def test_check_preempt(archive, monkeypatch):
    monkeypatch.setattr(models, "chat_idle", lambda: 300.0)
    models.mark_heavy_start(1000.0)
    models.check_preempt()  # no question at all
    jobs.touch_demand("chat", at=time.time() - 10)
    models.mark_heavy_start(time.time() - 100)
    with pytest.raises(models.Preempted):
        models.check_preempt()
    models.mark_heavy_start(time.time())  # the job started after the question
    models.check_preempt()
    jobs.touch_demand("chat", at=time.time() - 1000)  # an old question
    models.mark_heavy_start(1000.0)
    models.check_preempt()
    assert not models.chat_active()
    jobs.touch_demand("chat")
    assert models.chat_active()
