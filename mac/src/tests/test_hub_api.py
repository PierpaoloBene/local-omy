"""The hub's own endpoints: the widget, status, export for other apps, transcribe-now with mac_online."""

import json
import time
from contextlib import closing
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from helpers import record, write_day, write_segments
from test_compute import COMPUTE, MAC, PHONE, TOKEN, caps

WIDGET = "w" * 40


@pytest.fixture
def client(archive, monkeypatch):
    from omy import hubstate
    from omy_receiver.app import Config, create_app
    from omy import hubstats
    monkeypatch.setenv("OMY_ROLE", "hub")
    hubstate.PATH.unlink(missing_ok=True)
    hubstats._cache.clear()
    with TestClient(create_app(Config(token=TOKEN.encode(), archive=archive, compute_token=COMPUTE.encode(),
                                      widget_token=WIDGET.encode()))) as c:
        yield c


def test_the_widget_token_reads_only_the_widget(client, archive):
    w = {"Authorization": f"Bearer {WIDGET}"}
    assert client.get("/v1/widget").status_code == 401
    assert client.get("/v1/widget", headers=w).status_code == 200
    assert client.get("/v1/widget", headers=PHONE).status_code == 200
    assert client.get("/v1/people", headers=w).status_code == 401
    assert client.get("/v1/status", headers=w).status_code == 401
    assert client.post("/compute/v1/lease", headers={"Authorization": f"Bearer {WIDGET}", "X-Omy-Compute-Protocol": "1"},
                       json={}).status_code == 401


def test_the_widget_token_can_retry_failed_jobs_and_nothing_else(client, archive):
    from omy import jobs
    j = jobs.enqueue("transcribe", "x")
    jobs.fail(j, "boom", permanent=True)
    assert jobs.state_of(j) == "failed"
    w = {"Authorization": f"Bearer {WIDGET}"}
    assert client.post("/v1/jobs/retry", json={}).status_code == 401
    assert client.post("/v1/jobs/retry", json={"id": "x"}, headers=w).status_code == 400
    assert client.post("/v1/jobs/retry", json={"id": j + 5}, headers=w).json() == {"retried": 0}
    assert client.post("/v1/jobs/retry", json={"id": j}, headers=w).json() == {"retried": 1}
    assert jobs.state_of(j) == "queued"
    assert client.post("/v1/jobs/retry", json={}, headers=w).json() == {"retried": 0}


def test_widget_empty_is_honest(client):
    d = client.get("/v1/widget", headers=PHONE).json()
    assert d["today"]["segments"] == 0 and d["queue"]["queued"] == 0 and d["queue"]["by_kind"] == {}
    assert d["mac"]["online"] is False and d["mac"]["last_seen_s"] is None
    assert d["pendant"]["battery"] is None and d["asks"]["waiting"] == 0
    assert d["iphone_upload"]["last_upload_at"] is None


