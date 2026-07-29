import json

from translator import translate_request, translate_response, stream_translate


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


def test_translate_request_text_system_and_options():
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


def test_translate_request_tool_use_and_tool_result():
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


def test_translate_request_image_block():
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


def test_prompt_cache_key_disabled(monkeypatch):
    monkeypatch.setenv("CX2CC_PROMPT_CACHE_KEY", "off")

    result = translate_request({"messages": [{"role": "user", "content": "hi"}]})

    assert "prompt_cache_key" not in result


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
