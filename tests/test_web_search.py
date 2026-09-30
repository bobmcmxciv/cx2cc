"""Hosted web search: Claude Code's WebSearch side query through cx2cc.

Claude Code performs WebSearch as a separate Messages request carrying the
Anthropic server tool ``web_search_20250305`` (forced via tool_choice) and
reads ``server_tool_use`` / ``web_search_tool_result`` blocks back. cx2cc maps
that tool onto the upstream's hosted ``web_search`` tool and rebuilds those
blocks from the bridge's ``web_search_calls`` / ``annotations`` fields.
"""
import json

from translator import stream_translate, translate_request, translate_response


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


def _sse_line(obj: dict) -> str:
    return "data: " + json.dumps(obj)


# ---------------------------------------------------------------------------
# request side
# ---------------------------------------------------------------------------

def test_translate_request_maps_anthropic_web_search_tool(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "messages": [{"role": "user", "content": "Perform a web search for the query: x"}],
        "max_tokens": 100,
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
                "allowed_domains": ["openai.com"],
                "blocked_domains": [],
            },
            {"name": "lookup", "description": "d", "input_schema": {"type": "object", "properties": {}}},
        ],
        "tool_choice": {"type": "tool", "name": "web_search"},
    }

    result = translate_request(body)

    assert result["tools"][0] == {"type": "web_search", "filters": {"allowed_domains": ["openai.com"]}}
    assert result["tools"][1]["type"] == "function"
    assert result["tools"][1]["function"]["name"] == "lookup"
    assert result["tool_choice"] == {"type": "web_search"}


def test_translate_request_web_search_without_domains_is_bare(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{
            "type": "web_search_20250305", "name": "web_search", "max_uses": 8,
            "allowed_domains": [], "blocked_domains": ["example.com"],
        }],
    }

    result = translate_request(body)

    assert result["tools"] == [{"type": "web_search"}]
    assert "tool_choice" not in result


def test_translate_request_client_function_named_web_search_stays_a_function(monkeypatch):
    monkeypatch.setenv("CX2CC_STYLE", "off")
    body = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [{"name": "web_search", "input_schema": {"type": "object", "properties": {}}}],
        "tool_choice": {"type": "tool", "name": "web_search"},
    }

    result = translate_request(body)

    assert result["tools"][0]["type"] == "function"
    assert result["tool_choice"] == {"type": "function", "function": {"name": "web_search"}}


# ---------------------------------------------------------------------------
# non-stream response
# ---------------------------------------------------------------------------

def _web_search_message():
    return {
        "content": "Answer text",
        "web_search_calls": [
            {"id": "ws_1", "status": "completed", "action": {
                "type": "search", "query": "q1", "queries": ["q1", "q1b"],
                "sources": [{"type": "url", "url": "https://a.example/x"}]}},
            {"id": "ws_2", "status": "completed", "action": {"type": "open_page", "url": "https://a.example/x"}},
        ],
        "annotations": [
            {"type": "url_citation", "url_citation": {
                "url": "https://a.example/x?utm_source=openai", "title": "A", "start_index": 0, "end_index": 5}},
            {"type": "url_citation", "url_citation": {
                "url": "https://b.example/y?k=1&utm_source=openai", "title": "B", "start_index": 6, "end_index": 9}},
        ],
    }


