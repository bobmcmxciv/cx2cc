"""
cx2cc - Anthropic Messages API -> OpenAI Chat Completions API translation proxy.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

from translator import (
    UPSTREAM_MODEL,
    stream_translate,
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


def _usage_url() -> str | None:
    """Upstream endpoint behind GET /usage.

    Defaults to `<base URL without its /v1 suffix>/usage`, which matches
    codex-bridge; `CX2CC_USAGE_URL` overrides it for upstreams that expose
    usage elsewhere. Upstreams without such an endpoint answer 404, which is
    forwarded as-is.
    """
    explicit = os.environ.get("CX2CC_USAGE_URL", "").strip()
    if explicit:
        return explicit
    base_url = _get_upstream_base_url()
    if not base_url:
        return None
    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
    return f"{root}/usage"


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
    except Exception:
        log.exception("Request translation failed")
        return _err(400, "Translation error")

    if stream:
        return _handle_stream(openai_body, model_name, api_key=api_key)
    return _handle_nonstream(openai_body, model_name, api_key=api_key)


def _handle_stream(openai_body: dict, display_model: str, api_key: str = ""):
    chat_url = _chat_url()
    if not chat_url:
        return _err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(api_key)
    if not keys:
        return _err(502, "No upstream API key configured (set x-api-key header or CX2CC_UPSTREAM_API_KEY env)")

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

        def generate():
            try:
                for chunk in stream_translate(
                    upstream, display_model, use_upstream_model=_report_upstream_model()
                ):
                    yield chunk
            except Exception:
                log.exception("Stream translation error")

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    if last_upstream is not None:
        return _forward_err(last_upstream)
    return _err(502, "Upstream request failed for all configured keys")


def _handle_nonstream(openai_body: dict, display_model: str, api_key: str = ""):
    chat_url = _chat_url()
    if not chat_url:
        return _err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(api_key)
    if not keys:
        return _err(502, "No upstream API key configured (set x-api-key header or CX2CC_UPSTREAM_API_KEY env)")

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
                upstream.json(), display_model, use_upstream_model=_report_upstream_model()
            )
        except Exception:
            log.exception("Response translation failed")
            return _err(500, "Response translation error")

        log.info(
            "<- %s | in=%s out=%s",
            anthropic_resp["stop_reason"],
            anthropic_resp["usage"]["input_tokens"],
            anthropic_resp["usage"]["output_tokens"],
        )
        return jsonify(anthropic_resp)

    if last_upstream is not None:
        return _forward_err(last_upstream)
    return _err(502, "Upstream request failed for all configured keys")


@app.route("/v1/usage", methods=["GET"])
@app.route("/usage", methods=["GET"])
def handle_usage():
    """Forward the upstream's usage/quota JSON so CC Switch can display it.

    Same key discipline as /v1/messages: the caller's key is passed through as
    the upstream bearer token, so an invalid caller key comes back as the
    upstream's 401 rather than leaking anything. The body is returned verbatim
    on success only; upstream error bodies stay hidden as elsewhere.
    """
    usage_url = _usage_url()
    if not usage_url:
        return _err(502, "No upstream base URL configured (set CX2CC_UPSTREAM_BASE_URL)")

    keys = _candidate_keys(_request_api_key())
    if not keys:
        return _err(502, "No upstream API key configured (set x-api-key header or CX2CC_UPSTREAM_API_KEY env)")

    last_upstream = None
    for key in keys:
        try:
            upstream = requests.get(
                usage_url,
                headers={"Authorization": f"Bearer {key}"},
                timeout=30,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            _record_key_error(key)
            log.warning("Usage upstream connection failed: %s", type(exc).__name__)
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
            log.exception("Usage response was not valid JSON")
            return _err(502, "Upstream usage response was not valid JSON")

    if last_upstream is not None:
        return _forward_err(last_upstream)
    return _err(502, "Upstream request failed for all configured keys")


@app.route("/v1/models", methods=["GET"])
def handle_models():
    return jsonify(
        {
            "data": [
                {
                    "id": upstream_model(),
                    "object": "model",
                    "created": 1,
                    "owned_by": "upstream",
                }
            ]
        }
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "upstream_configured": bool(_get_upstream_base_url())})


def _err(code: int, msg: str):
    return jsonify({"type": "error", "error": {"type": "api_error", "message": msg}}), code


def _forward_err(upstream):
    log.error("Upstream request failed with status %s", upstream.status_code)
    return _err(upstream.status_code, f"Upstream request failed with status {upstream.status_code}")


def main() -> None:
    log.info("cx2cc starting on %s:%s", LISTEN_HOST, LISTEN_PORT)
    log.info("upstream_configured=%s model=%s", bool(_get_upstream_base_url()), upstream_model())
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
