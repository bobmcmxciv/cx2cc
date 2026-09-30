"""
cx2cc 格式翻译核心: Anthropic Messages API <-> OpenAI Chat Completions API
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import time
import uuid
from itertools import chain
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

log = logging.getLogger("cx2cc.translator")

DEFAULT_UPSTREAM_MODEL = "gpt-5.5"
# Kept as a module constant for backwards compatibility with existing imports.
UPSTREAM_MODEL = DEFAULT_UPSTREAM_MODEL


def upstream_model() -> str:
    """Model name to request upstream.

    Read at call time rather than import time so it picks up `.env`, which
    server.py loads after importing this module.
    """
    return os.environ.get("CX2CC_UPSTREAM_MODEL", "").strip() or DEFAULT_UPSTREAM_MODEL


def _strip_window_suffix(name: str) -> str:
    """Drop a trailing [..] marker (e.g. gpt-5.4[1m]) before allowlist checks.

    Claude Code encodes context-window overrides in the model-name suffix, so
    the 1M lane arrives as "gpt-5.4[1m]"; without stripping it fails the
    allowlist and silently serves the pinned 272k model (2026-08-11,
    HANDOFF-models-20260812)."""
    name = str(name or "").strip()
    if name.endswith("]"):
        i = name.rfind("[")
        if i > 0:
            return name[:i].strip()
    return name


# The bridge's /models (upstream catalog, all slugs verified serving) is the
# source of truth; cached here so resolve_model stays cheap per request.
_ALLOWED_TTL = 600
_allowed_cache: dict = {"ts": 0.0, "slugs": frozenset()}


def _upstream_allowed() -> frozenset:
    now = time.time()
    if now - _allowed_cache["ts"] < _ALLOWED_TTL:
        return _allowed_cache["slugs"]
    base = os.environ.get("CX2CC_UPSTREAM_BASE_URL", "").strip().rstrip("/")
    slugs = _allowed_cache["slugs"]
    if base:
        try:
            data = requests.get(f"{base}/models", timeout=5).json().get("data", [])
            fetched = frozenset(m.get("id") for m in data if m.get("id"))
            if fetched:
                slugs = fetched
        except Exception as exc:
            log.warning("upstream /models fetch failed, keeping cached allowlist: %s", exc)
    _allowed_cache["slugs"] = slugs
    _allowed_cache["ts"] = now
    return slugs


def _model_aliases() -> dict:
    """Client slug -> upstream slug remaps from CX2CC_MODEL_ALIASES.

    Format: "from=to,from2=to2". Applied after the window-suffix strip and
    before the passthrough allowlist, so a fleet that pins an old slug in
    ANTHROPIC_MODEL (e.g. gpt-5.6-sol) can be moved to a new default from the
    server side without touching every client first. Malformed entries are
    ignored. Empty by default, so nothing is remapped unless configured."""
    out: dict = {}
    for pair in os.environ.get("CX2CC_MODEL_ALIASES", "").split(","):
        if "=" not in pair:
            continue
        src, dst = pair.split("=", 1)
        if src.strip() and dst.strip():
            out[src.strip()] = dst.strip()
    return out


class UnknownModelError(ValueError):
    """An explicitly requested model that this proxy cannot serve.

    Raised instead of silently substituting the pinned default, so a picker
    that offered the slug (or a typo) fails loudly at the caller rather than
    running a different model than the one selected."""

    def __init__(self, requested: str, allowed: list[str]):
        self.requested = requested
        self.allowed = allowed
        super().__init__(
            f"model '{requested}' is not served by this proxy; available: {', '.join(allowed)}"
        )


# Names Claude Code sends on its own (no explicit choice by the caller): its
# built-in aliases and the concrete claude-* ids they resolve to. These carry no
# intent about a GPT slug, so they take the pinned upstream default.
_CLAUDE_FAMILY_ALIASES = frozenset({"opus", "sonnet", "haiku", "fable", "opusplan", "default", "auto"})


def _is_claude_family(name: str) -> bool:
    base = _strip_window_suffix(name).lower()
    return base.startswith("claude-") or base in _CLAUDE_FAMILY_ALIASES


def _unknown_model_policy() -> str:
    """CX2CC_UNKNOWN_MODEL=reject (default) | default.

    `reject` answers an explicit non-Claude slug the proxy cannot serve with a
    400; `default` restores the pre-2026-09-06 behaviour of silently routing it
    to CX2CC_UPSTREAM_MODEL."""
    value = os.environ.get("CX2CC_UNKNOWN_MODEL", "").strip().lower()
    return "default" if value == "default" else "reject"


def _passthrough_allowlist() -> set:
    return {
        m.strip()
        for m in os.environ.get("CX2CC_MODEL_PASSTHROUGH", "").split(",")
        if m.strip()
    } | set(_upstream_allowed())


def resolve_model(requested) -> str:
    """Model to send upstream for a client-requested model name.

    The allowlist is CX2CC_MODEL_PASSTHROUGH (comma-separated, static) plus
    whatever the upstream bridge advertises on /models. CX2CC_MODEL_ALIASES
    remaps a requested slug first (after stripping a trailing [..] window
    marker). Claude Code's own default names (claude-*, opus, sonnet, ...)
    route to the pinned upstream model. Any other slug that is neither
    allowlisted nor aliased raises UnknownModelError under the default
    CX2CC_UNKNOWN_MODEL=reject policy instead of being swapped silently."""
    allowed = _passthrough_allowlist()
    req = _strip_window_suffix(requested)
    aliased = _model_aliases().get(req, req)
    if aliased in allowed:
        return aliased
    if req in allowed:
        # Alias target is not servable (misconfigured CX2CC_MODEL_ALIASES):
        # keep the long-standing fallback to the pinned default.
        return upstream_model()
    if not req or _is_claude_family(req) or _unknown_model_policy() == "default":
        return upstream_model()
    raise UnknownModelError(str(requested), sorted(allowed | set(_model_aliases())))


def annotate_model_catalog(entries: list) -> dict:
    """Decorate an upstream /models listing with what *this* proxy does to it.

    - `served_as` on every entry whose id is remapped by CX2CC_MODEL_ALIASES
      (alias sources missing from the upstream list are appended with
      `source: "alias"`), so a picker can show "gpt-5.6-sol → gpt-6-astra"
      instead of pretending the selection runs unchanged;
    - `is_default` on the pinned upstream model;
    - the alias map, default model and unknown-model policy at top level."""
    aliases = _model_aliases()
    default = upstream_model()
    out: list[dict] = []
    seen: set = set()
    for raw in entries:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        entry = dict(raw)
        mid = entry["id"]
        entry["is_default"] = mid == default
        if mid in aliases and aliases[mid] != mid:
            entry["served_as"] = aliases[mid]
        out.append(entry)
        seen.add(mid)
    for src, dst in aliases.items():
        if src in seen or src == dst:
            continue
        out.append({
            "id": src, "object": "model", "created": 1, "owned_by": "cx2cc",
            "served_as": dst, "is_default": False, "source": "alias",
        })
        seen.add(src)
    return {
        "data": out,
        "default_model": default,
        "aliases": dict(aliases),
        "unknown_model_policy": _unknown_model_policy(),
    }


# An Anthropic tool_result may carry image blocks (Claude Code's Read tool
# returns a PNG that way). OpenAI's `tool` role is text-only, so the images ride
# in a user message emitted right after the tool messages. Both strings are
# constant on purpose — anything varying per request would break the upstream
# prompt cache at that offset for the rest of the conversation.
TOOL_RESULT_IMAGE_PLACEHOLDER = "[image output — the image itself follows in the next message]"
TOOL_RESULT_IMAGE_PREAMBLE = "Image content returned by the tool call(s) above:"


def prompt_cache_key_enabled() -> bool:
    """Whether to attach a per-conversation `prompt_cache_key` upstream.

    On by default; set CX2CC_PROMPT_CACHE_KEY=off for upstreams that reject
    unknown request fields.
    """
    return os.environ.get("CX2CC_PROMPT_CACHE_KEY", "").strip().lower() not in (
        "0", "off", "false", "no",
    )


DEFAULT_STYLE_PROMPT = """
<response_style>
以下是对本次会话**面向人类的文字输出**的格式要求，优先级高于其它风格约定；它只约束你写给用户看的文字，不改变工具调用、代码内容与判断标准。

