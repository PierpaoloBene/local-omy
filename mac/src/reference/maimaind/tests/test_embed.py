import json

from fakellm import FakeEmbedder, FakeLLM, say, tool

from brain import ask, embed, summarize
from brain.tools import Constraints, Tools

A = "20261020T170000Z-aaaa0001"


def test_sync_embeds_everything_once(conn):
    r = embed.sync(conn, FakeEmbedder())
    total = conn.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0]
    assert r.added == total and r.pending == 0
    again = embed.sync(conn, FakeEmbedder())
    assert (again.added, again.removed) == (0, 0)


def test_changed_and_deleted_items_are_reembedded_or_removed(conn):
    embed.sync(conn, FakeEmbedder())
    conn.execute("UPDATE paragraphs SET text = 'testo nuovo' WHERE id = ?", (f"{A}/0",))
    conn.execute("DELETE FROM paragraphs WHERE id = ?", (f"{A}/45000",))
    r = embed.sync(conn, FakeEmbedder())
    assert r.removed == 2 and r.added == 1


def test_summaries_are_embedded(conn):
    extraction = {"title": "Preventivo bagno", "quality": "good", "summary": [{"text": "Hai promesso il preventivo",
                  "refs": ["P3"]}], "topics": [], "commitments": [], "facts": [], "heard": []}
    summarize.summarize(conn, FakeLLM(say(json.dumps(extraction))), A, "fake")
    embed.sync(conn, FakeEmbedder())
    assert conn.execute("SELECT COUNT(*) FROM vec_map WHERE kind = 'summary'").fetchone()[0] == 1
    t = Tools(conn, embedder=FakeEmbedder())
    out = t.search("preventivo bagno")
    assert "«Preventivo bagno»" in out and "Riassunto: Hai promesso il preventivo" in out


def test_knn_respects_channels_and_dates(conn):
    embed.sync(conn, FakeEmbedder())
    q = FakeEmbedder().query("ristrutturazione dei ricordi")
    talk = embed.knn(conn, q, ("paragraph",), ("me", "said"))
    heard = embed.knn(conn, q, ("paragraph",), ("heard",))
    channels = lambda hits: {conn.execute("SELECT channel FROM paragraphs WHERE id = ?", (r,)).fetchone()[0]  # noqa
                             for _, r, _ in hits}
    assert channels(talk) <= {"me", "said"} and channels(heard) == {"heard"}
    dated = embed.knn(conn, q, ("paragraph",), ("me", "said"), "2026-10-27", "2026-10-28")
    assert {conn.execute("SELECT day FROM paragraphs WHERE id = ?", (r,)).fetchone()[0] for _, r, _ in dated} \
        <= {"2026-10-27", "2026-10-28"}


def test_search_keeps_the_constraints(conn):
    embed.sync(conn, FakeEmbedder())
    t = Tools(conn, Constraints(("me",)), embedder=FakeEmbedder())
    out = t.search("banche criptovalute", channel=["said", "heard"])
    assert "Persona 1" not in out and "Io" in out
    t = Tools(conn, embedder=FakeEmbedder())
    t.search("ristrutturazione", person="Rosa Linda")
    refs = [r for r in t.refs.by_ref.values() if r.kind == "paragraph"]
    assert refs and all(r.row["session"] in ("20261020T170000Z-aaaa0001", "20261028T180000Z-eeee0005") for r in refs)


def test_search_finds_by_meaning_what_words_miss(conn):
    """No word of the question is in the bathroom talk: only the vectors find it
    (the fake embedder makes words that share their first five letters close)."""
    embed.sync(conn, FakeEmbedder())
    assert "piastrelle" not in Tools(conn).search("piastrellista sorellastra")
    assert "piastrelle" in Tools(conn, embedder=FakeEmbedder()).search("piastrellista sorellastra")


class FakeReranker:
    """Relevance: the share of the question's words in the candidate."""

    def __init__(self):
        self.calls = []

    def scores(self, query, texts):
        self.calls.append((query, texts))
        q = {w for w in query.lower().split() if len(w) > 3}
        return [len(q & set(t.lower().replace(",", " ").replace(".", " ").split())) / max(1, len(q)) for t in texts]


def test_reranker_orders_and_drops_what_is_not_relevant(conn):
    rr = FakeReranker()
    t = Tools(conn, embedder=FakeEmbedder(), reranker=rr)
    embed.sync(conn, FakeEmbedder())
    out = t.search("vacanze sicilia siracusa giugno")
    lines = [ln for ln in out.splitlines() if ln.startswith("[")]
    assert "Sicilia" in lines[0] and "(pertinenza 1.00)" in lines[0]
    # The reranker read each candidate with context: the conversation it is in.
    assert any("Conversazione" in text for text in rr.calls[0][1])
    assert all("pertinenza 0.00" not in ln for ln in lines)


def test_search_without_reranker_still_works_when_it_fails(conn):
    class Broken:
        def scores(self, query, texts):
            raise ConnectionError("down")
    t = Tools(conn, reranker=Broken())
    assert "Sicilia" in t.search("vacanze Sicilia")


def test_the_model_gets_search_first(conn):
    embed.sync(conn, FakeEmbedder())
    llm = FakeLLM(say("In Sicilia [R1]."))
    a = ask.ask("Dove andiamo in vacanza?", conn, llm, embedder=FakeEmbedder())
    names = [t["function"]["name"] for t in llm.requests[0]["tools"]]
    assert names[0] == "search" and "search_semantic" not in names
    assert a.found


def test_a_text_too_long_for_the_server_is_cut_not_fatal():
    from brain.llm import LLMError

    class Server:
        def __init__(self):
            self.inputs = []

        def _request(self, path, body):
            self.inputs.append(body["input"])
            if any(len(t) > 600 for t in body["input"]):
                raise LLMError('/v1/embeddings: HTTP 500 {"message": "Context size has been exceeded."}')
            return {"data": [{"index": i, "embedding": [1.0] * embed.DIM} for i, _ in enumerate(body["input"])]}

    srv = Server()
    vectors = embed.Embedder(srv).embed(["breve", "x" * 1800])
    assert len(vectors) == 2 and srv.inputs[-1] == ["x" * 450]
