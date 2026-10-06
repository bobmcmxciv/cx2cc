"""
cx2cc - Anthropic Messages API -> OpenAI Chat Completions API translation proxy.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

import prompt_estimate
from translator import (
    UPSTREAM_MODEL,
    UnknownModelError,
    annotate_model_catalog,
    prepare_openai_passthrough,
    request_diag,
    stream_translate,
    tool_arg_schemas,
    translate_request,
    translate_response,
    upstream_model,
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
load_dotenv(APP_DIR / ".env", verbose=False)

LISTEN_PORT = int(os.environ.get("CX2CC_PORT", "8901"))
LISTEN_HOST = os.environ.get("CX2CC_HOST", "127.0.0.1")

_key_lock = threading.Lock()
_key_index = 0
_key_errors: dict[str, int] = {}

app = Flask(__name__)


def _configure_logging() -> None:
    handler: logging.Handler
    # In a windowed/frozen build sys.stderr/stdout can be None, which makes the
    # default StreamHandler raise on every log call. Fall back to a log file.
    if sys.stderr is not None:
        handler = logging.StreamHandler()
    else:
        log_dir = APP_DIR / "logs"
        try:
            log_dir.mkdir(exist_ok=True)
            handler = logging.FileHandler(log_dir / "cx2cc.out.log", encoding="utf-8")
        except Exception:
            handler = logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


_configure_logging()
log = logging.getLogger("cx2cc")


def _fallback_api_keys() -> list[str]:
    raw_keys = os.environ.get(
        "CX2CC_UPSTREAM_API_KEYS",
        os.environ.get("CX2CC_UPSTREAM_API_KEY", ""),
    )
    return [k.strip() for k in re.split(r"[,\n]+", raw_keys) if k.strip()]


def _report_upstream_model() -> bool:
    """Whether responses should name the model that actually served the request.

    Off by default, which preserves the original behaviour of echoing back the
    model the client asked for. Turning it on stops a client default such as
    `claude-opus-4-*` from being recorded as the model that ran.
    """
    return os.environ.get("CX2CC_REPORT_UPSTREAM_MODEL", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _request_api_key() -> str:
    """Read the caller's key from either header Claude Code might use.

    `ANTHROPIC_API_KEY` is sent as `x-api-key`, but `ANTHROPIC_AUTH_TOKEN` — which
    CC Switch and most proxy-style configs use — is sent as `Authorization: Bearer`.
    Accepting only the former made those configs fail with a confusing 502
    ("No upstream API key configured") even though the token was correct.
    """
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    auth = request.headers.get("Authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _get_upstream_base_url() -> str | None:
    base_url = os.environ.get("CX2CC_UPSTREAM_BASE_URL", "").strip().rstrip("/")
    return base_url or None


def _chat_url() -> str | None:
    base_url = _get_upstream_base_url()
    if not base_url:
        return None
    return f"{base_url}/chat/completions"


def _responses_url() -> str | None:
    """Upstream URL for the /v1/responses passthrough — codex-bridge only.

    codex-bridge's Responses endpoint lives on the same `/v1` prefix as chat
    completions (added alongside this route), so we reuse the configured base
    URL and just swap the tail.
    """
    base_url = _get_upstream_base_url()
    if not base_url:
        return None
    return f"{base_url}/responses"


def _images_url(kind: str = "generations") -> str | None:
    """Upstream URL for the /v1/images/<kind> passthroughs — codex-bridge only.

    `kind` is "generations" (text to image) or "edits" (reference images plus
    prompt). Same `/v1` prefix as chat completions and responses. The bridge
    turns the call into the ChatGPT backend's gpt-image-2 endpoint and answers
    OpenAI Images JSON, so nothing here needs to know about image models.
    """
    base_url = _get_upstream_base_url()
    if not base_url:
        return None
    return f"{base_url}/images/{kind}"


def _search_url() -> str | None:
    """Upstream URL for the /v1/alpha/search passthrough — codex-bridge only.

    Codex CLI 0.158+ runs web search client-side against
    `{provider base_url}/alpha/search`; the bridge serves the same path under
    the shared `/v1` prefix and forwards it to the ChatGPT backend.
    """
    base_url = _get_upstream_base_url()
    if not base_url:
        return None
    return f"{base_url}/alpha/search"


def _sibling_url(name: str) -> str | None:
    """`<base URL without its /v1 suffix>/<name>`, which is where codex-bridge
    keeps its side endpoints. Upstreams without one answer 404, which is
    forwarded as-is.
    """
    base_url = _get_upstream_base_url()
    if not base_url:
        return None
    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
    return f"{root}/{name}"


def _usage_url() -> str | None:
    """Upstream endpoint behind GET /usage.

    Defaults to the codex-bridge layout; `CX2CC_USAGE_URL` overrides it for
    upstreams that expose usage elsewhere.
    """
    explicit = os.environ.get("CX2CC_USAGE_URL", "").strip()
    if explicit:
        return explicit
    return _sibling_url("usage")


def _get_fallback_key(keys: list[str]) -> str | None:
    global _key_index
    if not keys:
        return None
    with _key_lock:
        start = _key_index % len(keys)
        _key_index = start
        while True:
            key = keys[_key_index]
            _key_index = (_key_index + 1) % len(keys)
            if _key_errors.get(key, 0) < 3:
                return key
            if _key_index == start:
                _key_errors.clear()
                return keys[0]


def _candidate_keys(request_key: str) -> list[str]:
    fallback_keys = _fallback_api_keys()
    keys: list[str] = []
    if request_key:
        keys.append(request_key)

    for _ in range(len(fallback_keys)):
        key = _get_fallback_key(fallback_keys)
        if key and key not in keys:
            keys.append(key)

    return keys


def _record_key_error(key: str) -> None:
    with _key_lock:
        _key_errors[key] = _key_errors.get(key, 0) + 1


def _record_key_ok(key: str) -> None:
    with _key_lock:
        _key_errors.pop(key, None)


def _make_upstream_request(url: str, json_body: dict, key: str, stream: bool, timeout: int):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return requests.post(url, json=json_body, headers=headers, stream=stream, timeout=timeout)


def _should_retry_key(status_code: int, error_body: str) -> bool:
    if status_code in (401, 403, 429):
        return True
    if status_code >= 500:
        return True
    if status_code == 400:
        lower = error_body.lower()
        triggers = [
            "quota",
            "balance",
            "insufficient",
            "exhausted",
            "limit",
            "overloaded",
            "余额",
            "用量不足",
            "已用完",
            "超额",
        ]
        return any(t in lower for t in triggers)
    return False


@app.route("/v1/messages", methods=["POST"])
@app.route("/messages", methods=["POST"])
def handle_messages():
    try:
        anthropic_body = request.get_json(force=True)
    except Exception as e:
        return _err(400, f"Invalid JSON: {e}")

    stream = anthropic_body.get("stream", False)
    model_name = anthropic_body.get("model", UPSTREAM_MODEL)
    msg_count = len(anthropic_body.get("messages", []))
    api_key = _request_api_key()

    log.info(
        "-> %s | stream=%s | msgs=%s | key_from_header=%s",
        model_name,
        stream,
        msg_count,
        bool(api_key),
    )

    try:
        openai_body = translate_request(anthropic_body)
    except UnknownModelError as exc:
        # Explicit slug this proxy cannot serve: say so instead of running the
        # pinned default under the caller's chosen name (CX2CC_UNKNOWN_MODEL).
        log.warning("-> rejected unknown model %r", exc.requested)
        return _err(400, str(exc), "invalid_request_error")
    except Exception:
        log.exception("Request translation failed")
        return _err(400, "Translation error")

    tool_schemas = tool_arg_schemas(anthropic_body.get("tools"))

    # Fingerprint every request so cache-miss forensics don't depend on data
    # that only exists while the request is in flight (see request_diag).
    diag = f"msgs={msg_count} {request_diag(openai_body)}"
    log.info("-> diag %s", diag)

    if stream:
        return _handle_stream(openai_body, model_name, api_key=api_key, diag=diag, tool_schemas=tool_schemas)
    return _handle_nonstream(openai_body, model_name, api_key=api_key, diag=diag, tool_schemas=tool_schemas)


def _prompt_estimate_for(openai_body: dict):
    """(conversation key, request size, estimated message_start usage).

    Keyed by the same per-conversation hash the upstream prompt cache uses, so
    one conversation's turns share an anchor while distinct conversations —
    including Task subagents, which open with a different first user message —
    keep their own.
    """
    key = openai_body.get("prompt_cache_key") or ""
    chars = prompt_estimate.request_chars(openai_body)
    return key, chars, prompt_estimate.estimate(key, chars)


def _handle_stream(openai_body: dict, display_model: str, api_key: str = "", diag: str = "",
                   tool_schemas: dict | None = None):
    chat_url = _chat_url()
    if not chat_url:
        return _err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(api_key)
    if not keys:
        # 401, not 502: a caller that sent no credential has a client-side
        # problem, and answering "bad gateway" sent every such misconfiguration
        # downstream looking like a cx2cc outage. See _request_api_key.
        return _err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    last_upstream = None
    for key in keys:
        try:
            upstream = _make_upstream_request(chat_url, openai_body, key, stream=True, timeout=600)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Stream upstream connection failed: %s", type(exc).__name__)
            continue

        if upstream.status_code != 200:
            err_body = upstream.text[:500]
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, err_body):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_err(upstream)

        _record_key_ok(key)
        upstream.encoding = "utf-8"

        est_key, est_chars, est_usage = _prompt_estimate_for(openai_body)

        def generate():
            try:
                for chunk in stream_translate(
                    upstream,
                    display_model,
                    use_upstream_model=_report_upstream_model(),
                    diag=diag,
                    prompt_usage=est_usage,
                    on_usage=lambda tokens, cached: prompt_estimate.record(
                        est_key, est_chars, tokens, cached
                    ),
                    tool_schemas=tool_schemas,
                ):
                    yield chunk
            except Exception as exc:
                # The upstream drops a chunked response mid-flight often enough
                # to matter (17 times in one 13 h window, as urllib3
                # ProtocolError "Response ended prematurely"). Ending the
                # generator quietly left the client with a truncated SSE stream
                # carrying no message_stop, which surfaces downstream as a bare
                # 502 with nothing to act on. Emit a real error event instead so
                # the client can report the cause and retry.
                log.exception("Stream translation error")
                yield _sse_stream_error(exc)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                # No "Connection" header here. It is hop-by-hop, so PEP 3333
                # forbids a WSGI app from setting it, and werkzeug does not strip
                # it: the response went out carrying both "keep-alive" (ours) and
                # "close" (werkzeug's own, which it always sends), which is a
                # malformed reply whose reuse semantics differ between clients.
            },
        )

    if last_upstream is not None:
        return _forward_err(last_upstream)
    return _err(502, "Upstream request failed for all configured keys")


def _handle_nonstream(openai_body: dict, display_model: str, api_key: str = "", diag: str = "",
                      tool_schemas: dict | None = None):
    chat_url = _chat_url()
    if not chat_url:
        return _err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(api_key)
    if not keys:
        # 401, not 502: a caller that sent no credential has a client-side
        # problem, and answering "bad gateway" sent every such misconfiguration
        # downstream looking like a cx2cc outage. See _request_api_key.
        return _err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    last_upstream = None
    for key in keys:
        try:
            upstream = _make_upstream_request(chat_url, openai_body, key, stream=False, timeout=300)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Nonstream upstream connection failed: %s", type(exc).__name__)
            continue

        if upstream.status_code != 200:
            err_body = upstream.text[:500]
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, err_body):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_err(upstream)

        _record_key_ok(key)
        try:
            anthropic_resp = translate_response(
                upstream.json(), display_model, use_upstream_model=_report_upstream_model(),
                tool_schemas=tool_schemas,
            )
        except Exception:
            log.exception("Response translation failed")
            return _err(500, "Response translation error")

        log.info(
            "<- %s | in=%s cached=%s out=%s | %s",
            anthropic_resp["stop_reason"],
            anthropic_resp["usage"]["input_tokens"],
            anthropic_resp["usage"].get("cache_read_input_tokens", 0),
            anthropic_resp["usage"]["output_tokens"],
            diag,
        )
        return jsonify(anthropic_resp)

    if last_upstream is not None:
        return _forward_err(last_upstream)
    return _err(502, "Upstream request failed for all configured keys")


@app.route("/openai/v1/responses", methods=["POST"])
@app.route("/v1/responses", methods=["POST"])
def handle_responses():
    """OpenAI Responses API passthrough, forwarded to codex-bridge /v1/responses.

    Codex CLI 0.135+ removed `wire_api = "chat"` and only issues Responses, so
    a client speaking Responses natively cannot use /v1/chat/completions. This
    route is a thin proxy — no body translation, upstream SSE bytes streamed
    back verbatim — that shares the same key rotation as the other paths. The
    heavy lifting (account pool, OAuth, refusal ladder) is done by codex-bridge.
    """
    try:
        body = request.get_json(force=True)
    except Exception as e:
        return _openai_err(400, f"Invalid JSON: {e}")

    if not isinstance(body, dict):
        return _openai_err(400, "Request body must be a JSON object")

    api_key = _request_api_key()
    stream = bool(body.get("stream", True))

    responses_url = _responses_url()
    if not responses_url:
        return _openai_err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(api_key)
    if not keys:
        return _openai_err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    log.info(
        "-> [responses] model=%s stream=%s key_from_header=%s",
        body.get("model"),
        stream,
        bool(api_key),
    )

    last_upstream = None
    for key in keys:
        try:
            upstream = _make_upstream_request(
                responses_url, body, key, stream=True, timeout=600
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Responses upstream connection failed: %s", type(exc).__name__)
            continue

        if upstream.status_code != 200:
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, upstream.text[:500]):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_openai_err(upstream)

        _record_key_ok(key)

        if stream:
            def generate():
                try:
                    for chunk in upstream.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
                except Exception as exc:
                    log.exception("Responses stream proxy error")
                    yield _openai_sse_error(exc)

            return Response(
                generate(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        # Non-stream shape is up to codex-bridge, which currently rejects it
        # (a stream=false request on /v1/responses returns a 400 from the
        # bridge). We already exercised the upstream call so the retry loop
        # can surface that error; just aggregate whatever it sent and forward.
        try:
            body_bytes = upstream.content
        finally:
            upstream.close()
        return Response(
            body_bytes,
            status=200,
            content_type=upstream.headers.get("Content-Type", "application/json"),
        )

    if last_upstream is not None:
        return _forward_openai_err(last_upstream)
    return _openai_err(502, "Upstream request failed for all configured keys")


@app.route("/openai/v1/images/generations", methods=["POST"])
@app.route("/v1/images/generations", methods=["POST"])
def handle_images_generations():
    """OpenAI Images API passthrough, forwarded to codex-bridge /v1/images/generations.

    A thin JSON proxy with the same key rotation as the other routes: the body
    ({prompt, size, quality, n, ...}) goes upstream unchanged and the JSON
    answer ({"data": [{"b64_json"}], ...}) comes back verbatim. This is what
    the gpt-image Claude Code skill calls, with the credentials the client
    already holds for /v1/messages. Long timeout on purpose: one image takes
    about 15 s upstream and `n` up to 4 is served sequentially.

    Error bodies are hidden like everywhere else in this file, except for the
    bridge's own `bridge_error` envelope, whose message is the only place a
    prompt rejection or quota park would reach the caller.
    """
    return _proxy_images("generations")


@app.route("/openai/v1/images/edits", methods=["POST"])
@app.route("/v1/images/edits", methods=["POST"])
def handle_images_edits():
    """Image-to-image passthrough, forwarded to codex-bridge /v1/images/edits.

    Same proxy as generations; the body additionally carries `images`, a list
    of data-URL reference images (up to 16, order meaningful) that the bridge
    forwards to the backend's edits endpoint. A 1 MB PNG is ~1.4 MB of JSON,
    so this route relies on the upstream chain (NPM, frp, waitress) accepting
    multi-megabyte bodies.
    """
    return _proxy_images("edits")


def _proxy_images(kind: str):
    try:
        body = request.get_json(force=True)
    except Exception as e:
        return _openai_err(400, f"Invalid JSON: {e}")

    if not isinstance(body, dict):
        return _openai_err(400, "Request body must be a JSON object")

    images_url = _images_url(kind)
    if not images_url:
        return _openai_err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    api_key = _request_api_key()
    keys = _candidate_keys(api_key)
    if not keys:
        return _openai_err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    inputs = body.get("images")
    log.info(
        "-> [images/%s] n=%s inputs=%s size=%s quality=%s key_from_header=%s",
        kind,
        body.get("n", 1),
        len(inputs) if isinstance(inputs, list) else (1 if body.get("image") else 0),
        body.get("size"),
        body.get("quality"),
        bool(api_key),
    )

    last_upstream = None
    for key in keys:
        try:
            upstream = _make_upstream_request(images_url, body, key, stream=False, timeout=600)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Images upstream connection failed: %s", type(exc).__name__)
            continue

        if upstream.status_code != 200:
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, upstream.text[:500]):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_bridge_err(upstream)

        _record_key_ok(key)
        try:
            payload = upstream.json()
        except ValueError:
            log.exception("Images upstream response was not valid JSON")
            return _openai_err(502, "Upstream images response was not valid JSON", err_type="api_error")
        log.info("<- [images/%s] images=%s size=%s", kind, len(payload.get("data") or []), payload.get("size"))
        return jsonify(payload)

    if last_upstream is not None:
        return _forward_bridge_err(last_upstream)
    return _openai_err(502, "Upstream request failed for all configured keys")


def _forward_bridge_err(upstream):
    """Like _forward_openai_err, but keeps codex-bridge's own error message."""
    try:
        err = (upstream.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    if isinstance(err, dict) and err.get("type") == "bridge_error" and err.get("message"):
        log.error("Upstream bridge request failed with status %s: %s", upstream.status_code, str(err["message"])[:200])
        return _openai_err(upstream.status_code, str(err["message"])[:500], err_type="api_error")
    return _forward_openai_err(upstream)


@app.route("/openai/v1/alpha/search", methods=["POST"])
@app.route("/v1/alpha/search", methods=["POST"])
def handle_alpha_search():
    """Codex standalone web search passthrough, forwarded to codex-bridge /v1/alpha/search.

    Codex CLI 0.158 (feature StandaloneWebSearch) no longer sends the hosted
    `web_search` tool with /v1/responses: the model calls a `web.run` function
    and the CLI POSTs {id, model, input, commands, settings} to
    `{base_url}/alpha/search`, expecting {encrypted_output, output, results}.
    Without this route every search from a downstream Codex answered 404.
    Body and answer travel unchanged; the bridge owns the model choice.
    """
    try:
        body = request.get_json(force=True)
    except Exception as e:
        return _openai_err(400, f"Invalid JSON: {e}")

    if not isinstance(body, dict):
        return _openai_err(400, "Request body must be a JSON object")

    search_url = _search_url()
    if not search_url:
        return _openai_err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    api_key = _request_api_key()
    keys = _candidate_keys(api_key)
    if not keys:
        return _openai_err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    commands = body.get("commands") if isinstance(body.get("commands"), dict) else {}
    log.info(
        "-> [alpha/search] model=%s queries=%s opens=%s key_from_header=%s",
        body.get("model"),
        len(commands.get("search_query") or []),
        len(commands.get("open") or []),
        bool(api_key),
    )

    last_upstream = None
    for key in keys:
        try:
            upstream = _make_upstream_request(search_url, body, key, stream=False, timeout=180)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Search upstream connection failed: %s", type(exc).__name__)
            continue

        if upstream.status_code != 200:
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, upstream.text[:500]):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_bridge_err(upstream)

        _record_key_ok(key)
        try:
            payload = upstream.json()
        except ValueError:
            log.exception("Search upstream response was not valid JSON")
            return _openai_err(502, "Upstream search response was not valid JSON", err_type="api_error")
        log.info("<- [alpha/search] results=%s", len(payload.get("results") or []))
        return jsonify(payload)

    if last_upstream is not None:
        return _forward_bridge_err(last_upstream)
    return _openai_err(502, "Upstream request failed for all configured keys")


