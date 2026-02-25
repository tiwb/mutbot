"""Tests for mutbot.proxy.translation -- Anthropic <-> OpenAI JSON 格式转换。"""

import json

import pytest

from mutbot.proxy.translation import (
    anthropic_request_to_openai,
    normalize_model_name,
    openai_response_to_anthropic,
    openai_sse_to_anthropic_events,
)


# ---------------------------------------------------------------------------
# normalize_model_name tests
# ---------------------------------------------------------------------------

class TestNormalizeModelName:

    def test_strips_date_suffix(self):
        assert normalize_model_name("claude-sonnet-4-20250514") == "claude-sonnet-4"

    def test_strips_date_suffix_opus(self):
        assert normalize_model_name("claude-opus-4-20250514") == "claude-opus-4"

    def test_no_suffix_unchanged(self):
        assert normalize_model_name("claude-sonnet-4") == "claude-sonnet-4"

    def test_empty_string(self):
        assert normalize_model_name("") == ""

    def test_partial_date_not_stripped(self):
        # Only 8-digit date suffixes should be stripped
        assert normalize_model_name("claude-sonnet-4-2025") == "claude-sonnet-4-2025"

    def test_date_in_middle_not_stripped(self):
        assert normalize_model_name("claude-20250514-sonnet") == "claude-20250514-sonnet"

    def test_gpt_model_no_date(self):
        assert normalize_model_name("gpt-4o") == "gpt-4o"


# ---------------------------------------------------------------------------
# anthropic_request_to_openai tests
# ---------------------------------------------------------------------------

