"""A fake embedding llama-server: hashed bag of words -> 1024 dims (unnormalized)."""

import hashlib
import json
import re
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np


def vec(text: str, dim: int = 1024) -> list[float]:
    v = np.zeros(dim)
    text = unicodedata.normalize("NFD", text.casefold())
    for w in re.findall(r"\w+", text):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 512] += 1.0  # only the first 512 dims carry meaning
    v[600] = 0.5  # junk the truncation drops
    return (v * 3.0).tolist()


class FakeEmbedServer:
    def __init__(self):
        self.requests: list[list[str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                inputs = body["input"]
                outer.requests.append(inputs)
                data = [{"index": i, "embedding": vec(t)} for i, t in enumerate(inputs)]
                out = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def embedded_texts(self) -> int:
        return sum(len(r) for r in self.requests)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
