"""llama-server: started on demand, on 127.0.0.1 only, and talked to through its
OpenAI-compatible API (http.client: no client library)."""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from brain.config import Settings, private_dir

LOAD_TIMEOUT = 300
# Host RAM for llama-server's prompt cache, in MiB: one or two long prompts.
CACHE_RAM_MIB = 1536
# Tokens the drafter proposes at a time: 3 was the best of 2, 3 and 4 on this Mac.
DRAFT_MAX = 3


class LLMError(Exception):
    pass


class Aborted(LLMError):
    """The request was cut off on purpose (a question came in): try again later."""


class LLM:
    """A client for one llama-server. abort(), from another thread, cuts off the
    request in progress: llama-server stops generating when the connection drops."""

    def __init__(self, base_url: str, timeout: float = 900, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        u = urllib.parse.urlsplit(self.base_url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self.timeout = timeout
        self.api_key = api_key
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        self._aborted = False

    def abort(self) -> bool:
        with self._lock:
            conn = self._conn
            self._aborted = conn is not None
        if conn is not None and conn.sock is not None:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return conn is not None

    def _open(self, path: str, body: dict | None, timeout: float | None) -> tuple[http.client.HTTPConnection,
                                                                                    http.client.HTTPResponse]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        with self._lock:
            self._conn, self._aborted = conn, False
        try:
            conn.request("POST" if data is not None else "GET", path, body=data, headers=headers)
            r = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            self._done(conn)
            raise self._error(path, e)
        if r.status != 200:
            text = r.read()[:500].decode(errors="replace")
            self._done(conn)
            raise LLMError(f"{path}: HTTP {r.status} {text}")
        return conn, r

    def _done(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            if self._conn is conn:
                self._conn = None
        conn.close()

    def _error(self, path: str, e: Exception) -> LLMError:
        return Aborted(f"{path}: aborted") if self._aborted else LLMError(f"{path}: {e}")

    def _request(self, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        conn, r = self._open(path, body, timeout)
        try:
            return json.loads(r.read())
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise self._error(path, e)
        finally:
            self._done(conn)

    def healthy(self) -> bool:
        try:
            return self._request("/health", timeout=2).get("status") == "ok"
        except LLMError:
            return False

    def model_alias(self) -> str | None:
        try:
            return (self._request("/v1/models", timeout=5).get("data") or [{}])[0].get("id")
        except LLMError:
            return None

    def chat(self, messages: list[dict], tools: list[dict] | None = None, tool_choice: str | None = None,
             max_tokens: int = 1024, temperature: float = 0.2, response_format: dict | None = None,
             on_token: Callable[[str], None] | None = None) -> dict:
        """One completion; returns {"message", "timings", "usage"}. With on_token,
        the answer is streamed and on_token gets each piece of text as it comes."""
        body: dict = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        if response_format:
            body["response_format"] = response_format
        if on_token is None:
            r = self._request("/v1/chat/completions", body)
            try:
                message = r["choices"][0]["message"]
            except (KeyError, IndexError):
                raise LLMError(f"unexpected answer: {str(r)[:300]}")
            return {"message": message, "timings": r.get("timings") or {}, "usage": r.get("usage") or {}}
        return self._stream({**body, "stream": True}, on_token)

    def _stream(self, body: dict, on_token: Callable[[str], None]) -> dict:
        """Server-sent events: content arrives in pieces, tool calls as argument fragments by index."""
        path = "/v1/chat/completions"
        conn, r = self._open(path, body, None)
        message: dict = {"role": "assistant", "content": ""}
        calls: dict[int, dict] = {}
        timings: dict = {}
        try:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                timings = chunk.get("timings") or timings
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        message["content"] += delta["content"]
                        on_token(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                                 "function": {"name": "", "arguments": ""}})
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["function"]["name"] += fn.get("name") or ""
                        c["function"]["arguments"] += fn.get("arguments") or ""
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise self._error(path, e)
        finally:
            self._done(conn)
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        return {"message": message, "timings": timings, "usage": {}}


class Server:
    """The chat llama-server process: one model at a time, on llm_port."""

    name = "llm"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.port = getattr(settings, f"{self.name}_port")
        self.pid_file = settings.data_dir / f"{self.name}.pid"
        self.log_file = settings.logs_dir / f"maimaind-{self.name}.log"
        # llama-server answers any web origin (CORS *): without a key, a page open
        # in the browser could use it through 127.0.0.1.
        self.key_file = settings.data_dir / "llm.key"
        self.client = LLM(f"http://127.0.0.1:{self.port}", api_key=self._key())

    def _key(self) -> str:
        try:
            return self.key_file.read_text().strip()
        except FileNotFoundError:
            private_dir(self.key_file.parent)
            key = secrets.token_urlsafe(32)
            fd = os.open(self.key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
            return key

    def pid(self) -> int | None:
        try:
            pid = int(self.pid_file.read_text())
            os.kill(pid, 0)
            return pid
        except (FileNotFoundError, ValueError, ProcessLookupError):
            return None
        except PermissionError:
            return None

    def command(self, profile: dict) -> list[str]:
        exe = shutil.which("llama-server") or "/opt/homebrew/bin/llama-server"
        cmd = [exe, "-m", str(self.settings.model_path(profile)), "--alias", profile["name"],
               "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(profile.get("ctx", 32768)), "-ngl", "99", "-fa", "on",
               # q8_0 KV cache: half the memory of f16, no measurable loss.
               "-ctk", "q8_0", "-ctv", "q8_0",
               # One conversation at a time: the whole context to it.
               "-np", "1", "--jinja", "--no-webui", "--api-key-file", str(self.key_file),
               # Prompts already read, kept in RAM to start the next request from them:
               # 8 GiB by default, which on 16 GB pushes the Mac into swap (measured:
               # +4.4 GiB after eight different prompts). The slot keeps the last one anyway.
               "--cache-ram", str(profile.get("cache_ram", CACHE_RAM_MIB)),
               "-rea", profile.get("reasoning", "auto")]
        if "reasoning_budget" in profile:
            cmd += ["--reasoning-budget", str(profile["reasoning_budget"])]
        draft = profile.get("draft")
        if draft and self.settings.model_path(draft).is_file():
            # Speculative decoding with the MTP drafter: same text, fewer passes of the big model.
            cmd += ["--model-draft", str(self.settings.model_path(draft)), "--spec-type", "draft-mtp",
                    "--spec-draft-n-max", str(profile.get("draft_max", DRAFT_MAX))]
        return cmd + list(profile.get("args", []))

    def start(self, profile: dict) -> LLM:
        """Load the model, or reuse the server if it already has it."""
        if self.client.healthy():
            alias = self.client.model_alias()
            if alias == profile["name"]:
                return self.client
            if self.pid() is None:
                raise LLMError(f"port {self.port} is taken by another server ({alias})")
            self.stop()
        path = self.settings.model_path(profile)
        if not path.is_file():
            raise LLMError(f"model {profile['name']} missing: brain model pull {profile['name']}")
        private_dir(self.settings.data_dir)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a") as log:
            log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {profile['name']}\n")
            log.flush()
            proc = subprocess.Popen(self.command(profile), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        self.pid_file.write_text(str(proc.pid))
        deadline = time.monotonic() + LOAD_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.pid_file.unlink(missing_ok=True)
                raise LLMError(f"llama-server exited ({proc.returncode}), see {self.log_file}")
            if self.client.healthy():
                return self.client
            time.sleep(0.5)
        self.stop()
        raise LLMError(f"model not loaded in {LOAD_TIMEOUT} s, see {self.log_file}")

    def stop(self) -> bool:
        pid = self.pid()
        if pid is None:
            self.pid_file.unlink(missing_ok=True)
            return False
        os.kill(pid, signal.SIGTERM)
        if not self._gone(pid, 10):
            os.kill(pid, signal.SIGKILL)
            # Freeing the GPU memory can take a while even then (measured: ~15 s), and
            # until the process is gone its port still answers.
            self._gone(pid, 30)
        self.pid_file.unlink(missing_ok=True)
        return True

    @staticmethod
    def _gone(pid: int, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def status(self) -> dict:
        return {"running": self.client.healthy(), "pid": self.pid(), "model": self.client.model_alias(),
                "port": self.port, "log": str(self.log_file)}


def footprint_gib(pid: int) -> float | None:
    """The process's physical footprint (Metal buffers included), from footprint(1)."""
    r = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True)
    m = re.search(r"phys_footprint:\s*([\d.]+)\s*([KMG])B", r.stdout)
    if not m:
        return None
    return round(float(m.group(1)) / {"K": 2**20, "M": 2**10, "G": 1}[m.group(2)], 2)


class EmbedServer(Server):
    """The embedding llama-server, on embed_port: small (~0.7 GB), next to the chat one."""

    name = "embed"

    def profile(self) -> dict:
        name = self.settings.embed_model
        return {"name": name, **self.settings.embed_models[name]}

    def command(self, profile: dict) -> list[str]:
        exe = shutil.which("llama-server") or "/opt/homebrew/bin/llama-server"
        # GPU memory is shared with the chat model (~7.3 GiB) within the ~11.8 GiB macOS
        # gives the GPU on 16 GB: the compute buffer grows with -ub (~600 MiB per 1,024
        # tokens), the KV cache with -c. One text is at most ~700 tokens (MAX_CHARS).
        # Measured: 1.4 GiB, instead of 2.7 with -c 8192 -ub 8192 (that ran out of memory).
        return [exe, "-m", str(self.settings.model_path(profile)), "--alias", profile["name"],
                "--host", "127.0.0.1", "--port", str(self.port), "-c", str(profile.get("ctx", 2048)),
                "-b", "1024", "-ub", "1024", "-fa", "on", "-ngl", "99", "--embedding", "--pooling", "last",
                "--cache-ram", "0", "--no-webui", "--api-key-file", str(self.key_file)] + list(profile.get("args", []))

    def start(self, profile: dict | None = None) -> LLM:
        return super().start(profile or self.profile())


class Reranker:
    """The reranking llama-server's /v1/rerank: a cross-encoder reads the question
    and each candidate together and says how well the candidate answers it."""

    def __init__(self, llm: LLM):
        self.llm = llm

    def scores(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        r = self.llm._request("/v1/rerank", {"query": query, "documents": texts}, timeout=60)
        out = [0.0] * len(texts)
        for item in r.get("results") or []:
            out[item["index"]] = float(item["relevance_score"])
        return out


class RerankServer(EmbedServer):
    """The reranker (Qwen3-Reranker-0.6B, ~0.7 GB), on rerank_port. Several
    candidates are scored at once: -np slots in one unified KV cache."""

    name = "rerank"

    def profile(self) -> dict:
        name = self.settings.rerank_model
        return {"name": name, **self.settings.rerank_models[name]}

    def command(self, profile: dict) -> list[str]:
        exe = shutil.which("llama-server") or "/opt/homebrew/bin/llama-server"
        # A candidate is ~350 tokens (RERANK_CHARS, the question, the model's template),
        # more with numbers or OCR noise, and one longer than a physical batch fails the
        # whole request: batches of 1,024, 4 slots. Measured: 1.6 GiB of GPU memory.
        return [exe, "-m", str(self.settings.model_path(profile)), "--alias", profile["name"],
                "--host", "127.0.0.1", "--port", str(self.port), "-c", str(profile.get("ctx", 4096)),
                "-np", "4", "--kv-unified", "-b", "2048", "-ub", "1024", "-fa", "on", "-ngl", "99",
                "--rerank", "--pooling", "rank", "--cache-ram", "0", "--no-webui", "--api-key-file", str(self.key_file)] \
            + list(profile.get("args", []))
