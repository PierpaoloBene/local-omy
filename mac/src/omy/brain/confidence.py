"""Confidence of the extracted items from the token logprobs of the generated JSON.

The generated text is scanned (a small JSON scanner, no model) recording where each
string VALUE sits (byte offsets, path like ("impegni_e_todo", 0, "cosa")). Each token
is mapped to its byte offsets in the output; an item's confidence is the mean and
minimum token probability over the tokens that overlap its string values (refs are
not counted: they are ids, not content).
"""

from __future__ import annotations

import math
import statistics

THRESHOLD = 0.40
# Only these fields are listed in da_rivedere when their minima falls under THRESHOLD.
REVIEW_FIELDS = ("impegni_e_todo", "date_ed_eventi_citati", "entita.persone")
SKIP_KEYS = {"refs"}


def value_spans(text: str) -> list[tuple[tuple, int, int]]:
    """(path, start, end) byte offsets of the content of every string value (not keys)."""
    offs = [0]
    for ch in text:
        offs.append(offs[-1] + len(ch.encode()))
    n = len(text)
    out: list[tuple[tuple, int, int]] = []

    def ws(i):
        while i < n and text[i] in " \t\r\n":
            i += 1
        return i

    def string(i):  # text[i] == '"'; returns (content_start, content_end, next_i)
        j = i + 1
        while j < n and text[j] != '"':
            j += 2 if text[j] == "\\" else 1
        return i + 1, j, j + 1

    def value(i, path):
        i = ws(i)
        if i >= n:
            return i
        c = text[i]
        if c == '"':
            s, e, i = string(i)
            out.append((path, offs[min(s, n)], offs[min(e, n)]))
            return i
        if c == "{":
            i = ws(i + 1)
            while i < n and text[i] != "}":
                if text[i] != '"':
                    return n  # malformed (truncated or not JSON): stop
                s, e, i = string(i)
                key = text[s:e]
                i = ws(i)
                if i < n and text[i] == ":":
                    i = value(i + 1, path + (key,))
                i = ws(i)
                if i < n and text[i] == ",":
                    i = ws(i + 1)
            return i + 1
        if c == "[":
            i, k = ws(i + 1), 0
            while i < n and text[i] != "]":
                i = value(i, path + (k,))
                k += 1
                i = ws(i)
                if i < n and text[i] == ",":
                    i = ws(i + 1)
            return i + 1
        while i < n and text[i] not in ",}] \t\r\n":  # number, true, false, null
            i += 1
        return i

    value(0, ())
    return out


def token_probs(tokens: list[dict]) -> list[tuple[int, int, float]]:
    """(byte start, byte end, probability) of each token."""
    out, pos = [], 0
    for t in tokens or []:
        b = t.get("bytes")
        size = len(b) if isinstance(b, list) else len(str(t.get("token", "")).encode())
        lp = t.get("logprob")
        if lp is not None:
            out.append((pos, pos + size, math.exp(min(lp, 0.0))))
        pos += size
    return out


def _stats(probs: list[float]) -> dict | None:
    if not probs:
        return None
    return {"media": round(statistics.fmean(probs), 3), "minima": round(min(probs), 3)}


def _path_str(path: tuple) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return out


def item_confs(text: str, tokens: list[dict]) -> dict[str, dict]:
    """"impegni_e_todo[0]" / "entita.persone[1]" / ... -> {"media", "minima"}, for every list item
    of the output; items whose values no token covers are left out."""
    tp = token_probs(tokens)
    if not tp:
        return {}
    groups: dict[str, list[float]] = {}
    for path, s, e in value_spans(text):
        if any(p in SKIP_KEYS for p in path if isinstance(p, str)):
            continue
        idx = next((i for i, p in enumerate(path) if isinstance(p, int)), None)
        if idx is None:
            continue
        key = _path_str(path[:idx + 1])
        groups.setdefault(key, []).extend(p for ts, te, p in tp if ts < e and te > s)
    return {k: c for k, v in groups.items() if (c := _stats(v))}


def note_conf(text: str, tokens: list[dict]) -> dict | None:
    """Mean and minimum over all string values of the output (refs excluded)."""
    tp = token_probs(tokens)
    spans = [(s, e) for path, s, e in value_spans(text) if not any(p in SKIP_KEYS for p in path if isinstance(p, str))]
    return _stats([p for ts, te, p in tp if any(ts < e and te > s for s, e in spans)])


def merge_conf(a: dict | None, b: dict | None) -> dict | None:
    """Two readings of the same item (merged duplicates): the weaker one."""
    if not a or not b:
        return a or b
    return {"media": round((a["media"] + b["media"]) / 2, 3), "minima": min(a["minima"], b["minima"])}


def combine(confs: list[dict]) -> dict | None:
    """Note-level confidence from item-level ones: mean of means, min of minima."""
    confs = [c for c in confs if c]
    if not confs:
        return None
    return {"media": round(statistics.fmean(c["media"] for c in confs), 3), "minima": min(c["minima"] for c in confs)}


def da_rivedere(note: dict, threshold: float = THRESHOLD) -> list[dict]:
    out = []
    for field in REVIEW_FIELDS:
        node = note
        for k in field.split("."):
            node = node.get(k, []) if isinstance(node, dict) else []
        for i, item in enumerate(node if isinstance(node, list) else []):
            c = item.get("conf") if isinstance(item, dict) else None
            if c and c.get("minima") is not None and c["minima"] < threshold:
                out.append({"campo": f"{field}[{i}]", "motivo": f"confidenza {c['minima']:.2f} < {threshold:.2f}"})
    return out
