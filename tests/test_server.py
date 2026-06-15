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
    monkeypatch.delenv("CX2CC_UPSTREAM_BASE_URL", raising=False)
    monkeypatch.delenv("CX2CC_UPSTREAM_API_KEY", raising=False)
    monkeypatch.delenv("CX2CC_UPSTREAM_API_KEYS", raising=False)
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
    assert response.json["status"] == "ok"
    assert response.json["upstream_configured"] is False


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

    assert response.status_code == 502
    assert "No upstream API key" in response.json["error"]["message"]


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


def test_fallback_keys_retry_once_each(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://example.test/v1")
    server_module._FALLBACK_API_KEYS[:] = ["bad", "good"]
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
