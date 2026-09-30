import asyncio
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cx2cc_gateway.app import RUNTIME, build_app  # noqa: E402
from cx2cc_gateway.config import Config  # noqa: E402
from cx2cc_gateway.db import Database, now_ms  # noqa: E402
from cx2cc_gateway.security import hash_api_key, hash_password, key_display_prefix  # noqa: E402

UPSTREAM_TOKEN = "internal-bridge-token-0123456789"


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class FakeUpstream:
    """Stands in for cx2cc: records what it received, answers canned bodies."""

    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()
        self.hold_stream = False

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/v1/messages", self.messages)
        app.router.add_post("/v1/chat/completions", self.chat)
        app.router.add_post("/v1/responses", self.responses)
        app.router.add_post("/v1/images/generations", self.images)
        app.router.add_get("/usage", self.usage)
        app.router.add_get("/accounts", self.accounts)
        app.router.add_get("/v1/models", self.models)
        app.router.add_get("/health", self.health)
        return app

    def _note(self, request, body=None):
        self.calls.append({
            "path": request.path,
            "query": request.query_string,
            "headers": dict(request.headers),
            "body": body,
        })

    async def messages(self, request):
        body = await request.json()
        self._note(request, body)
        if not body.get("stream"):
            return web.json_response({
                "id": "msg_1", "type": "message", "role": "assistant", "model": "gpt-6.1-sol",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 1000, "cache_read_input_tokens": 800, "output_tokens": 20},
            })
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        await resp.write(sse("message_start", {
            "type": "message_start",
            "message": {"id": "msg_1", "model": "gpt-6.1-sol", "usage": {"input_tokens": 50, "output_tokens": 0}},
        }))
        if self.hold_stream:
            await self.release.wait()
        await resp.write(sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                     "delta": {"type": "text_delta", "text": "hi"}}))
        await resp.write(sse("message_delta", {
            "type": "message_delta", "delta": {"stop_reason": "end_turn"},
            "usage": {"input_tokens": 12000, "output_tokens": 300, "cache_read_input_tokens": 10000},
        }))
        await resp.write(sse("message_stop", {"type": "message_stop"}))
        await resp.write_eof()
        return resp

    async def chat(self, request):
        body = await request.json()
        self._note(request, body)
        if not body.get("stream"):
            return web.json_response({
                "id": "c1", "object": "chat.completion", "model": "gpt-6-luna",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 10,
                          "prompt_tokens_details": {"cached_tokens": 100}},
            })
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"id":"c1","model":"gpt-6-luna","choices":[{"delta":{"content":"o"}}],"usage":null}\n\n')
        await resp.write(b'data: {"id":"c1","model":"gpt-6-luna","choices":[],"usage":{"prompt_tokens":700,'
                         b'"completion_tokens":5,"prompt_tokens_details":{"cached_tokens":600}}}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    async def responses(self, request):
        body = await request.json()
        self._note(request, body)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(sse("response.created", {"type": "response.created",
                                                  "response": {"model": "gpt-6.1-sol", "usage": None}}))
        await resp.write(sse("response.completed", {
            "type": "response.completed",
            "response": {"model": "gpt-6.1-sol", "usage": {
                "input_tokens": 2000, "input_tokens_details": {"cached_tokens": 1500}, "output_tokens": 40}},
        }))
        await resp.write_eof()
        return resp

    async def images(self, request):
        body = await request.json()
        self._note(request, body)
        return web.json_response({"data": [{"b64_json": "AAAA"}], "model": "gpt-image-2",
                                  "usage": {"input_tokens": 30, "output_tokens": 200}})

    async def usage(self, request):
        self._note(request)
        return web.json_response({"email": "someone@example.com", "user_id": "u-1", "account_id": "a-1",
                                  "plan_type": "pro", "rate_limit": {"primary_window": {"used_percent": 40}}})

    async def accounts(self, request):
        self._note(request)
        return web.json_response({"active": "pro-1", "accounts": [
            {"id": "pro-1", "plan": "pro", "email": "someone@example.com", "path": "C:\\secret\\pro.json",
             "active": True, "used_percent": 40}]})

    async def models(self, request):
        self._note(request)
        return web.json_response({"object": "list", "default_model": "gpt-6.1-sol",
                                  "aliases": {"gpt-6-sol": "gpt-6.1-sol"},
                                  "data": [{"id": "gpt-6.1-sol"}, {"id": "gpt-6-luna"}]})

    async def health(self, request):
        return web.json_response({"status": "ok", "upstream_configured": True})


@pytest.fixture
def fake():
    return FakeUpstream()


@pytest.fixture
async def gw(aiohttp_server, aiohttp_client, tmp_path, fake):
    server = await aiohttp_server(fake.app())
    cfg = Config(
        listen_host="127.0.0.1", listen_port=0, upstream=str(server.make_url("")).rstrip("/"),
        upstream_token=UPSTREAM_TOKEN, data_dir=tmp_path, api_prefix="/api", cookie_secure=False,
        trust_proxy=True, session_hours=12, upstream_read_timeout=60, max_body_mb=96,
    )
    db = Database(cfg.db_path)
    app = build_app(cfg, db)
    client = await aiohttp_client(app)
    rt = app[RUNTIME]
    # Let the first upkeep pass load the model catalog.
    for _ in range(50):
        if rt.catalog.payload:
            break
        await asyncio.sleep(0.02)
    fake.calls.clear()
    return Gateway(client, rt, db, fake)


class Gateway:
    def __init__(self, client, rt, db, fake):
        self.client = client
        self.rt = rt
        self.db = db
        self.fake = fake

    def add_key(self, alias, secret, scopes="chat,images,usage", created_by=None, **extra):
        now = now_ms()
        cols = {
            "alias": alias, "owner": extra.pop("owner", ""), "key_hash": hash_api_key(secret),
            "key_prefix": key_display_prefix(secret), "scopes": scopes, "created_at": now,
            "updated_at": now, "created_by": created_by, **extra,
        }
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self.db.execute(f"INSERT INTO api_keys ({names}) VALUES ({marks})", list(cols.values()))
        self.rt.keys.reload()
        return cur.lastrowid

    def add_user(self, username, role, password="correct-horse-battery"):
        cur = self.db.execute(
            "INSERT INTO users (username, role, password_hash, created_at) VALUES (?,?,?,?)",
            (username, role, hash_password(password), now_ms()),
        )
        return cur.lastrowid

    def rows(self, sql="SELECT * FROM requests ORDER BY id", args=()):
        self.rt.recorder.flush()
        return self.db.all(sql, args)
