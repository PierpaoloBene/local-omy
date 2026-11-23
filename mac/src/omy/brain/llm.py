"""Client of a running llama-server (OpenAI-style /v1/chat/completions), stdlib only.

A connection error (server not there, or killed in the middle of a request when
the worker preempts the organizer) raises LLMUnavailable; the worker turns it
into a requeue. Any other failure (HTTP error, bad body) is LLMError.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

TIMEOUT = 600


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """The server cannot be reached or dropped the request."""


@dataclass
class LLMResult:
    text: str
    data: dict | None
    tokens: list[dict] = field(default_factory=list)  # {"token", "logprob", "bytes"}
    timings: dict = field(default_factory=dict)
    finish_reason: str = ""


def chat(base_url: str, system: str, user: str, schema: dict, max_tokens: int = 1500) -> LLMResult:
    body = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.1, "max_tokens": max_tokens, "stream": False, "cache_prompt": True,
        "logprobs": True, "top_logprobs": 1,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "nota", "strict": True, "schema": schema}},
    }
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"llama-server HTTP {e.code}: {e.read()[:200]!r}") from e
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException, socket.timeout, OSError) as e:
        raise LLMUnavailable(str(e)) from e
    except ValueError as e:
        raise LLMError(f"invalid response body: {e}") from e
    choice = (resp.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""  # unstripped: token offsets map onto it
    try:
        data = json.loads(text)
        data = data if isinstance(data, dict) else None
    except ValueError:
        data = None
    return LLMResult(text=text, data=data, tokens=((choice.get("logprobs") or {}).get("content")) or [],
                     timings=resp.get("timings") or {}, finish_reason=choice.get("finish_reason") or "")
