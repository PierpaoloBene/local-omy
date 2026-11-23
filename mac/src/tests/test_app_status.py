"""What the Mac app reads: omy status (worker), omy brain status, omy index --all."""

import json
import sys
import time

import pytest

from omy import cli, jobs, macsetup, status, worker
from omy.brain import NOTES


@pytest.fixture(autouse=True)
def sandbox(archive, monkeypatch):
    monkeypatch.setattr(macsetup, "agent_info",
                        lambda name: {"loaded": True, "running": True, "pid": 1, "last_exit": None, "up_to_date": True})
    monkeypatch.setattr(worker, "on_ac", lambda: True)
    yield


def test_status_reports_the_worker_not_periodic(monkeypatch, archive):
    worker.STATUS.parent.mkdir(parents=True, exist_ok=True)
    worker.STATUS.write_text(json.dumps({
        "state": "running", "heartbeat": time.time(), "loaded": "chat",
        "job": {"id": 1, "kind": "organize", "key": "s1", "started": 5.0},
        "queue": {"queued": 2, "by_kind": {"index": {"queued": 2}}, "running": [],
                  "failed": [{"id": 3, "kind": "organize", "key": "s0", "error": "boom", "finished": 1.0}]}}))
    w = status._worker()
    assert w["running"] and w["state"] == "running" and w["loaded_model"] == "chat"
    assert w["job"]["kind"] == "organize" and w["queued"] == 2 and w["failed"][0]["error"] == "boom"
    assert w["heartbeat_age"] < 5
    assert set(status.LOGS) == {"receiver", "worker", "models"}


def test_periodic_key_only_if_its_plist_exists(monkeypatch, archive):
    monkeypatch.setattr(status.battery, "battery", lambda: {})
    monkeypatch.setattr(status.battery, "firmware", lambda: {})
    monkeypatch.setattr(status.iphone, "status", lambda: {})
    monkeypatch.setattr(status.macsetup, "tailscale_info", lambda: {})
    monkeypatch.setattr(status, "_archive", lambda: {})
    monkeypatch.setattr(status, "_upload", lambda: {})
    monkeypatch.setattr(status, "_transcripts", lambda: {})
    monkeypatch.setattr(macsetup, "_plist_path", lambda n: archive / f"{n}.plist")
    assert "periodic" not in status.status() and "worker" in status.status()
    (archive / "periodic.plist").write_text("x")
    assert "periodic" in status.status()


def test_worker_without_status_file(archive):
    w = status._worker()
    assert w["state"] is None and w["queued"] == 0 and w["failed"] == [] and w["heartbeat_age"] is None


def test_brain_status_empty(archive):
    b = status.brain_status()
    assert b["notes"] == 0 and b["last_note"] is None
    assert b["index"] == {"chunks": 0, "version": 0}
    assert b["vault"].endswith("brain") and b["on_ac"] is True


def test_brain_status_counts_notes(archive):
    for day, sid, title in (("01", "a", "Prima"), ("02", "b", "Seconda")):
        d = NOTES / "2026" / "10" / day
        d.mkdir(parents=True)
        (d / f"0800-x-{sid}.json").write_text(json.dumps({"id": sid, "titolo": title, "data": f"2026-10-{day}"}))
    b = status.brain_status()
    assert b["notes"] == 2
    assert b["last_note"] == {"id": "b", "titolo": "Seconda", "data": "2026-10-02"}


def test_index_all_enqueues_and_wakes(archive, monkeypatch, capsys):
    woke = []
    monkeypatch.setattr(jobs, "wake", lambda: woke.append(1))
    monkeypatch.setattr(sys, "argv", ["omy", "--json", "index", "--all"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"queued": True}
    assert jobs.summary()["by_kind"]["index"]["queued"] == 1 and woke


def test_brain_status_cli(archive, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["omy", "--json", "brain", "status"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["notes"] == 0
