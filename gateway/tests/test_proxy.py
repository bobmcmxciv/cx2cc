import asyncio
import json

from conftest import UPSTREAM_TOKEN

from cx2cc_gateway.db import now_ms

KEY = "sk-cx2cc-test-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "sk-cx2cc-test-key-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"


async def test_missing_key_is_rejected_without_calling_upstream(gw):
    r = await gw.client.post("/api/v1/messages", json={"model": "x", "messages": []})
    assert r.status == 401
    body = await r.json()
    assert body["type"] == "error" and body["error"]["type"] == "authentication_error"
    assert gw.fake.calls == []
    [row] = gw.rows()
    assert row["error"] == "missing_key" and row["key_id"] is None


async def test_unknown_key_is_rejected_and_fingerprinted(gw):
    r = await gw.client.post("/api/v1/chat/completions", headers={"Authorization": "Bearer nope-nope-nope"},
                             json={"model": "x", "messages": []})
    assert r.status == 401
    body = await r.json()
    assert body["error"]["type"] == "authentication_error"  # OpenAI envelope on the OpenAI route
    assert gw.fake.calls == []
    [row] = gw.rows()
    assert row["error"] == "invalid_key" and row["key_fingerprint"]


async def test_stream_passthrough_swaps_credentials_and_records_usage(gw):
    kid = gw.add_key("bob-mac", KEY)
    r = await gw.client.post(
        "/api/v1/messages?beta=true",
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "X-Real-IP": "203.0.113.9"},
        json={"model": "gpt-6-sol", "stream": True, "messages": [{"role": "user", "content": "hi"}],
              "metadata": {"user_id": "user_abc_account__session_0f5c2d0e-1111-2222-3333-444455556666"}},
    )
    assert r.status == 200
    text = await r.text()
    assert "message_stop" in text and r.headers["X-Cx2cc-Request-Id"]
    [call] = gw.fake.calls
    assert call["query"] == "beta=true"
    assert call["headers"]["Authorization"] == f"Bearer {UPSTREAM_TOKEN}"
    assert "x-api-key" not in {k.lower() for k in call["headers"]}
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    [row] = gw.rows()
    assert row["key_id"] == kid and row["alias"] == "bob-mac"
    assert row["stream"] == 1 and row["status"] == 200 and row["error"] is None
    assert (row["input_tokens"], row["cached_tokens"], row["output_tokens"]) == (12000, 10000, 300)
    # (12000-10000)*1 + 10000*0.1 + 300*8
    assert row["weighted"] == 2000 + 1000 + 2400
    assert row["model_requested"] == "gpt-6-sol" and row["model_served"] == "gpt-6.1-sol"
    assert row["client_ip"] == "203.0.113.9"
    assert row["session_id"] == "0f5c2d0e-1111-2222-3333-444455556666"
    daily = gw.db.one("SELECT * FROM usage_daily WHERE key_id = ?", (kid,))
    assert daily["requests"] == 1 and daily["weighted"] == 5400 and daily["model"] == "gpt-6.1-sol"
    assert gw.db.one("SELECT last_used_at FROM api_keys WHERE id = ?", (kid,))["last_used_at"]


async def test_stream_is_forwarded_incrementally(gw):
    gw.add_key("bob-mac", KEY)
    gw.fake.hold_stream = True
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY},
                             json={"model": "m", "stream": True, "messages": []})
    first = await asyncio.wait_for(r.content.readuntil(b"\n\n"), timeout=5)
    assert b"message_start" in first
    gw.fake.release.set()
    rest = await r.text()
    assert "message_stop" in rest


async def test_nonstream_chat_and_responses_usage(gw):
    gw.add_key("peter", KEY)
    r = await gw.client.post("/api/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                             json={"model": "gpt-6-luna", "messages": []})
    assert r.status == 200 and (await r.json())["usage"]["prompt_tokens"] == 500
    r = await gw.client.post("/api/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                             json={"model": "gpt-6-luna", "stream": True, "messages": []})
    assert r.status == 200 and "[DONE]" in await r.text()
    r = await gw.client.post("/api/v1/responses", headers={"Authorization": f"Bearer {KEY}"},
                             json={"model": "gpt-6.1-sol", "input": "hi", "stream": True,
                                   "prompt_cache_key": "conv-123"})
    assert r.status == 200 and "response.completed" in await r.text()
    rows = gw.rows()
    assert [(x["route"], x["input_tokens"], x["cached_tokens"], x["output_tokens"]) for x in rows] == [
        ("chat", 500, 100, 10), ("chat", 700, 600, 5), ("responses", 2000, 1500, 40),
    ]
    assert rows[2]["session_id"] == "conv-123"


async def test_scopes_and_usage_sanitizing(gw):
    gw.add_key("plain", KEY)
    gw.add_key("admin-key", OTHER, scopes="chat,images,usage,accounts")
    r = await gw.client.get("/api/accounts", headers={"x-api-key": KEY})
    assert r.status == 403
    r = await gw.client.get("/api/accounts", headers={"x-api-key": OTHER})
    assert r.status == 200
    r = await gw.client.get("/api/usage", headers={"x-api-key": KEY})
    usage = await r.json()
    assert usage["plan_type"] == "pro" and "email" not in usage and "account_id" not in usage
    r = await gw.client.get("/api/usage", headers={"x-api-key": OTHER})
    assert (await r.json())["email"] == "someone@example.com"
    gw.add_key("chat-only", "sk-cx2cc-chat-only-cccccccccccccccccccc", scopes="chat")
    r = await gw.client.post("/api/v1/images/generations",
                             headers={"x-api-key": "sk-cx2cc-chat-only-cccccccccccccccccccc"},
                             json={"prompt": "cat"})
    assert r.status == 403
    assert (await r.json())["error"]["code"] == "scope_not_allowed"


async def test_model_allowlist_follows_aliases(gw):
    gw.add_key("sol-only", KEY, allowed_models="gpt-6.1-sol")
    ok = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY},
                              json={"model": "gpt-6-sol[1m]", "messages": []})
    assert ok.status == 200
    denied = await gw.client.post("/api/v1/chat/completions", headers={"x-api-key": KEY},
                                  json={"model": "gpt-6-luna", "messages": []})
    assert denied.status == 403
    assert (await denied.json())["error"]["code"] == "model_not_allowed"
    default = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    assert default.status == 200
    claude = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY},
                                  json={"model": "claude-opus-4-8", "messages": []})
    assert claude.status == 200  # cx2cc serves Claude names with its default model
    gw.add_key("luna-only", OTHER, allowed_models="gpt-6-luna")
    claude = await gw.client.post("/api/v1/messages", headers={"x-api-key": OTHER},
                                  json={"model": "claude-opus-4-8", "messages": []})
    assert claude.status == 403


