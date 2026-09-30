import json

import pytest

from translator import (
    DEFAULT_STYLE_PROMPT,
    TOOL_RESULT_IMAGE_PLACEHOLDER,
    TOOL_RESULT_IMAGE_PREAMBLE,
    prepare_openai_passthrough,
    request_diag,
    resolve_model,
    stream_translate,
    translate_request,
    translate_response,
)


class FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        yield from self._lines


def event_payloads(chunks):
    events = []
    for chunk in chunks:
        parts = chunk.strip().split("\n")
        event = parts[0].removeprefix("event: ")
        data = json.loads(parts[1].removeprefix("data: "))
        events.append((event, data))
    return events


def test_translate_request_text_system_and_options(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
        "temperature": 0.2,
        "stop_sequences": ["STOP"],
    }

    result = translate_request(body)

    assert result["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    assert result["max_tokens"] == 100
    assert result["temperature"] == 0.2
    assert result["stop"] == ["STOP"]


def test_translate_request_tool_use_and_tool_result(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll call a tool."},
                    {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "cx2cc"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"},
                ],
            },
        ],
        "tools": [
            {"name": "search", "description": "Search", "input_schema": {"type": "object"}},
        ],
        "tool_choice": {"type": "tool", "name": "search"},
    }

    result = translate_request(body)

    assert result["messages"][0]["tool_calls"][0]["id"] == "toolu_1"
    assert result["messages"][0]["tool_calls"][0]["function"]["name"] == "search"
    assert json.loads(result["messages"][0]["tool_calls"][0]["function"]["arguments"]) == {"q": "cx2cc"}
    assert result["messages"][1] == {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"}
    assert result["tools"][0]["function"]["name"] == "search"
    assert result["tool_choice"] == {"type": "function", "function": {"name": "search"}}


def test_translate_request_image_block(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
                    },
                ],
            }
        ]
    }

    result = translate_request(body)

    content = result["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Describe"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}


