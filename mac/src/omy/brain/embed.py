"""Embeddings for the index: Qwen3-Embedding-0.6B served by llama-server
(`--embedding --pooling last`, port 8092, see omy.models "embed").

POST {base}/v1/embeddings (the OpenAI-compatible endpoint llama-server
documents; with pooling it returns one normalized vector per input). The
vectors of 1024 dimensions are cut to the first EMBED_DIM (Matryoshka
truncation) and normalized again.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import numpy as np

from omy import models

EMBED_DIM = 512
MAX_CHARS = 2000        # longer texts are cut (the server's context is 2048 tokens)
TIMEOUT = 120.0
QUERY_WAIT = 10.0       # a question may arrive while the worker is still loading the embed model
QUERY_PREFIX = "Instruct: Dato una domanda, trova i passaggi dell'archivio personale che rispondono\nQuery: "


class EmbedError(Exception):
    """The embedding server is missing, not ready or answered badly."""


def _base(query: bool) -> str:
    try:
        if not query:
            return models.ensure("embed")  # the worker's index job: load it (stops the organizer)
        deadline = time.monotonic() + QUERY_WAIT
        while "embed" not in models.state().get("companions", []):
            if time.monotonic() > deadline:
                raise EmbedError("the embedding server is not loaded")
            time.sleep(0.2)
        return models.base_url("embed")
    except models.ModelError as e:
        raise EmbedError(str(e)) from e


def _post(base: str, texts: list[str]) -> list[list[float]]:
    body = json.dumps({"input": texts}).encode()
    req = urllib.request.Request(f"{base}/v1/embeddings", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())["data"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        raise EmbedError(f"embedding request failed: {e}") from e
    data = sorted(data, key=lambda d: d.get("index", 0))
    if len(data) != len(texts):
        raise EmbedError(f"expected {len(texts)} embeddings, got {len(data)}")
    return [d["embedding"] for d in data]


def embed(texts: list[str], query: bool = False) -> np.ndarray:
    """(len(texts), EMBED_DIM) float32, L2-normalized. For queries `texts`
    get the instruction prefix the model was trained with."""
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    prefix = QUERY_PREFIX if query else ""
    base = _base(query)
    raw = _post(base, [prefix + t[:MAX_CHARS] for t in texts])
    mat = np.asarray(raw, dtype=np.float32)
    if mat.ndim != 2 or mat.shape[1] < EMBED_DIM:
        raise EmbedError(f"unexpected embedding shape {mat.shape}")
    mat = mat[:, :EMBED_DIM]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return (mat / np.where(norms == 0, 1.0, norms)).astype(np.float32)
