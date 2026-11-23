import numpy as np
import pytest

from fake_embed import FakeEmbedServer
from omy import models
from omy.brain import embed


@pytest.fixture
def server(monkeypatch):
    s = FakeEmbedServer()
    monkeypatch.setattr(models, "ensure", lambda role: s.url)
    monkeypatch.setattr(models, "state", lambda: {"loaded": "chat", "companions": ["embed"]})
    monkeypatch.setattr(models, "base_url", lambda role: s.url)
    yield s
    s.close()


def test_embed_truncates_and_normalizes(server):
    out = embed.embed(["ciao mondo", "altro"], query=False)
    assert out.shape == (2, 512) and out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)


def test_query_prefix(server):
    embed.embed(["dove ho messo le chiavi"], query=True)
    assert server.requests[-1][0].startswith("Instruct: Dato una domanda")
    assert server.requests[-1][0].endswith("Query: dove ho messo le chiavi")
    embed.embed(["testo"], query=False)
    assert server.requests[-1] == ["testo"]


def test_empty(server):
    assert embed.embed([], False).shape == (0, 512)


def test_unavailable_server(monkeypatch):
    monkeypatch.setattr(models, "ensure", lambda role: "http://127.0.0.1:1")
    with pytest.raises(embed.EmbedError):
        embed.embed(["x"], False)

    def boom(role):
        raise models.ModelError("no model")

    monkeypatch.setattr(models, "ensure", boom)
    with pytest.raises(embed.EmbedError):
        embed.embed(["x"], False)


def test_query_without_companion(monkeypatch):
    monkeypatch.setattr(models, "state", lambda: {"loaded": "chat", "companions": []})
    monkeypatch.setattr(models, "chat_active", lambda: True)  # a question is pending: it waits
    monkeypatch.setattr(embed, "QUERY_WAIT", 0.3)
    with pytest.raises(embed.EmbedError):
        embed.embed(["x"], True)


def test_query_falls_back_at_once_when_nothing_is_loading(monkeypatch):
    import time
    monkeypatch.setattr(models, "state", lambda: {"loaded": None, "starting": None, "companions": []})
    monkeypatch.setattr(models, "chat_active", lambda: False)
    monkeypatch.setattr(embed, "QUERY_WAIT", 5.0)
    t0 = time.monotonic()
    with pytest.raises(embed.EmbedError):
        embed.embed(["x"], True)
    assert time.monotonic() - t0 < 1.0


def test_query_waits_while_the_worker_loads(monkeypatch, server):
    import time
    t0 = time.monotonic()
    monkeypatch.setattr(models, "state", lambda: {
        "loaded": None, "starting": "chat",
        "companions": ["embed"] if time.monotonic() - t0 > 0.5 else []})
    monkeypatch.setattr(models, "chat_active", lambda: False)
    assert embed.embed(["x"], True).shape[0] == 1
    assert time.monotonic() - t0 >= 0.5
