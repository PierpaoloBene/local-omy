"""brain eval: questions with known answers, to compare models on the same data.

Measures, per question and overall: time, model rounds, prompt and generated
tokens, tool calls with valid arguments, references cited that no tool returned
(stripped from the answer, counted here), and whether the answer is right: it
cites the expected sessions within the allowed channels, or says "not found"
when that is the truth.
"""

from __future__ import annotations

import json
import re
import statistics
import tempfile
import time
import tomllib
from datetime import date
from pathlib import Path

from brain import ask as ask_mod
from brain import db, docs, fixtures, gate, ingest
from brain.config import Settings, private_dir
from brain import embed
from brain.llm import LLM, EmbedServer, Reranker, RerankServer, Server

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval"
# "I found nothing about X; with her you talked about Y [1]" is a right answer to
# a question whose truth is "nothing": it says no, and cites what there is.
NEGATIVE = re.compile(r"^\s*(non ho trovato|non risulta|non c'è|no[,.!]|non hai|i found nothing|no,)", re.I)


class EvalError(Exception):
    pass


# Documents of the fixtures, besides tests/data: name -> text. Written in the formats
# the scan reads, and in folders, as on a real Mac; several are found only by
# meaning (no word of the question is in them).
FIXTURE_DOCS = {
    "Auto/polizza-rca-2026.txt": "Allianz S.p.A. Polizza n. 4471-2026 Responsabilità Civile Autoveicoli.\n"
                                 "Contraente: Pierpaolo. Veicolo: Fiat Panda targa GH 512 KX.\n"
                                 "Decorrenza 15/03/2026, scadenza 15/03/2027. Premio annuo lordo 482,00 euro.",
    "Salute/Referti/esami-maggio.md": "# Laboratorio Analisi San Marco\nReferto del 10/05/2026, paziente Pierpaolo.\n"
                                      "TSH 2,1 mUI/L (0,4-4,0). Colesterolo totale 192 mg/dl. Glicemia 88 mg/dl.\n"
                                      "Emocromo nella norma.",
    "Casa/bolletta-enel-ottobre.html": "<html><body><h1>Enel Energia</h1><p>Bolletta luce di ottobre 2026, "
                                       "fornitura Via Roma 10 Milano.</p><p>Importo da pagare: 82,50 euro entro il "
                                       "15/11/2026.</p></body></html>",
    "Lavoro/offerta-atlante.txt": "Spett.le Pierpaolo, in riferimento al progetto Atlante le inviamo l'offerta per "
                                  "il modulo di reportistica: 18.500 euro, consegna in 10 settimane. Fornitore: "
                                  "Dataplus Srl, referente Marco Neri.",
}


def fixture_docs(settings: Settings, base: Path) -> Settings:
    """The test documents (tests/data and FIXTURE_DOCS) in a folder of their own, as if on the Mac."""
    import dataclasses
    import shutil
    root = base / "Documenti"
    root.mkdir()
    for name in ("contratto-testo.pdf", "scansione.png"):
        shutil.copy(EVAL_DIR.parent / "tests" / "data" / name, root / name)
    (root / "spesa.md").write_text("Lista della spesa: latte, uova, pane.\n")
    for name, text in FIXTURE_DOCS.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return dataclasses.replace(settings, doc_roots=[str(root)], doc_exclude=[], doc_images=True,
                               doc_exts=[".pdf", ".md", ".png", ".txt", ".html"], vault=base / "vault")


def score(q: dict, answer: ask_mod.Answer) -> dict:
    cited = {c["session"] for c in answer.citations if c.get("session")}
    if q.get("documents"):
        names = {Path(c["path"]).name for c in answer.citations if c["kind"] == "document"}
        return {**_row(q, answer), "correct": bool(names & set(q["documents"])), "channels_ok": True}
    channels_ok = all(c["channel"] in q.get("channels", ["me", "said", "heard"])
                      for c in answer.citations if c["kind"] == "paragraph")
    if q.get("not_found"):
        correct = not answer.found or bool(NEGATIVE.match(answer.text))
    else:
        correct = bool(cited & set(q.get("sessions", []))) and channels_ok
    if q.get("documents_too"):
        # Both: the conversation and the document.
        names = {Path(c["path"]).name for c in answer.citations if c["kind"] == "document"}
        correct = correct and bool(names & set(q["documents_too"]))
    return {**_row(q, answer), "correct": correct, "channels_ok": channels_ok}