**先说结论，再给依据。** 每次开口先给出这一步的结果或判断，再补支撑细节。不要把结论留到最后一段。

**成段写，不要碎片化。** 一次输出就是一个完整的意思单元：用完整自然段（通常 2-5 句）表达，句子之间要有逻辑连接（因此/但是/所以先…再…）。禁止把一个意思拆成多次几十字的短输出。

**不要为每个工具调用配旁白。** 连续执行多个工具时保持静默，把它们当作一个动作组；等这组动作有了结果，再用一段话交代"做了什么 → 得到什么 → 因此下一步"。只有当你要做的事有风险、耗时很长、或偏离用户预期时，才在动手前单独说明。

**不复述工具输出。** 用户能看到命令和结果。只写输出里**不能直接读出**的东西：它意味着什么、是否符合预期、下一步因此怎么变。

**结构服从内容，不要套模板。** 三项以上可并列、可对照的事实才用列表或表格；一两点就用句子说完。不要给短回答加小标题，不要用标题包裹只有一句话的段落。

**长度与信息量匹配。** 简单问题两三句话答完；复杂结论才展开。宁可一段密实的话，也不要五行空洞的条目。
</response_style>
""".strip()


def style_prompt() -> str:
    """Human-facing output style addendum appended to the system prompt.

    Upstreams driven through a Claude-shaped harness tend to emit one short
    narration block per tool call, which fragments the transcript (observed:
    194 assistant text blocks in one session, median 65 chars, 57% under 80).
    This addendum asks for consolidated paragraphs instead.

    Set CX2CC_STYLE=off to disable, or CX2CC_STYLE_PROMPT to a file path or
    literal text to override the default.
    """
    if os.environ.get("CX2CC_STYLE", "").strip().lower() in ("0", "off", "false", "no"):
        return ""
    override = os.environ.get("CX2CC_STYLE_PROMPT", "").strip()
    if override:
        try:
            if os.path.isfile(override):
                with open(override, encoding="utf-8") as fh:
                    return fh.read().strip()
        except OSError as exc:
            log.warning("CX2CC_STYLE_PROMPT unreadable (%s); using inline value", exc)
        return override
    return DEFAULT_STYLE_PROMPT


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


def prepare_openai_passthrough(body: dict) -> dict:
    """Prepare a client-sent OpenAI Chat Completions body for the upstream.

    Mirrors the small conveniences translate_request adds when the client
    speaks Anthropic — model allowlisting, prompt_cache_key attachment, and
    include_usage on streams — without touching the semantic shape of the
    request. The style prompt is intentionally left off: OpenAI-format callers
    compose their own system messages and typically don't want the Claude
    Code output rules injected here.
    """
    prepared = dict(body)
    prepared["model"] = resolve_model(prepared.get("model"))

    if prompt_cache_key_enabled() and not prepared.get("prompt_cache_key"):
        prepared["prompt_cache_key"] = _prompt_cache_key_from_openai(prepared)

    if prepared.get("stream") and "stream_options" not in prepared:
        prepared["stream_options"] = {"include_usage": True}

    return prepared


def _prompt_cache_key_from_openai(body: dict) -> str:
    """Same idea as _prompt_cache_key, but the body is already in OpenAI shape.

    OpenAI puts the system prompt as a `role: system` message rather than a
    top-level `system` field, so we pull it out of the messages list before
    hashing. Keying on (first system, first user) keeps every turn of one
    conversation on the same upstream cache node.
    """
    system_text = ""
    first_user = None
    for msg in body.get("messages", []):
        role = msg.get("role")
        content = msg.get("content")
        if role == "system" and not system_text:
            system_text = _openai_content_text(content)
        elif role == "user" and first_user is None:
            first_user = content
            break
    seed = json.dumps(
        [system_text, first_user],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _openai_content_text(content) -> str:
    """Flatten an OpenAI message content (str or parts list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


def request_diag(openai_body: dict) -> str:
    """One-line fingerprint of the translated request, for cache diagnostics.

    A cumulative hash over the serialized request is snapshotted at fixed
    message indexes. Consecutive turns of one conversation resend the previous
    prompt as a prefix, so at every index both turns cover, the snapshots must
    be identical. When upstream `cached_tokens` collapses between turns, the
    first index whose snapshot changed brackets where the prompt diverged —
    and if none changed, the content was intact and the upstream simply
    dropped a cache it could have used. That distinction cannot be recovered
    after the fact, which is why it is logged per request.
    """
    tools = openai_body.get("tools", [])
    msgs = openai_body.get("messages", [])
    h = hashlib.sha1()
    h.update(json.dumps(tools, ensure_ascii=False).encode("utf-8"))
    marks = [f"t:{h.hexdigest()[:8]}"]
    for i, m in enumerate(msgs, 1):
        h.update(json.dumps(m, ensure_ascii=False).encode("utf-8"))
        if i in (1, 2, 8, 32, 128, 512):
            marks.append(f"{i}:{h.hexdigest()[:8]}")
    marks.append(f"{len(msgs)}:{h.hexdigest()[:8]}")
    key = openai_body.get("prompt_cache_key", "-")
    return f"key={key[:8]} tools={len(tools)} h[{' '.join(marks)}]"

# ============================================================
# 请求翻译: Anthropic -> OpenAI
# ============================================================

def translate_request(body: dict) -> dict:
    """将 Anthropic Messages API 请求翻译为 OpenAI Chat Completions 格式"""
    openai_body = {
        "model": resolve_model(body.get("model")),
        "messages": [],
    }

    # System prompt -> system role message
    system_content = body.get("system")
    system_text = ""
    if system_content:
        if isinstance(system_content, list):
            system_text = "\n".join(
                b["text"] for b in system_content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            system_text = str(system_content)
    # Appended, never prepended: the upstream caches by prompt prefix, so a
    # constant suffix keeps every later turn's prefix identical (one-time miss
    # on the first request after a change). _prompt_cache_key hashes the
    # original `body["system"]`, so the conversation key is unaffected.
    style = style_prompt()
    if style:
        system_text = (system_text.rstrip() + "\n\n" + style) if system_text.strip() else style
    if system_text.strip():
        openai_body["messages"].append({"role": "system", "content": system_text})

    # Messages: content blocks -> OpenAI messages
    for msg_idx, msg in enumerate(body.get("messages", [])):
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, str):
            openai_body["messages"].append({"role": role, "content": content})
        elif isinstance(content, list):
            converted = _convert_content_blocks(role, content, msg_idx)
            openai_body["messages"].extend(converted)

    # Tools
    tools = body.get("tools")
    server_web_search = False
    if tools:
        openai_tools = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            if _is_server_web_search(t):
                openai_tools.append(_web_search_tool_spec(t))
                server_web_search = True
            elif t.get("name"):  # nameless built-ins (computer, bash, ...) have no client schema
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object", "properties": {}})
                    }
                })
        openai_body["tools"] = openai_tools

    # Tool choice
    tc = body.get("tool_choice")
    if tc:
        if (
            server_web_search
            and isinstance(tc, dict)
            and tc.get("type") == "tool"
            and tc.get("name") == WEB_SEARCH_TOOL_NAME
        ):
            # Forcing the hosted tool: there is no function of that name upstream.
            openai_body["tool_choice"] = {"type": "web_search"}
        else:
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


