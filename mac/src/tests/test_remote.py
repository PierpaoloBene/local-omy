"""omy on the Mac as a client of the hub: with hub_url set the same commands print the same JSON, from the hub."""

import json
import sys

import pytest
from fastapi.testclient import TestClient

from helpers import record, write_day, write_segments
from test_compute import TOKEN

from omy import cli, macsetup, remote


@pytest.fixture
def rig(archive, monkeypatch, capsys):
    from omy_receiver.app import Config, create_app
    monkeypatch.setenv("OMY_ROLE", "hub")      # the hub's receiver (and the omy it runs)...
    app_client = TestClient(create_app(Config(token=TOKEN.encode(), archive=archive)))
    monkeypatch.delenv("OMY_ROLE")             # ...and the Mac's omy under test

    class Fake(remote.Remote):
        def call(self, method, path, body=None, query=None, raw=False, timeout=120.0):
            r = app_client.request(method, path, json=body, params={k: v for k, v in (query or {}).items() if v is not None},
                                   headers={"Authorization": f"Bearer {self.token}"})
            if r.status_code != 200:
                raise remote.RemoteError(f"the hub answered: {r.json().get('error')}")
            return r.content if raw else r.json()
    monkeypatch.setattr(remote, "Remote", Fake)
    monkeypatch.setattr(macsetup, "read_token", lambda: TOKEN)

    def omy(*argv, remote_on):
        monkeypatch.setattr(remote, "hub_url", (lambda: "http://hub.test") if remote_on else (lambda: None))
        monkeypatch.setattr(sys, "argv", ["omy", "--json", *argv])
        assert cli.main() == 0
        return json.loads(capsys.readouterr().out)
    return omy


def seed(archive):
    write_segments(archive, n=1)
    write_day(archive, "2026-10-29", [
        record("20261029T080000Z-0000abcd", "2026-10-29", "10:00", "Io", "me", "Parliamo del giardino"),
        record("20261029T080000Z-0000abcd", "2026-10-29", "10:05", "Persona 1", None, "Va bene")])


def test_remote_output_is_the_local_output(rig, archive):
    seed(archive)
    for argv in (("transcripts", "list"), ("transcripts", "day", "2026-10-29"), ("transcripts", "search", "giardino"),
                 ("transcripts", "conversations"), ("people", "list"), ("places", "list"), ("speakers", "todo")):
        assert rig(*argv, remote_on=True) == rig(*argv, remote_on=False), argv


def test_changes_go_to_the_hub(rig, archive):
    seed(archive)
    rig("places", "add", "Casa", "45.0", "9.0", "--radius", "100", remote_on=True)
    assert [p["name"] for p in json.loads((archive / "places.json").read_text())["places"]] == ["Casa"]
    rig("places", "rename", "Casa", "Casa mia", remote_on=True)
    assert rig("places", "list", remote_on=False)["places"][0]["name"] == "Casa mia"
    rig("places", "remove", "Casa mia", remote_on=True)
    assert rig("places", "list", remote_on=False)["places"] == []
    out = rig("features", "set", "mac_audio", "off", remote_on=True)
    assert next(f for f in out["features"] if f["key"] == "mac_audio")["on"] is False
    assert rig("features", "list", remote_on=False)["features"] == rig("features", "list", remote_on=True)["features"]


def test_hub_errors_are_printed_like_any_omy_error(rig, capsys, monkeypatch, archive):
    monkeypatch.setattr(remote, "hub_url", lambda: "http://hub.test")
    monkeypatch.setattr(sys, "argv", ["omy", "--json", "people", "rename", "0123456789ab", "Anna"])
    assert cli.main() == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_status_has_the_keys_the_app_reads(rig, archive, monkeypatch):
    seed(archive)
    monkeypatch.setattr(macsetup, "tailscale_info", lambda: {"installed": True, "serving": False, "url": None, "iphones": []})
    monkeypatch.setattr(macsetup, "agent_info", lambda name: {"loaded": True, "running": True, "pid": 1, "last_exit": 0,
                                                              "up_to_date": True})
    from omy import iphone
    monkeypatch.setattr(iphone, "status", lambda: {"team_id": None, "device_udid": None, "expires": None, "days_left": None,
                                                    "installed_at": None, "needs_reopen": False, "device": None})
    st = rig("status", remote_on=True)
    for key in ("receiver", "worker", "tailscale", "archive", "upload", "iphone", "transcripts", "battery", "firmware", "logs"):
        assert key in st
    assert st["archive"]["path"] == "http://hub.test" and st["archive"]["today_segments"] >= 0
    assert st["tailscale"]["serving"] is True and st["transcripts"]["pending_sessions"] >= 0
    assert set(st["worker"]) >= {"loaded", "running", "state", "queued"}


def test_without_hub_url_everything_is_local(rig, archive, monkeypatch):
    seed(archive)
    monkeypatch.setattr(remote, "Remote", lambda *a: pytest.fail("went to the hub"))
    assert rig("transcripts", "list", remote_on=False)["days"]


def test_transcribe_pending_wakes_the_hub(rig, archive):
    out = rig("transcribe", "--pending", remote_on=True)
    assert out["transcribed"] == [] and out["mac_online"] is False and "started" in out
