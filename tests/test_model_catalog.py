"""Model catalog semantics (2026-09-06).

Two guarantees a picker built from `/v1/models` relies on:

1. What the list says is what runs: alias remaps are exposed as `served_as`,
   the pinned default is flagged, and the unknown-model policy is published.
2. An explicit slug the proxy cannot serve is answered with a 400, not run as
   the pinned default under the caller's chosen name. Claude Code's own default
   names (claude-*, opus, sonnet, ...) still map to the pinned default, since
   they carry no intent about a GPT slug.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import translator  # noqa: E402
from translator import UnknownModelError, annotate_model_catalog, resolve_model  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-6-astra")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-6-astra,gpt-5.6-sol,gpt-5.6-terra")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "gpt-5.6-sol=gpt-6-astra,gpt-5.4=gpt-6-astra")
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "")
    monkeypatch.delenv("CX2CC_UNKNOWN_MODEL", raising=False)
    # The upstream allowlist cache is process-global; make it empty and fresh.
    monkeypatch.setattr(translator, "_allowed_cache", {"ts": 0.0, "slugs": frozenset()})


# --- resolve_model --------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "claude-opus-4-8", "claude-fable-5-1", "claude-haiku-4-5-20251001",
    "opus", "sonnet[1m]", "fable", "haiku", "opusplan", "default", "auto", "", None,
])
def test_claude_family_names_take_the_pinned_default(name):
    assert resolve_model(name) == "gpt-6-astra"


def test_explicit_unknown_slug_is_rejected_by_default():
    with pytest.raises(UnknownModelError) as excinfo:
        resolve_model("gpt-4o")
    err = excinfo.value
    assert err.requested == "gpt-4o"
    # The message names what *can* be requested (allowlist + alias sources).
    assert set(err.allowed) >= {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4"}
    assert "gpt-4o" in str(err) and "gpt-6-astra" in str(err)


def test_unknown_slug_with_window_suffix_is_rejected_too():
    with pytest.raises(UnknownModelError):
        resolve_model("gpt-4o[1m]")


def test_legacy_policy_silently_routes_unknown_slug_to_default(monkeypatch):
    monkeypatch.setenv("CX2CC_UNKNOWN_MODEL", "default")
    assert resolve_model("gpt-4o") == "gpt-6-astra"


def test_allowlisted_and_aliased_slugs_still_resolve():
    assert resolve_model("gpt-5.6-terra") == "gpt-5.6-terra"
    assert resolve_model("gpt-5.6-sol") == "gpt-6-astra"
    assert resolve_model("gpt-5.4[1m]") == "gpt-6-astra"


def test_upstream_catalog_slugs_are_allowed_even_if_not_in_passthrough(monkeypatch):
    monkeypatch.setattr(translator, "_allowed_cache", {"ts": 9e12, "slugs": frozenset({"gpt-5.4-mini"})})
    assert resolve_model("gpt-5.4-mini") == "gpt-5.4-mini"


# --- annotate_model_catalog -----------------------------------------------------

def test_annotation_exposes_aliases_default_and_policy():
    upstream = [
        {"id": "gpt-6-astra", "object": "model", "context_window": 272000},
        {"id": "gpt-5.6-sol", "object": "model", "context_window": 272000},
        {"id": "gpt-5.6-terra", "object": "model"},
    ]
    out = annotate_model_catalog(upstream)
    by_id = {m["id"]: m for m in out["data"]}

    assert by_id["gpt-6-astra"]["is_default"] is True
    assert "served_as" not in by_id["gpt-6-astra"]
    # Listed upstream but remapped here: say so on the entry itself.
    assert by_id["gpt-5.6-sol"]["served_as"] == "gpt-6-astra"
    assert by_id["gpt-5.6-sol"]["context_window"] == 272000   # upstream fields kept
    # Alias source absent upstream (gpt-5.4 left the catalog): appended, tagged.
    assert by_id["gpt-5.4"] == {
        "id": "gpt-5.4", "object": "model", "created": 1, "owned_by": "cx2cc",
        "served_as": "gpt-6-astra", "is_default": False, "source": "alias",
    }
    assert out["default_model"] == "gpt-6-astra"
    assert out["aliases"] == {"gpt-5.6-sol": "gpt-6-astra", "gpt-5.4": "gpt-6-astra"}
    assert out["unknown_model_policy"] == "reject"


def test_annotation_without_aliases_is_a_plain_mirror(monkeypatch):
    monkeypatch.delenv("CX2CC_MODEL_ALIASES", raising=False)
    out = annotate_model_catalog([{"id": "gpt-6-astra"}, {"id": "gpt-5.6-sol"}, {"bogus": 1}])
    assert [m["id"] for m in out["data"]] == ["gpt-6-astra", "gpt-5.6-sol"]
    assert all("served_as" not in m for m in out["data"])
    assert out["aliases"] == {}


# --- server routes --------------------------------------------------------------

@pytest.fixture()
def server_module(monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_API_KEY", "")
    monkeypatch.setenv("CX2CC_UPSTREAM_API_KEYS", "")
    monkeypatch.setenv("CX2CC_USAGE_URL", "")
    if "server" in sys.modules:
        del sys.modules["server"]
    module = importlib.import_module("server")
    yield module
    if "server" in sys.modules:
        del sys.modules["server"]


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body
        self.status_code = 200

    def json(self):
        return self._body


def test_models_route_mirrors_bridge_and_annotates(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://bridge.test/v1")
    seen: dict = {}

    def fake_get(url, params=None, timeout=None, **kwargs):
        seen["url"], seen["params"] = url, params
        return _FakeResponse({
            "object": "list",
            "data": [
                {"id": "gpt-6-astra", "display_name": "GPT-6-Astra", "context_window": 272000,
                 "max_context_window": 872000, "reasoning_efforts": ["low", "ultra"], "source": "catalog"},
                {"id": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "context_window": 272000,
                 "max_context_window": 272000, "source": "catalog"},
            ],
            "default_model": "gpt-6-astra",
            "client_version": "0.153.4",
            "catalog_fetched_at": 1.0,
            "catalog_stale": False,
            "catalog_error": None,
        })

    monkeypatch.setattr(server_module.requests, "get", fake_get)
    client = server_module.app.test_client()

    body = client.get("/v1/models").get_json()
    assert seen["url"] == "https://bridge.test/v1/models" and seen["params"] is None
    ids = [m["id"] for m in body["data"]]
    assert ids == ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.4"]
    by_id = {m["id"]: m for m in body["data"]}
    assert by_id["gpt-6-astra"]["is_default"] is True
    assert by_id["gpt-6-astra"]["reasoning_efforts"] == ["low", "ultra"]
    assert by_id["gpt-5.6-sol"]["served_as"] == "gpt-6-astra"
    assert by_id["gpt-5.4"]["source"] == "alias"
    assert body["default_model"] == "gpt-6-astra"
    assert body["unknown_model_policy"] == "reject"
    # Bridge provenance is passed through untouched.
    assert body["client_version"] == "0.153.4"
    assert body["catalog_stale"] is False

    # ?refresh=1 is forwarded so the bridge can bypass its TTL.
    client.get("/v1/models?refresh=1")
    assert seen["params"] == {"refresh": "1"}


def test_models_route_falls_back_to_pinned_default_when_bridge_is_down(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://bridge.test/v1")

    def broken_get(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(server_module.requests, "get", broken_get)
    body = server_module.app.test_client().get("/v1/models").get_json()
    ids = [m["id"] for m in body["data"]]
    assert ids[0] == "gpt-6-astra"
    assert body["data"][0]["is_default"] is True
    assert body["data"][0]["source"] == "config"
    assert body["catalog_stale"] is True


def test_messages_rejects_unknown_explicit_model_with_400(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://bridge.test/v1")
    calls: list = []
    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: calls.append(1))

    resp = server_module.app.test_client().post(
        "/v1/messages",
        data=json.dumps({"model": "gpt-4o", "max_tokens": 8,
                         "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        headers={"x-api-key": "k"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert "gpt-4o" in body["error"]["message"]
    assert "gpt-6-astra" in body["error"]["message"]
    assert calls == [], "nothing must reach upstream"


def test_openai_passthrough_rejects_unknown_explicit_model_with_400(server_module, monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_BASE_URL", "https://bridge.test/v1")
    calls: list = []
    monkeypatch.setattr(server_module.requests, "post", lambda *a, **k: calls.append(1))

    resp = server_module.app.test_client().post(
        "/openai/v1/chat/completions",
        data=json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}),
        content_type="application/json",
        headers={"authorization": "Bearer k"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "gpt-4o" in body["error"]["message"]
    assert calls == []
