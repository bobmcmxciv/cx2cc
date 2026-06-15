"""
cx2cc - Anthropic Messages API -> OpenAI Chat Completions API translation proxy.
"""
from __future__ import annotations

import logging
import os
import re
import threading

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

from translator import UPSTREAM_MODEL, stream_translate, translate_request, translate_response

load_dotenv(verbose=False)

LISTEN_PORT = int(os.environ.get("CX2CC_PORT", "8901"))
LISTEN_HOST = os.environ.get("CX2CC_HOST", "127.0.0.1")

_FALLBACK_RAW_KEYS = os.environ.get(
    "CX2CC_UPSTREAM_API_KEYS",
    os.environ.get("CX2CC_UPSTREAM_API_KEY", ""),
)
_FALLBACK_API_KEYS = [k.strip() for k in re.split(r"[,\n]+", _FALLBACK_RAW_KEYS) if k.strip()]
_key_lock = threading.Lock()
_key_index = 0
_key_errors: dict[str, int] = {}

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cx2cc")


def _get_upstream_base_url() -> str | None:
    base_url = os.environ.get("CX2CC_UPSTREAM_BASE_URL", "").strip().rstrip("/")
    return base_url or None


def _chat_url() -> str | None:
    base_url = _get_upstream_base_url()
    if not base_url:
        return None
    return f"{base_url}/chat/completions"


def _get_fallback_key() -> str | None:
    global _key_index
    if not _FALLBACK_API_KEYS:
        return None
    with _key_lock:
        start = _key_index
        n = len(_FALLBACK_API_KEYS)
        while True:
            key = _FALLBACK_API_KEYS[_key_index]
            _key_index = (_key_index + 1) % n
            if _key_errors.get(key, 0) < 3:
                return key
            if _key_index == start:
                _key_errors.clear()
                return _FALLBACK_API_KEYS[0]


def _candidate_keys(request_key: str) -> list[str]:
    keys: list[str] = []
    if request_key:
        keys.append(request_key)

    for _ in range(len(_FALLBACK_API_KEYS)):
        key = _get_fallback_key()
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
    api_key = request.headers.get("x-api-key", "").strip()

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
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            _record_key_error(key)
            log.warning("Stream connect error with an upstream key: %s", e)
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
                for chunk in stream_translate(upstream, display_model):
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
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            _record_key_error(key)
            log.warning("Nonstream connect error with an upstream key: %s", e)
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
            anthropic_resp = translate_response(upstream.json(), display_model)
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


@app.route("/v1/models", methods=["GET"])
def handle_models():
    return jsonify(
        {
            "data": [
                {
                    "id": UPSTREAM_MODEL,
                    "object": "model",
                    "created": 1,
                    "owned_by": "upstream",
                }
            ]
        }
    )


@app.route("/health", methods=["GET"])
def health():
    upstream = _get_upstream_base_url()
    return jsonify({"status": "ok", "upstream_configured": bool(upstream), "upstream": upstream})


def _err(code: int, msg: str):
    return jsonify({"type": "error", "error": {"type": "api_error", "message": msg}}), code


def _forward_err(upstream):
    try:
        body = upstream.json()
        msg = body.get("error", {}).get("message", "") or upstream.text[:500]
    except Exception:
        msg = upstream.text[:500]
    log.error("Upstream %s: %s", upstream.status_code, msg)
    return _err(upstream.status_code, f"Upstream: {msg}")


if __name__ == "__main__":
    upstream = _get_upstream_base_url()
    log.info("cx2cc starting on %s:%s", LISTEN_HOST, LISTEN_PORT)
    log.info("Upstream: %s -> %s", upstream or "UNCONFIGURED", UPSTREAM_MODEL)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)