def _row(q: dict, answer: ask_mod.Answer) -> dict:
    calls = answer.tool_calls
    return {
        "id": q["id"], "found": answer.found,
        "seconds": answer.seconds, "rounds": answer.rounds,
        "tool_calls": len(calls), "tool_calls_valid": sum(1 for c in calls if c["valid"]),
        "invalid_refs": len(answer.invalid_refs),
        "prompt_tokens": sum(t.get("prompt_n", 0) for t in answer.timings),
        "gen_tokens": sum(t.get("predicted_n", 0) for t in answer.timings),
        "gen_tok_s": round(statistics.mean([t["predicted_per_second"] for t in answer.timings
                                            if t.get("predicted_per_second")] or [0]), 1),
        "answer": answer.text,
    }


def summarize(rows: list[dict]) -> dict:
    calls = sum(r["tool_calls"] for r in rows)
    return {
        "questions": len(rows),
        "correct": sum(r["correct"] for r in rows),
        "seconds_median": round(statistics.median([r["seconds"] for r in rows]), 1) if rows else 0,
        "seconds_total": round(sum(r["seconds"] for r in rows), 1),
        "tool_calls_valid": f"{sum(r['tool_calls_valid'] for r in rows)}/{calls}",
        "invalid_refs": sum(r["invalid_refs"] for r in rows),
        "gen_tok_s": round(statistics.mean([r["gen_tok_s"] for r in rows if r["gen_tok_s"]] or [0]), 1),
    }


def run_questions(conn, llm: LLM, questions: dict, embedder=None, setup=None, reranker=None) -> list[dict]:
    today = date.fromisoformat(questions["today"]) if "today" in questions else None
    rows = []
    for q in questions["q"]:
        answer = ask_mod.ask(q["ask"], conn, llm, today=date.fromisoformat(q["today"]) if "today" in q else today,
                             embedder=embedder, setup=setup, reranker=reranker)
        rows.append(score(q, answer))
    return rows


def run(settings: Settings, model: str | None, set_name: str, keep: bool = False, semantic: bool = True,
        rerank: bool = True) -> dict:
    profile = settings.profile(model)
    g = gate.check(settings)
    if not g.open:
        raise EvalError(f"not now: {g.reason}")
    if set_name == "fixtures":
        questions = tomllib.loads((EVAL_DIR / "questions.toml").read_text())
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        conn = db.connect(base / "index.sqlite")
        ingest.sync(conn, fixtures.write(base / "transcripts"))
        ingest.sync_people(conn, fixtures.PEOPLE)
        doc_settings = fixture_docs(settings, base)
        docs.scan(conn, doc_settings)
        while docs.extract_next(conn, doc_settings):
            pass
    elif set_name == "real":
        path = EVAL_DIR / "questions.local.toml"
        if not path.is_file():
            raise EvalError(f"write your questions in {path} (same format as questions.toml)")
        questions = tomllib.loads(path.read_text())
        conn = db.connect(settings.db_path)
        ingest.sync(conn, settings.transcripts)
    else:
        raise EvalError(f"unknown set {set_name!r}: fixtures or real")

    embedder = reranker = None
    esrv, rsrv, server = EmbedServer(settings), RerankServer(settings), Server(settings)
    cards, classify_s = [], None
    try:
        if semantic and settings.model_path(esrv.profile()).is_file():
            embedder = embed.Embedder(esrv.start())
            embed.sync(conn, embedder)
        if rerank and settings.model_path(rsrv.profile()).is_file():
            reranker = Reranker(rsrv.start())
        t0 = time.monotonic()
        llm = server.start(profile)
        load_s = round(time.monotonic() - t0, 1)
        if set_name == "fixtures":
            t0 = time.monotonic()
            while docs.classify_next(conn, llm):
                pass
            classify_s = round(time.monotonic() - t0, 1)
            cards = [{"file": Path(d["path"]).name, "kind": d["kind"], "area": d["area"], "title": d["title"]}
                     for d in conn.execute("SELECT * FROM documents WHERE status != 'unreadable'")]
            if embedder:
                embed.sync(conn, embedder)
        rows = run_questions(conn, llm, questions, embedder, docs.setup_tools(conn, doc_settings if set_name ==
                                                                               "fixtures" else settings), reranker)
    finally:
        # Always, whatever failed: three servers left behind fill the GPU's memory.
        if not keep:
            server.stop()
            esrv.stop()
            rsrv.stop()
    result = {"model": profile["name"], "set": set_name, "semantic": embedder is not None,
              "rerank": reranker is not None, "load_seconds": load_s, **summarize(rows), "rows": rows,
              "cards": cards}
    if classify_s is not None:
        result["classify_seconds"] = classify_s
    out = private_dir(settings.data_dir / "evals") / f"{time.strftime('%Y%m%d-%H%M%S')}-{profile['name']}-{set_name}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    result["saved"] = str(out)
    return result