class TestAnthropicRequestToOpenai:

    def test_simple_text_user_message(self):
        body = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = anthropic_request_to_openai(body)
        assert result["model"] == "claude-sonnet-4"
        assert result["max_tokens"] == 1024
        assert len(result["messages"]) == 1
        assert result["messages"][0] == {"role": "user", "content": "Hello"}

    def test_system_prompt_string(self):
        body = {
            "model": "claude-sonnet-4",
            "system": "You are a helpful assistant.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_request_to_openai(body)
        assert result["messages"][0] == {
            "role": "system",
            "content": "You are a helpful assistant.",
        }
        assert result["messages"][1] == {"role": "user", "content": "Hi"}

    def test_system_prompt_list(self):
        body = {
            "model": "claude-sonnet-4",
            "system": [
                {"type": "text", "text": "You are helpful."},
                {"type": "text", "text": "Be concise."},
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_request_to_openai(body)
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "You are helpful.\nBe concise."

    def test_no_system_prompt(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_request_to_openai(body)
        assert all(m["role"] != "system" for m in result["messages"])

    def test_assistant_text_message_string(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
        }
        result = anthropic_request_to_openai(body)
        assert result["messages"][1] == {"role": "assistant", "content": "Hi there!"}

    def test_assistant_text_message_content_blocks(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Hello"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me think."},
                        {"type": "text", "text": "Here is the answer."},
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "Let me think.\nHere is the answer."
        assert "tool_calls" not in assistant_msg

    def test_assistant_tool_use_blocks(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "What's the weather?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me check."},
                        {
                            "type": "tool_use",
                            "id": "tu_123",
                            "name": "get_weather",
                            "input": {"city": "Tokyo"},
                        },
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "Let me check."
        assert len(assistant_msg["tool_calls"]) == 1
        tc = assistant_msg["tool_calls"][0]
        assert tc["id"] == "tu_123"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "Tokyo"}

    def test_assistant_tool_use_only_no_text(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Search"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_1",
                            "name": "search",
                            "input": {"q": "test"},
                        },
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        assistant_msg = result["messages"][1]
        assert assistant_msg["content"] is None
        assert len(assistant_msg["tool_calls"]) == 1

    def test_user_tool_result_string_content(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_123",
                            "content": "Sunny, 25C",
                        },
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        tool_msg = result["messages"][0]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "tu_123"
        assert tool_msg["content"] == "Sunny, 25C"

    def test_user_tool_result_list_content(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_456",
                            "content": [
                                {"type": "text", "text": "Line 1"},
                                {"type": "text", "text": "Line 2"},
                            ],
                        },
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        tool_msg = result["messages"][0]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "tu_456"
        assert tool_msg["content"] == "Line 1\nLine 2"

    def test_user_text_and_tool_result_combined(self):
        """User message with both text blocks and tool_result blocks."""
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Here is the result:"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_789",
                            "content": "42",
                        },
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        # Should produce both a user message and a tool message
        roles = [m["role"] for m in result["messages"]]
        assert "user" in roles
        assert "tool" in roles
        tool_msg = next(m for m in result["messages"] if m["role"] == "tool")
        assert tool_msg["content"] == "42"
        user_msg = next(m for m in result["messages"] if m["role"] == "user")
        assert user_msg["content"] == "Here is the result:"

    def test_tools_conversion(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get the weather for a city.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "City name"},
                        },
                        "required": ["city"],
                    },
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_weather"
        assert tool["function"]["description"] == "Get the weather for a city."
        assert tool["function"]["parameters"]["properties"]["city"]["type"] == "string"

    def test_tool_choice_any_dict(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "any"},
        }
        result = anthropic_request_to_openai(body)
        assert result["tool_choice"] == "required"

    def test_tool_choice_auto_dict(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "auto"},
        }
        result = anthropic_request_to_openai(body)
        assert result["tool_choice"] == "auto"

    def test_tool_choice_specific_tool_dict(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
        result = anthropic_request_to_openai(body)
        assert result["tool_choice"] == {
            "type": "function",
            "function": {"name": "get_weather"},
        }

    def test_tool_choice_any_string(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": "any",
        }
        result = anthropic_request_to_openai(body)
        assert result["tool_choice"] == "required"

    def test_tool_choice_auto_string(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": "auto",
        }
        result = anthropic_request_to_openai(body)
        assert result["tool_choice"] == "auto"

    def test_no_tool_choice(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_request_to_openai(body)
        assert "tool_choice" not in result

    def test_no_max_tokens(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_request_to_openai(body)
        assert "max_tokens" not in result

    def test_empty_messages(self):
        body = {"model": "claude-sonnet-4", "messages": []}
        result = anthropic_request_to_openai(body)
        assert result["messages"] == []

    def test_user_content_list_text_only(self):
        """User message with list of text blocks (no tool_result)."""
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part 1"},
                        {"type": "text", "text": "Part 2"},
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        user_msgs = [m for m in result["messages"] if m["role"] == "user"]
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "Part 1\nPart 2"

    def test_multiple_tool_uses_in_assistant(self):
        body = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Do tasks"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_a",
                            "name": "tool_a",
                            "input": {"x": 1},
                        },
                        {
                            "type": "tool_use",
                            "id": "tu_b",
                            "name": "tool_b",
                            "input": {"y": 2},
                        },
                    ],
                },
            ],
        }
        result = anthropic_request_to_openai(body)
        assistant_msg = result["messages"][1]
        assert len(assistant_msg["tool_calls"]) == 2
        assert assistant_msg["tool_calls"][0]["id"] == "tu_a"
        assert assistant_msg["tool_calls"][1]["id"] == "tu_b"


# ---------------------------------------------------------------------------
# openai_response_to_anthropic tests
# ---------------------------------------------------------------------------

