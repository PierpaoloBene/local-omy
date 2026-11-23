import socket

import pytest
from fake_llama import FakeLlama

from omy.brain import llm

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def test_chat_parses_result_and_sends_the_documented_request():
    with FakeLlama(lambda body, n: {"a": "ciao"}) as srv:
        r = llm.chat(srv.url, "sys", "user", SCHEMA, max_tokens=123)
    assert r.data == {"a": "ciao"} and r.finish_reason == "stop"
    assert r.tokens and set(r.tokens[0]) >= {"token", "logprob", "bytes"}
    assert r.timings["prompt_n"] == 100
    body = srv.calls[0]
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert body["temperature"] == 0.1 and body["max_tokens"] == 123
    assert body["logprobs"] is True and body["top_logprobs"] == 1 and body["cache_prompt"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA


def test_connection_refused_is_unavailable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(llm.LLMUnavailable):
        llm.chat(f"http://127.0.0.1:{port}", "s", "u", SCHEMA)


def test_dropped_connection_is_unavailable():
    with FakeLlama(lambda body, n: None) as srv, pytest.raises(llm.LLMUnavailable):
        llm.chat(srv.url, "s", "u", SCHEMA)


def test_http_error_is_llm_error_not_unavailable():
    with FakeLlama(lambda body, n: 500) as srv, pytest.raises(llm.LLMError) as e:
        llm.chat(srv.url, "s", "u", SCHEMA)
    assert not isinstance(e.value, llm.LLMUnavailable)
