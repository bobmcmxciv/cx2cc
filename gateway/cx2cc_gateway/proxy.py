"""The API front door: authenticate the caller's own key, enforce its limits,
forward to cx2cc with the internal token, stream the answer back and audit it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass

import aiohttp
from aiohttp import web

from .db import now_ms
from .keys import KeyRecord, model_allowed
from .runtime import Runtime
from .security import key_fingerprint
from .usage import SSESniffer, Usage, apply_object

log = logging.getLogger("cx2cc_gateway.proxy")


@dataclass(frozen=True)
class Route:
    name: str
    shape: str          # "anthropic" or "openai": the error envelope clients expect
    scope: str | None   # key scope required; None = public
    known: bool = True


def _routes() -> dict[str, Route]:
    table: dict[str, Route] = {}

    def add(paths, route):
        for p in paths:
            table[p] = route

    add(("/v1/messages", "/messages"), Route("messages", "anthropic", "chat"))
    add(("/v1/chat/completions", "/openai/v1/chat/completions"), Route("chat", "openai", "chat"))
    add(("/v1/responses", "/openai/v1/responses"), Route("responses", "openai", "chat"))
    add(("/v1/alpha/search", "/openai/v1/alpha/search"), Route("search", "openai", "chat"))
    add(
        (
            "/v1/images/generations", "/openai/v1/images/generations",
            "/v1/images/edits", "/openai/v1/images/edits",
        ),
        Route("images", "openai", "images"),
    )
    add(("/usage", "/v1/usage"), Route("usage", "anthropic", "usage"))
    add(("/accounts", "/v1/accounts"), Route("accounts", "anthropic", "accounts"))
    add(("/v1/models", "/models", "/openai/v1/models"), Route("models", "openai", None))
    add(("/health",), Route("health", "anthropic", None))
    return table


ROUTES = _routes()
# Routes that consume the model quota; model allowlists apply to these.
MODEL_ROUTES = frozenset({"messages", "chat", "responses"})

# Never forwarded upstream: hop-by-hop headers, the caller's credentials (the
# gateway substitutes its own) and proxy bookkeeping. Accept-Encoding is
# dropped so the upstream answers uncompressed and usage can be read in flight.
_DROP_REQUEST = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "x-api-key", "authorization", "accept-encoding", "cookie", "x-real-ip",
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-forwarded-prefix",
})
_DROP_RESPONSE = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "trailers", "transfer-encoding", "upgrade", "content-length", "server", "date",
})

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    502: "api_error",
}

_KEY_PROBLEMS = {
    "invalid_key": "Invalid API key",
    "key_disabled": "This API key is disabled",
    "key_revoked": "This API key has been revoked",
    "key_expired": "This API key has expired",
    "key_rotated": "This API key was rotated; use the new key",
}

_LIMIT_MESSAGES = {
    "concurrency_limit": "Too many concurrent requests for this API key",
    "rpm_limit": "Request rate limit reached for this API key",
    "daily_quota": "Daily token quota reached for this API key",
    "weekly_quota": "7-day token quota reached for this API key",
}

_SESSION_RE = re.compile(r"session_([0-9a-fA-F-]{36})")
_SESSION_JSON_RE = re.compile(r'"session_id"\s*:\s*"([^"]{8,64})"')


def presented_key(request: web.Request) -> str:
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def client_ip(request: web.Request, trust_proxy: bool) -> str:
    if trust_proxy:
        real = request.headers.get("X-Real-IP", "").strip()
        if real:
            return real[:64]
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.remote or "")[:64]


def error_response(route: Route, status: int, message: str, rid: str, code: str | None = None,
                   headers: dict | None = None) -> web.Response:
    etype = _ERROR_TYPES.get(status, "api_error")
    if route.shape == "openai":
        body = {"error": {"message": message, "type": etype, "code": code}}
    else:
        body = {"type": "error", "error": {"type": etype, "message": message}}
    h = {"X-Cx2cc-Request-Id": rid}
    if headers:
        h.update(headers)
    return web.json_response(body, status=status, headers=h)


def _sse_error(route: Route, message: str) -> bytes:
    if route.shape == "openai":
        payload = {"error": {"type": "api_error", "message": message}}
        return f"data: {json.dumps(payload)}\n\n".encode()
    payload = {"type": "error", "error": {"type": "api_error", "message": message}}
    return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()


def session_hint(body: dict, request: web.Request) -> str | None:
    """Best-effort conversation id, so one session's calls can be grouped."""
    for header in ("x-claude-code-session-id", "session_id", "session-id", "conversation_id"):
        v = request.headers.get(header)
        if v:
            return v.strip()[:64]
    md = body.get("metadata")
    if isinstance(md, dict):
        uid = md.get("user_id")
        if isinstance(uid, str):
            m = _SESSION_RE.search(uid) or _SESSION_JSON_RE.search(uid)
            if m:
                return m.group(1)
    pck = body.get("prompt_cache_key")
    if isinstance(pck, str) and pck:
        return pck[:64]
    return None