def _read_png_tool_result(text=None, data="abc"):
    """The shape Claude Code's Read tool returns for a PNG."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]
    if text is not None:
        content.insert(0, {"type": "text", "text": text})
    return {"type": "tool_result", "tool_use_id": "toolu_1", "content": content}


def _read_png_body(*tool_results, user_text=None):
    content = list(tool_results)
    if user_text is not None:
        content.append({"type": "text", "text": user_text})
    return {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "a.png"}}],
            },
            {"role": "user", "content": content},
        ]
    }


def test_tool_result_image_reaches_upstream(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")

    result = translate_request(_read_png_body(_read_png_tool_result()))

    # The tool message stays text-only (OpenAI's `tool` role carries no images),
    # and the image rides in a user message right after it.
    tool_msg, carrier = result["messages"][1], result["messages"][2]
    assert tool_msg == {
        "role": "tool",
        "tool_call_id": "toolu_1",
        "content": TOOL_RESULT_IMAGE_PLACEHOLDER,
    }
    assert carrier["role"] == "user"
    assert carrier["content"] == [
        {"type": "text", "text": TOOL_RESULT_IMAGE_PREAMBLE},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
    ]


def test_tool_result_keeps_its_text_alongside_the_image(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")

    result = translate_request(_read_png_body(_read_png_tool_result(text="1 file read")))

    assert result["messages"][1]["content"] == "1 file read"
    assert result["messages"][2]["content"][1]["image_url"]["url"] == "data:image/png;base64,abc"


def test_tool_result_images_are_emitted_after_all_tool_messages(monkeypatch):
    """`tool` messages must stay adjacent to the assistant turn they answer."""
    monkeypatch.setenv("CX2CC_STYLE", "off")
    second = dict(_read_png_tool_result(data="def"), tool_use_id="toolu_2")

    result = translate_request(_read_png_body(_read_png_tool_result(), second))

    assert [m["role"] for m in result["messages"]] == ["assistant", "tool", "tool", "user"]
    urls = [p["image_url"]["url"] for p in result["messages"][3]["content"][1:]]
    assert urls == ["data:image/png;base64,abc", "data:image/png;base64,def"]


def test_text_only_tool_result_gets_no_placeholder(monkeypatch):
    """The placeholder must not leak onto a sibling result that had no image."""
    monkeypatch.setenv("CX2CC_STYLE", "off")
    text_only = {"type": "tool_result", "tool_use_id": "toolu_2", "content": [{"type": "text", "text": ""}]}

    result = translate_request(_read_png_body(_read_png_tool_result(), text_only))

    assert result["messages"][1]["content"] == TOOL_RESULT_IMAGE_PLACEHOLDER
    assert result["messages"][2]["content"] == ""


def test_image_only_user_message_does_not_crash(monkeypatch):
    """A lone image part has no "text" key to unwrap it by."""
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}}
                ],
            }
        ]
    }

    result = translate_request(body)

    assert result["messages"][0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}}
    ]


def test_tool_result_image_translation_is_deterministic(monkeypatch):
    """Same history in, byte-identical request out — or the prompt cache misses."""
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = _read_png_body(_read_png_tool_result(), user_text="what does it say?")

    first = translate_request(body)
    second = translate_request(body)

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_prompt_cache_key_stable_across_turns():
    turn1 = {
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Start task"}],
    }
    turn2 = {
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Start task"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Working"},
                    {"type": "tool_use", "id": "t1", "name": "search", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
        ],
    }

    key1 = translate_request(turn1)["prompt_cache_key"]
    key2 = translate_request(turn2)["prompt_cache_key"]

    assert key1 == key2


def test_prompt_cache_key_differs_between_conversations():
    conv_a = {"system": "You are helpful.", "messages": [{"role": "user", "content": "Task A"}]}
    conv_b = {"system": "You are helpful.", "messages": [{"role": "user", "content": "Task B"}]}
    conv_c = {"system": "Other system.", "messages": [{"role": "user", "content": "Task A"}]}

    keys = {
        translate_request(conv_a)["prompt_cache_key"],
        translate_request(conv_b)["prompt_cache_key"],
        translate_request(conv_c)["prompt_cache_key"],
    }

    assert len(keys) == 3


def test_style_prompt_appended_to_system():
    result = translate_request(
        {"system": "You are helpful.", "messages": [{"role": "user", "content": "hi"}]}
    )

    system = result["messages"][0]
    assert system["role"] == "system"
    # appended, not prepended: the original prompt must stay at the front so the
    # upstream prefix cache still matches across turns
    assert system["content"].startswith("You are helpful.")
    assert system["content"].endswith(DEFAULT_STYLE_PROMPT)


def test_style_prompt_injected_when_no_system():
    result = translate_request({"messages": [{"role": "user", "content": "hi"}]})

    assert result["messages"][0] == {"role": "system", "content": DEFAULT_STYLE_PROMPT}


def test_style_prompt_does_not_change_cache_key(monkeypatch):
    body = {"system": "You are helpful.", "messages": [{"role": "user", "content": "hi"}]}
    with_style = translate_request(body)["prompt_cache_key"]
    monkeypatch.setenv("CX2CC_STYLE", "off")
    without_style = translate_request(body)["prompt_cache_key"]

    assert with_style == without_style


def test_style_prompt_override_inline(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE_PROMPT", "BE TERSE")

    result = translate_request({"messages": [{"role": "user", "content": "hi"}]})

    assert result["messages"][0]["content"] == "BE TERSE"


def test_style_prompt_override_from_file(monkeypatch, tmp_path):
    path = tmp_path / "style.md"
    path.write_text("FROM FILE", encoding="utf-8")
    monkeypatch.setenv("CX2CC_STYLE_PROMPT", str(path))

    result = translate_request({"messages": [{"role": "user", "content": "hi"}]})

    assert result["messages"][0]["content"] == "FROM FILE"


def test_prompt_cache_key_disabled(monkeypatch):
    monkeypatch.setenv("CX2CC_PROMPT_CACHE_KEY", "off")

    result = translate_request({"messages": [{"role": "user", "content": "hi"}]})

    assert "prompt_cache_key" not in result


def test_missing_tool_use_id_fallback_is_deterministic(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "search", "input": {"q": "a"}},
                    {"type": "tool_use", "name": "search", "input": {"q": "a"}},
                ],
            },
        ],
    }

    first = translate_request(body)
    second = translate_request(body)

    # Retransmitting identical history must serialize identically, or the
    # upstream prompt cache breaks at this offset on every later turn.
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    ids = [tc["id"] for tc in first["messages"][1]["tool_calls"]]
    assert len(set(ids)) == 2


def _diag_marks(diag):
    marks = diag.split("h[", 1)[1].rstrip("]").split()
    return dict(m.split(":") for m in marks)


def test_request_diag_snapshots_match_on_shared_prefix():
    turn1 = {
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Start task"}],
    }
    turn2 = {
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "Start task"},
            {"role": "assistant", "content": "Done"},
        ],
    }

    diag1 = request_diag(translate_request(turn1))
    diag2 = request_diag(translate_request(turn2))

    marks1, marks2 = _diag_marks(diag1), _diag_marks(diag2)
    # Indexes covered by both turns hash identically; the longer turn's extra
    # content shows up only at indexes beyond the shared prefix.
    assert marks1["t"] == marks2["t"]
    assert marks1["1"] == marks2["1"]
    assert marks1["2"] != marks2["1"]


def test_request_diag_detects_prefix_divergence():
    base = {
        "system": "You are helpful.",
        "messages": [{"role": "user", "content": "Start task"}],
    }
    changed = {
        "system": "You are helpful!",
        "messages": [{"role": "user", "content": "Start task"}],
    }

    marks_a = _diag_marks(request_diag(translate_request(base)))
    marks_b = _diag_marks(request_diag(translate_request(changed)))

    assert marks_a["1"] != marks_b["1"]


def test_translate_response_text_tool_and_usage():
    openai_body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "Need a tool",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "lookup", "arguments": '{"id": 1}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }

    result = translate_response(openai_body, "claude-opus-4-8")

    assert result["model"] == "claude-opus-4-8"
    assert result["stop_reason"] == "tool_use"
    assert result["content"][0] == {"type": "text", "text": "Need a tool"}
    assert result["content"][1]["input"] == {"id": 1}
    assert result["usage"]["input_tokens"] == 3
    assert result["usage"]["output_tokens"] == 4


def test_stream_translate_text_events():
    lines = [
        'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{"content":" there"},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}',
        "data: [DONE]",
    ]

    events = event_payloads(list(stream_translate(FakeStreamResponse(lines), "claude-sonnet-4-6")))

    assert events[0][0] == "message_start"
    assert events[1][0] == "content_block_start"
    assert events[2] == (
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
    )
    assert events[-2][1]["delta"]["stop_reason"] == "end_turn"
    assert events[-1][0] == "message_stop"


def test_prepare_openai_passthrough_leaves_body_intact(monkeypatch):
    """Only additive/allowlist changes — the shape and fields the client sent stay put."""
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-5.5")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-5.5,gpt-5.4")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "")
    monkeypatch.setenv("CX2CC_PROMPT_CACHE_KEY", "off")

    body = {
        "model": "gpt-5.4",
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
        "temperature": 0.3,
        "tools": [{"type": "function", "function": {"name": "noop", "parameters": {}}}],
    }

    prepared = prepare_openai_passthrough(body)

    assert prepared["model"] == "gpt-5.4"
    assert prepared["messages"] == body["messages"]
    assert prepared["temperature"] == 0.3
    assert prepared["tools"] == body["tools"]
    # Not injected when disabled.
    assert "prompt_cache_key" not in prepared
    assert "stream_options" not in prepared
    # Original dict is not mutated.
    assert "prompt_cache_key" not in body


def test_prepare_openai_passthrough_unknown_model_falls_back_under_default_policy(monkeypatch):
    # Pre-2026-09-06 behaviour, now opt-in: CX2CC_UNKNOWN_MODEL=default.
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-5.5")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "")
    monkeypatch.setenv("CX2CC_UNKNOWN_MODEL", "default")

    prepared = prepare_openai_passthrough({
        "model": "unknown-slug",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert prepared["model"] == "gpt-5.5"


def test_prepare_openai_passthrough_unknown_model_is_rejected_by_default(monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-5.5")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-5.5")
    monkeypatch.delenv("CX2CC_UNKNOWN_MODEL", raising=False)

    from translator import UnknownModelError

    with pytest.raises(UnknownModelError) as excinfo:
        prepare_openai_passthrough({
            "model": "unknown-slug",
            "messages": [{"role": "user", "content": "hi"}],
        })
    assert excinfo.value.requested == "unknown-slug"
    assert "gpt-5.5" in excinfo.value.allowed


def test_prepare_openai_passthrough_adds_cache_key_and_stream_options(monkeypatch):
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-5.5")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "")
    monkeypatch.setenv("CX2CC_PROMPT_CACHE_KEY", "on")

    prepared = prepare_openai_passthrough({
        "model": "gpt-5.5",
        "stream": True,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first turn"},
        ],
    })

    assert prepared["stream_options"] == {"include_usage": True}
    assert prepared["prompt_cache_key"]

    # Stable per (system, first-user), so both turns of the same conversation
    # hash to the same routing key.
    prepared2 = prepare_openai_passthrough({
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first turn"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "second turn"},
        ],
    })
    assert prepared2["prompt_cache_key"] == prepared["prompt_cache_key"]

    # Different first user message → different key.
    prepared3 = prepare_openai_passthrough({
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "different opener"},
        ],
    })
    assert prepared3["prompt_cache_key"] != prepared["prompt_cache_key"]


def test_prepare_openai_passthrough_preserves_client_cache_key(monkeypatch):
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-5.5")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "")
    monkeypatch.setenv("CX2CC_PROMPT_CACHE_KEY", "on")

    prepared = prepare_openai_passthrough({
        "model": "gpt-5.5",
        "prompt_cache_key": "client-supplied",
        "messages": [{"role": "user", "content": "hi"}],
    })

    assert prepared["prompt_cache_key"] == "client-supplied"


def test_prepare_openai_passthrough_does_not_inject_style_prompt(monkeypatch):
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-5.5")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "")
    """OpenAI-format callers compose their own system messages; the Claude Code
    output-style addendum belongs only on the Anthropic path."""
    monkeypatch.setenv("CX2CC_STYLE", "on")

    prepared = prepare_openai_passthrough({
        "model": "gpt-5.5",
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
    })

    assert prepared["messages"][0]["content"] == "you are helpful"


def test_resolve_model_alias_remaps_pinned_slug(monkeypatch):
    """A fleet that pins gpt-5.6-sol client-side can be moved server-side."""
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-6-astra")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-6-astra,gpt-5.6-sol,gpt-5.6-terra")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "gpt-5.6-sol=gpt-6-astra, gpt-5.4=gpt-6-astra")

    assert resolve_model("gpt-5.6-sol") == "gpt-6-astra"
    # Window marker is stripped before the alias lookup.
    assert resolve_model("gpt-5.6-sol[1m]") == "gpt-6-astra"
    assert resolve_model("gpt-5.4") == "gpt-6-astra"
    # Non-aliased passthrough slugs are untouched.
    assert resolve_model("gpt-5.6-terra") == "gpt-5.6-terra"
    # Unknown names still take the pinned default.
    assert resolve_model("claude-opus-4-8") == "gpt-6-astra"


def test_resolve_model_alias_target_must_be_allowlisted(monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-5.6-terra,gpt-5.6-sol")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "gpt-5.6-sol=not-a-real-slug")

    assert resolve_model("gpt-5.6-sol") == "gpt-5.6-terra"


def test_resolve_model_alias_ignores_malformed_entries(monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-6-astra")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-6-astra,gpt-5.6-sol")
    monkeypatch.setenv("CX2CC_MODEL_ALIASES", "garbage,=x,gpt-5.6-sol=")

    assert resolve_model("gpt-5.6-sol") == "gpt-5.6-sol"


def test_resolve_model_no_alias_env_is_identity(monkeypatch):
    monkeypatch.setenv("CX2CC_UPSTREAM_MODEL", "gpt-6-astra")
    monkeypatch.setenv("CX2CC_MODEL_PASSTHROUGH", "gpt-6-astra,gpt-5.6-sol")
    monkeypatch.delenv("CX2CC_MODEL_ALIASES", raising=False)

    assert resolve_model("gpt-5.6-sol") == "gpt-5.6-sol"