def test_translate_response_web_search_blocks():
    openai_body = {
        "model": "gpt-6-sol",
        "choices": [{"finish_reason": "stop", "message": _web_search_message()}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }

    result = translate_response(openai_body, "claude-opus-4-8", use_upstream_model=True)

    assert [b["type"] for b in result["content"]] == ["server_tool_use", "web_search_tool_result", "text"]
    use, found, text = result["content"]
    assert use == {"type": "server_tool_use", "id": "ws_1", "name": "web_search", "input": {"query": "q1"}}
    assert found["tool_use_id"] == "ws_1"
    assert [(r["type"], r["url"], r["title"]) for r in found["content"]] == [
        ("web_search_result", "https://a.example/x", "A"),
        ("web_search_result", "https://b.example/y?k=1", "B"),
    ]
    assert text == {"type": "text", "text": "Answer text"}
    assert result["stop_reason"] == "end_turn"
    assert result["usage"]["server_tool_use"] == {"web_search_requests": 1}


def test_translate_response_citations_without_search_calls_get_a_synthesized_search():
    message = _web_search_message()
    message.pop("web_search_calls")
    openai_body = {"choices": [{"finish_reason": "stop", "message": message}], "usage": {}}

    result = translate_response(openai_body, "m")

    use, found, _text = result["content"]
    assert use["type"] == "server_tool_use" and use["input"] == {"query": ""}
    assert found["tool_use_id"] == use["id"]
    assert [r["url"] for r in found["content"]] == ["https://a.example/x", "https://b.example/y?k=1"]


def test_translate_response_without_web_search_has_no_server_tool_usage():
    openai_body = {"choices": [{"finish_reason": "stop", "message": {"content": "hi"}}], "usage": {}}

    result = translate_response(openai_body, "m")

    assert result["content"] == [{"type": "text", "text": "hi"}]
    assert "server_tool_use" not in result["usage"]


# ---------------------------------------------------------------------------
# streaming response
# ---------------------------------------------------------------------------

def _delta(delta: dict, finish=None, **extra) -> str:
    obj = {"choices": [{"delta": delta, "finish_reason": finish}]}
    obj.update(extra)
    return _sse_line(obj)


def test_stream_translate_web_search_events():
    search = {"id": "ws_1", "status": "completed", "action": {
        "type": "search", "query": "q1", "sources": [{"type": "url", "url": "https://a.example/x"}]}}
    page_read = {"id": "ws_2", "status": "completed", "action": {"type": "open_page", "url": "https://a.example/x"}}
    citation = {"type": "url_citation", "url_citation": {
        "url": "https://a.example/x?utm_source=openai", "title": "A", "start_index": 0, "end_index": 5}}
    lines = [
        _sse_line({"model": "gpt-6-sol", "choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}),
        _delta({"web_search_calls": [search]}),
        _delta({"web_search_calls": [page_read]}),
        _delta({"content": "Found"}),
        _delta({"annotations": [citation]}),
        _delta({"content": " it"}, finish="stop", usage={"prompt_tokens": 10, "completion_tokens": 2}),
        "data: [DONE]",
    ]

    events = event_payloads(list(stream_translate(
        FakeStreamResponse(lines), "claude-sonnet-4-6", use_upstream_model=True,
    )))

    assert events[0][1]["message"]["model"] == "gpt-6-sol"
    assert events[1] == ("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "server_tool_use", "id": "ws_1", "name": "web_search", "input": {}},
    })
    assert events[2][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"query": "q1"}'}
    assert events[3] == ("content_block_stop", {"type": "content_block_stop", "index": 0})
    assert events[4][1]["index"] == 1 and events[4][1]["content_block"] == {"type": "text", "text": ""}
    assert events[5][1]["delta"]["text"] == "Found"
    assert events[6][1]["delta"]["text"] == " it"
    assert events[7] == ("content_block_stop", {"type": "content_block_stop", "index": 1})
    assert events[8][0] == "content_block_start" and events[8][1]["index"] == 2
    assert events[8][1]["content_block"] == {
        "type": "web_search_tool_result",
        "tool_use_id": "ws_1",
        "content": [{
            "type": "web_search_result", "url": "https://a.example/x", "title": "A",
            "encrypted_content": "", "page_age": None,
        }],
    }
    assert events[9] == ("content_block_stop", {"type": "content_block_stop", "index": 2})
    assert events[10][0] == "message_delta"
    assert events[10][1]["delta"]["stop_reason"] == "end_turn"
    assert events[10][1]["usage"]["input_tokens"] == 10
    assert events[10][1]["usage"]["server_tool_use"] == {"web_search_requests": 1}
    assert events[11][0] == "message_stop"


def test_stream_translate_search_after_text_closes_the_text_block():
    lines = [
        _delta({"content": "Let me look."}),
        _delta({"web_search_calls": [{"id": "ws_1", "action": {"type": "search", "query": "q"}}]}),
        _delta({"content": "Nothing found."}, finish="stop"),
        "data: [DONE]",
    ]

    events = event_payloads(list(stream_translate(FakeStreamResponse(lines), "m")))

    starts = [(d["index"], d["content_block"]["type"]) for e, d in events if e == "content_block_start"]
    stops = [d["index"] for e, d in events if e == "content_block_stop"]
    assert starts == [(0, "text"), (1, "server_tool_use"), (2, "text"), (3, "web_search_tool_result")]
    assert stops == [0, 1, 2, 3]
    # A search that reported no sources and drew no citations: an empty result,
    # which Claude Code renders as "No links found.".
    result = [d for e, d in events if e == "content_block_start"][3]["content_block"]
    assert result["content"] == []


def test_stream_translate_plain_text_has_no_search_blocks():
    lines = [_delta({"content": "hi"}, finish="stop"), "data: [DONE]"]

    events = event_payloads(list(stream_translate(FakeStreamResponse(lines), "m")))

    assert [e for e, _ in events] == [
        "message_start", "content_block_start", "content_block_delta", "content_block_stop",
        "message_delta", "message_stop",
    ]
    assert "server_tool_use" not in events[-2][1]["usage"]


def test_stream_translate_text_after_tool_call_opens_a_new_block():
    lines = [
        _delta({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "f", "arguments": "{}"}}]}),
        _delta({"content": "after"}, finish="stop"),
        "data: [DONE]",
    ]

    events = event_payloads(list(stream_translate(FakeStreamResponse(lines), "m")))

    starts = [(d["index"], d["content_block"]["type"]) for e, d in events if e == "content_block_start"]
    stops = [d["index"] for e, d in events if e == "content_block_stop"]
    assert starts == [(0, "tool_use"), (1, "text")]
    assert stops == [0, 1]