@app.route("/openai/v1/chat/completions", methods=["POST"])
@app.route("/v1/chat/completions", methods=["POST"])
def handle_chat_completions():
    """OpenAI Chat Completions passthrough for clients that speak OpenAI natively.

    A parallel entry point to /v1/messages: the request is not translated —
    it is forwarded as-is after model allowlist resolution and (when enabled)
    an auto-attached prompt_cache_key — and the upstream reply is proxied
    back verbatim. This is how non-Claude platforms that already speak the
    OpenAI Chat Completions dialect can share the same key rotation and
    upstream pool without going through Anthropic-shape translation.
    """
    try:
        openai_body = request.get_json(force=True)
    except Exception as e:
        return _openai_err(400, f"Invalid JSON: {e}")

    if not isinstance(openai_body, dict):
        return _openai_err(400, "Request body must be a JSON object")

    api_key = _request_api_key()
    stream = bool(openai_body.get("stream", False))
    msg_count = len(openai_body.get("messages", []))

    try:
        prepared = prepare_openai_passthrough(openai_body)
    except UnknownModelError as exc:
        log.warning("-> [openai] rejected unknown model %r", exc.requested)
        return _openai_err(400, str(exc), "invalid_request_error")
    except Exception:
        log.exception("OpenAI passthrough prep failed")
        return _openai_err(400, "Failed to prepare request")

    log.info(
        "-> [openai] %s | stream=%s | msgs=%s | key_from_header=%s",
        prepared.get("model"),
        stream,
        msg_count,
        bool(api_key),
    )
    diag = f"[openai] msgs={msg_count} {request_diag(prepared)}"
    log.info("-> diag %s", diag)

    if stream:
        return _handle_openai_stream(prepared, api_key=api_key, diag=diag)
    return _handle_openai_nonstream(prepared, api_key=api_key, diag=diag)


