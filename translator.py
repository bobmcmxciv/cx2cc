"""
cx2cc 格式翻译核心: Anthropic Messages API <-> OpenAI Chat Completions API
"""
from __future__ import annotations
import json
import os
import uuid
from itertools import chain

DEFAULT_UPSTREAM_MODEL = "gpt-5.5"
# Kept as a module constant for backwards compatibility with existing imports.
UPSTREAM_MODEL = DEFAULT_UPSTREAM_MODEL


def upstream_model() -> str:
    """Model name to request upstream.

    Read at call time rather than import time so it picks up `.env`, which
    server.py loads after importing this module.
    """
    return os.environ.get("CX2CC_UPSTREAM_MODEL", "").strip() or DEFAULT_UPSTREAM_MODEL


def prompt_cache_key_enabled() -> bool:
    """Whether to attach a per-conversation `prompt_cache_key` upstream.

    On by default; set CX2CC_PROMPT_CACHE_KEY=off for upstreams that reject
    unknown request fields.
    """
    return os.environ.get("CX2CC_PROMPT_CACHE_KEY", "").strip().lower() not in (
        "0", "off", "false", "no",
    )


def _prompt_cache_key(body: dict) -> str:
    """Stable per-conversation cache key for the upstream.

    OpenAI-compatible backends route prompt-cache lookups by `prompt_cache_key`:
    without one, consecutive turns of the same conversation can land on
    different cache nodes and miss a cache that provably exists (observed hit
    rates around 13% on agentic sessions, versus 99% once keyed). Every turn of
    one conversation repeats the same system prompt and first user message, so
    hashing those yields a constant key per conversation while distinct
    conversations still spread across nodes.
    """
    first_user = None
    for msg in body.get("messages", []):
        if msg.get("role") == "user":
            first_user = msg.get("content")
            break
    seed = json.dumps(
        [body.get("system", ""), first_user],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

# ============================================================
# 请求翻译: Anthropic -> OpenAI
# ============================================================

def translate_request(body: dict) -> dict:
    """将 Anthropic Messages API 请求翻译为 OpenAI Chat Completions 格式"""
    openai_body = {
        "model": upstream_model(),
        "messages": [],
    }

    # System prompt -> system role message
    system_content = body.get("system")
    if system_content:
        if isinstance(system_content, list):
            system_text = "\n".join(
                b["text"] for b in system_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            system_text = str(system_content)
        if system_text.strip():
            openai_body["messages"].append({"role": "system", "content": system_text})

    # Messages: content blocks -> OpenAI messages
    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, str):
            openai_body["messages"].append({"role": role, "content": content})
        elif isinstance(content, list):
            converted = _convert_content_blocks(role, content)
            openai_body["messages"].extend(converted)

    # Tools
    tools = body.get("tools")
    if tools:
        openai_body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}})
                }
            }
            for t in tools
            if t.get("name")  # skip built-in tools like computer, bash, text_editor
        ]

    # Tool choice
    tc = body.get("tool_choice")
    if tc:
        openai_body["tool_choice"] = _convert_tool_choice(tc)

    # Simple passthrough fields
    for a_field, o_field in [
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stream", "stream"),
    ]:
        if a_field in body:
            openai_body[o_field] = body[a_field]

    # OpenAI-compatible upstreams omit the usage chunk while streaming unless it is
    # explicitly requested, so without this the whole stream carries no token counts.
    if openai_body.get("stream"):
        openai_body["stream_options"] = {"include_usage": True}

    if body.get("stop_sequences"):
        openai_body["stop"] = body["stop_sequences"]

    if body.get("top_k") is not None:
        pass  # OpenAI doesn't support top_k

    if prompt_cache_key_enabled():
        openai_body["prompt_cache_key"] = _prompt_cache_key(body)

    return openai_body