def _convert_content_blocks(role: str, blocks: list, msg_idx: int = 0) -> list:
    """将 Anthropic content_blocks 数组转换为 OpenAI message(s)"""
    text_parts = []
    openai_content = []
    tool_calls = []
    tool_msgs = []
    tool_result_images = []

    for block_idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        t = block.get("type")

        if t == "text":
            text = block.get("text", "")
            text_parts.append(text)
            openai_content.append({"type": "text", "text": text})
        elif t == "tool_use" and role == "assistant":
            # The fallback id must be a pure function of the block's position:
            # a random one would serialize differently on every retransmission
            # of the same history, breaking the upstream prompt cache at this
            # offset for the rest of the conversation.
            tool_calls.append({
                "id": block.get("id") or f"call_m{msg_idx}b{block_idx}",
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=True)
                }
            })
        elif t == "tool_result" and role == "user":
            content = block.get("content", "")
            images = []
            if isinstance(content, list):
                texts = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        texts.append(c.get("text", ""))
                    elif c.get("type") == "image":
                        # An image left in an OpenAI `tool` message is accepted
                        # (HTTP 200) but never seen — the upstream answered "the
                        # image appears blank" for one sent that way, measured
                        # against codex-bridge. So collect them here and hand
                        # them to the model in a user message right after the
                        # tool messages; dropping them, as this did before, made
                        # every image a tool returned invisible the same way.
                        url = _convert_image_block(c)
                        if url:
                            images.append({"type": "image_url", "image_url": {"url": url}})
                content = "\n".join(texts)
            if images and not str(content):
                content = TOOL_RESULT_IMAGE_PLACEHOLDER
            tool_result_images.extend(images)
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
                messages.append({"role": "user", "content": _user_content(non_tool_content)})
            messages.extend(tool_msgs)
            # After, never before: the tool messages have to stay adjacent to the
            # assistant turn whose tool_calls they answer.
            if tool_result_images:
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": TOOL_RESULT_IMAGE_PREAMBLE}] + tool_result_images,
                })
        elif non_tool_content:
            messages.append({"role": "user", "content": _user_content(non_tool_content)})

    return messages