async def test_rpm_limit(gw):
    gw.add_key("slow", KEY, rpm_limit=2)
    for _ in range(2):
        r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
        assert r.status == 200
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    assert r.status == 429 and int(r.headers["Retry-After"]) >= 1
    assert (await r.json())["error"]["type"] == "rate_limit_error"


async def test_daily_quota(gw):
    gw.add_key("capped", KEY, daily_limit=6000)
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY},
                             json={"stream": True, "messages": []})
    assert r.status == 200
    await r.text()
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY},
                             json={"stream": True, "messages": []})
    assert r.status == 200  # 5400 < 6000
    await r.text()
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    assert r.status == 429
    assert (await r.json())["error"]["message"].startswith("Daily token quota")


async def test_disabled_and_revoked_keys_are_attributed(gw):
    kid = gw.add_key("gone", KEY, status="disabled")
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    assert r.status == 401
    gw.db.execute("UPDATE api_keys SET status = 'revoked' WHERE id = ?", (kid,))
    gw.rt.keys.reload()
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    assert r.status == 401
    assert "revoked" in (await r.json())["error"]["message"]
    rows = gw.rows()
    assert [(x["key_id"], x["error"]) for x in rows] == [(kid, "key_disabled"), (kid, "key_revoked")]
    assert gw.fake.calls == []


async def test_expired_key(gw):
    gw.add_key("old", KEY, expires_at=now_ms() - 1000)
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    assert r.status == 401
    assert gw.rows()[0]["error"] == "key_expired"


async def test_public_routes_stay_anonymous(gw):
    r = await gw.client.get("/api/health")
    assert r.status == 200 and (await r.json())["status"] == "ok"
    r = await gw.client.get("/api/v1/models")
    assert r.status == 200
    assert gw.rows() == []
    r = await gw.client.head("/api/api/hello")
    assert r.status == 404
    r = await gw.client.get("/api/whatever")
    assert r.status == 404


async def test_upstream_unreachable(gw):
    gw.add_key("k", KEY)
    object.__setattr__(gw.rt.cfg, "upstream", "http://127.0.0.1:9")
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY}, json={"messages": []})
    assert r.status == 502
    assert gw.rows()[0]["error"] == "upstream_unreachable"


async def test_images_route_records_image_tokens(gw):
    gw.add_key("artist", KEY)
    r = await gw.client.post("/api/v1/images/generations", headers={"x-api-key": KEY},
                             json={"prompt": "a cat", "size": "1024x1024"})
    assert r.status == 200 and (await r.json())["data"][0]["b64_json"] == "AAAA"
    [row] = gw.rows()
    assert row["route"] == "images" and row["output_tokens"] == 200 and row["model_served"] == "gpt-image-2"


async def test_client_disconnect_mid_stream(gw):
    kid = gw.add_key("quitter", KEY)
    gw.fake.hold_stream = True
    r = await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY},
                             json={"stream": True, "messages": []})
    await asyncio.wait_for(r.content.readuntil(b"\n\n"), timeout=5)
    r.close()
    await asyncio.sleep(0.1)
    gw.fake.release.set()
    for _ in range(50):
        rows = gw.rows()
        if rows:
            break
        await asyncio.sleep(0.05)
    assert rows[0]["key_id"] == kid
    assert rows[0]["status"] == 499 and rows[0]["error"] == "client_closed"


async def test_request_log_body_never_stored(gw):
    gw.add_key("private", KEY)
    secret_text = "top secret prompt content"
    await gw.client.post("/api/v1/messages", headers={"x-api-key": KEY},
                         json={"messages": [{"role": "user", "content": secret_text}]})
    dump = json.dumps(gw.rows())
    assert secret_text not in dump and KEY not in dump