def _convert_content_blocks(role: str, blocks: list) -> list:
    """将 Anthropic content_blocks 数组转换为 OpenAI message(s)"""
    text_parts = []
    openai_content = []
    tool_calls = []
    tool_msgs = []

    for block in blocks:
        if not isinstance(block, dict):
            continue
        t = block.get("type")

        if t == "text":
            text = block.get("text", "")
            text_parts.append(text)
            openai_content.append({"type": "text", "text": text})
        elif t == "tool_use" and role == "assistant":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=True)
                }
            })
        elif t == "tool_result" and role == "user":
            content = block.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
                )
            tool_msgs.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": str(content)
            })
        elif t == "image" and role == "user":
            image_url = _convert_image_block(block)
            if image_url:
                openai_content.append({"type": "image_url", "image_url": {"url": image_url}})
        elif t in ("thinking", "redacted_thinking"):
            pass  # 丢弃

    messages = []

    if role == "assistant":
        content_str = "\n".join(text_parts) if text_parts else None
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": content_str,
                "tool_calls": tool_calls
            })
        elif content_str:
            messages.append({"role": "assistant", "content": content_str})
    else:  # user
        non_tool_content = openai_content or ([{"type": "text", "text": "\n".join(text_parts)}] if text_parts else [])
        if tool_msgs:
            if non_tool_content:
                messages.append({"role": "user", "content": non_tool_content if len(non_tool_content) > 1 else non_tool_content[0]["text"]})
            messages.extend(tool_msgs)
        elif non_tool_content:
            messages.append({"role": "user", "content": non_tool_content if len(non_tool_content) > 1 else non_tool_content[0]["text"]})

    return messages


def _convert_image_block(block: dict) -> str | None:
    source = block.get("source") or {}
    source_type = source.get("type")

    if source_type == "base64":
        media_type = source.get("media_type", "image/jpeg")
        data = source.get("data")
        if data:
            return f"data:{media_type};base64,{data}"
    if source_type == "url":
        return source.get("url")
    return None


def _convert_tool_choice(tc):
    """Anthropic tool_choice -> OpenAI tool_choice"""
    if isinstance(tc, dict):
        t = tc.get("type", "auto")
        if t == "any":
            return "required"
        elif t == "tool":
            return {"type": "function", "function": {"name": tc.get("name", "")}}
        elif t == "auto":
            return "auto"
    if tc == "any":
        return "required"
    return tc


# ============================================================
# 非流式响应翻译: OpenAI -> Anthropic
# ============================================================

def translate_response(
    openai_body: dict,
    display_model: str = "claude-sonnet-4-6",
    use_upstream_model: bool = False,
) -> dict:
    """OpenAI Chat Completion -> Anthropic Message

    With ``use_upstream_model``, the reported model is the one the upstream says
    it served, instead of echoing back whatever the client asked for.
    """
    if use_upstream_model:
        display_model = openai_body.get("model") or display_model
    choice = openai_body.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")
    usage = openai_body.get("usage", {})

    content_blocks = []

    text_content = message.get("content")
    if text_content:
        content_blocks.append({"type": "text", "text": text_content})

    for tc in message.get("tool_calls", []) or []:
        fn = tc.get("function", {})
        try:
            inp = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            inp = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": fn.get("name", ""),
            "input": inp
        })

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": display_model,
        "content": content_blocks,
        "stop_reason": _map_finish_reason(finish),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    }


def _map_finish_reason(fr: str) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }.get(fr, "end_turn")


# ============================================================
# 流式 SSE 响应翻译: OpenAI -> Anthropic
# ============================================================