class TestOpenaiResponseToAnthropic:

    def test_text_response(self):
        data = {
            "id": "chatcmpl-abc123",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_response_to_anthropic(data, model="claude-sonnet-4")
        assert result["id"] == "chatcmpl-abc123"
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["model"] == "claude-sonnet-4"
        assert len(result["content"]) == 1
        assert result["content"][0] == {"type": "text", "text": "Hello!"}
        assert result["stop_reason"] == "end_turn"
        assert result["stop_sequence"] is None
        assert result["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_tool_calls_response(self):
        data = {
            "id": "chatcmpl-xyz",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Tokyo"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 15},
        }
        result = openai_response_to_anthropic(data)
        assert result["stop_reason"] == "tool_use"
        # No text block since content is None (falsy)
        tool_blocks = [b for b in result["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["id"] == "call_1"
        assert tool_blocks[0]["name"] == "get_weather"
        assert tool_blocks[0]["input"] == {"city": "Tokyo"}

    def test_text_and_tool_calls(self):
        data = {
            "id": "chatcmpl-both",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Let me check.",
                        "tool_calls": [
                            {
                                "id": "call_2",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"q":"test"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 30, "completion_tokens": 20},
        }
        result = openai_response_to_anthropic(data)
        assert len(result["content"]) == 2
        assert result["content"][0] == {"type": "text", "text": "Let me check."}
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][1]["name"] == "search"

    def test_stop_reason_end_turn(self):
        data = {
            "id": "x",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }
        assert openai_response_to_anthropic(data)["stop_reason"] == "end_turn"

    def test_stop_reason_tool_use(self):
        data = {
            "id": "x",
            "choices": [
                {"message": {"content": "ok"}, "finish_reason": "tool_calls"}
            ],
            "usage": {},
        }
        assert openai_response_to_anthropic(data)["stop_reason"] == "tool_use"

    def test_stop_reason_max_tokens(self):
        data = {
            "id": "x",
            "choices": [
                {"message": {"content": "ok"}, "finish_reason": "length"}
            ],
            "usage": {},
        }
        assert openai_response_to_anthropic(data)["stop_reason"] == "max_tokens"

    def test_stop_reason_unknown_passthrough(self):
        data = {
            "id": "x",
            "choices": [
                {"message": {"content": "ok"}, "finish_reason": "content_filter"}
            ],
            "usage": {},
        }
        assert openai_response_to_anthropic(data)["stop_reason"] == "content_filter"

    def test_usage_mapping(self):
        data = {
            "id": "x",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        result = openai_response_to_anthropic(data)
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 50

    def test_usage_defaults_to_zero(self):
        data = {
            "id": "x",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
        result = openai_response_to_anthropic(data)
        assert result["usage"] == {"input_tokens": 0, "output_tokens": 0}

    def test_model_from_data_fallback(self):
        data = {
            "id": "x",
            "model": "gpt-4o",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }
        result = openai_response_to_anthropic(data)
        assert result["model"] == "gpt-4o"

    def test_model_param_overrides_data(self):
        data = {
            "id": "x",
            "model": "gpt-4o",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {},
        }
        result = openai_response_to_anthropic(data, model="claude-sonnet-4")
        assert result["model"] == "claude-sonnet-4"

    def test_invalid_tool_arguments_json(self):
        """Malformed JSON in tool arguments should result in empty dict."""
        data = {
            "id": "x",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_bad",
                                "function": {
                                    "name": "broken",
                                    "arguments": "{invalid json",
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
        result = openai_response_to_anthropic(data)
        tool_block = result["content"][0]
        assert tool_block["input"] == {}

    def test_empty_choices_graceful(self):
        data = {"id": "x", "choices": [{}], "usage": {}}
        result = openai_response_to_anthropic(data)
        assert result["content"] == []
        assert result["stop_reason"] == ""

    def test_multiple_tool_calls(self):
        data = {
            "id": "x",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_a",
                                "function": {
                                    "name": "tool_a",
                                    "arguments": '{"x":1}',
                                },
                            },
                            {
                                "id": "call_b",
                                "function": {
                                    "name": "tool_b",
                                    "arguments": '{"y":2}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
        result = openai_response_to_anthropic(data)
        tool_blocks = [b for b in result["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 2
        assert tool_blocks[0]["name"] == "tool_a"
        assert tool_blocks[1]["name"] == "tool_b"


# ---------------------------------------------------------------------------
# openai_sse_to_anthropic_events tests
# ---------------------------------------------------------------------------

def _collect_events(lines: list[str], model: str = "test-model") -> list[tuple[str, dict]]:
    """Helper: collect SSE events and parse their JSON data."""
    events = []
    for event_type, json_str in openai_sse_to_anthropic_events(iter(lines), model=model):
        events.append((event_type, json.loads(json_str)))
    return events


class TestOpenaiSseToAnthropicEvents:

    # -- Text stream ---

    def test_text_stream_basic(self):
        """Simple text-only stream: message_start, block_start, deltas, block_stop, delta, stop."""
        lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        types = [e[0] for e in events]

        assert types[0] == "message_start"
        assert types[1] == "content_block_start"
        assert types[2] == "content_block_delta"
        assert types[3] == "content_block_delta"
        # finish_reason triggers block_stop + message_delta
        assert "content_block_stop" in types
        assert "message_delta" in types
        # [DONE] triggers final message_delta + message_stop
        assert types[-1] == "message_stop"

    def test_text_stream_content_values(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"!"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)

        # Check message_start structure
        msg_start = events[0][1]
        assert msg_start["type"] == "message_start"
        assert msg_start["message"]["role"] == "assistant"
        assert msg_start["message"]["model"] == "test-model"

        # Check content block start
        block_start = events[1][1]
        assert block_start["type"] == "content_block_start"
        assert block_start["content_block"]["type"] == "text"
        assert block_start["index"] == 0

        # Check deltas
        delta1 = events[2][1]
        assert delta1["delta"]["type"] == "text_delta"
        assert delta1["delta"]["text"] == "Hi"

        delta2 = events[3][1]
        assert delta2["delta"]["text"] == "!"

    def test_text_stream_model_propagated(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines, model="claude-opus-4")
        msg_start = events[0][1]
        assert msg_start["message"]["model"] == "claude-opus-4"

    # -- Tool call stream ---

    def test_tool_call_stream(self):
        """Stream with a single tool call."""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"get_weather","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"ci"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"ty\\":\\"NYC\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        types = [e[0] for e in events]

        assert types[0] == "message_start"

        # Find tool block start
        tool_starts = [(i, e) for i, (t, e) in enumerate(events) if t == "content_block_start"]
        assert len(tool_starts) >= 1
        tool_block = tool_starts[0][1]
        assert tool_block["content_block"]["type"] == "tool_use"
        assert tool_block["content_block"]["id"] == "call_1"
        assert tool_block["content_block"]["name"] == "get_weather"

        # Find input_json_delta events
        json_deltas = [
            e for t, e in events if t == "content_block_delta"
            and e.get("delta", {}).get("type") == "input_json_delta"
        ]
        assert len(json_deltas) == 2
        combined = "".join(d["delta"]["partial_json"] for d in json_deltas)
        assert json.loads(combined) == {"city": "NYC"}

        # Stop reason should be tool_use
        msg_deltas = [e for t, e in events if t == "message_delta"]
        tool_delta = [d for d in msg_deltas if d["delta"].get("stop_reason") == "tool_use"]
        assert len(tool_delta) >= 1

    # -- Combined text + tool stream ---

    def test_text_then_tool_call_stream(self):
        """Stream with text content followed by a tool call."""
        lines = [
            'data: {"choices":[{"delta":{"content":"Let me check."},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_2","function":{"name":"search","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"q\\":\\"test\\"}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        types = [e[0] for e in events]

        # message_start first
        assert types[0] == "message_start"

        # text block should start then be stopped before tool block
        block_starts = [
            (i, e) for i, (t, e) in enumerate(events)
            if t == "content_block_start"
        ]
        assert len(block_starts) == 2  # text block + tool block
        assert block_starts[0][1]["content_block"]["type"] == "text"
        assert block_starts[1][1]["content_block"]["type"] == "tool_use"

        # text block_stop should come before tool block_start
        text_block_stop_idx = next(
            i for i, (t, e) in enumerate(events)
            if t == "content_block_stop" and e["index"] == 0
        )
        tool_block_start_idx = block_starts[1][0]
        assert text_block_stop_idx < tool_block_start_idx

        # text delta
        text_deltas = [
            e for t, e in events if t == "content_block_delta"
            and e.get("delta", {}).get("type") == "text_delta"
        ]
        assert len(text_deltas) == 1
        assert text_deltas[0]["delta"]["text"] == "Let me check."

    # -- Edge cases ---

    def test_non_data_lines_ignored(self):
        """Lines not starting with 'data: ' should be silently skipped."""
        lines = [
            "",
            "event: message",
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}',
            ": comment",
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        text_deltas = [
            e for t, e in events if t == "content_block_delta"
            and e.get("delta", {}).get("type") == "text_delta"
        ]
        assert len(text_deltas) == 1
        assert text_deltas[0]["delta"]["text"] == "ok"

    def test_invalid_json_skipped(self):
        """Invalid JSON data lines should be skipped without error."""
        lines = [
            "data: {broken json",
            'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        text_deltas = [
            e for t, e in events if t == "content_block_delta"
            and e.get("delta", {}).get("type") == "text_delta"
        ]
        assert len(text_deltas) == 1

    def test_done_yields_message_stop(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        assert events[-1][0] == "message_stop"
        assert events[-1][1]["type"] == "message_stop"

    def test_done_stop_reason_default_end_turn(self):
        """[DONE] without prior finish_reason should produce end_turn."""
        lines = [
            'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        msg_deltas = [e for t, e in events if t == "message_delta"]
        # The [DONE] handler emits end_turn
        done_delta = msg_deltas[-1]
        assert done_delta["delta"]["stop_reason"] == "end_turn"

    def test_usage_in_separate_chunk(self):
        """Usage info may arrive in a chunk without choices."""
        lines = [
            'data: {"usage":{"prompt_tokens":42,"completion_tokens":0}}',
            'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)

        # message_start should include usage
        msg_start = events[0][1]
        assert msg_start["type"] == "message_start"
        assert msg_start["message"]["usage"]["input_tokens"] == 42

    def test_usage_tracked_across_chunks(self):
        """Output token count accumulates and appears in final message_delta."""
        lines = [
            'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}],"usage":{"prompt_tokens":10,"completion_tokens":1}}',
            'data: {"choices":[{"delta":{"content":"y"},"finish_reason":null}],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":3}}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        msg_deltas = [e for t, e in events if t == "message_delta"]
        # Last message_delta (from finish_reason) should have accumulated tokens
        last_delta = msg_deltas[-1]
        assert last_delta["usage"]["output_tokens"] == 3

    def test_stop_reason_length_maps_to_max_tokens(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        msg_deltas = [e for t, e in events if t == "message_delta"]
        length_delta = [d for d in msg_deltas if d["delta"].get("stop_reason") == "max_tokens"]
        assert len(length_delta) >= 1

    def test_empty_stream(self):
        """Stream with only [DONE] (no content)."""
        lines = ["data: [DONE]"]
        events = _collect_events(lines)
        # Should produce message_delta + message_stop at minimum
        types = [e[0] for e in events]
        assert "message_delta" in types
        assert "message_stop" in types

    def test_block_indices_increment(self):
        """Block indices should auto-increment for each new content block."""
        lines = [
            'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"fn","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        block_starts = [e for t, e in events if t == "content_block_start"]
        assert block_starts[0]["index"] == 0  # text block
        assert block_starts[1]["index"] == 1  # tool block

    def test_multiple_tool_calls_in_stream(self):
        """Two distinct tool calls in a single stream."""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a","function":{"name":"foo","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"x\\":1}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_b","function":{"name":"bar","arguments":""}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":{"arguments":"{\\"y\\":2}"}}]},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        events = _collect_events(lines)
        block_starts = [e for t, e in events if t == "content_block_start"]
        assert len(block_starts) == 2
        assert block_starts[0]["content_block"]["name"] == "foo"
        assert block_starts[1]["content_block"]["name"] == "bar"


# ---------------------------------------------------------------------------
# StreamState tests
# ---------------------------------------------------------------------------

class TestStreamState:

    def test_message_start_event_structure(self):
        from mutbot.proxy.translation import StreamState
        state = StreamState(model="test-model")
        state.input_tokens = 50
        event = state._message_start_event()
        assert event["type"] == "message_start"
        assert event["message"]["model"] == "test-model"
        assert event["message"]["role"] == "assistant"
        assert event["message"]["usage"]["input_tokens"] == 50
        assert event["message"]["stop_reason"] is None

    def test_content_block_start_text(self):
        from mutbot.proxy.translation import StreamState
        state = StreamState()
        block = state._content_block_start("text")
        assert block["type"] == "content_block_start"
        assert block["index"] == 0
        assert block["content_block"]["type"] == "text"
        assert block["content_block"]["text"] == ""

    def test_content_block_start_tool_use(self):
        from mutbot.proxy.translation import StreamState
        state = StreamState()
        block = state._content_block_start("tool_use", id="call_1", name="my_tool")
        assert block["content_block"]["type"] == "tool_use"
        assert block["content_block"]["id"] == "call_1"
        assert block["content_block"]["name"] == "my_tool"
        assert block["content_block"]["input"] == {}

    def test_block_index_auto_increments(self):
        from mutbot.proxy.translation import StreamState
        state = StreamState()
        b0 = state._content_block_start("text")
        b1 = state._content_block_start("tool_use", id="x", name="y")
        b2 = state._content_block_start("text")
        assert b0["index"] == 0
        assert b1["index"] == 1
        assert b2["index"] == 2

    def test_content_block_stop(self):
        from mutbot.proxy.translation import StreamState
        state = StreamState()
        state._content_block_start("text")  # block_index -> 0
        stop = state._content_block_stop()
        assert stop["type"] == "content_block_stop"
        assert stop["index"] == 0