def _user_content(parts: list):
    """Collapse to a bare string only when the message is a single text part.

    A lone image part has no "text" key, so unwrapping it by index raised
    KeyError and failed the whole request.
    """
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts


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
# Hosted web search: Anthropic web_search_<date> <-> OpenAI web_search
# ============================================================

WEB_SEARCH_TOOL_NAME = "web_search"


def _is_server_web_search(tool: dict) -> bool:
    """Anthropic's hosted web search tool.

    It arrives as ``{"type": "web_search_20250305", "name": "web_search", ...}``
    with no ``input_schema`` — the server runs it. Turning it into an empty
    function (what every named tool used to become) made the upstream emit a
    bare ``web_search`` call with no arguments and nobody to run it, so Claude
    Code's WebSearch came back with no links at all (observed 2026-09-28).
    """
    return (
        str(tool.get("type") or "").startswith("web_search")
        and "input_schema" not in tool
    )


def _web_search_tool_spec(tool: dict) -> dict:
    """Anthropic web search tool -> OpenAI ``{"type": "web_search"}`` tool.

    ``allowed_domains`` maps onto the upstream's ``filters``; ``user_location``
    has the same keys on both sides. ``blocked_domains`` and ``max_uses`` have
    no upstream counterpart and are dropped.
    """
    spec: dict = {"type": "web_search"}
    allowed = tool.get("allowed_domains")
    if allowed:
        spec["filters"] = {"allowed_domains": [str(d) for d in allowed]}
    location = tool.get("user_location")
    if isinstance(location, dict) and location:
        spec["user_location"] = location
    if tool.get("blocked_domains"):
        log.info("web_search blocked_domains dropped: upstream has no blocklist")
    return spec