def stream_translate(
    response,
    display_model: str = "claude-sonnet-4-6",
    use_upstream_model: bool = False,
):
    """生成器: 从 OpenAI SSE stream 读取，逐事件生成 Anthropic SSE

    With ``use_upstream_model``, the first upstream chunk is read before
    message_start is emitted, so the model reported to the client is the one that
    actually served the request rather than the one the client asked for.
    """
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    lines = response.iter_lines(decode_unicode=True)
    buffered: list[str] = []
    if use_upstream_model:
        for line in lines:
            buffered.append(line)
            if line and line.startswith("data: "):
                payload = line[6:].strip()
                if payload and payload != "[DONE]":
                    try:
                        served = json.loads(payload).get("model")
                    except json.JSONDecodeError:
                        served = None
                    if served:
                        display_model = served
                    break

    # Emit message_start
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": display_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}
        }
    })

    state = "IDLE"  # IDLE | IN_TEXT | IN_TOOL
    content_idx = 0
    tool_states = {}  # idx -> {id, name, args_str, anthropic_idx}
    output_tokens = 0
    input_tokens = 0
    cache_read_tokens = 0
    active_tool_idx = -1
    finish_reason = None

    for line in chain(buffered, lines):
        if not line:
            continue
        if not line.startswith("data: "):
            continue

        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break

        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices", [])
        if not choices:
            continue

        delta = choices[0].get("delta", {}) or {}
        fr = choices[0].get("finish_reason")
        if fr:
            finish_reason = fr

        # --- Text content ---
        text = delta.get("content")
        if text:
            if state == "IN_TOOL" and active_tool_idx >= 0:
                _close_tool(active_tool_idx, tool_states)
                yield _sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": tool_states[active_tool_idx]["anthropic_idx"]
                })
                active_tool_idx = -1
                state = "IDLE"

            if state != "IN_TEXT":
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": content_idx,
                    "content_block": {"type": "text", "text": ""}
                })
                state = "IN_TEXT"

            yield _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": content_idx,
                "delta": {"type": "text_delta", "text": text}
            })

        # --- Tool calls ---
        tool_calls = delta.get("tool_calls") or []
        for tc in tool_calls:
            oai_idx = tc.get("index", 0)

            if oai_idx not in tool_states:
                # New tool call block — close previous block if any
                if state == "IN_TEXT":
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop", "index": content_idx
                    })
                    content_idx += 1
                elif state == "IN_TOOL" and active_tool_idx >= 0:
                    prev_anth_idx = tool_states[active_tool_idx]["anthropic_idx"]
                    _close_tool(active_tool_idx, tool_states)
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop", "index": prev_anth_idx
                    })
                    content_idx += 1

                tool_states[oai_idx] = {
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "args_str": "",
                    "anthropic_idx": content_idx,
                }
                active_tool_idx = oai_idx
                state = "IN_TOOL"

                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": content_idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_states[oai_idx]["id"],
                        "name": tool_states[oai_idx]["name"],
                        "input": {}
                    }
                })

            args = tc.get("function", {}).get("arguments", "")
            if args:
                tool_states[oai_idx]["args_str"] += args
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_states[oai_idx]["anthropic_idx"],
                    "delta": {"type": "input_json_delta", "partial_json": args}
                })

        # Track usage. Streaming upstreams send this once, near the end, so every
        # field has to be captured here or it is lost: the Anthropic side has no
        # other chance to learn the input size.
        u = chunk.get("usage")
        if u:
            output_tokens = u.get("completion_tokens", output_tokens)
            input_tokens = u.get("prompt_tokens", input_tokens)
            details = u.get("prompt_tokens_details") or {}
            cache_read_tokens = details.get("cached_tokens", cache_read_tokens)

    # Close final content block
    if state == "IN_TEXT":
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": content_idx})
    elif state == "IN_TOOL" and active_tool_idx >= 0:
        st = tool_states[active_tool_idx]
        yield _sse("content_block_stop", {
            "type": "content_block_stop", "index": st["anthropic_idx"]
        })

    # message_delta
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": _map_finish_reason(finish_reason or "stop"),
            "stop_sequence": None
        },
        # message_start had to be emitted before the upstream said anything, so it
        # could only claim input_tokens=0. Report the real figures here instead;
        # otherwise every streaming request is recorded as having consumed no
        # input at all, which is what made streamed usage look like zero.
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": 0,
        }
    })

    # message_stop
    yield _sse("message_stop", {"type": "message_stop"})


def _close_tool(idx: int, tool_states: dict):
    """Pop tool state entry (cleanup only — caller emits SSE events)."""
    tool_states.pop(idx, {})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"
