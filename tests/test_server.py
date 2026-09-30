import importlib
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def server_module(monkeypatch):
    # Set to empty rather than delete: importing server re-runs load_dotenv,
    # which fills in deleted variables from a real .env sitting next to
    # server.py (override=False leaves existing ones alone). With a real .env
    # loaded, "unconfigured upstream" tests end up hitting a live upstream.
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "")
    monkeypatch.setenv("CX2CC_UPSTREAM_API_KEY", "")
    monkeypatch.setenv("CX2CC_UPSTREAM_API_KEYS", "")
    monkeypatch.setenv("CX2CC_USAGE_URL", "")
    # These route tests send an arbitrary "gpt-5.5" and are about upstream
    # config / key handling, not model policy. Keep the legacy silent-default
    # behaviour here; the reject policy has its own tests in test_model_catalog.
    monkeypatch.setenv("CX2CC_UNKNOWN_MODEL", "default")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "")
    if "server" in sys.modules:
        del sys.modules["server"]
    module = importlib.import_module("server")
    yield module
    if "server" in sys.modules:
        del sys.modules["server"]


def test_health_reports_unconfigured_upstream(server_module):
    client = server_module.app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json == {"status": "ok", "upstream_configured": False}


def test_health_does_not_expose_upstream_url(server_module, monkeypatch):
    private_url = "https://private-sentinel.example.test/secret-path/v1"
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", private_url)
    client = server_module.app.test_client()

    response = client.get("/health")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.json == {"status": "ok", "upstream_configured": True}
    assert private_url not in body
    assert "private-sentinel" not in body
    assert "secret-path" not in body


