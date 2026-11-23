"""A scripted stand-in for llama-server: each round returns the next step."""

import json


def tool(name, **args):
    return {"tool_calls": [{"id": f"call-{name}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def say(text):
    return {"content": text}


class FakeLLM:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def chat(self, messages, tools=None, tool_choice=None, on_token=None, **kw):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice})
        step = self.steps.pop(0) if self.steps else say("")
        if callable(step):
            step = step(messages)
        if on_token and step.get("content"):
            for word in step["content"].split(" "):
                on_token(word + " ")
        return {"message": {"role": "assistant", **step}, "timings": {"prompt_n": 10, "predicted_n": 5},
                "usage": {}}

    def tool_results(self):
        """The text of every tool result the model was given, in order."""
        last = self.requests[-1]["messages"]
        return [m["content"] for m in last if m["role"] == "tool"]


class FakeEmbedder:
    """Deterministic vectors: words hashed into 1024 buckets (shared words = close)."""

    def embed(self, texts):
        import hashlib
        import math
        import re
        out = []
        for t in texts:
            v = [0.0] * 1024
            for w in re.findall(r"\w{4,}", t.lower()):
                v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % 1024] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out

    def query(self, text):
        return self.embed([text])[0]
