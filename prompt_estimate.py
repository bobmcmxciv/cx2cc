"""Anchored estimate of the prompt size, for `message_start.usage`.

Why this exists
---------------
A Claude Code client reads the *assistant message's* token usage from the
`message_start` event and never revises it from `message_delta` (verified
against Claude Code with hapi CLI 0.25.1: the assistant SDK message keeps
`{"input_tokens": 0, "output_tokens": 0}` while the same turn's `result.usage`
carries the real 28,222). cx2cc has to emit `message_start` before the upstream
has said anything, and an OpenAI-compatible upstream only reports
`prompt_tokens` once the stream ends — so `message_start` could only ever claim
zero, and every session through cx2cc showed an empty context meter
(`0% · 0 / 1.0M`).

The estimate
------------
A conversation's next prompt is highly predictable from its previous one: each
turn resends the whole prefix and appends to it. So keep the last two *real*
measurements per conversation and extrapolate along the line through them —
only the appended part is ever estimated:

    estimate = last_tokens + (cur_chars - last_chars) * delta_ratio
    delta_ratio = (last_tokens - prev_tokens) / (last_chars - prev_chars)

Scaling through the origin (`last_tokens * cur_chars / last_chars`) was tried
first and runs a few percent low, because a request carries a fixed overhead —
system prompt, tool schemas, per-message framing — that does not grow with the
appended text. Differencing two measurements cancels that constant out. The
origin form is kept as the fallback for the first turn after an anchor exists,
and for /compact, where the request shrinks instead of growing.

Properties that matter here:

* **Self-calibrating.** The tokens-per-character ratio is learned from this
  conversation's own measurements, so Chinese, code, and English prose each get
  their own ratio instead of a hardcoded chars/4 that underestimates CJK
  several-fold.
* **Self-correcting.** Every response re-anchors on the real count, so error
  cannot accumulate across turns — only the appended delta is ever estimated.
* **Compaction-safe.** After /compact the request shrinks; the fallback form
  scales down instead of needing a special case.

It is still an estimate. Exactness is not reachable on this path: the upstream
does not report `prompt_tokens` until the response is complete, and
`message_start` must precede the content. The real figures continue to be sent
in `message_delta` (and so remain what the client's `result.usage` reports).
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict

# Cold start only: a conversation's first request has no anchor to scale from.
# A real Claude Code request through this bridge measured 28,269 prompt tokens
# for ~117k serialized characters — 0.24 tokens/char, because the bulk of a
# first request is the English system prompt and tool schemas rather than user
# prose. 0.25 is deliberately at that low end: the client also decides when to
# auto-compact from this number, so a first-request guess that runs high would
# compact a session that had plenty of room, while one that runs low merely
# defers to the real measurement that arrives one call later.
#
# Averaging measurements across conversations was tried and is worse: one
# CJK-heavy conversation (~0.75 tokens/char) dragged the shared ratio up and
# made an unrelated session's first call read 87,669 against a real 28,269.
DEFAULT_TOKENS_PER_CHAR = 0.25

# Bounded so a long-lived process cannot grow without limit; conversations are
# evicted least-recently-used and simply fall back to the cold-start path.
MAX_CONVERSATIONS = 512

# Guard rails for the differenced ratio: a retry that appends almost nothing, or
# a turn whose delta is one huge tool result, can make a single interval wildly
# unrepresentative. Outside this band the safer origin-scaling form is used.
MIN_TOKENS_PER_CHAR = 0.05
MAX_TOKENS_PER_CHAR = 2.0

_lock = threading.Lock()
_anchors: "OrderedDict[str, dict]" = OrderedDict()


# An image's token cost is set by its dimensions, not by the length of its
# base64 payload: a 1920x1080 screenshot serializes to ~330k characters and
# costs the upstream ~1.5k tokens. Measured literally, one such image inflates
# the estimate to ~94k — and the client decides when to auto-compact from this
# number, so a session would compact itself the moment it looked at a
# screenshot. Each image is therefore charged a fixed char budget instead of
# its payload. 6000 lands near the real cost at a typical learned ratio
# (0.25 tokens/char), and errs low by design, as the cold-start constant does.
IMAGE_CHAR_EQUIVALENT = 6000
_IMAGE_STANDIN = {"type": "image_url", "image_url": {"url": "i" * IMAGE_CHAR_EQUIVALENT}}


def _without_image_payloads(messages: list) -> list:
    """Messages with every image payload swapped for a fixed-size stand-in."""
    out = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            out.append(msg)
            continue
        parts = [
            _IMAGE_STANDIN
            if isinstance(p, dict) and p.get("type") == "image_url"
            else p
            for p in content
        ]
        out.append({**msg, "content": parts} if parts != content else msg)
    return out


def request_chars(openai_body: dict) -> int:
    """Serialized size of the parts the upstream counts as prompt tokens.

    Tools and messages only: everything else in the body (model name, sampling
    parameters) is not part of the prompt.
    """
    try:
        payload = [
            openai_body.get("tools") or [],
            _without_image_payloads(openai_body.get("messages") or []),
        ]
        return len(json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


def _delta_ratio(anchor: dict) -> float | None:
    """tokens-per-char of the text appended between the last two measurements."""
    prev = anchor.get("prev")
    if not prev:
        return None
    d_chars = anchor["chars"] - prev["chars"]
    d_tokens = anchor["tokens"] - prev["tokens"]
    if d_chars <= 0 or d_tokens <= 0:
        return None
    ratio = d_tokens / d_chars
    if ratio < MIN_TOKENS_PER_CHAR or ratio > MAX_TOKENS_PER_CHAR:
        return None
    return ratio


def estimate(key: str, chars: int) -> dict:
    """Estimated `{input_tokens, cache_read_input_tokens}` for this request.

    The cache split mirrors the conversation's last real split, so the usage
    the client records stays shaped like the truth instead of booking a fully
    cached prompt as fresh input.
    """
    if chars <= 0:
        return {"input_tokens": 0, "cache_read_input_tokens": 0}

    with _lock:
        anchor = _anchors.get(key)
        if anchor:
            _anchors.move_to_end(key)

    if anchor and anchor["chars"] > 0:
        cache_ratio = anchor["cache_ratio"]
        delta_ratio = _delta_ratio(anchor)
        if delta_ratio is not None and chars >= anchor["chars"]:
            total = int(round(anchor["tokens"] + (chars - anchor["chars"]) * delta_ratio))
        else:
            total = int(round(anchor["tokens"] * chars / anchor["chars"]))
    else:
        total = int(round(chars * DEFAULT_TOKENS_PER_CHAR))
        cache_ratio = 0.0

    total = max(0, total)
    cached = int(round(total * cache_ratio))
    cached = min(cached, total)
    return {"input_tokens": total - cached, "cache_read_input_tokens": cached}


def record(key: str, chars: int, prompt_tokens: int, cache_read_tokens: int = 0) -> None:
    """Re-anchor a conversation on the real counts the upstream reported."""
    if not key or chars <= 0 or prompt_tokens <= 0:
        return
    cache_ratio = 0.0
    if 0 < cache_read_tokens <= prompt_tokens:
        cache_ratio = cache_read_tokens / prompt_tokens
    with _lock:
        previous = _anchors.get(key)
        # The older point has to stay a *distinct* one: a retry resends the same
        # request, and pairing a measurement with itself gives a zero-width
        # interval. Carry the previous interval forward in that case rather than
        # dropping back to origin scaling for the rest of the conversation.
        prev = None
        if previous:
            prev = (
                {"chars": previous["chars"], "tokens": previous["tokens"]}
                if previous["chars"] != chars
                else previous.get("prev")
            )
        _anchors[key] = {
            "chars": chars,
            "tokens": prompt_tokens,
            "cache_ratio": cache_ratio,
            "prev": prev,
        }
        _anchors.move_to_end(key)
        while len(_anchors) > MAX_CONVERSATIONS:
            _anchors.popitem(last=False)


def stats() -> dict:
    with _lock:
        return {"conversations": len(_anchors)}