def _handle_openai_stream(openai_body: dict, api_key: str, diag: str):
    chat_url = _chat_url()
    if not chat_url:
        return _openai_err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(api_key)
    if not keys:
        return _openai_err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    last_upstream = None
    for key in keys:
        try:
            upstream = _make_upstream_request(chat_url, openai_body, key, stream=True, timeout=600)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Stream upstream connection failed: %s", type(exc).__name__)
            continue

        if upstream.status_code != 200:
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, upstream.text[:500]):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_openai_err(upstream)

        _record_key_ok(key)

        def generate():
            try:
                # Proxy the upstream SSE bytes untouched. No translation happens
                # on the OpenAI path, so the client sees the exact chat.completion
                # .chunk events the upstream emitted, including its own [DONE]
                # sentinel.
                for chunk in upstream.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
            except Exception as exc:
                # Same rationale as the Anthropic path: a stream that dies after
                # its headers still owes the client an explanation, otherwise the
                # request surfaces downstream as a bare 502 with nothing to act
                # on.
                log.exception("OpenAI stream proxy error")
                yield _openai_sse_error(exc)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    if last_upstream is not None:
        return _forward_openai_err(last_upstream)
    return _openai_err(502, "Upstream request failed for all configured keys")


def _handle_openai_nonstream(openai_body: dict, api_key: str, diag: str):
    chat_url = _chat_url()
    if not chat_url:
        return _openai_err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(api_key)
    if not keys:
        return _openai_err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    last_upstream = None
    for key in keys:
        try:
            upstream = _make_upstream_request(chat_url, openai_body, key, stream=False, timeout=300)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Nonstream upstream connection failed: %s", type(exc).__name__)
            continue

        if upstream.status_code != 200:
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, upstream.text[:500]):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_openai_err(upstream)

        _record_key_ok(key)
        try:
            body = upstream.json()
        except Exception:
            log.exception("Upstream chat/completions response was not valid JSON")
            return _openai_err(502, "Upstream response was not valid JSON")

        usage = body.get("usage") or {}
        log.info(
            "<- [openai] in=%s cached=%s out=%s | %s",
            usage.get("prompt_tokens", 0),
            (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            usage.get("completion_tokens", 0),
            diag,
        )
        return jsonify(body)

    if last_upstream is not None:
        return _forward_openai_err(last_upstream)
    return _openai_err(502, "Upstream request failed for all configured keys")


@app.route("/v1/usage", methods=["GET"])
@app.route("/usage", methods=["GET"])
def handle_usage():
    """Forward the upstream's usage/quota JSON so CC Switch can display it.

    Same key discipline as /v1/messages: the caller's key is passed through as
    the upstream bearer token, so an invalid caller key comes back as the
    upstream's 401 rather than leaking anything. The body is returned verbatim
    on success only; upstream error bodies stay hidden as elsewhere.
    """
    return _forward_get(_usage_url(), "usage")


@app.route("/v1/accounts", methods=["GET"])
@app.route("/accounts", methods=["GET"])
def handle_accounts():
    """Forward an upstream account-pool view, for upstreams that keep one.

    Upstreams that multiplex several subscriptions (e.g. codex-bridge) expose
    which one is serving and which are parked on a spent quota. Upstreams
    without the endpoint answer 404 and that is forwarded as-is.
    """
    return _forward_get(_sibling_url("accounts"), "accounts")


def _forward_get(url: str | None, label: str):
    if not url:
        return _err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(_request_api_key())
    if not keys:
        # 401, not 502: a caller that sent no credential has a client-side
        # problem, and answering "bad gateway" sent every such misconfiguration
        # downstream looking like a cx2cc outage. See _request_api_key.
        return _err(
            401,
            "No API key supplied (send x-api-key, or Authorization: Bearer)",
            err_type="authentication_error",
        )

    last_upstream = None
    for key in keys:
        try:
            upstream = requests.get(
                url,
                headers={"Authorization": f"Bearer {key}"},
                params=request.args,
                timeout=30,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("%s upstream connection failed: %s", label, type(exc).__name__)
            continue

        if upstream.status_code != 200:
            last_upstream = upstream
            if _should_retry_key(upstream.status_code, upstream.text[:500]):
                _record_key_error(key)
                log.warning("Upstream key retry eligible (%s)", upstream.status_code)
                continue
            return _forward_err(upstream)

        _record_key_ok(key)
        try:
            return jsonify(upstream.json())
        except Exception:
            log.exception("%s response was not valid JSON", label)
            return _err(502, f"Upstream {label} response was not valid JSON")

    if last_upstream is not None:
        return _forward_err(last_upstream)
    return _err(502, "Upstream request failed for all configured keys")


@app.route("/openai/v1/models", methods=["GET"])
@app.route("/v1/models", methods=["GET"])
@app.route("/models", methods=["GET"])
def handle_models():
    # Mirror the bridge's list so the passthrough slugs are visible to clients;
    # the bridge is the single source of truth for what upstream serves. On top
    # of it, annotate what *this* proxy does: alias remaps (`served_as`), the
    # pinned default and the unknown-model policy — so a picker built from this
    # list never shows a selection that silently runs as something else. Fall
    # back to the single configured model if the upstream is unreachable.
    # `?refresh=1` is forwarded so a client can force the bridge past its TTL.
    base = _get_upstream_base_url()
    upstream_meta: dict = {}
    data: list = []
    if base:
        params = {"refresh": "1"} if request.args.get("refresh", "").strip().lower() in ("1", "true", "yes") else None
        try:
            upstream_list = requests.get(f"{base}/models", params=params, timeout=8).json()
            data = [m for m in upstream_list.get("data", []) if m.get("id")]
            upstream_meta = {
                k: upstream_list.get(k)
                for k in ("client_version", "catalog_fetched_at", "catalog_stale", "catalog_error")
                if k in upstream_list
            }
        except Exception:
            log.warning("upstream /models unreachable; falling back to single model")
    if not data:
        data = [{
            "id": upstream_model(),
            "object": "model",
            "created": 1,
            "owned_by": "upstream",
            "source": "config",
        }]
        upstream_meta.setdefault("catalog_stale", True)
    annotated = annotate_model_catalog(data)
    return jsonify({"object": "list", **upstream_meta, **annotated})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "upstream_configured": bool(_get_upstream_base_url())})


