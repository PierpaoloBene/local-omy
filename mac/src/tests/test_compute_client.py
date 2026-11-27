"""The Mac's compute worker against the hub's compute API (in process). The models are not run: what the
tests replace is exec_*, the part that calls whisper, pyannote and llama-server; the staging, the
leases, the heartbeat, the upload and the completion are the real code."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from helpers import write_segments
from test_compute import COMPUTE, TOKEN, caps, sha, transcript

from omy import compute, jobs, models, worker


class TestHub(compute.Hub):
    __test__ = False

    def __init__(self, client):
        super().__init__("http://hub.test", COMPUTE)
        self.c = client
        self.calls = []

    def request(self, method, path, *, body=None, data=None, headers=None, timeout=60.0):
        self.calls.append((method, path))
        r = self.c.request(method, path, json=body, content=data, headers=self._headers(headers))
        return compute.Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content)

    def download(self, path, dest, headers, start=0):
        h = self._headers(headers)
        if start:
            h["Range"] = f"bytes={start}-"
            self.calls.append(("RANGE", f"{start}"))
        r = self.c.get(path, headers=h)
        if r.status_code in (200, 206):
            with dest.open("ab" if r.status_code == 206 else "wb") as f:
                f.write(r.content)
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}


@pytest.fixture
def rig(archive, tmp_path, monkeypatch):
    from omy import hubstate
    from omy_receiver.app import Config, create_app
    hubstate.PATH.unlink(missing_ok=True)
    monkeypatch.setattr(worker, "on_ac", lambda: True)
    monkeypatch.setattr(worker, "in_window", lambda *a, **k: True)
    monkeypatch.setattr(models, "chat_active", lambda now=None: False)
    monkeypatch.setattr(models, "state", lambda: {"loaded": None, "companions": []})
    with TestClient(create_app(Config(token=TOKEN.encode(), archive=archive, compute_token=COMPUTE.encode()))) as c:
        hub = TestHub(c)
        w = compute.ComputeWorker(hub, workspace=tmp_path / "ws", context_dir=tmp_path / "ctx")
        monkeypatch.setattr(w, "capabilities", lambda: caps())
        monkeypatch.setattr(compute, "STATUS_EVERY", 0)
        w.check_workspace()
        yield w, hub, archive


def fake_transcribe(w, ids):
    def run(job, lease):
        for f in lease["inputs"]:
            assert (w.ws / f["path"]).is_file()
        out = w.ws / lease["outputs"][0]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(transcript(ids)))
        return [out], {}
    return run


def test_a_job_end_to_end(rig, monkeypatch):
    w, hub, archive = rig
    ids = write_segments(archive)
    j = jobs.enqueue("transcribe", ids[0])
    monkeypatch.setattr(w, "exec_transcribe", fake_transcribe(w, ids))
    assert w.step(wait=0, load_models=False) is True
    assert jobs.state_of(j) == "done"
    assert (archive / "transcripts" / "2026-10-29" / f"{ids[0]}.json").is_file()
    assert (archive / "transcripts" / "2026-10-29.md").is_file()
    assert not (w.ws / "raw").exists() and not (w.ws / "transcripts").exists()      # the workspace is wiped
    assert ("POST", "/compute/v1/status") in hub.calls
    assert w.step(wait=0, load_models=False) is False                                # nothing more


def test_the_audio_arrives_intact_and_resumes_from_a_partial_file(rig, monkeypatch):
    w, hub, archive = rig
    ids = write_segments(archive, n=1)
    jobs.enqueue("transcribe", ids[0])
    seen = {}

    def run(job, lease):
        for f in lease["inputs"]:
            seen[f["path"]] = (w.ws / f["path"]).read_bytes()
        raise models.Preempted("stop here")
    monkeypatch.setattr(w, "exec_transcribe", run)
    # a partial download left by a crash
    full = (archive / "raw" / "audio" / "2026-10-29" / f"{ids[0]}.bin").read_bytes()
    part = w.ws / "raw" / "audio" / "2026-10-29" / f"{ids[0]}.bin.part"
    monkeypatch.setattr(w, "wipe", lambda: None)
    part.parent.mkdir(parents=True)
    part.write_bytes(full[:100])
    w.step(wait=0, load_models=False)
    assert seen[f"raw/audio/2026-10-29/{ids[0]}.bin"] == full
    assert ("RANGE", "100") in hub.calls                      # only the missing part was asked for


def test_a_corrupt_download_is_rejected(rig, monkeypatch):
    w, hub, archive = rig
    ids = write_segments(archive, n=1)
    j = jobs.enqueue("transcribe", ids[0])
    orig = hub.download

    def bad(path, dest, headers, start=0):
        status, h = orig(path, dest, headers, start)
        if path.endswith(".bin"):
            with dest.open("ab") as f:
                f.write(b"x")                     # a byte too many
        return status, h
    hub.download = bad
    monkeypatch.setattr(w, "exec_transcribe", lambda *a: pytest.fail("ran on corrupt input"))
    w.step(wait=0, load_models=False)
    from contextlib import closing
    assert jobs.state_of(j) == "queued"
    with closing(jobs._connect()) as db:
        assert "hash never matched" in db.execute("SELECT error FROM jobs WHERE id=?", (j,)).fetchone()[0]


def test_preempted_job_goes_back_without_using_an_attempt(rig, monkeypatch):
    w, hub, archive = rig
    ids = write_segments(archive)
    j = jobs.enqueue("diarize", ids[0])
    (archive / "transcripts" / "2026-10-29").mkdir(parents=True)
    (archive / "transcripts" / "2026-10-29" / f"{ids[0]}.json").write_text(json.dumps(transcript(ids)))

    def run(job, lease):
        w.preempted = True                   # a chat question arrived (guard_once)
        raise RuntimeError("terminated")
    monkeypatch.setattr(w, "exec_diarize", run)
    w.step(wait=0, load_models=False)
    assert jobs.state_of(j) == "queued" and jobs.get(j).attempts == 0


def test_a_failure_is_reported_with_a_retry_time(rig, monkeypatch):
    w, hub, archive = rig
    ids = write_segments(archive)
    j = jobs.enqueue("transcribe", ids[0])
    monkeypatch.setattr(w, "exec_transcribe", lambda *a: (_ for _ in ()).throw(RuntimeError("whisper exploded")))
    w.step(wait=0, load_models=False)
    assert jobs.state_of(j) == "queued" and jobs.get(j).attempts == 1
    assert w.last_error and "whisper exploded" in w.last_error
    assert w.step(wait=0, load_models=False) is False       # waits for its retry time (300 s)


def test_a_lost_lease_discards_the_work(rig, monkeypatch):
    w, hub, archive = rig
    ids = write_segments(archive)
    j = jobs.enqueue("transcribe", ids[0])
    run = fake_transcribe(w, ids)

    def slow(job, lease):
        out = run(job, lease)
        jobs.reap(now=9e12)                                 # the Mac slept: the lease expired...
        jobs.next_job(["transcribe"], lease_for="other")    # ...and another worker took the job
        return out
    monkeypatch.setattr(w, "exec_transcribe", slow)
    w.step(wait=0, load_models=False)
    assert jobs.state_of(j) == "leased"                    # still the other worker's
    assert not (archive / "transcripts" / "2026-10-29" / f"{ids[0]}.json").exists()


def test_heartbeat_keeps_the_lease(rig, monkeypatch):
    w, hub, archive = rig
    ids = write_segments(archive)
    jobs.enqueue("transcribe", ids[0])
    monkeypatch.setattr(compute, "HEARTBEAT_EVERY", 0.05)
    run = fake_transcribe(w, ids)

    def slow(job, lease):
        time.sleep(0.4)
        return run(job, lease)
    monkeypatch.setattr(w, "exec_transcribe", slow)
    w.step(wait=0, load_models=False)
    assert sum(1 for m, p in hub.calls if p.endswith("/heartbeat")) >= 3


def test_dry_run_reports_and_gives_the_job_back(rig, capsys):
    w, hub, archive = rig
    ids = write_segments(archive)
    j = jobs.enqueue("transcribe", ids[0])
    assert w.step(wait=0, dry_run=True, load_models=False) is True
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "transcribe" and out["files"] == 6 and out["bytes"] > 0
    assert jobs.state_of(j) == "queued" and jobs.get(j).attempts == 0
    assert not (w.ws / "raw").exists()                       # nothing downloaded


def test_only_limits_the_kinds(rig):
    w, hub, archive = rig
    ids = write_segments(archive)
    jobs.enqueue("transcribe", ids[0])
    assert w.step(wait=0, only=["embed"], load_models=False) is False
    assert jobs.pending(["transcribe"]) == 1


def test_a_hub_that_is_down_does_not_stop_the_worker(rig, monkeypatch):
    w, hub, archive = rig

    def down(*a, **k):
        raise compute.HubDown("no route to host")
    hub.request = down
    monkeypatch.setattr(compute, "RETRY_START", 0.0)
    monkeypatch.setattr(compute, "RETRY_MAX", 0.0)
    assert w.step(wait=0, load_models=False) is False
    assert w.hub_down_since is not None


def test_a_workspace_that_looks_like_an_archive_is_never_wiped(tmp_path):
    (tmp_path / "raw" / "audio").mkdir(parents=True)
    (tmp_path / "raw" / "audio" / "precious.bin").write_bytes(b"x")
    w = compute.ComputeWorker(compute.Hub("http://x", "t"), workspace=tmp_path)
    with pytest.raises(SystemExit):
        w.check_workspace()
    assert (tmp_path / "raw" / "audio" / "precious.bin").exists()


def test_context_is_uploaded_before_diarize(rig, tmp_path):
    w, hub, archive = rig
    (tmp_path / "ctx").mkdir()
    (tmp_path / "ctx" / "audio-2026-10-29.jsonl").write_text('{"t":1}\n')
    w.sync_context()
    assert (archive / "context" / "audio-2026-10-29.jsonl").read_text() == '{"t":1}\n'


def test_embed_job_end_to_end(rig, monkeypatch):
    import numpy as np
    from contextlib import closing
    import sqlite3
    from omy.brain import embed, index
    w, hub, archive = rig
    with closing(index._connect()) as db, db:
        index._insert(db, "s1", [{"kind": "testo", "day": "2026-10-29", "start": "x", "t": 0.0, "row_id": "r", "persons": [],
                                  "title": "t", "field": "f", "text": f"chunk {i}"} for i in range(40)], "h", {})
    j = jobs.enqueue("embed", "all")
    monkeypatch.setattr(embed, "embed", lambda texts, query=False: np.ones((len(texts), embed.EMBED_DIM), dtype=np.float32))
    assert w.step(wait=0, load_models=False) is True
    with closing(sqlite3.connect(index.INDEX_PATH)) as db:
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 40
    assert jobs.state_of(j) == "done"
