"""Token usage extraction from the three response dialects cx2cc speaks.

cx2cc answers in the Anthropic Messages shape (/v1/messages), the OpenAI Chat
Completions shape (/v1/chat/completions) and the OpenAI Responses shape
(/v1/responses), each either as one JSON body or as an SSE stream. The gateway
passes every byte through untouched and only reads along to learn the model
that served the request, its token counts and any error.

Counting follows OpenAI semantics, which is also what cx2cc reports on the
Anthropic path: `input_tokens` already includes the cached part.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

_MAX_PARTIAL_LINE = 8 * 1024 * 1024


@dataclass
class Usage:
    model: str | None = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    def apply(self, u) -> None:
        if not isinstance(u, dict):
            return
        inp = _first_int(u, "input_tokens", "prompt_tokens")
        out = _first_int(u, "output_tokens", "completion_tokens")
        cached = u.get("cache_read_input_tokens")
        if cached is None:
            cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        if cached is None:
            cached = (u.get("input_tokens_details") or {}).get("cached_tokens")
        if inp is None and out is None:
            return
        # Later frames supersede earlier ones (message_start only carries an
        # estimate; message_delta carries the real figures).
        self.input_tokens = max(int(inp or 0), 0)
        self.output_tokens = max(int(out or 0), 0)
        self.cached_tokens = min(max(_as_int(cached), 0), self.input_tokens)

    def set_model(self, model) -> None:
        if isinstance(model, str) and model and self.model is None:
            self.model = model[:80]

    def set_error(self, err) -> None:
        if self.error:
            return
        if isinstance(err, dict):
            text = err.get("message") or err.get("type") or err.get("code")
        else:
            text = err
        if text:
            self.error = str(text)[:300]

    def weighted(self, w_uncached: float, w_cached: float, w_output: float) -> int:
        uncached = self.input_tokens - self.cached_tokens
        return int(round(uncached * w_uncached + self.cached_tokens * w_cached + self.output_tokens * w_output))


def _as_int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _first_int(d: dict, *names):
    for n in names:
        v = d.get(n)
        if v is not None:
            return _as_int(v)
    return None


def apply_object(usage: Usage, obj) -> None:
    """Fold one decoded JSON object (an SSE frame or a whole body) into usage."""
    if not isinstance(obj, dict):
        return
    t = obj.get("type")
    if t == "message_start":
        msg = obj.get("message") or {}
        usage.set_model(msg.get("model"))
        usage.apply(msg.get("usage"))
    elif t == "message_delta":
        usage.apply(obj.get("usage"))
    elif t == "error":
        usage.set_error(obj.get("error") if isinstance(obj.get("error"), dict) else obj)
    elif isinstance(t, str) and t.startswith("response."):
        resp = obj.get("response")
        if isinstance(resp, dict):
            usage.set_model(resp.get("model"))
            usage.apply(resp.get("usage"))
            if t in ("response.failed", "response.incomplete"):
                usage.set_error(resp.get("error") or (resp.get("incomplete_details") or {}).get("reason") or t)
    else:
        # Chat Completions chunk / body, Anthropic message body, Images body,
        # or an OpenAI-shaped error envelope.
        usage.set_model(obj.get("model"))
        usage.apply(obj.get("usage"))
        if isinstance(obj.get("error"), dict):
            usage.set_error(obj["error"])


class SSESniffer:
    """Reads SSE bytes as they stream past and keeps the usage it sees."""

    def __init__(self) -> None:
        self.usage = Usage()
        self._partial = b""

    def feed(self, chunk: bytes) -> None:
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        if len(self._partial) > _MAX_PARTIAL_LINE:
            self._partial = b""
        for line in lines:
            self._line(line)

    def finish(self) -> Usage:
        if self._partial:
            self._line(self._partial)
            self._partial = b""
        return self.usage

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        interesting = (
            b'"usage"' in payload
            or b'"error"' in payload
            or b'"response.' in payload
            or (self.usage.model is None and b'"model"' in payload)
        )
        if not interesting:
            return
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        apply_object(self.usage, obj)


def from_body(body: bytes) -> Usage:
    usage = Usage()
    try:
        obj = json.loads(body)
    except ValueError:
        return usage
    apply_object(usage, obj)
    return usage