def _clean_citation_url(url) -> str:
    """Strip the ``utm_source=openai`` marker the upstream appends to citations."""
    url = str(url or "").strip()
    if "utm_source" not in url:
        return url
    parts = urlsplit(url)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k == "utm_source" and v == "openai")
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _citation_index(annotations) -> tuple[dict, list]:
    """(url -> title, cited urls in order) from chat-completions annotations."""
    titles: dict = {}
    order: list = []
    for ann in annotations or []:
        if not isinstance(ann, dict):
            continue
        cite = ann.get("url_citation") if ann.get("type") == "url_citation" else None
        if not isinstance(cite, dict):
            cite = ann  # tolerate the flat Responses spelling
        url = _clean_citation_url(cite.get("url"))
        if not url:
            continue
        if url not in titles:
            order.append(url)
            titles[url] = ""
        if cite.get("title") and not titles[url]:
            titles[url] = str(cite["title"])
    return titles, order


def _search_result(url: str, title: str) -> dict:
    return {
        "type": "web_search_result",
        "url": url,
        "title": title or (urlsplit(url).netloc or url),
        "encrypted_content": "",
        "page_age": None,
    }


def _web_searches(calls, annotations) -> list[dict]:
    """Group the upstream's search calls and citations into Anthropic-shaped searches.

    One entry per completed ``search`` action (page reads — open_page,
    find_in_page — have no Anthropic counterpart). The URLs a search consulted
    come from ``action.sources``; titles from the citations. Cited URLs no
    search claimed ride on the last search, or on a synthesized one when the
    upstream cited without reporting any search call.
    """
    titles, cited = _citation_index(annotations)
    searches: list[dict] = []
    for call in calls or []:
        if not isinstance(call, dict):
            continue
        action = call.get("action") or {}
        if action.get("type", "search") != "search":
            continue
        urls: list = []
        for src in action.get("sources") or []:
            url = _clean_citation_url(src.get("url")) if isinstance(src, dict) else ""
            if url and url not in urls:
                urls.append(url)
        queries = action.get("queries") or []
        query = action.get("query") or (queries[0] if queries else "")
        searches.append({
            "id": call.get("id") or f"srvtoolu_{uuid.uuid4().hex[:24]}",
            "query": str(query),
            "urls": urls,
        })
    claimed = {u for s in searches for u in s["urls"]}
    leftover = [u for u in cited if u not in claimed]
    if leftover:
        if not searches:
            searches.append({"id": f"srvtoolu_{uuid.uuid4().hex[:24]}", "query": "", "urls": []})
        searches[-1]["urls"].extend(leftover)
    for s in searches:
        s["results"] = [_search_result(u, titles.get(u, "")) for u in s["urls"]]
    return searches


