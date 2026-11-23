"""A fake llama-server: canned chat completions with logprobs, in a thread."""

from __future__ import annotations

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


def tokens_for(text: str, low: dict[str, float] | None = None, default: float = 0.97, size: int = 4) -> list[dict]:
    """llama-server style logprob tokens: chunks of `size` characters; chunks that overlap
    an occurrence of a key of `low` get that probability."""
    low = low or {}
    marks = [(m, p) for sub, p in low.items() for m in _occurrences(text, sub)]
    out = []
    for i in range(0, len(text), size):
        chunk = text[i:i + size]
        p = default
        for (s, e), pr in marks:
            if i < e and i + len(chunk) > s:
                p = pr
        out.append({"token": chunk, "logprob": math.log(p), "bytes": list(chunk.encode()), "top_logprobs": []})
    return out


def _occurrences(text: str, sub: str):
    start = text.find(sub)
    while start != -1:
        yield (start, start + len(sub))
        start = text.find(sub, start + 1)


class FakeLlama:
    """respond(body, n) -> dict (JSON content) | (dict, {sub: prob}) | None (drop the connection)
    | int (HTTP error status). n counts calls from 0."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                n = len(outer.calls)
                outer.calls.append(body)
                got = outer.respond(body, n)
                if got is None:
                    self.connection.close()
                    return
                if isinstance(got, int):
                    self.send_response(got)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data, low = got if isinstance(got, tuple) else (got, None)
                text = json.dumps(data, ensure_ascii=False)
                payload = json.dumps({
                    "choices": [{"message": {"content": text}, "finish_reason": "stop",
                                 "logprobs": {"content": tokens_for(text, low)}}],
                    "timings": {"prompt_n": 100, "predicted_n": len(text) // 4},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