def test_widget_with_work_and_a_mac(client, archive):
    from datetime import datetime as dt, timedelta, timezone
    from omy import asks, hubstate, jobs
    start = (dt.now(timezone.utc) - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S")
    ids = write_segments(archive, start=start, n=2, tag="0000cafe")
    meta = archive / "raw" / "audio"
    for p in meta.rglob("*.json"):
        d = json.loads(p.read_text())
        d["battery"], d["firmware"] = 74, "3.0.21"
        p.write_text(json.dumps(d))
    jobs.enqueue("transcribe", "a")
    jobs.enqueue("diarize", "b")
    jobs.next_job(["diarize"], lease_for="mac")
    asks.create("Cosa ho deciso per la cucina?", [], None)
    hubstate.record("mac", "b", caps(), {"state": "running", "job": {"kind": "diarize", "key": "b"}})
    d = client.get("/v1/widget", headers=PHONE).json()
    assert d["today"]["segments"] == 2 and d["today"]["minutes"] == 10.0 and d["today"]["last_end"]
    assert d["queue"]["by_kind"] == {"transcribe": {"queued": 1, "active": 0}, "diarize": {"queued": 0, "active": 1}}
    assert d["queue"]["queued"] == 1 and d["queue"]["active"] == 1 and d["queue"]["oldest_age_min"] is not None
    assert d["mac"]["online"] and d["mac"]["version_ok"] and d["mac"]["last_seen_s"] < 5
    assert d["pendant"]["battery"] == 74 and d["pendant"]["firmware"] == "3.0.21"
    assert d["asks"]["waiting"] == 1 and d["asks"]["items"][0]["question"].startswith("Cosa ho deciso")
    assert d["disk"]["free_gb"] > 0


def test_a_mac_with_other_versions_is_flagged(client):
    from omy import hubstate
    hubstate.record("mac", "b", caps(versions={"prompt": 2}))
    m = client.get("/v1/widget", headers=PHONE).json()["mac"]
    assert m["version_ok"] is False and "prompt" in m["version_problems"]["organize"]


def test_organize_jobs_that_waited_through_the_night_are_marked(archive):
    from omy import hubstats, jobs
    from omy.sessions import LOCAL_TZ
    jobs.enqueue("organize", "o1")
    with closing(jobs._connect()) as db:
        db.execute("UPDATE jobs SET created=? WHERE key='o1'",
                   (datetime(2026, 11, 27, 2, 0, tzinfo=LOCAL_TZ).timestamp(),))
    jobs.enqueue("organize", "o2")                       # queued this morning, after the window: not missed
    with closing(jobs._connect()) as db:
        db.execute("UPDATE jobs SET created=? WHERE key='o2'",
                   (datetime(2026, 11, 27, 8, 30, tzinfo=LOCAL_TZ).timestamp(),))
    assert hubstats.queue(datetime(2026, 11, 27, 6, 59, tzinfo=LOCAL_TZ))["organize_missed"] == 0   # still night
    assert hubstats.queue(datetime(2026, 11, 27, 7, 1, tzinfo=LOCAL_TZ))["organize_missed"] == 1
    # o2 was queued at 08:30: it had no window yet, it is not "missed" until the next morning
    assert hubstats.queue(datetime(2026, 11, 27, 9, 0, tzinfo=LOCAL_TZ))["organize_missed"] == 1


def test_status_and_battery(client, archive):
    write_segments(archive, n=1)
    s = client.get("/v1/status", headers=PHONE).json()
    assert s["archive"]["today_segments"] >= 0 and "mac" in s and s["last_segment_start"] == "2026-10-29T08:00:00Z"
    assert client.get("/v1/battery", headers=PHONE).json() == {"level": None}


def test_transcribe_now_wakes_the_scheduler_and_says_if_the_mac_is_there(client, archive):
    from omy import hubstate, jobs
    r = client.post("/v1/transcribe", headers=PHONE).json()
    assert r["mac_online"] is False and r["started"] is True
    hubstate.record("mac", "b", caps())
    jobs.enqueue("transcribe", "x")
    r = client.post("/v1/transcribe", headers=PHONE).json()
    assert r["mac_online"] is True and r["running"] is True and r["started"] is False


def test_brain_status_on_the_hub_comes_from_the_macs_report(client):
    from omy import hubstate
    hubstate.record("mac", "b", caps(), {"loaded": "chat"})
    client.post("/compute/v1/status", headers=MAC, json={"state": {"state": "idle", "loaded": "chat"}})
    d = client.get("/v1/brain/status", headers=PHONE).json()
    assert d["worker"]["state"] == "idle" and d["models"]["loaded"] == "chat" and d["mac_online"] is True


def test_export_for_other_apps(client, archive):
    write_day(archive, "2026-10-29", [record("20261029T080000Z-0000abcd", "2026-10-29", "10:00", "Io", "me", "ciao")])
    days = client.get("/v1/export/days", headers=PHONE).json()["days"]
    assert [d["day"] for d in days] == ["2026-10-29"]
    assert client.get(f"/v1/export/days?since={days[0]['mtime'] + 1}", headers=PHONE).json()["days"] == []
    r = client.get("/v1/export/2026-10-29.jsonl", headers=PHONE)
    assert r.status_code == 200 and json.loads(r.text.splitlines()[0])["text"] == "ciao"
    again = client.get("/v1/export/2026-10-29.jsonl", headers={**PHONE, "If-Modified-Since": r.headers["last-modified"]})
    assert again.status_code == 304
    assert client.get("/v1/export/2026-11-01.jsonl", headers=PHONE).status_code == 404
    assert client.get("/v1/export/nope.jsonl", headers=PHONE).status_code == 400
    assert client.get("/v1/export/days").status_code == 401


def test_subprocesses_do_not_get_the_tokens(archive, monkeypatch):
    """omy_env: the receiver runs `omy ...` for the phone; the tokens never reach those processes."""
    import omy_receiver.app as app
    monkeypatch.setenv("OMY_TOKEN", TOKEN)
    monkeypatch.setenv("OMY_COMPUTE_TOKEN", COMPUTE)
    monkeypatch.setenv("OMY_WIDGET_TOKEN", WIDGET)
    seen = {}
    import asyncio
    real = asyncio.create_subprocess_exec

    async def spy(*a, **k):
        seen.update(k["env"])
        return await real(*a, **k)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    c = TestClient(app.create_app(app.Config(token=TOKEN.encode(), archive=archive)))
    assert c.get("/v1/people", headers=PHONE).status_code == 200
    assert seen and not any(k.startswith("OMY_") and "TOKEN" in k for k in seen)
