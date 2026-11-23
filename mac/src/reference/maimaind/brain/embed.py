"""Embeddings for semantic search (sqlite-vec).

Embedded: paragraphs (their text), session summaries (title and points) and
document chunks (with the title, kind and folder of their document before them). One vec0 table holds them all, with the metadata the
filters need (kind, channel, day); vec_map says what each vector is and the hash
of the text it was made from, so a changed text is embedded again and a vector
whose item is gone is deleted (no triggers: ingest never needs sqlite-vec).

Model: Qwen3-Embedding-0.6B, last-token pooling, 1024 dimensions, normalized,
cosine distance. Queries get the instruction prefix the model was trained with;
documents are embedded as they are.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass

import sqlite_vec

from brain.llm import LLM, LLMError

DIM = 1024
BATCH = 32
# Characters per text: longer ones are cut (a paragraph is at most 5 minutes of speech).
MAX_CHARS = 2000
QUERY_INSTRUCTION = ("Instruct: Given a question, retrieve passages of conversations or documents that answer it\n"
                     "Query: ")

SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0(
  id INTEGER PRIMARY KEY, embedding float[{DIM}] distance_metric=cosine,
  kind TEXT, channel TEXT, day TEXT);
CREATE TABLE IF NOT EXISTS vec_map(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, text_hash TEXT NOT NULL, UNIQUE(kind, ref));
"""


def load(conn: sqlite3.Connection) -> None:
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(SCHEMA)


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Embedder:
    """The embedding llama-server's /v1/embeddings."""

    def __init__(self, llm: LLM):
        self.llm = llm

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self.llm._request("/v1/embeddings", {"input": [t[:MAX_CHARS] for t in texts]})
        except LLMError as e:
            # A text of many short tokens (numbers, symbols, OCR noise) longer than the
            # server's batch or context: one at a time, and one too long cut in half until it fits.
            if not ("too large" in str(e) or "exceeded" in str(e)) or all(len(t) <= MAX_CHARS // 4 for t in texts):
                raise
            if len(texts) > 1:
                return [v for t in texts for v in self.embed([t])]
            return self.embed([texts[0][:min(len(texts[0]), MAX_CHARS) // 2]])
        data = sorted(r["data"], key=lambda d: d["index"])
        out = [normalize(d["embedding"]) for d in data]
        if any(len(v) != DIM for v in out):
            raise ValueError(f"expected {DIM} dimensions")
        return out

    def query(self, text: str) -> list[float]:
        return self.embed([QUERY_INSTRUCTION + text])[0]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def wanted(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, str, str, str]]:
    """(kind, ref) -> (text, text_hash, channel, day) for everything that should have a vector."""
    out = {}
    for r in conn.execute("SELECT id, text, channel, day FROM paragraphs"):
        out[("paragraph", r["id"])] = (r["text"], _hash(r["text"]), r["channel"], r["day"])
    for r in conn.execute("SELECT s.session, s.title, s.data, x.day FROM summaries s "
                          "JOIN sessions x ON x.id = s.session WHERE s.quality != 'noise'"):
        data = json.loads(r["data"])
        text = r["title"] + "\n" + "\n".join(i["text"] for i in data["summary"])
        heard_only = not data["summary"] and data["heard"]
        if heard_only:
            text = r["title"] + "\n" + "\n".join(h["summary"] for h in data["heard"])
        out[("summary", r["session"])] = (text, _hash(text), "heard" if heard_only else "said", r["day"])
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'doc_chunks'").fetchone():
        from brain.docs import place
        # Contextual chunks: the card's title, kind and folder before the text, so that
        # a chunk that never names its document is still found by it. Only once the
        # card exists (it would be embedded twice otherwise), copies left out.
        for r in conn.execute("SELECT c.id, c.text, c.page, d.title, d.kind, d.path, d.doc_date FROM doc_chunks c "
                              "JOIN documents d ON d.sha256 = c.doc WHERE d.status NOT IN ('missing', 'unreadable') "
                              "AND d.classified = 1 AND d.duplicate_of IS NULL"):
            text = f"Documento «{r['title']}» ({r['kind']}) in {place(r['path'])}, pagina {r['page'] or 1}.\n{r['text']}"
            out[("chunk", str(r["id"]))] = (text, _hash(text), "doc", r["doc_date"] or "")
    return out


@dataclass
class EmbedReport:
    added: int = 0
    removed: int = 0
    pending: int = 0


def sync(conn: sqlite3.Connection, embedder: Embedder | None, limit: int | None = None) -> EmbedReport:
    """Delete the vectors of items gone or changed; embed what is missing (at
    most limit items: the worker does it a batch at a time). Without an
    embedder, only count what is pending."""
    load(conn)
    want = wanted(conn)
    have = {(r["kind"], r["ref"]): (r["id"], r["text_hash"]) for r in conn.execute("SELECT * FROM vec_map")}
    report = EmbedReport()
    with conn:
        for key, (vid, h) in have.items():
            if key not in want or want[key][1] != h:
                conn.execute("DELETE FROM vec_items WHERE id = ?", (vid,))
                conn.execute("DELETE FROM vec_map WHERE id = ?", (vid,))
                report.removed += 1
    todo = [(k, v) for k, v in want.items() if k not in have or have[k][1] != v[1]]
    report.pending = len(todo)
    if embedder is None:
        return report
    todo = todo[:limit] if limit else todo
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        vectors = embedder.embed([v[0] for _, v in batch])
        with conn:
            for ((kind, ref), (_, h, channel, day)), vec in zip(batch, vectors):
                cur = conn.execute("INSERT INTO vec_map(kind, ref, text_hash) VALUES (?, ?, ?)", (kind, ref, h))
                conn.execute("INSERT INTO vec_items(id, embedding, kind, channel, day) VALUES (?, ?, ?, ?, ?)",
                             (cur.lastrowid, sqlite_vec.serialize_float32(vec), kind, channel, day))
                report.added += 1
    report.pending -= report.added
    return report


def knn(conn: sqlite3.Connection, vector: list[float], kinds: tuple[str, ...], channels: tuple[str, ...],
        date_from: str | None = None, date_to: str | None = None, k: int = 50) -> list[tuple[str, str, float]]:
    """Nearest items: (kind, ref, distance), filtered in the index itself."""
    load(conn)
    where = [f"v.kind IN ({', '.join('?' for _ in kinds)})"]
    params: list = [*kinds]
    # Summaries are "said" or "heard", chunks "doc": the channel filter only constrains talk.
    talk = [c for c in channels]
    where.append(f"v.channel IN ({', '.join('?' for _ in talk + ['doc'])})")
    params += talk + ["doc"]
    if date_from:
        where.append("v.day >= ?")
        params.append(date_from)
    if date_to:
        where.append("v.day <= ?")
        params.append(date_to)
    rows = conn.execute(
        f"SELECT m.kind, m.ref, v.distance FROM vec_items v JOIN vec_map m ON m.id = v.id "
        f"WHERE v.embedding MATCH ? AND k = ? AND {' AND '.join(where)} ORDER BY v.distance",
        [sqlite_vec.serialize_float32(vector), k, *params]).fetchall()
    return [(r["kind"], r["ref"], r["distance"]) for r in rows]