def test_messages_requires_upstream_url(server_module):
    client = server_module.app.test_client()

    response = client.post(
        "/v1/messages",
        json={"model": "claude-opus-4-8", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 502
    assert "CX2CC_UPSTREAM_BASE_URL" in response.json["error"]["message"]


def test_messages_requires_key_when_upstream_configured(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    client = server_module.app.test_client()

    response = client.post(
        "/v1/messages",
        json={"model": "claude-opus-4-8", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
    )

    # 401, not 502 — a caller with no credential is a client-side problem, and
    # reporting it as a gateway failure is what made every downstream
    # misconfiguration look like a cx2cc outage.
    assert response.status_code == 401
    assert response.json["error"]["type"] == "authentication_error"
    assert "No API key supplied" in response.json["error"]["message"]


def test_nonstream_success(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }

    calls = []

    def fake_post(url, json, headers, stream, timeout):
        calls.append((url, json, headers, stream, timeout))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/messages",
        json={"model": "claude-opus-4-8", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 200
    assert response.json["content"] == [{"type": "text", "text": "hello"}]
    assert calls[0][0] == "https://example.test/v1/chat/completions"
    assert calls[0][2]["Authorization"] == "Bearer key"


def test_stream_abort_emits_error_event(server_module, monkeypatch):
    """A stream that dies after its headers still has to say why.

    The upstream drops chunked responses mid-flight (urllib3 "Response ended
    prematurely"). The generator used to swallow that and just stop, leaving the
    client a truncated SSE stream with no message_stop and no error — which
    downstream renders as a bare 502 with nothing to act on.
    """
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 200
        encoding = "utf-8"

    monkeypatch.setattr(
        server_module.requests,
        "post",
        lambda url, json, headers, stream, timeout: FakeResponse(),
    )

    def exploding_stream(*args, **kwargs):
        yield "event: message_start\ndata: {}\n\n"
        raise ConnectionError("Response ended prematurely")

    monkeypatch.setattr(server_module, "stream_translate", exploding_stream)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-4-8",
            "max_tokens": 10,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-api-key": "key"},
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "event: message_start" in body
    assert "event: error" in body
    assert "ConnectionError" in body
    assert '"type": "error"' in body


def test_fallback_keys_retry_once_each(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CX2CC_UPSTREAM_API_KEYS", "bad,good")
    server_module._key_errors.clear()
    server_module._key_index = 0

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self.text = body

        def json(self):
            if self.status_code == 200:
                return {
                    "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            return {"error": {"message": self.text}}

    keys = []

    def fake_post(url, json, headers, stream, timeout):
        key = headers["Authorization"].removeprefix("Bearer ")
        keys.append(key)
        if key == "bad":
            return FakeResponse(429, "quota exhausted")
        return FakeResponse(200, "")

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/messages",
        json={"model": "claude-opus-4-8", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json["content"] == [{"type": "text", "text": "ok"}]
    assert keys == ["bad", "good"]


def test_usage_requires_upstream_url(server_module):
    client = server_module.app.test_client()

    response = client.get("/usage", headers={"x-api-key": "key"})

    assert response.status_code == 502
    assert "CX2CC_UPSTREAM_BASE_URL" in response.json["error"]["message"]


def test_usage_requires_key_when_upstream_configured(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    client = server_module.app.test_client()

    response = client.get("/usage")

    assert response.status_code == 401
    assert response.json["error"]["type"] == "authentication_error"
    assert "No API key supplied" in response.json["error"]["message"]


def test_usage_forwards_upstream_json(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    payload = {
        "plan_type": "prolite",
        "rate_limit": {"allowed": True, "primary_window": {"used_percent": 6}},
    }

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    calls = []

    def fake_get(url, headers, timeout, **kwargs):
        calls.append((url, headers, timeout))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "get", fake_get)
    client = server_module.app.test_client()

    response = client.get("/usage", headers={"x-api-key": "key"})

    assert response.status_code == 200
    assert response.json == payload
    # The /v1 suffix is stripped so the bridge root's /usage is hit.
    assert calls[0][0] == "https://example.test/usage"
    assert calls[0][1]["Authorization"] == "Bearer key"


def test_usage_v1_alias_and_explicit_url_override(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CX2CC_USAGE_URL", "https://elsewhere.test/quota")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"ok": True}

    calls = []

    def fake_get(url, headers, timeout, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "get", fake_get)
    client = server_module.app.test_client()

    response = client.get("/v1/usage", headers={"Authorization": "Bearer key"})

    assert response.status_code == 200
    assert calls == ["https://elsewhere.test/quota"]


def test_accounts_forwards_pool_view_with_query(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    payload = {"active": "pro-a", "accounts": [{"id": "pro-a", "available": True}]}

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    calls = []

    def fake_get(url, headers, timeout, **kwargs):
        calls.append((url, kwargs.get("params")))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "get", fake_get)
    client = server_module.app.test_client()

    response = client.get("/accounts?usage=0", headers={"x-api-key": "key"})

    assert response.status_code == 200
    assert response.json == payload
    assert calls[0][0] == "https://example.test/accounts"
    # The query string travels on, so ?usage=0 / ?account=<id> keep working
    # through the proxy.
    assert dict(calls[0][1]) == {"usage": "0"}


def test_usage_upstream_error_body_is_not_exposed(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    secret = "USAGE_SECRET_SENTINEL_456"

    class FakeResponse:
        status_code = 404
        text = secret

        def json(self):
            return {"error": secret}

    monkeypatch.setattr(server_module.requests, "get", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.get("/usage", headers={"x-api-key": "key"})

    assert response.status_code == 404
    assert secret not in response.get_data(as_text=True)
    assert response.json["error"]["message"] == "Upstream request failed with status 404"


def test_usage_bad_key_forwards_401_without_fallback(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    server_module._key_errors.clear()
    server_module._key_index = 0

    class FakeResponse:
        status_code = 401
        text = "unauthorized"

    monkeypatch.setattr(server_module.requests, "get", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.get("/usage", headers={"x-api-key": "wrong"})

    assert response.status_code == 401


def test_upstream_error_body_is_not_exposed_or_logged(server_module, monkeypatch, caplog):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://private-sentinel.example.test/secret-path/v1")
    secret = "ACCOUNT_SECRET_SENTINEL_123"

    class FakeResponse:
        status_code = 400
        text = secret

        def json(self):
            return {"error": {"message": secret}}

    monkeypatch.setattr(server_module.requests, "post", lambda *args, **kwargs: FakeResponse())
    client = server_module.app.test_client()

    with caplog.at_level("ERROR", logger="cx2cc"):
        response = client.post(
            "/v1/messages",
            json={"model": "claude-opus-4-8", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "key"},
        )

    response_body = response.get_data(as_text=True)
    assert response.status_code == 400
    assert response.json["error"]["message"] == "Upstream request failed with status 400"
    assert secret not in response_body
    assert secret not in caplog.text
    assert "private-sentinel" not in caplog.text
    assert "secret-path" not in caplog.text


def test_openai_chat_completions_requires_upstream_url(server_module):
    client = server_module.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 502
    # OpenAI-shape envelope: `{"error": {...}}` with no top-level `type`.
    assert "type" not in response.json
    assert "CX2CC_UPSTREAM_BASE_URL" in response.json["error"]["message"]


def test_openai_chat_completions_requires_key(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    client = server_module.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 401
    assert response.json["error"]["type"] == "authentication_error"
    assert "No API key supplied" in response.json["error"]["message"]


def test_openai_chat_completions_nonstream_proxies_body(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-5.5")
    upstream_payload = {
        "id": "chatcmpl-xyz",
        "object": "chat.completion",
        "model": "gpt-5.5",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "hi back"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }

    class FakeResponse:
        status_code = 200

        def json(self):
            return upstream_payload

    calls = []

    def fake_post(url, json, headers, stream, timeout):
        calls.append((url, json, headers, stream, timeout))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "messages": [
                {"role": "system", "content": "you are helpful"},
                {"role": "user", "content": "hi"},
            ],
            "temperature": 0.4,
        },
        headers={"x-api-key": "key"},
    )

    # Body is passed through verbatim — no Anthropic-shape translation.
    assert response.status_code == 200
    assert response.json == upstream_payload

    # Forwarded to the upstream's /chat/completions with the caller's key.
    assert calls[0][0] == "https://example.test/v1/chat/completions"
    assert calls[0][2]["Authorization"] == "Bearer key"

    forwarded = calls[0][1]
    # Client fields survive unchanged.
    assert forwarded["temperature"] == 0.4
    assert forwarded["messages"][0]["role"] == "system"
    # Model resolution: an allowlisted model is kept as-is.
    assert forwarded["model"] == "gpt-5.5"
    # prompt_cache_key auto-attached because the client didn't send one.
    assert "prompt_cache_key" in forwarded


def test_openai_chat_completions_openai_prefix_alias(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.post(
        "/openai/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 200
    assert response.json["choices"][0]["message"]["content"] == "ok"


def test_openai_chat_completions_unknown_model_falls_back(server_module, monkeypatch):
    """Model resolution matches the Anthropic path: unknown slugs pin to upstream_model()."""
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-5.5")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    calls = []

    def fake_post(url, json, headers, stream, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    # Keep the upstream /models call from hitting the network.
    monkeypatch.setattr(server_module.requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("blocked")))
    client = server_module.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "not-in-allowlist", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 200
    assert calls[0]["model"] == "gpt-5.5"


def test_openai_chat_completions_stream_proxies_upstream_bytes(server_module, monkeypatch):
    """Streaming path forwards the upstream SSE bytes untouched (no translation)."""
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 200

        def iter_content(self, chunk_size=None):
            yield b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n'
            yield b"data: [DONE]\n\n"

    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.5",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"x-api-key": "key"},
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    # No Anthropic message_start / content_block_delta events — the OpenAI path
    # is a raw proxy, so the client sees exactly what the upstream sent.
    assert "message_start" not in body
    assert '"delta":{"content":"he"}' in body
    assert '"delta":{"content":"llo"}' in body
    assert "[DONE]" in body


def test_openai_chat_completions_falls_back_across_keys(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CX2CC_UPSTREAM_API_KEYS", "bad,good")
    server_module._key_errors.clear()
    server_module._key_index = 0

    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self.text = body

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    keys_used = []

    def fake_post(url, json, headers, stream, timeout):
        key = headers["Authorization"].removeprefix("Bearer ")
        keys_used.append(key)
        if key == "bad":
            return FakeResponse(429, "quota exhausted")
        return FakeResponse(200, "")

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert keys_used == ["bad", "good"]


def test_responses_requires_upstream_url(server_module):
    client = server_module.app.test_client()

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.6-sol", "input": [{"type": "message", "role": "user",
                                                   "content": [{"type": "input_text", "text": "hi"}]}]},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 502
    # OpenAI-shape envelope (no top-level "type") — the /openai path uses that shape.
    assert "type" not in response.json
    assert "CX2CC_UPSTREAM_BASE_URL" in response.json["error"]["message"]


def test_responses_requires_key(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    client = server_module.app.test_client()

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.6-sol", "input": []},
    )

    assert response.status_code == 401
    assert response.json["error"]["type"] == "authentication_error"


def test_responses_stream_proxies_upstream_sse(server_module, monkeypatch):
    """Streaming path forwards codex-bridge SSE bytes untouched (no translation)."""
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 200

        def iter_content(self, chunk_size=None):
            yield b'data: {"type":"response.output_text.delta","delta":"he"}\n\n'
            yield b'data: {"type":"response.output_text.delta","delta":"llo"}\n\n'
            yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":2,"total_tokens":7}}}\n\n'
            yield b"data: [DONE]\n\n"

    calls = []

    def fake_post(url, json, headers, stream, timeout):
        calls.append((url, json, headers, stream))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-5.6-sol",
            "stream": True,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
        },
        headers={"x-api-key": "key"},
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    # Forwarded to the /responses tail of the configured base URL.
    assert calls[0][0] == "https://example.test/v1/responses"
    # No chat.completion translation — the raw Responses SSE events pass through.
    assert '"type":"response.output_text.delta"' in body
    assert '"delta":"he"' in body
    assert '"delta":"llo"' in body
    assert '"response.completed"' in body
    assert "[DONE]" in body


def test_responses_openai_prefix_alias(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 200

        def iter_content(self, chunk_size=None):
            yield b"data: [DONE]\n\n"

    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.post(
        "/openai/v1/responses",
        json={"model": "gpt-5.6-sol", "input": []},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 200
    assert b"[DONE]" in response.get_data()


def test_responses_falls_back_across_keys(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("CX2CC_UPSTREAM_API_KEYS", "bad,good")
    server_module._key_errors.clear()
    server_module._key_index = 0

    class FakeResponse:
        def __init__(self, status_code, body=b""):
            self.status_code = status_code
            self.text = body.decode() if isinstance(body, bytes) else str(body)
            self._body = body

        def iter_content(self, chunk_size=None):
            yield self._body

    keys_used = []

    def fake_post(url, json, headers, stream, timeout):
        key = headers["Authorization"].removeprefix("Bearer ")
        keys_used.append(key)
        if key == "bad":
            return FakeResponse(429, "quota exhausted")
        return FakeResponse(200, b"data: [DONE]\n\n")

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.6-sol", "input": []},
    )

    assert response.status_code == 200
    assert keys_used == ["bad", "good"]


def test_connection_exception_does_not_log_url(server_module, monkeypatch, caplog):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://private-sentinel.example.test/secret-path/v1")

    def fail(*args, **kwargs):
        raise server_module.requests.exceptions.ConnectionError(
            "failed to reach https://private-sentinel.example.test/secret-path/v1"
        )

    monkeypatch.setattr(server_module.requests, "post", fail)
    client = server_module.app.test_client()

    with caplog.at_level("WARNING", logger="cx2cc"):
        response = client.post(
            "/v1/messages",
            json={"model": "claude-opus-4-8", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "key"},
        )

    assert response.status_code == 502
    assert "ConnectionError" in caplog.text
    assert "private-sentinel" not in caplog.text
    assert "secret-path" not in caplog.text


# --- /v1/images/generations passthrough -------------------------------------


def test_images_requires_upstream_url(server_module):
    client = server_module.app.test_client()

    response = client.post(
        "/v1/images/generations", json={"prompt": "a cat"}, headers={"x-api-key": "key"}
    )

    assert response.status_code == 502
    assert "CX2CC_UPSTREAM_BASE_URL" in response.json["error"]["message"]


def test_images_requires_key_when_upstream_configured(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    client = server_module.app.test_client()

    response = client.post("/v1/images/generations", json={"prompt": "a cat"})

    assert response.status_code == 401
    assert response.json["error"]["type"] == "authentication_error"


def test_images_forwards_body_and_returns_upstream_json(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    payload = {
        "created": 1,
        "data": [{"b64_json": "AAAA"}],
        "output_format": "png",
        "size": "1254x1254",
        "model": "gpt-image-2",
    }
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    def fake_post(url, json, headers, stream, timeout):
        calls.append((url, json, headers, stream, timeout))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    body = {"prompt": "a cat", "size": "1024x1024", "quality": "low", "n": 2}
    response = client.post(
        "/v1/images/generations", json=body, headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert response.json == payload
    url, sent, headers, stream, timeout = calls[0]
    assert url == "https://example.test/v1/images/generations"
    # Body travels unchanged: the bridge owns the model choice and the `n` loop.
    assert sent == body
    assert headers["Authorization"] == "Bearer secret"
    assert stream is False
    assert timeout >= 180


def test_images_openai_alias_route(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": []}

    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.post(
        "/openai/v1/images/generations", json={"prompt": "x"}, headers={"x-api-key": "key"}
    )

    assert response.status_code == 200
    assert response.json == {"data": []}


def test_images_bridge_error_message_is_forwarded(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 400
        text = '{"error": {"message": "prompt is required", "type": "bridge_error", "code": 400}}'

        def json(self):
            return {"error": {"message": "prompt is required", "type": "bridge_error", "code": 400}}

    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.post("/v1/images/generations", json={}, headers={"x-api-key": "key"})

    assert response.status_code == 400
    assert response.json["error"]["message"] == "prompt is required"
    assert response.json["error"]["type"] == "api_error"


def test_images_generic_upstream_error_body_is_not_exposed(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    secret = "IMAGES_SECRET_SENTINEL_789"

    class FakeResponse:
        status_code = 500
        text = secret

        def json(self):
            return {"detail": secret}

    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.post(
        "/v1/images/generations", json={"prompt": "x"}, headers={"x-api-key": "key"}
    )

    assert response.status_code == 500
    assert secret not in response.get_data(as_text=True)


# --- /v1/images/edits passthrough (reference images) ------------------------


def test_images_edits_forwards_images_list_to_edits_url(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    payload = {"created": 2, "data": [{"b64_json": "BBBB"}], "size": "1024x1536"}
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    def fake_post(url, json, headers, stream, timeout):
        calls.append((url, json, stream, timeout))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    body = {
        "prompt": "put the animal from image 1 into a forest",
        "images": [{"image_url": "data:image/png;base64,AAAA"}, "BBBB"],
        "size": "1024x1536",
    }
    response = client.post("/v1/images/edits", json=body, headers={"x-api-key": "key"})

    assert response.status_code == 200
    assert response.json == payload
    url, sent, stream, timeout = calls[0]
    assert url == "https://example.test/v1/images/edits"
    # Reference images travel untouched; normalisation is the bridge's job.
    assert sent == body
    assert stream is False
    assert timeout >= 180


def test_images_edits_openai_alias_route(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": []}

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/openai/v1/images/edits", json={"prompt": "x", "images": ["AAAA"]},
        headers={"x-api-key": "key"},
    )

    assert response.status_code == 200
    assert calls == ["https://example.test/v1/images/edits"]


def test_images_edits_requires_key_when_upstream_configured(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    client = server_module.app.test_client()

    response = client.post("/v1/images/edits", json={"prompt": "x", "images": ["AAAA"]})

    assert response.status_code == 401


# --- /v1/alpha/search passthrough (Codex standalone web search) -------------


SEARCH_BODY = {
    "id": "search-session",
    "model": "gpt-6-astra",
    "input": [{"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": "Search the web"}]}],
    "commands": {"search_query": [{"q": "standalone web search"}]},
    "settings": {"allowed_callers": ["direct"]},
}


def test_alpha_search_forwards_body_and_returns_upstream_json(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    payload = {
        "encrypted_output": "ciphertext",
        "output": "Search result",
        "results": [{"type": "text_result", "ref_id": "turn0search0", "future_field": {"x": 1}}],
    }
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return payload

    def fake_post(url, json, headers, stream, timeout):
        calls.append((url, json, headers, stream))
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/v1/alpha/search", json=SEARCH_BODY, headers={"Authorization": "Bearer secret"}
    )

    assert response.status_code == 200
    assert response.json == payload
    url, sent, headers, stream = calls[0]
    assert url == "https://example.test/v1/alpha/search"
    assert sent == SEARCH_BODY
    assert headers["Authorization"] == "Bearer secret"
    assert stream is False


def test_alpha_search_openai_alias_route(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"output": "", "results": []}

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr(server_module.requests, "post", fake_post)
    client = server_module.app.test_client()

    response = client.post(
        "/openai/v1/alpha/search", json=SEARCH_BODY, headers={"x-api-key": "key"}
    )

    assert response.status_code == 200
    assert calls == ["https://example.test/v1/alpha/search"]


def test_alpha_search_requires_key_when_upstream_configured(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    client = server_module.app.test_client()

    response = client.post("/v1/alpha/search", json=SEARCH_BODY)

    assert response.status_code == 401
    assert response.json["error"]["type"] == "authentication_error"


def test_alpha_search_requires_upstream_url(server_module):
    client = server_module.app.test_client()

    response = client.post("/v1/alpha/search", json=SEARCH_BODY, headers={"x-api-key": "key"})

    assert response.status_code == 502
    assert "CX2CC_UPSTREAM_BASE_URL" in response.json["error"]["message"]


def test_alpha_search_bridge_error_message_is_forwarded(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")

    class FakeResponse:
        status_code = 429
        text = '{"error": {"message": "Upstream error 429: usage_limit_reached", "type": "bridge_error"}}'

        def json(self):
            return {"error": {"message": "Upstream error 429: usage_limit_reached", "type": "bridge_error"}}

    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: FakeResponse())
    client = server_module.app.test_client()

    response = client.post("/v1/alpha/search", json=SEARCH_BODY, headers={"x-api-key": "key"})

    assert response.status_code == 429
    assert "usage_limit_reached" in response.json["error"]["message"]
