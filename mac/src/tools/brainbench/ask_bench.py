"""Latency of a small model for the "Chiedi" tab: time to first token and decode speed.

Works with any OpenAI-compatible server (llama-server, mlx_lm.server) using streaming.
The context is real transcript text (segments.json), ~2,500 tokens, like a RAG answer.

    python3 -I tools/brainbench/ask_bench.py --url http://127.0.0.1:8091 --label "q35-4b llama" [--model NAME]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

BASE = Path.home() / "OmiArchive" / "cache" / "brainbench"
QUESTIONS = ["Di cosa si è parlato in questa conversazione?",
             "Chi ha preso un impegno e per quando?",
             "Sono stati citati luoghi o persone? Quali?"]
SYSTEM = ("Rispondi in italiano, in 2-4 frasi, solo con ciò che è scritto nelle note qui sotto. "
          "Cita i paragrafi come [P3]. Se le note non contengono la risposta, dillo.\n\nNOTE:\n")


def ask(url: str, model: str | None, context: str, question: str) -> dict:
    body = {"messages": [{"role": "system", "content": SYSTEM + context},
                         {"role": "user", "content": question}],
            "stream": True, "max_tokens": 200, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False}}
    if model:
        body["model"] = model
    req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    first = None
    pieces = []
    timings = {}
    with urllib.request.urlopen(req, timeout=600) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            ev = json.loads(line[5:])
            timings = ev.get("timings") or timings
            for ch in ev.get("choices") or []:
                piece = (ch.get("delta") or {}).get("content") or ""
                if piece and first is None:
                    first = time.perf_counter()
                pieces.append(piece)
    end = time.perf_counter()
    n = len(pieces)
    return {"ttft_s": round((first or end) - t0, 2), "total_s": round(end - t0, 2),
            "chunks": n, "decode_chunks_per_s": round(n / max(end - (first or end), 1e-6), 1),
            "prompt_per_second": timings.get("prompt_per_second"),
            "predicted_per_second": timings.get("predicted_per_second"),
            "draft_n": timings.get("draft_n"), "draft_n_accepted": timings.get("draft_n_accepted"),
            "answer": "".join(pieces)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8091")
    p.add_argument("--label", required=True)
    p.add_argument("--model")
    a = p.parse_args()
    segs = json.loads((BASE / "segments.json").read_text())
    ask(a.url, a.model, "nessuna nota", "ciao")  # warm-up with another context: no prefix reuse
    out = BASE / "ask_results.jsonl"
    for q, seg in zip(QUESTIONS, segs):  # a different context each time, as with real retrieval
        context = "\n".join(seg["lines"])[:9000]  # ~2.5-3k tokens
        r = {"label": a.label, "question": q, **ask(a.url, a.model, context, q)}
        with out.open("a") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(a.label, q[:30], "ttft", r["ttft_s"], "total", r["total_s"],
              "pp", r["prompt_per_second"], "tg", r["predicted_per_second"] or r["decode_chunks_per_s"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