def _err(code: int, msg: str, err_type: str = "api_error"):
    return jsonify({"type": "error", "error": {"type": err_type, "message": msg}}), code


def _openai_err(code: int, msg: str, err_type: str = "invalid_request_error"):
    """OpenAI-shaped error envelope for the /openai passthrough path.

    OpenAI clients don't recognize Anthropic's `{"type": "error", "error": ...}`
    shape and surface it as an unparseable response, so the OpenAI path uses
    the `{"error": {"message", "type", "code"}}` shape the OpenAI SDK expects.
    """
    return jsonify({"error": {"message": msg, "type": err_type, "code": None}}), code


def _openai_sse_error(exc: Exception) -> bytes:
    """OpenAI-shaped SSE error frame for a stream that died after its headers."""
    payload = {
        "error": {
            "type": "api_error",
            "message": (
                f"Upstream ended the stream early ({type(exc).__name__}). "
                "The request did not complete; retry it."
            ),
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n".encode("utf-8")


def _forward_openai_err(upstream):
    log.error("Upstream request failed with status %s", upstream.status_code)
    return _openai_err(
        upstream.status_code,
        f"Upstream request failed with status {upstream.status_code}",
        err_type="api_error",
    )


def _sse_stream_error(exc: Exception) -> str:
    """An Anthropic `error` SSE event, for a stream that died after its headers.

    The status line is long gone by then, so this is the only channel left to
    tell the client what happened.
    """
    payload = {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": (
                f"Upstream ended the stream early ({type(exc).__name__}). "
                "The request did not complete; retry it."
            ),
        },
    }
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=True)}\n\n"


