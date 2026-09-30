import json

import prompt_estimate
from translator import stream_translate

from tests.test_translator import FakeStreamResponse, event_payloads


def fresh():
    """Each test starts from an empty anchor table."""
    prompt_estimate._anchors.clear()


def image_body(payload):
    return {
        "messages": [
            {"role": "tool", "tool_call_id": "t1", "content": "read ok"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "here it is"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{payload}"}},
                ],
            },
        ]
    }


def test_image_payload_is_charged_a_fixed_budget():
    """A screenshot's char count must not drive the context meter."""
    small = prompt_estimate.request_chars(image_body("a" * 1_000))
    huge = prompt_estimate.request_chars(image_body("a" * 330_000))
    assert small == huge
    # And the budget is what is actually charged, not the payload.
    assert abs(huge - prompt_estimate.request_chars({"messages": image_body("")["messages"][:1]})) < (
        prompt_estimate.IMAGE_CHAR_EQUIVALENT + 200
    )


def test_image_does_not_blow_up_the_estimate():
    fresh()
    prompt_estimate.record("k", 40_000, 10_000)
    prompt_estimate.record("k", 44_000, 11_000)  # learned ratio: 0.25 tokens/char
    chars = 44_000 + prompt_estimate.request_chars(image_body("a" * 330_000))
    est = prompt_estimate.estimate("k", chars)["input_tokens"]
    # A 1920x1080 image really costs ~1.5k tokens; unbounded it read ~94k.
    assert est < 14_000


def test_text_content_is_still_measured_in_full():
    """Only image payloads are stood in for."""
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "x" * 5_000}]}]}
    assert prompt_estimate.request_chars(body) > 5_000


def test_cold_start_uses_default_ratio():
    fresh()
    assert prompt_estimate.estimate("k", 1000) == {
        "input_tokens": 250,
        "cache_read_input_tokens": 0,
    }


def test_anchor_scales_by_growth():
    fresh()
    prompt_estimate.record("k", 1000, 400)
    # Same size -> the measured value verbatim; twice the size -> twice the count.
    assert prompt_estimate.estimate("k", 1000)["input_tokens"] == 400
    assert prompt_estimate.estimate("k", 2000)["input_tokens"] == 800


def test_anchor_mirrors_cache_split():
    fresh()
    prompt_estimate.record("k", 1000, 400, 300)
    est = prompt_estimate.estimate("k", 1000)
    assert est == {"input_tokens": 100, "cache_read_input_tokens": 300}
    # Context size is what the client sums, so the split must not lose tokens.
    assert est["input_tokens"] + est["cache_read_input_tokens"] == 400


def test_two_measurements_extrapolate_along_the_delta():
    fresh()
    # 1000 chars -> 400 tokens, then +1000 chars -> +200 tokens: the appended
    # text runs at 0.2 tokens/char even though the whole prompt averages 0.3.
    prompt_estimate.record("k", 1000, 400)
    prompt_estimate.record("k", 2000, 600)
    # Origin-scaling would say 600 * 3000/2000 = 900; differencing says 800,
    # which is what the fixed request overhead makes correct.
    assert prompt_estimate.estimate("k", 3000)["input_tokens"] == 800


def test_absurd_delta_ratio_falls_back_to_scaling():
    fresh()
    prompt_estimate.record("k", 1000, 400)
    prompt_estimate.record("k", 1010, 900)  # 50 tokens/char — a bad interval
    assert prompt_estimate.estimate("k", 2020)["input_tokens"] == 1800


def test_repeated_identical_request_keeps_a_usable_interval():
    fresh()
    prompt_estimate.record("k", 1000, 400)
    prompt_estimate.record("k", 2000, 600)
    prompt_estimate.record("k", 2000, 600)  # retry: same size, no new interval
    assert prompt_estimate.estimate("k", 3000)["input_tokens"] == 800


def test_compaction_scales_down():
    fresh()
    prompt_estimate.record("k", 1000, 400)
    assert prompt_estimate.estimate("k", 250)["input_tokens"] == 100


def test_conversations_are_independent():
    fresh()
    prompt_estimate.record("a", 1000, 400)
    # A second conversation has no anchor of its own and must not borrow one:
    # a CJK-heavy neighbour once inflated an unrelated session's first call to
    # 87,669 against a real 28,269. Cold start is the fixed conservative ratio.
    assert prompt_estimate.estimate("b", 1000)["input_tokens"] == 250
    prompt_estimate.record("b", 1000, 100)
    assert prompt_estimate.estimate("a", 1000)["input_tokens"] == 400
    assert prompt_estimate.estimate("b", 1000)["input_tokens"] == 100


def test_zero_and_missing_inputs_are_safe():
    fresh()
    assert prompt_estimate.estimate("k", 0)["input_tokens"] == 0
    prompt_estimate.record("", 10, 10)
    prompt_estimate.record("k", 0, 10)
    prompt_estimate.record("k", 10, 0)
    assert prompt_estimate.stats()["conversations"] == 0


def test_lru_eviction_is_bounded():
    fresh()
    for i in range(prompt_estimate.MAX_CONVERSATIONS + 10):
        prompt_estimate.record(f"k{i}", 1000, 400)
    assert prompt_estimate.stats()["conversations"] == prompt_estimate.MAX_CONVERSATIONS


def test_request_chars_covers_tools_and_messages_only():
    fresh()
    body = {"model": "x", "temperature": 0.5, "tools": [], "messages": [{"role": "user", "content": "hi"}]}
    same_prompt_other_params = {"model": "y", "tools": [], "messages": body["messages"]}
    assert prompt_estimate.request_chars(body) == prompt_estimate.request_chars(same_prompt_other_params)
    assert prompt_estimate.request_chars({}) == len(json.dumps([[], []]))


def test_message_start_carries_the_estimate_and_delta_carries_the_truth():
    """The whole point: a client that only reads message_start still sees a size."""
    lines = [
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":1234,"completion_tokens":5,'
        '"prompt_tokens_details":{"cached_tokens":1000}}}',
        "data: [DONE]",
    ]
    seen = {}
    events = event_payloads(stream_translate(
        FakeStreamResponse(lines),
        "gpt-5.6-sol",
        prompt_usage={"input_tokens": 900, "cache_read_input_tokens": 100},
        on_usage=lambda tokens, cached: seen.update(tokens=tokens, cached=cached),
    ))
    kinds = dict(events)
    assert kinds["message_start"]["message"]["usage"] == {
        "input_tokens": 900,
        "output_tokens": 0,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 0,
    }
    assert kinds["message_delta"]["usage"]["input_tokens"] == 1234
    assert kinds["message_delta"]["usage"]["cache_read_input_tokens"] == 1000
    # ...and the real counts re-anchor the estimate for the next turn.
    assert seen == {"tokens": 1234, "cached": 1000}


def test_stream_without_estimate_still_emits_valid_message_start():
    lines = ['data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}', "data: [DONE]"]
    events = dict(event_payloads(stream_translate(FakeStreamResponse(lines), "gpt-5.6-sol")))
    assert events["message_start"]["message"]["usage"]["input_tokens"] == 0