def classify(path: str) -> Route:
    route = ROUTES.get(path)
    if route:
        return route
    shape = "openai" if path.startswith("/openai/") else "anthropic"
    return Route("other", shape, "chat", known=False)


class Proxy:
    def __init__(self, rt: Runtime):
        self.rt = rt

    async def handle(self, request: web.Request) -> web.StreamResponse:
        rt = self.rt
        rid = secrets.token_hex(8)
        t0 = time.monotonic()
        prefix = rt.cfg.api_prefix
        raw_path = request.path
        if prefix and raw_path.startswith(prefix):
            raw_path = raw_path[len(prefix):]
        path = "/" + raw_path.lstrip("/")
        route = classify(path)
        secret = presented_key(request)

        row = {
            "rid": rid,
            "ts": now_ms(),
            "key_id": None,
            "alias": None,
            "method": request.method,
            "path": path[:200],
            "route": route.name,
            "model_requested": None,
            "model_served": None,
            "stream": 0,
            "status": 0,
            "error": None,
            "input_tokens": 0,
            "cached_tokens": 0,
            "output_tokens": 0,
            "weighted": 0,
            "req_bytes": request.content_length,
            "resp_bytes": 0,
            "ttfb_ms": None,
            "duration_ms": None,
            "client_ip": client_ip(request, rt.cfg.trust_proxy),
            "user_agent": request.headers.get("User-Agent", "")[:200] or None,
            "session_id": None,
            "key_fingerprint": None,
        }

        def finish(resp: web.StreamResponse, status: int | None = None, error: str | None = None):
            row["status"] = status if status is not None else resp.status
            if error:
                row["error"] = error
            row["duration_ms"] = int((time.monotonic() - t0) * 1000)
            rt.recorder.submit(row)
            return resp

        if not secret:
            if route.scope is None:
                # Public routes (health, model list) stay anonymous and are not
                # audited, exactly as cx2cc serves them today.
                return await self._forward(request, path, route, None, b"", row, audit=False)
            if not route.known:
                return error_response(route, 404, "Not found", rid)
            resp = error_response(
                route, 401, "No API key supplied (send x-api-key, or Authorization: Bearer)", rid,
                code="missing_api_key",
            )
            return finish(resp, error="missing_key")

        rec, problem = rt.keys.authenticate(secret)
        if rec is not None:
            row["key_id"] = rec.id
            row["alias"] = rec.alias
        if problem:
            row["key_fingerprint"] = key_fingerprint(secret) if rec is None else None
            resp = error_response(route, 401, _KEY_PROBLEMS.get(problem, "Invalid API key"), rid,
                                  code="invalid_api_key")
            return finish(resp, error=problem)

        if route.scope and route.scope not in rec.scopes:
            resp = error_response(
                route, 403, f"This API key is not allowed to use '{route.name}'", rid,
                code="scope_not_allowed",
            )
            return finish(resp, error="scope_denied")

        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            resp = error_response(route, 413, "Request body too large", rid)
            return finish(resp, error="body_too_large")
        row["req_bytes"] = len(body)
        parsed = await _parse_json(body) if body else None
        if isinstance(parsed, dict):
            model = parsed.get("model")
            if isinstance(model, str):
                row["model_requested"] = model[:80]
            row["session_id"] = session_hint(parsed, request)
        else:
            parsed = None

        if route.name in MODEL_ROUTES and not model_allowed(
            rec, row["model_requested"], rt.catalog.aliases, rt.catalog.default_model
        ):
            resp = error_response(
                route, 403,
                f"Model '{row['model_requested'] or 'default'}' is not allowed for this API key",
                rid, code="model_not_allowed",
            )
            return finish(resp, error="model_denied")

        limited = rt.limiter.check(rec, rt.settings.tz_offset_minutes)
        if limited:
            reason, retry_after = limited
            resp = error_response(
                route, 429, _LIMIT_MESSAGES[reason], rid, code=reason,
                headers={"Retry-After": str(retry_after)},
            )
            return finish(resp, error=reason)

        rt.limiter.acquire(rec)
        try:
            return await self._forward(request, path, route, rec, body, row, audit=True, t0=t0)
        finally:
            rt.limiter.release(rec)

    async def _forward(self, request: web.Request, path: str, route: Route, rec: KeyRecord | None,
                       body: bytes, row: dict, audit: bool, t0: float | None = None):
        rt = self.rt
        t0 = t0 if t0 is not None else time.monotonic()
        rid = row["rid"]
        url = rt.cfg.upstream + path
        if request.query_string:
            url += "?" + request.query_string
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
        headers["Authorization"] = f"Bearer {rt.cfg.upstream_token}"

        def done(resp, error=None):
            # Idempotent: the cancellation path below may race a normal finish.
            if audit and not row.get("_recorded"):
                row["_recorded"] = True
                if row["status"] == 0:
                    row["status"] = resp.status if resp is not None else 499
                if error and not row["error"]:
                    row["error"] = error
                row["duration_ms"] = int((time.monotonic() - t0) * 1000)
                rt.recorder.submit(row)
            return resp

        try:
            return await self._exchange(request, url, headers, body, route, rec, row, done, t0)
        except asyncio.CancelledError:
            # The server cancels the handler when the caller disconnects (if
            # handler cancellation is on); the attempt still belongs in the log.
            row["status"] = 499
            done(None, error="client_closed")
            raise

    async def _exchange(self, request, url, headers, body, route: Route, rec: KeyRecord | None,
                        row: dict, done, t0: float):
        rt = self.rt
        rid = row["rid"]
        try:
            upstream_cm = rt.session.request(
                request.method, url, data=body if body else None, headers=headers,
                allow_redirects=False,
            )
            up = await upstream_cm.__aenter__()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("[%s] upstream unreachable: %s", rid, type(exc).__name__)
            resp = error_response(route, 502, "cx2cc upstream unreachable; retry shortly", rid)
            return done(resp, error="upstream_unreachable")

        try:
            row["ttfb_ms"] = int((time.monotonic() - t0) * 1000)
            resp_headers = {k: v for k, v in up.headers.items() if k.lower() not in _DROP_RESPONSE}
            resp_headers["X-Cx2cc-Request-Id"] = rid
            ctype = up.headers.get("Content-Type", "")

            if "text/event-stream" in ctype:
                row["stream"] = 1
                return await self._stream(request, up, route, resp_headers, row, done)

            data = await up.read()
            row["resp_bytes"] = len(data)
            usage = Usage()
            if data and "json" in ctype:
                decoded = await _parse_json(data)
                if isinstance(decoded, dict):
                    apply_object(usage, decoded)
                    if (route.name == "usage" and up.status == 200 and rec is not None
                            and "accounts" not in rec.scopes):
                        # Quota numbers are for everyone; which subscription
                        # serves them is not.
                        for field in ("email", "user_id", "account_id"):
                            decoded.pop(field, None)
                        data = json.dumps(decoded, ensure_ascii=False).encode("utf-8")
            if up.status >= 400 and not usage.error:
                usage.error = f"upstream_{up.status}"
            self._apply_usage(row, usage)
            resp = web.Response(body=data, status=up.status, headers=resp_headers)
            return done(resp)
        finally:
            await upstream_cm.__aexit__(None, None, None)

    async def _stream(self, request, up, route: Route, resp_headers: dict, row: dict, done):
        resp = web.StreamResponse(status=up.status, headers=resp_headers)
        sniffer = SSESniffer()
        error = None
        try:
            await resp.prepare(request)
        except ConnectionError:
            row["status"] = 499
            return done(resp, error="client_closed")
        try:
            async for chunk in up.content.iter_any():
                if not chunk:
                    continue
                sniffer.feed(chunk)
                row["resp_bytes"] += len(chunk)
                try:
                    await resp.write(chunk)
                except ConnectionError:
                    # The caller went away; dropping the upstream response
                    # (on return) cancels the generation behind it.
                    error = "client_closed"
                    row["status"] = 499
                    break
        except asyncio.CancelledError:
            self._apply_usage(row, sniffer.finish())
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
            error = "upstream_stream_error"
            log.warning("[%s] upstream stream ended early: %s", row["rid"], type(exc).__name__)
            try:
                await resp.write(_sse_error(
                    route, f"Upstream ended the stream early ({type(exc).__name__}). "
                           "The request did not complete; retry it.",
                ))
            except ConnectionError:
                pass
        self._apply_usage(row, sniffer.finish())
        if error is None:
            try:
                await resp.write_eof()
            except ConnectionError:
                error = "client_closed"
                row["status"] = 499
        return done(resp, error=error)

    def _apply_usage(self, row: dict, usage: Usage) -> None:
        s = self.rt.settings
        row["model_served"] = usage.model
        row["input_tokens"] = usage.input_tokens
        row["cached_tokens"] = usage.cached_tokens
        row["output_tokens"] = usage.output_tokens
        row["weighted"] = usage.weighted(s.w_uncached, s.w_cached, s.w_output)
        if usage.error and not row["error"]:
            row["error"] = usage.error
        if row["key_id"] is not None and row["weighted"]:
            self.rt.limiter.add_usage(row["key_id"], row["weighted"], s.tz_offset_minutes)


async def _parse_json(data: bytes):
    try:
        if len(data) > 256 * 1024:
            return await asyncio.to_thread(json.loads, data)
        return json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