def _server_tool_use_block(search: dict) -> dict:
    return {
        "type": "server_tool_use",
        "id": search["id"],
        "name": WEB_SEARCH_TOOL_NAME,
        "input": {"query": search["query"]},
    }


def _web_search_result_block(search: dict) -> dict:
    return {
        "type": "web_search_tool_result",
        "tool_use_id": search["id"],
        "content": search["results"],
    }


def _web_search_blocks(calls, annotations) -> list[dict]:
    blocks: list[dict] = []
    for search in _web_searches(calls, annotations):
        blocks.append(_server_tool_use_block(search))
        blocks.append(_web_search_result_block(search))
    return blocks


def _server_tool_use_events(index: int, tool_id: str, query: str):
    """The three SSE events the Anthropic API streams for one hosted search."""
    yield _sse("content_block_start", {
        "type": "content_block_start",
        "index": index,
        "content_block": {
            "type": "server_tool_use",
            "id": tool_id,
            "name": WEB_SEARCH_TOOL_NAME,
            "input": {},
        },
    })
    yield _sse("content_block_delta", {
        "type": "content_block_delta",
        "index": index,
        "delta": {
            "type": "input_json_delta",
            "partial_json": json.dumps({"query": query}, ensure_ascii=False),
        },
    })
    yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})


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

    # Hosted web search first, in the Anthropic API's order: the searches and
    # their results precede the text that cites them.
    content_blocks.extend(
        _web_search_blocks(message.get("web_search_calls"), message.get("annotations"))
    )
    searches = sum(1 for b in content_blocks if b.get("type") == "server_tool_use")

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

    usage_out = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    if searches:
        usage_out["server_tool_use"] = {"web_search_requests": searches}

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": display_model,
        "content": content_blocks,
        "stop_reason": _map_finish_reason(finish),
        "stop_sequence": None,
        "usage": usage_out,
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
    diag: str = "",
    prompt_usage: dict | None = None,
    on_usage=None,
):
    """生成器: 从 OpenAI SSE stream 读取，逐事件生成 Anthropic SSE

    With ``use_upstream_model``, the first upstream chunk is read before
    message_start is emitted, so the model reported to the client is the one that
    actually served the request rather than the one the client asked for.

    ``prompt_usage`` seeds ``message_start`` with an estimated prompt size (see
    prompt_estimate.py). A Claude Code client takes the assistant message's
    usage from ``message_start`` alone and never revises it from the
    ``message_delta`` below, so leaving it at zero is what left every session
    through this bridge showing an empty context meter. ``on_usage`` is called
    with the real counts once the upstream reports them, to re-anchor that
    estimate for the conversation's next turn.
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
            # Estimated, not measured — the upstream reveals prompt_tokens only
            # at stream end, and this event has to go out first. The real
            # figures still follow in message_delta.
            "usage": {
                "input_tokens": (prompt_usage or {}).get("input_tokens", 0),
                "output_tokens": 0,
                "cache_read_input_tokens": (prompt_usage or {}).get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": 0,
            }
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
    search_calls: list[dict] = []   # hosted web searches, in arrival order
    annotations: list[dict] = []    # url_citation annotations: the page titles
    emitted_search_ids: set = set()

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
                # Read the index before _close_tool pops the entry, and move
                # past it: the text that follows is a new block, not this one.
                prev_anth_idx = tool_states[active_tool_idx]["anthropic_idx"]
                _close_tool(active_tool_idx, tool_states)
                yield _sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": prev_anth_idx
                })
                content_idx += 1
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

        # --- Hosted web search ---
        # The bridge reports each finished upstream search as a
        # `web_search_calls` delta and each citation as an `annotations` delta.
        # The search itself is announced right away (Claude Code shows the query
        # as progress); its result block waits for stream end, when the
        # citations that carry the page titles have all arrived.
        for call in delta.get("web_search_calls") or []:
            if not isinstance(call, dict):
                continue
            action = call.get("action") or {}
            if action.get("type", "search") != "search":
                continue
            if not call.get("id"):
                call["id"] = f"srvtoolu_{uuid.uuid4().hex[:24]}"
            search_calls.append(call)

            if state == "IN_TEXT":
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": content_idx})
                content_idx += 1
                state = "IDLE"
            elif state == "IN_TOOL" and active_tool_idx >= 0:
                prev_anth_idx = tool_states[active_tool_idx]["anthropic_idx"]
                _close_tool(active_tool_idx, tool_states)
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": prev_anth_idx})
                content_idx += 1
                active_tool_idx = -1
                state = "IDLE"

            queries = action.get("queries") or []
            query = action.get("query") or (queries[0] if queries else "")
            for event in _server_tool_use_events(content_idx, call["id"], str(query)):
                yield event
            emitted_search_ids.add(call["id"])
            content_idx += 1

        anns = delta.get("annotations")
        if anns:
            annotations.extend(a for a in anns if isinstance(a, dict))

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
        content_idx += 1
    elif state == "IN_TOOL" and active_tool_idx >= 0:
        st = tool_states[active_tool_idx]
        yield _sse("content_block_stop", {
            "type": "content_block_stop", "index": st["anthropic_idx"]
        })
        content_idx += 1

    # Web search results, now that every citation (and so every title) is in.
    searches = _web_searches(search_calls, annotations)
    for search in searches:
        if search["id"] not in emitted_search_ids:
            # Citations without a reported search call: announce the
            # synthesized search here so the result has a tool_use to answer.
            for event in _server_tool_use_events(content_idx, search["id"], search["query"]):
                yield event
            content_idx += 1
        yield _sse("content_block_start", {
            "type": "content_block_start",
            "index": content_idx,
            "content_block": _web_search_result_block(search),
        })
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": content_idx})
        content_idx += 1

    # The usage frame arrives once, at stream end, so this is the only place
    # streamed cache statistics can be recorded.
    if diag:
        log.info(
            "<- stream in=%s cached=%s out=%s | %s",
            input_tokens, cache_read_tokens, output_tokens, diag,
        )

    # Re-anchor the prompt-size estimate on the counts that actually came back,
    # so the next turn of this conversation estimates only its own delta.
    if on_usage and input_tokens:
        try:
            on_usage(input_tokens, cache_read_tokens)
        except Exception:
            log.exception("prompt estimate re-anchor failed")

    # message_start had to be emitted before the upstream said anything, so it
    # could only claim input_tokens=0. Report the real figures here instead;
    # otherwise every streaming request is recorded as having consumed no
    # input at all, which is what made streamed usage look like zero.
    usage_out = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_tokens,
        "cache_creation_input_tokens": 0,
    }
    if searches:
        usage_out["server_tool_use"] = {"web_search_requests": len(searches)}

    # message_delta
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": _map_finish_reason(finish_reason or "stop"),
            "stop_sequence": None
        },
        "usage": usage_out,
    })

    # message_stop
    yield _sse("message_stop", {"type": "message_stop"})


def _close_tool(idx: int, tool_states: dict):
    """Pop tool state entry (cleanup only — caller emits SSE events)."""
    tool_states.pop(idx, {})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"
