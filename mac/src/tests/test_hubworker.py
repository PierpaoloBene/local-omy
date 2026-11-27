"""The hub's scheduler: scans, reaps, runs the jobs that need no model, queues the embedding for the Mac."""

import json
import sqlite3
from contextlib import closing

import pytest

from helpers import record, write_day, write_segments
from omy import jobs


@pytest.fixture
def hub(archive, monkeypatch):
    from omy import hubstate
    monkeypatch.setenv("OMY_ROLE", "hub")
    hubstate.PATH.unlink(missing_ok=True)
    from omy.hubworker import HubWorker
    return HubWorker()


def doc(ids):
    from omy import transcribe
    return {"version": 1, "session": ids[0], "device_id": "p", "start": "2026-10-29T08:00:00.000Z",
            "end": "2026-10-29T08:15:00.000Z", "segments": ids, "pipeline": transcribe.PIPELINE, "speech": []}


def test_scan_queues_what_the_mac_will_take(hub, archive):
    from omy import jobs
    ids = write_segments(archive)
    out = hub.scan()
    assert out["transcribe"] == 1 and jobs.queued_by_kind() == {"transcribe": 1}
    d = archive / "transcripts" / "2026-10-29"
    d.mkdir(parents=True)
    (d / f"{ids[0]}.json").write_text(json.dumps(doc(ids)))
    jobs.finish(jobs.next_job(["transcribe"]).id)
    hub.scan()
    assert "diarize" in jobs.queued_by_kind()          # the Mac has not said it cannot: the job waits for it


def test_diarize_waits_for_nobody_when_the_mac_cannot(hub, archive):
    from omy import hubstate, jobs
    ids = write_segments(archive)
    d = archive / "transcripts" / "2026-10-29"
    d.mkdir(parents=True)
    (d / f"{ids[0]}.json").write_text(json.dumps(doc(ids)))
    hubstate.record("mac", "b", {"kinds": ["transcribe"], "versions": hubstate.versions()})
    hub.scan()
    assert "diarize" not in jobs.queued_by_kind()      # no pyannote on the Mac: no point in queueing


def test_step_runs_index_without_vectors_and_queues_embed(hub, archive):
    from omy import jobs
    from omy.brain import index
    sid = "20261029T080000Z-0000abcd"
    write_day(archive, "2026-10-29", [record(sid, "2026-10-29", "10:00", "Io", "me", "Parliamo del progetto casa nuova " * 4)])
    jobs.enqueue("index", "all")
    assert hub.step() is True
    with closing(sqlite3.connect(index.INDEX_PATH)) as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    assert jobs.queued_by_kind() == {"embed": 1}
    assert index.search("progetto casa")[0]["lexical"]  # BM25 works without the Mac


def test_step_reaps_and_never_runs_the_macs_jobs(hub, archive):
    from omy import jobs
    j = jobs.enqueue("transcribe", "s")
    jobs.next_job(["transcribe"], lease_for="mac", now=1.0)
    assert hub.step() is False
    assert jobs.state_of(j) == "queued"                # the lease was long expired
    assert hub.step() is False and jobs.state_of(j) == "queued"   # and the hub does not run it itself


def test_step_drops_questions_nobody_answered(hub, archive):
    import time
    from omy import asks
    old = asks.create("una domanda vecchia", [], None)
    from contextlib import closing
    with closing(jobs._connect()) as db:
        db.execute("UPDATE asks SET created=? WHERE id=?", (time.time() - asks.EXPIRE_S - 10, old))
    hub.step()
    assert asks.get(old)["state"] == "expired"


def test_vault_batch_waits_for_organize_only_while_the_mac_is_there(hub, archive, monkeypatch):
    from omy import hubstate, jobs, worker
    calls = []
    from omy.brain import vault
    monkeypatch.setattr(vault, "update_links", lambda b: calls.append(("links", b)))
    monkeypatch.setattr(vault, "render_entities", lambda b: calls.append(("entities", b)))
    monkeypatch.setattr(vault, "render_indexes", lambda: calls.append(("indexes",)))
    monkeypatch.setattr(hub, "_drop_unreadable", lambda b: b)
    worker.add_to_batch("s1")
    jobs.enqueue("organize", "s2")
    hubstate.record("mac", "b", {"kinds": ["organize"]})   # the Mac is there and will run s2: wait
    hub.finalize_batch()
    assert calls == []
    monkeypatch.setattr(hubstate, "online", lambda *a, **k: False)   # asleep: do not hold the vault back
    hub.finalize_batch()
    assert calls[0] == ("links", ["s1"]) and not worker.BATCH.exists()
