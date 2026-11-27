"""The compute API: the Mac's compute worker pulls jobs from the hub (docs/hub-migration.md, 2)."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from helpers import record, write_day, write_segments

TOKEN, COMPUTE = "t" * 40, "c" * 40
PHONE = {"Authorization": f"Bearer {TOKEN}"}
MAC = {"Authorization": f"Bearer {COMPUTE}", "X-Omy-Compute-Protocol": "1"}


def caps(**over):
    from omy import hubstate
    v = hubstate.versions()
    v.update(over.pop("versions", {}))
    return {"protocol": 1, "kinds": ["transcribe", "diarize", "organize", "embed"], "versions": v,
            "organizer_model": "test-27b", **over}


def lease_body(**over):
    return {"worker_id": "mac", "boot_id": "b1", "kinds": ["transcribe", "diarize", "organize", "embed"],
            "forced_kinds": [], "wait_s": 0, "capabilities": caps(), "state": {"state": "idle"}, **over}


@pytest.fixture
def client(archive):
    from omy import hubstate
    from omy_receiver.app import Config, create_app
    hubstate.PATH.unlink(missing_ok=True)
    with TestClient(create_app(Config(token=TOKEN.encode(), archive=archive, compute_token=COMPUTE.encode()))) as c:
        yield c


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def old_end(archive, **kw):
    """Segments long enough ago for the session to be closed."""
    return write_segments(archive, start="2026-10-29T08:00:00", **kw)


def transcript(session_ids, pipeline=None, text="ciao a tutti"):
    from omy import transcribe
    return {"version": 1, "session": session_ids[0], "device_id": "pendant-1", "start": "2026-10-29T08:00:00.000Z",
            "end": "2026-10-29T08:15:00.000Z", "segments": session_ids, "model": transcribe.WHISPER_MODEL,
            "vad": transcribe.VAD_MODEL, "language": "auto", "pipeline": pipeline or transcribe.PIPELINE,
            "frames_decoded": 10, "frames_concealed": 0,
            "speech": [{"start": "2026-10-29T08:00:05.000Z", "end": "2026-10-29T08:00:09.000Z", "language": "it",
                        "text": text, "conf": 0.9, "words": []}]}


def put(client, job, lease, name, data: bytes):
    return client.put(f"/compute/v1/jobs/{job}/outputs/{name}", content=data,
                      headers={**MAC, "X-Lease-Id": lease, "X-Sha256": sha(data)})


def complete(client, job, lease, outputs, **extra):
    return client.post(f"/compute/v1/jobs/{job}/complete", headers=MAC,
                       json={"lease_id": lease, "outputs": outputs, "versions": caps()["versions"], **extra})


# --- who may call -----------------------------------------------------------------------------

def test_tokens_are_separate(client):
    body = lease_body()
    assert client.post("/compute/v1/lease", json=body).status_code == 401
    assert client.post("/compute/v1/lease", json=body, headers=PHONE).status_code == 401  # the phone's token
    assert client.post("/compute/v1/lease", json=body, headers={"Authorization": f"Bearer {COMPUTE}"}).status_code == 426
    assert client.post("/compute/v1/lease", json=body, headers=MAC).status_code == 204
    # and the Mac's token opens nothing of the phone's API
    assert client.get("/v1/people", headers={"Authorization": f"Bearer {COMPUTE}"}).status_code == 401


def test_without_a_compute_token_the_api_is_not_there(archive):
    from omy_receiver.app import Config, create_app
    c = TestClient(create_app(Config(token=TOKEN.encode(), archive=archive)))
    assert c.post("/compute/v1/lease", json=lease_body(), headers=MAC).status_code == 404


def test_config_rejects_equal_tokens(monkeypatch, tmp_path):
    from omy_receiver import app
    monkeypatch.setenv("OMY_TOKEN", TOKEN)
    monkeypatch.setenv("OMY_COMPUTE_TOKEN", TOKEN)
    with pytest.raises(app.ConfigError):
        app.load_config()
    monkeypatch.setenv("OMY_COMPUTE_TOKEN", "short")
    with pytest.raises(app.ConfigError):
        app.load_config()


# --- transcribe ---------------------------------------------------------------------------------

def test_transcribe_round_trip(client, archive):
    from omy import jobs
    ids = old_end(archive)
    jobs.enqueue("transcribe", ids[0])
    r = client.post("/compute/v1/lease", json=lease_body(), headers=MAC)
    assert r.status_code == 200
    lease = r.json()
    job, lid = lease["job"]["id"], lease["job"]["lease_id"]
    assert lease["job"]["kind"] == "transcribe" and lease["params"]["segments"] == ids
    paths = [i["path"] for i in lease["inputs"]]
    assert sum(p.endswith(".bin") for p in paths) == 3 and sum(p.endswith(".json") for p in paths) == 3
    assert lease["outputs"] == [f"transcripts/2026-10-29/{ids[0]}.json"]
    # the audio: whole, then a range, with the hash of the sidecar
    bin_path = next(p for p in paths if p.endswith(".bin"))
    full = client.get(f"/compute/v1/jobs/{job}/inputs/{bin_path}", headers={**MAC, "X-Lease-Id": lid})
    assert full.status_code == 200 and sha(full.content) == full.headers["x-sha256"]
    part = client.get(f"/compute/v1/jobs/{job}/inputs/{bin_path}", headers={**MAC, "X-Lease-Id": lid, "Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == full.content[10:20]
    # not without the lease, not other files
    assert client.get(f"/compute/v1/jobs/{job}/inputs/{bin_path}", headers=MAC).status_code == 409
    assert client.get(f"/compute/v1/jobs/{job}/inputs/voices/me.npy", headers={**MAC, "X-Lease-Id": lid}).status_code == 404
    assert client.get(f"/compute/v1/jobs/{job}/inputs/raw/audio/../../receiver.json",
                      headers={**MAC, "X-Lease-Id": lid}).status_code == 404

    doc = json.dumps(transcript(ids)).encode()
    name = lease["outputs"][0]
    assert put(client, job, lid, name, doc).status_code == 200
    assert put(client, job, lid, name, doc).status_code == 200          # idempotent
    assert put(client, job, lid, "transcripts/2026-10-29/other.json", doc).status_code == 400
    bad = client.put(f"/compute/v1/jobs/{job}/outputs/{name}", content=doc, headers={**MAC, "X-Lease-Id": lid, "X-Sha256": "0" * 64})
    assert bad.status_code == 400
    r = complete(client, job, lid, [{"name": name, "sha256": sha(doc)}])
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert complete(client, job, lid, [{"name": name, "sha256": sha(doc)}]).json()["status"] == "duplicate"
    assert (archive / name).read_text() == doc.decode()
    assert (archive / "transcripts" / "2026-10-29.md").is_file()          # the day is rendered on the hub
    assert jobs.state_of(job) == "done" and jobs.summary()["by_kind"]["index"]["queued"] == 1
    assert not list((archive / "tmp" / "compute").glob("*"))              # staging cleaned


def test_complete_rejects_what_is_not_the_job(client, archive):
    from omy import jobs
    ids = old_end(archive)
    jobs.enqueue("transcribe", ids[0])
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    job, lid, name = lease["job"]["id"], lease["job"]["lease_id"], lease["outputs"][0]
    old = json.dumps(transcript(ids, pipeline=4)).encode()
    put(client, job, lid, name, old)
    r = complete(client, job, lid, [{"name": name, "sha256": sha(old)}])
    assert r.status_code == 422 and "pipeline" in r.json()["detail"]
    assert not (archive / name).exists()
    # an output that was never uploaded
    assert complete(client, job, lid, [{"name": name, "sha256": "1" * 64}]).status_code in (400, 409)


def test_a_session_that_grew_keeps_the_work_done(client, archive):
    """The Mac transcribed 3 segments; a 4th arrived meanwhile: the transcript is a prefix, it is kept and the
    scan will transcribe only the new one (as with any prior)."""
    from omy import jobs, transcribe
    ids = old_end(archive)
    jobs.enqueue("transcribe", ids[0])
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    job, lid, name = lease["job"]["id"], lease["job"]["lease_id"], lease["outputs"][0]
    more = write_segments(archive, n=4)
    doc = json.dumps(transcript(ids)).encode()
    put(client, job, lid, name, doc)
    assert complete(client, job, lid, [{"name": name, "sha256": sha(doc)}]).status_code == 200
    todo, _ = transcribe.plan([transcribe.find_session(ids[0]).day])
    (session, prior), = todo
    assert prior is not None and [s.id for s in transcribe.remaining(session, prior)] == more[3:]
    # the next lease has the 3 done segments as empty audio and only the new one to download
    jobs.enqueue("transcribe", ids[0])
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    empties = [i["path"] for i in lease["inputs"] if i.get("empty")]
    assert len(empties) == 3 and any(i["path"] == f"transcripts/2026-10-29/{ids[0]}.json" for i in lease["inputs"])


def test_a_regrouped_session_is_stale(client, archive):
    from omy import jobs
    ids = old_end(archive)
    jobs.enqueue("transcribe", ids[0])
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    job, lid, name = lease["job"]["id"], lease["job"]["lease_id"], lease["outputs"][0]
    doc = json.dumps(transcript([ids[0], "20261029T080500Z-deadbeef"])).encode()
    put(client, job, lid, name, doc)
    r = complete(client, job, lid, [{"name": name, "sha256": sha(doc)}])
    assert r.status_code == 409 and r.json()["error"] == "stale"
    assert jobs.state_of(job) == "queued"  # planned again from what the hub has now


def test_lost_lease_and_reaper(client, archive):
    from omy import jobs
    ids = old_end(archive)
    j = jobs.enqueue("transcribe", ids[0])
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    lid = lease["job"]["lease_id"]
    hb = client.post(f"/compute/v1/jobs/{j}/heartbeat", json={"lease_id": lid}, headers=MAC)
    assert hb.status_code == 200 and hb.json()["cancel"] is False
    assert jobs.reap(now=9e12) == [j]                                  # the Mac went to sleep
    assert client.post(f"/compute/v1/jobs/{j}/heartbeat", json={"lease_id": lid}, headers=MAC).status_code == 409
    # a second Mac worker takes it; the first one's complete is stale
    lease2 = client.post("/compute/v1/lease", json=lease_body(worker_id="mac2", boot_id="x"), headers=MAC).json()
    doc = json.dumps(transcript(ids)).encode()
    r = complete(client, j, lid, [])
    assert r.status_code == 409 and lease2["job"]["lease_id"] != lid


def test_release_and_fail(client, archive):
    from omy import jobs
    ids = old_end(archive)
    j = jobs.enqueue("transcribe", ids[0])
    lid = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()["job"]["lease_id"]
    assert client.post(f"/compute/v1/jobs/{j}/release", json={"lease_id": lid, "reason": "preempted"}, headers=MAC).json() == {"released": True}
    assert jobs.get(j).attempts == 0
    lid = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()["job"]["lease_id"]
    r = client.post(f"/compute/v1/jobs/{j}/fail", json={"lease_id": lid, "error": "whisper crashed", "retry_after": 300}, headers=MAC)
    assert r.json() == {"retry": True} and jobs.state_of(j) == "queued"
    lid = client.post("/compute/v1/lease", json=lease_body(), headers=MAC)
    assert lid.status_code == 204  # waiting for its retry time


def test_a_restarted_mac_drops_its_old_leases(client, archive):
    from omy import jobs
    ids = old_end(archive)
    j = jobs.enqueue("transcribe", ids[0])
    first = client.post("/compute/v1/lease", json=lease_body(boot_id="b1"), headers=MAC).json()
    assert jobs.state_of(j) == "leased"
    second = client.post("/compute/v1/lease", json=lease_body(boot_id="b2"), headers=MAC).json()
    assert second["job"]["id"] == j and second["job"]["lease_id"] != first["job"]["lease_id"]


def test_a_mac_with_other_versions_gets_no_job(client, archive):
    from omy import hubstate, jobs
    ids = old_end(archive)
    j = jobs.enqueue("transcribe", ids[0])
    old = lease_body(capabilities=caps(versions={"pipeline": 4}))
    assert client.post("/compute/v1/lease", json=old, headers=MAC).status_code == 204
    assert jobs.state_of(j) == "queued"
    assert "pipeline" in hubstate.version_problems()["transcribe"]
    # a kind it does not do either
    none = lease_body(capabilities=caps(kinds=["embed"]))
    assert client.post("/compute/v1/lease", json=none, headers=MAC).status_code == 204
    assert client.post("/compute/v1/lease", json=lease_body(), headers=MAC).status_code == 200


def test_open_session_is_not_transcribed(client, archive):
    from datetime import datetime, timezone
    from omy import jobs
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    ids = write_segments(archive, start=now, n=1, tag="0000beef")
    j = jobs.enqueue("transcribe", ids[0])
    assert client.post("/compute/v1/lease", json=lease_body(), headers=MAC).status_code == 204
    assert jobs.state_of(j) == "done"  # nothing to do yet: the scan queues it when it closes


# --- diarize ------------------------------------------------------------------------------------

def test_diarize_round_trip(client, archive):
    import numpy as np
    from omy import diarize, jobs, transcribe
    ids = old_end(archive)
    session = transcribe.find_session(ids[0])
    (archive / "transcripts" / "2026-10-29").mkdir(parents=True)
    (archive / "transcripts" / "2026-10-29" / f"{ids[0]}.json").write_text(json.dumps(transcript(ids)))
    j = jobs.enqueue("diarize", ids[0])
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    assert lease["job"]["kind"] == "diarize" and len(lease["inputs"]) == 6
    lid = lease["job"]["lease_id"]
    v = np.zeros(8, dtype=np.float32)
    v[0] = 1
    result = diarize.result_to_json({"turns": [(0.0, 5.0, "SPEAKER_00")], "embeddings": {"SPEAKER_00": v},
                                     "seconds": {"SPEAKER_00": 5.0}})
    doc = json.dumps(result).encode()
    assert put(client, j, lid, "diarize-result.json", doc).status_code == 200
    # the Mac echoes the input hash it was given: a changed session would be stale
    stale = complete(client, j, lid, [{"name": "diarize-result.json", "sha256": sha(doc)}], input_hash="0" * 64)
    assert stale.status_code == 409
    lid = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()["job"]["lease_id"]
    put(client, j, lid, "diarize-result.json", doc)
    r = complete(client, j, lid, [{"name": "diarize-result.json", "sha256": sha(doc)}],
                 input_hash=lease["params"]["input_hash"])
    assert r.status_code == 200, r.text
    spk = json.loads(diarize.speakers_path(session).read_text())
    assert spk["version"] == diarize.VERSION and spk["turns"][0]["speaker"] == "Persona 1"
    assert (diarize.CLUSTERS / f"{ids[0]}.npz").is_file()                 # the cluster embeddings stay on the hub


# --- organize and embed --------------------------------------------------------------------------

def test_organize_commit_is_checked_against_the_transcript(client, archive):
    from omy import jobs
    from omy.brain import SCHEMA_VERSION, organize
    ids = old_end(archive)
    sid = ids[0]
    write_day(archive, "2026-10-29", [record(sid, "2026-10-29", "10:00", "Io", "me",
                                             "Una frase abbastanza lunga da organizzare nella nota di prova, " * 3)])
    j = jobs.enqueue("organize", sid)
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    assert lease["job"]["kind"] == "organize"
    assert [i["path"] for i in lease["inputs"]] == ["transcripts/2026-10-29.jsonl"]
    lid, h = lease["job"]["lease_id"], lease["params"]["input_hash"]
    assert h == organize.input_hash(sid)
    name = f"brain/note/2026/10/29/1000-prova-{sid[-8:]}.json"
    note = {"id": sid, "titolo": "Prova", "data": "2026-10-29", "riassunto": "x", "punti_chiave": [], "entita": {},
            "impegni_e_todo": [], "date_ed_eventi_citati": [], "ascoltato": [], "tag": [], "collegamenti": [],
            "elaborazione": {"modello": "test", "prompt_version": organize.PROMPT_VERSION,
                             "schema_version": SCHEMA_VERSION, "input_hash": "stale"}}
    data = json.dumps(note).encode()
    assert put(client, j, lid, name, data).status_code == 200
    r = complete(client, j, lid, [{"name": name, "sha256": sha(data)}])
    assert r.status_code == 409 and not (archive / name).exists() and jobs.state_of(j) == "queued"
    # with the right hash it is written, rendered and queued for the vault batch
    lid = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()["job"]["lease_id"]
    note["elaborazione"]["input_hash"] = h
    data = json.dumps(note).encode()
    put(client, j, lid, name, data)
    r = complete(client, j, lid, [{"name": name, "sha256": sha(data)}])
    assert r.status_code == 200, r.text
    assert (archive / name).is_file()
    assert json.loads((archive / "cache" / "organize-batch.json").read_text()) == [sid]


def test_embed_round_trip(client, archive):
    import base64
    import sqlite3
    import numpy as np
    from contextlib import closing
    from omy import jobs
    from omy.brain import embed, index
    with closing(index._connect()) as db, db:
        index._insert(db, "s1", [{"kind": "testo", "day": "2026-10-29", "start": "x", "t": 0.0, "row_id": "r", "persons": [],
                                  "title": "t", "field": "f", "text": f"chunk {i}"} for i in range(3)], "h", {})
    j = jobs.enqueue("embed", "all")
    lease = client.post("/compute/v1/lease", json=lease_body(), headers=MAC).json()
    chunks = lease["payload"]["chunks"]
    assert [c["text"] for c in chunks] == ["chunk 0", "chunk 1", "chunk 2"]
    vec = np.ones(embed.EMBED_DIM, dtype=np.float16)
    out = json.dumps({"vectors": {str(c["id"]): base64.b64encode(vec.tobytes()).decode() for c in chunks}}).encode()
    lid = lease["job"]["lease_id"]
    put(client, j, lid, "embed-output.json", out)
    assert complete(client, j, lid, [{"name": "embed-output.json", "sha256": sha(out)}]).json()["embedded"] == 3
    with closing(sqlite3.connect(index.INDEX_PATH)) as db:
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 3
    jobs.enqueue("embed", "all")
    assert client.post("/compute/v1/lease", json=lease_body(), headers=MAC).status_code == 204  # nothing missing


# --- status, context, profile ---------------------------------------------------------------------

def test_status_feeds_brain_status(client, archive, monkeypatch):
    monkeypatch.setenv("OMY_ROLE", "hub")
    from omy import worker
    r = client.post("/compute/v1/status", headers=MAC,
                    json={"worker_id": "mac", "boot_id": "b1", "capabilities": caps(),
                          "state": {"state": "running", "job": {"kind": "transcribe", "key": "s"}, "loaded": "chat"}})
    assert r.status_code == 200
    st = worker.read_status()
    assert st["state"] == "running" and st["queue"]["queued"] == 0 and worker.heartbeat_fresh()


def test_context_only_grows(client, archive):
    def up(data: bytes):
        return client.put("/compute/v1/context/2026-10-29", content=data, headers={**MAC, "X-Sha256": sha(data)})
    assert up(b"a\nb\n").json() == {"status": "stored"}
    assert up(b"a\n").json() == {"status": "kept"}
    assert up(b"a\nb\nc\n").json() == {"status": "stored"}
    assert up(b"a\nb\nc\n").json() == {"status": "same"}
    assert (archive / "context" / "audio-2026-10-29.jsonl").read_text() == "a\nb\nc\n"
    assert client.put("/compute/v1/context/nope", content=b"x", headers={**MAC, "X-Sha256": sha(b"x")}).status_code == 400


def test_profile_upload(client, archive):
    import io
    import numpy as np
    buf = io.BytesIO()
    np.save(buf, np.ones(16, dtype=np.float32))
    assert client.put("/compute/v1/profile", content=buf.getvalue(), headers=MAC).status_code == 200
    assert (archive / "voices" / "me.npy").is_file()
    assert client.put("/compute/v1/profile", content=b"junk", headers=MAC).status_code == 400
