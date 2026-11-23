"""Benchmark of a local LLM "organizer" (transcript segment -> JSON note).

  select   pick real sessions and cut segments (no model needed)
  run      send the segments to an ALREADY RUNNING llama-server and record metrics
  report   markdown table from results.jsonl

Data (personal!) lives in ~/OmiArchive/cache/brainbench, never in the repo.
Endpoint: POST /v1/chat/completions with response_format json_schema, logprobs, and the
llama-server `timings` object (prompt_n, prompt_ms, prompt_per_second, predicted_n,
predicted_per_second, draft_n, draft_n_accepted, cache_n).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # python -I does not add the script's directory

import dates  # noqa: E402

BASE = Path.home() / "OmiArchive" / "cache" / "brainbench"
TRANSCRIPTS = Path.home() / "OmiArchive" / "transcripts"
CHARS_PER_TOKEN = 3.2
MIN_TOKENS, MAX_TOKENS_SEG = 2000, 4000
WEEKDAYS = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
VALUE_KEYS = ("chi", "cosa", "nome", "testo_data")
REF_RE = re.compile(r"^P[0-9]+$")


# --- tokens / segments ------------------------------------------------------------------

def http_json(url: str, body: dict | None = None, timeout: float = 600) -> dict:
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def count_tokens(text: str, url: str | None = None) -> int:
    if url:
        try:
            return len(http_json(url.rstrip("/") + "/tokenize", {"content": text}, 60)["tokens"])
        except Exception:  # noqa: BLE001 - fall back to the estimate
            pass
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def load_sessions(folder: Path = TRANSCRIPTS) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(folder.glob("*.jsonl")):
        for ln in f.read_text().splitlines():
            if ln.strip():
                try:
                    out[json.loads(ln)["session"]].append(json.loads(ln))
                except (ValueError, KeyError):
                    continue
    return out


def para_line(n: int, r: dict) -> str:
    via = r.get("via") or {}
    who = f"[MEDIA {via.get('app') or 'Video'}]" if via.get("kind") == "media" else (r.get("speaker") or "?")
    return f"[P{n}] {r['start'][11:16]} {who}: {r['text']}"


def build_segment(rows: list[dict], target_tokens: int, url: str | None = None) -> dict:
    """Consecutive paragraphs from the start of the session up to target_tokens."""
    rows = sorted(rows, key=lambda r: r["start"])
    lines: list[str] = []
    used: list[dict] = []
    total = 0
    for r in rows:
        ln = para_line(len(lines) + 1, r)
        n = count_tokens(ln, url)
        if used and total + n > target_tokens:
            break
        lines.append(ln)
        used.append(r)
        total += n
    names = sorted({r["speaker"] for r in used if r.get("person") and r["person"] != "me" and r.get("speaker")})
    start = used[0]["start"]
    return {"session": used[0]["session"], "start": start, "day": start[:10],
            "weekday": WEEKDAYS[datetime.fromisoformat(start).weekday()], "my_name": "Io",
            "people": names, "speakers": len({r["speaker"] for r in used}), "n_paragraphs": len(lines),
            "tokens": total, "lines": lines}


def select_segments(sessions: dict[str, list[dict]], n: int = 5, url: str | None = None, seed: int = 7) -> list[dict]:
    stats = []
    for sid, rows in sessions.items():
        chars = sum(len(r["text"]) for r in rows)
        stats.append((chars, sid, len({r["speaker"] for r in rows})))
    full = int(MIN_TOKENS * CHARS_PER_TOKEN)  # enough text for a 2,000-token segment
    pool = []
    for chars, speakers in ((full, 2), (full, 1), (1500, 2), (1500, 1)):  # best tier with enough sessions
        pool = sorted(s for s in stats if s[2] >= speakers and s[0] >= chars)
        if len(pool) >= n:
            break
    if not pool:
        return []
    rnd = random.Random(seed)
    picks = []
    for i in range(min(n, len(pool))):
        lo = len(pool) * i // min(n, len(pool))
        hi = max(lo + 1, len(pool) * (i + 1) // min(n, len(pool)))
        picks.append(pool[rnd.randrange(lo, hi)])
    targets = [MIN_TOKENS + (MAX_TOKENS_SEG - MIN_TOKENS) * i // max(1, len(picks) - 1) for i in range(len(picks))]
    segs = []
    for (chars, sid, _), target in zip(picks, targets):
        seg = build_segment(sessions[sid], min(target, MAX_TOKENS_SEG), url)
        seg["id"] = f"s{len(segs) + 1}-{sid[:8]}"
        segs.append(seg)
    return segs


def system_prompt(seg: dict) -> str:
    return (HERE / "prompt.md").read_text().format(
        my_name=seg["my_name"], weekday=seg["weekday"], day=seg["day"],
        people=", ".join(seg["people"]) or "nessuna oltre all'utente")


# --- validation -------------------------------------------------------------------------

def _type_ok(v, t) -> bool:
    ts = t if isinstance(t, list) else [t]
    for x in ts:
        if (x == "string" and isinstance(v, str)) or (x == "null" and v is None) or \
           (x == "array" and isinstance(v, list)) or (x == "object" and isinstance(v, dict)) or \
           (x == "integer" and isinstance(v, int) and not isinstance(v, bool)) or \
           (x == "number" and isinstance(v, (int, float)) and not isinstance(v, bool)) or \
           (x == "boolean" and isinstance(v, bool)):
            return True
    return False


def validate(schema: dict, data, path: str = "$") -> list[str]:
    """Errors for the JSON Schema subset used here (type, properties, required, items,
    maxItems, maxLength, pattern, enum)."""
    try:
        import jsonschema
        v = jsonschema.Draft202012Validator(schema)
        return [f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in v.iter_errors(data)]
    except ImportError:
        pass
    errs: list[str] = []
    if "type" in schema and not _type_ok(data, schema["type"]):
        return [f"{path}: wrong type"]
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: not in enum")
    if isinstance(data, str):
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            errs.append(f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errs.append(f"{path}: pattern")
    if isinstance(data, list):
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errs.append(f"{path}: too many items")
        for i, x in enumerate(data):
            errs += validate(schema.get("items", {}), x, f"{path}[{i}]")
    if isinstance(data, dict):
        for k in schema.get("required", []):
            if k not in data:
                errs.append(f"{path}: missing {k}")
        for k, sub in schema.get("properties", {}).items():
            if k in data:
                errs += validate(sub, data[k], f"{path}.{k}")
    return errs


def collect_refs(node) -> list:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += (v if isinstance(v, list) else [v]) if k == "refs" else collect_refs(v)
    elif isinstance(node, list):
        for x in node:
            out += collect_refs(x)
    return out


def refs_valid(data, n_paragraphs: int) -> bool:
    return all(isinstance(r, str) and REF_RE.match(r) and 1 <= int(r[1:]) <= n_paragraphs
               for r in collect_refs(data))


def add_iso(data: dict, reference: datetime) -> None:
    """Code, not the model, resolves dates."""
    def iso(text):
        d = dates.resolve(text, reference) if isinstance(text, str) else None
        return d.isoformat() if d else None
    for i in data.get("impegni_e_todo") or []:
        if isinstance(i, dict):
            i["scadenza_iso"] = iso(i.get("scadenza_testo"))
    for i in data.get("date_ed_eventi_citati") or []:
        if isinstance(i, dict):
            i["data_iso"] = iso(i.get("testo_data"))


# --- confidence from logprobs ------------------------------------------------------------

def confidence(text: str, tokens: list[dict]) -> dict:
    """Mean and min token probability over tokens inside the string VALUES of chi, cosa,
    nome, testo_data. Tokens are mapped to the generated text through their UTF-8 byte
    offsets; if no such value is found, all tokens are used (scope = "all")."""
    if not tokens:
        return {"mean": None, "min": None, "n": 0, "scope": "none"}
    spans = []
    raw = text.encode()
    for m in re.finditer(r'"(%s)"\s*:\s*"((?:[^"\\]|\\.)*)"' % "|".join(VALUE_KEYS), text):
        spans.append((len(text[:m.start(2)].encode()), len(text[:m.end(2)].encode())))
    probs_all, probs_val, pos = [], [], 0
    for t in tokens:
        b = t.get("bytes")
        size = len(b) if isinstance(b, list) else len(str(t.get("token", "")).encode())
        p = math.exp(t["logprob"]) if t.get("logprob") is not None else None
        if p is not None:
            probs_all.append(p)
            if any(pos < e and pos + size > s for s, e in spans):
                probs_val.append(p)
        pos += size
    del raw
    probs, scope = (probs_val, "values") if probs_val else (probs_all, "all")
    if not probs:
        return {"mean": None, "min": None, "n": 0, "scope": "none"}
    return {"mean": statistics.fmean(probs), "min": min(probs), "n": len(probs), "scope": scope}


# --- memory ------------------------------------------------------------------------------

class RssSampler(threading.Thread):
    def __init__(self, pid: int | None, every: float = 0.5):
        super().__init__(daemon=True)
        self.pid, self.every, self.max_kb, self._stop_ev = pid, every, 0, threading.Event()

    def run(self):
        while self.pid and not self._stop_ev.is_set():
            try:
                out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.pid)], capture_output=True, text=True).stdout
                self.max_kb = max(self.max_kb, int(out.strip() or 0))
            except Exception:  # noqa: BLE001
                pass
            self._stop_ev.wait(self.every)

    def stop(self) -> float:
        self._stop_ev.set()
        return self.max_kb / 1024 / 1024


def to_gb(value: str) -> float | None:
    m = re.match(r"([\d.]+)\s*([KMGT]?)", value.strip())
    if not m:
        return None
    return float(m[1]) * {"": 1 / 1024**3, "K": 1 / 1024**2, "M": 1 / 1024, "G": 1, "T": 1024}[m[2]]


def parse_vmmap(text: str) -> dict:
    out = {}
    if m := re.search(r"^Physical footprint:\s*(\S+)", text, re.M):
        out["footprint_gb"] = to_gb(m[1])
    if m := re.search(r"^Physical footprint \(peak\):\s*(\S+)", text, re.M):
        out["footprint_peak_gb"] = to_gb(m[1])
    return out


def vmmap(pid: int | None) -> dict:
    if not pid:
        return {}
    try:
        return parse_vmmap(subprocess.run(["vmmap", "--summary", str(pid)], capture_output=True, text=True,
                                          timeout=60).stdout)
    except Exception:  # noqa: BLE001
        return {}


# --- running -----------------------------------------------------------------------------

def chat(url: str, system: str, user: str, schema: dict, max_tokens: int = 1500) -> dict:
    return http_json(url.rstrip("/") + "/v1/chat/completions", {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.1, "max_tokens": max_tokens, "stream": False, "cache_prompt": True,
        "logprobs": True, "top_logprobs": 1,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "nota", "strict": True, "schema": schema}},
    })


def run_segment(url: str, seg: dict, schema: dict, out_file: Path | None = None) -> dict:
    t0 = time.monotonic()
    resp = chat(url, system_prompt(seg), "\n".join(seg["lines"]), schema)
    wall = time.monotonic() - t0
    choice = (resp.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    tm = resp.get("timings") or {}
    toks = ((choice.get("logprobs") or {}).get("content")) or []
    draft_n, acc = tm.get("draft_n"), tm.get("draft_n_accepted")
    row = {"kind": "segment", "segment": seg["id"], "segment_tokens": seg["tokens"], "wall_s": round(wall, 3),
           "prompt_n": tm.get("prompt_n"), "cache_n": tm.get("cache_n"),
           "prompt_tps": tm.get("prompt_per_second"),
           "predicted_n": tm.get("predicted_n", (resp.get("usage") or {}).get("completion_tokens")),
           "decode_tps": tm.get("predicted_per_second"), "draft_n": draft_n, "draft_accepted": acc,
           "acceptance": (acc / draft_n) if draft_n else None,
           "finish_reason": choice.get("finish_reason")}
    parsed = None
    try:
        parsed = json.loads(text)
        row["json_ok"] = isinstance(parsed, dict)
    except ValueError:
        row["json_ok"] = False
    row["schema_ok"] = bool(row["json_ok"]) and not validate(schema, parsed)
    row["refs_ok"] = bool(row["json_ok"]) and refs_valid(parsed, seg["n_paragraphs"])
    c = confidence(text, toks)
    row.update(conf_mean=c["mean"], conf_min=c["min"], conf_scope=c["scope"], conf_tokens=c["n"])
    if row["json_ok"]:
        add_iso(parsed, datetime.fromisoformat(seg["start"]))
    if out_file:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps({"raw": text, "parsed": parsed, "timings": tm}, ensure_ascii=False, indent=1))
    return row


def prefix_test(url: str, segs: list[dict], schema: dict) -> dict:
    """Same system prompt, different user text: how much of the 2nd prompt was recomputed?"""
    a, b = segs[0], segs[min(1, len(segs) - 1)]
    system = system_prompt(a)
    chat(url, system, "\n".join(a["lines"]), schema, max_tokens=8)
    r = chat(url, system, "\n".join(b["lines"]), schema, max_tokens=8)
    tm = r.get("timings") or {}
    total = (r.get("usage") or {}).get("prompt_tokens")
    n, cache = tm.get("prompt_n"), tm.get("cache_n")
    total = total or ((n or 0) + (cache or 0)) or None
    return {"kind": "prefix", "prompt_n": n, "cache_n": cache, "total_prompt_tokens": total,
            "system_tokens_est": count_tokens(system),
            "prefix_reused": bool(total and n is not None and n < total)}


def cmd_run(a) -> int:
    base = Path(a.base)
    segs = json.loads((base / "segments.json").read_text())
    schema = json.loads((HERE / "schema.json").read_text())
    sampler = RssSampler(a.pid)
    sampler.start()
    rows = []
    for seg in segs:
        row = run_segment(a.url, seg, schema, base / "out" / re.sub(r"\W+", "_", a.label) / f"{seg['id']}.json")
        rows.append(row)
        print(f"{seg['id']}: {row['wall_s']}s prompt {row['prompt_n']}@{row['prompt_tps']} "
              f"decode {row['predicted_n']}@{row['decode_tps']} json={row['json_ok']} schema={row['schema_ok']} "
              f"refs={row['refs_ok']}", flush=True)
    if a.repeat_prefix:
        rows.append(prefix_test(a.url, segs, schema))
        print("prefix:", rows[-1])
    rss_gb = sampler.stop()
    mem = {"rss_max_gb": rss_gb, **vmmap(a.pid)}
    stamp = datetime.now().isoformat(timespec="seconds")
    with (base / "results.jsonl").open("a") as f:
        for row in rows:
            f.write(json.dumps({"label": a.label, "ts": stamp, **row, **mem}, ensure_ascii=False) + "\n")
    print("memory:", mem)
    return 0


def cmd_select(a) -> int:
    base = Path(a.base)
    sessions = load_sessions(Path(a.transcripts))
    segs = select_segments(sessions, a.n, a.url, a.seed)
    base.mkdir(parents=True, exist_ok=True)
    (base / "segments.json").write_text(json.dumps(segs, ensure_ascii=False, indent=1))
    for s in segs:
        print(f"{s['id']}  {s['day']}  {s['n_paragraphs']} paragrafi  {s['speakers']} voci  ~{s['tokens']} token")
    print(f"{len(segs)} segmenti da {len(sessions)} sessioni -> {base / 'segments.json'}")
    return 0 if segs else 1


# --- report ------------------------------------------------------------------------------

def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def fmt(x, nd=1):
    return "-" if x is None else f"{x:.{nd}f}"


def report(rows: list[dict]) -> str:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r.get("kind", "segment") == "segment":
            groups[r["label"]].append(r)
    head = ("| config | n | prompt tok/s | decode tok/s | accettazione | wall tot s | picco GB | JSON % | schema % "
            "| refs % | confidenza |\n|---|---|---|---|---|---|---|---|---|---|---|")
    lines = [head]
    pct = lambda g, k: 100 * sum(bool(r.get(k)) for r in g) / len(g)  # noqa: E731
    for label, g in groups.items():
        peak = max([x for r in g for x in (r.get("footprint_peak_gb"), r.get("rss_max_gb")) if x] or [0]) or None
        lines.append(f"| {label} | {len(g)} | {fmt(med(r.get('prompt_tps') for r in g))} | "
                     f"{fmt(med(r.get('decode_tps') for r in g))} | {fmt(mean(r.get('acceptance') for r in g), 2)} | "
                     f"{fmt(sum(r.get('wall_s') or 0 for r in g))} | {fmt(peak, 2)} | {fmt(pct(g, 'json_ok'), 0)} | "
                     f"{fmt(pct(g, 'schema_ok'), 0)} | {fmt(pct(g, 'refs_ok'), 0)} | "
                     f"{fmt(mean(r.get('conf_mean') for r in g), 3)} |")
    return "\n".join(lines)


def cmd_report(a) -> int:
    f = Path(a.base) / "results.jsonl"
    if not f.exists():
        print("nessun risultato", file=sys.stderr)
        return 1
    print(report([json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base", default=str(BASE), help="cartella dati (fuori dal repo)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("-n", type=int, default=5)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--url", help="llama-server per contare i token con /tokenize")
    s.add_argument("--transcripts", default=str(TRANSCRIPTS))
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8090")
    r.add_argument("--label", required=True)
    r.add_argument("--pid", type=int)
    r.add_argument("--repeat-prefix", action="store_true")
    sub.add_parser("report")
    a = p.parse_args(argv)
    return {"select": cmd_select, "run": cmd_run, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
