import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omy_receiver.app import Config, ConfigError, create_app, load_token

TOKEN = "t" * 40
FIXTURE = (Path(__file__).parent / "fixtures" / "sample.bin").read_bytes()
# 22:30 UTC on 24 Oct 2026 is 00:30 on 25 Oct in Rome (CEST): tests the day folder.
SEG_ID = "20261024T223000Z-0123abcd"


def make_meta(body: bytes, seg_id: str = SEG_ID, **over) -> dict:
    meta = {
        "id": seg_id,
        "device_id": "DEV-1",
        "codec": 21,
        "start": "2026-10-24T22:30:00.123Z",
        "end": "2026-10-24T22:35:00.140Z",
        "frame_count": 6,
        "first_packet_index": 65534,
        "last_packet_index": 4,
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    meta.update(over)
    return meta


def headers(body: bytes, meta: dict | None = None, seg_id: str = SEG_ID, token: str = TOKEN) -> dict:
    meta = meta if meta is not None else make_meta(body, seg_id)
    raw = json.dumps(meta).encode()
    return {
        "Authorization": f"Bearer {token}",
        "X-Segment-Id": seg_id,
        "X-Sha256": hashlib.sha256(body).hexdigest(),
        "X-Segment-Meta": base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "OmiArchive"


@pytest.fixture
def client(archive):
    return TestClient(create_app(Config(token=TOKEN.encode(), archive=archive, max_body=1024)))


def post(client, body=FIXTURE, seg_id=SEG_ID, **kw):
    h = kw.pop("h", None) or headers(body, seg_id=seg_id, **kw)
    return client.post(f"/v1/segments/{seg_id}", content=body, headers=h)


def test_stores_segment_in_rome_day_folder(client, archive):
    r = post(client)
    assert r.status_code == 200
    assert r.json() == {"id": SEG_ID, "sha256": hashlib.sha256(FIXTURE).hexdigest(), "status": "stored"}
    day = archive / "raw" / "audio" / "2026-10-25"
    assert (day / f"{SEG_ID}.bin").read_bytes() == FIXTURE
    assert json.loads((day / f"{SEG_ID}.json").read_text())["device_id"] == "DEV-1"
    assert list((archive / "tmp").iterdir()) == []


def test_same_id_same_hash_is_idempotent(client):
    assert post(client).json()["status"] == "stored"
    r = post(client)
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"


def test_same_id_different_hash_conflicts(client, archive):
    post(client)
    other = FIXTURE + b"\x00" * 6  # still a valid header
    r = post(client, body=other)
    assert r.status_code == 409
    day = archive / "raw" / "audio" / "2026-10-25"
    assert (day / f"{SEG_ID}.bin").read_bytes() == FIXTURE
    assert list((archive / "tmp").iterdir()) == []


@pytest.mark.parametrize("auth", [None, "", "Bearer", "Bearer wrong", f"Basic {TOKEN}", f"bearer{TOKEN}"])
def test_rejects_bad_auth(client, archive, auth):
    h = headers(FIXTURE)
    if auth is None:
        del h["Authorization"]
    else:
        h["Authorization"] = auth
    r = post(client, h=h)
    assert r.status_code == 401
    assert not (archive / "raw" / "audio" / "2026-10-25").exists()


@pytest.mark.parametrize("bad_id", [
    "..%2F..%2Fetc%2Fpasswd",
    "20261024T223000Z-0123ABCD",          # uppercase hex
    "20261024T223000Z-0123abcd.bin",
    "20261024T223000Z-0123abc",
    "２０261024T223000Z-0123abcd",          # non-ASCII digits
    "20261024T223000Z-0123abcd%0A",       # trailing newline
])
def test_rejects_bad_segment_id(client, archive, bad_id):
    h = headers(FIXTURE, seg_id=bad_id) if bad_id.isascii() else headers(FIXTURE)
    r = client.post(f"/v1/segments/{bad_id}", content=FIXTURE, headers=h)
    assert r.status_code in (400, 404)
    assert not (archive / "raw" / "audio").exists() or not any((archive / "raw" / "audio").iterdir())


def test_rejects_header_id_mismatch(client):
    h = headers(FIXTURE)
    h["X-Segment-Id"] = "20261024T223000Z-ffffffff"
    assert post(client, h=h).status_code == 400


def test_rejects_body_hash_mismatch(client, archive):
    h = headers(FIXTURE)
    r = post(client, body=FIXTURE[:-1], h=h)
    assert r.status_code == 400
    assert list((archive / "tmp").iterdir()) == []


def test_rejects_non_segment_body(client):
    body = b"NOPE" + FIXTURE[4:]
    assert post(client, body=body).status_code == 400


def test_rejects_oversized_body(client, archive):
    body = FIXTURE + b"\x00" * 2000
    assert post(client, body=body).status_code == 413
    assert list((archive / "tmp").iterdir()) == []


@pytest.mark.parametrize("over", [
    {"id": "20261024T223000Z-ffffffff"},
    {"sha256": "0" * 64},
    {"start": "2026-10-24T22:31:00.000Z"},   # does not match id
    {"start": "2026-10-24 22:30:00"},
    {"codec": "21"},
    {"codec": True},
    {"extra": 1},
])
def test_rejects_bad_meta(client, over):
    meta = make_meta(FIXTURE, **over)
    assert post(client, h=headers(FIXTURE, meta=meta)).status_code == 400


def test_rejects_missing_meta_field(client):
    meta = make_meta(FIXTURE)
    del meta["device_id"]
    assert post(client, h=headers(FIXTURE, meta=meta)).status_code == 400


def test_no_docs_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_token_file_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("OMY_TOKEN", raising=False)
    f = tmp_path / "token"
    f.write_text(TOKEN + "\n")
    monkeypatch.setenv("OMY_TOKEN_FILE", str(f))

    os.chmod(f, 0o644)
    with pytest.raises(ConfigError, match="mode 600"):
        load_token()
    os.chmod(f, 0o600)
    assert load_token() == TOKEN.encode()

    link = tmp_path / "link"
    link.symlink_to(f)
    monkeypatch.setenv("OMY_TOKEN_FILE", str(link))
    with pytest.raises(ConfigError):
        load_token()


def test_short_token_refused(monkeypatch):
    monkeypatch.setenv("OMY_TOKEN", "short")
    with pytest.raises(ConfigError, match="at least"):
        load_token()
