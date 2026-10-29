"""The receiver checks what the phone sends before running omy."""

import pytest
from fastapi.testclient import TestClient

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(archive):
    from omy_receiver.app import Config, create_app
    return TestClient(create_app(Config(token=TOKEN.encode(), archive=archive)))


@pytest.mark.parametrize("path", ["/v1/transcripts/2026-13-45", "/v1/voices/2026-02-30"])
def test_impossible_day(client, path):
    assert client.get(path, headers=AUTH).status_code == 400


def test_no_token(client):
    assert client.get("/v1/voices/20261029T080000Z-0000abcd/SPEAKER_00/sample").status_code == 401
    assert client.get("/v1/people").status_code == 401


@pytest.mark.parametrize("body", [{"labels": []}, {"labels": ["x"]}, {"labels": ["SPEAKER_00"], "unknown": "yes"}])
def test_unknown_body(client, body):
    r = client.post("/v1/voices/20261029T080000Z-0000abcd/unknown", json=body, headers=AUTH)
    assert r.status_code == 400


@pytest.mark.parametrize("q", ["hours=0", "hours=x", "hours=100000"])
def test_unlabeled_hours(client, q):
    assert client.get(f"/v1/unlabeled?{q}", headers=AUTH).status_code == 400


def test_place_with_a_slash(client):
    add = {"name": "Casa/Ufficio", "lat": 45.0, "lon": 9.0, "radius": 150}
    assert client.post("/v1/places", json=add, headers=AUTH).status_code == 200
    r = client.put("/v1/places/Casa%2FUfficio", json={"name": "Casa"}, headers=AUTH)
    assert r.status_code == 200, r.text
    assert client.delete("/v1/places/Casa", headers=AUTH).status_code == 200
    assert client.get("/v1/places", headers=AUTH).json()["places"] == []


def test_large_answers_are_compressed(client, archive):
    from helpers import record, write_day
    write_day(archive, "2026-10-29", [record(f"20261029T{i:02d}0000Z-000000{i:02d}", "2026-10-29", "10:00",
                                             "Io", "me", "parole " * 50) for i in range(20)])
    r = client.get("/v1/conversations", headers={**AUTH, "Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