def _forward_err(upstream):
    log.error("Upstream request failed with status %s", upstream.status_code)
    return _err(upstream.status_code, f"Upstream request failed with status {upstream.status_code}")


def main() -> None:
    log.info("cx2cc starting on %s:%s", LISTEN_HOST, LISTEN_PORT)
    log.info("upstream_configured=%s model=%s", bool(_get_upstream_base_url()), upstream_model())
    # Werkzeug's dev server answers every request with `Connection: close` and
    # tears the socket down the instant the SSE generator finishes. Clients that
    # talk to it over a raw TCP forward (txfa608's sshd RemoteForward) hit a
    # close-vs-last-bytes race on fast tiny streams and report "Connection
    # closed mid-response" (claude-cli 2.1.226 then retries the turn forever).
    # Waitress keeps connections alive and closes cleanly, which removes the
    # race; fall back to werkzeug if waitress is ever missing.
    try:
        from waitress import serve as _serve
    except ImportError:
        log.warning("waitress not installed; falling back to werkzeug dev server")
        app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)
        return
    # threads: each in-flight request (streams included) holds a thread for its
    # whole lifetime, so this is the concurrent-request ceiling for the fleet.
    # connection_limit (waitress default 100): counts keep-alive sockets too;
    # at fleet scale the default flaps into "no longer accepting new
    # connections" and even /health times out (observed 2026-08-24).
    _serve(
        app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        threads=64,
        connection_limit=512,
        channel_timeout=900,
    )


if __name__ == "__main__":
    main()
