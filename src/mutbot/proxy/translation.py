"""mutbot.proxy.translation -- Anthropic ↔ OpenAI JSON 格式转换。

代理层的 JSON-to-JSON 直接转换（不经过 mutagent 内部类型）。
用于代理在不同格式的客户端和后端之间桥接。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator


# ---------------------------------------------------------------------------
# 模型名称归一化
# ---------------------------------------------------------------------------

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def normalize_model_name(model: str) -> str:
    """Strip 日期后缀：claude-sonnet-4-20250514 → claude-sonnet-4"""
    return _DATE_SUFFIX_RE.sub("", model)


# ---------------------------------------------------------------------------
# 请求转换：Anthropic → OpenAI
# ---------------------------------------------------------------------------

def anthropic_request_to_openai(body: dict[str, Any]) -> dict[str, Any]:
    """将 Anthropic Messages 格式请求转换为 OpenAI Chat Completions 格式。"""
    openai_messages: list[dict[str, Any]] = []

    # system → messages[0]
    system = body.get("system")
    if system:
        if isinstance(system, str):
            openai_messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            # Anthropic system can be a list of content blocks
            text_parts = [b.get("text", "") for b in system if b.get("type") == "text"]
            openai_messages.append({"role": "system", "content": "\n".join(text_parts)})

    # messages
    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                openai_messages.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # May contain text and tool_result blocks
                text_parts = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_result":
                        # Each tool_result becomes a separate "tool" message
                        tool_content = block.get("content", "")
                        if isinstance(tool_content, list):
                            tool_content = "\n".join(
                                b.get("text", "") for b in tool_content
                                if b.get("type") == "text"
                            )
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": tool_content,
                        })
                if text_parts:
                    openai_messages.insert(-len([
                        b for b in content if b.get("type") == "tool_result"
                    ]) or len(openai_messages), {
                        "role": "user",
                        "content": "\n".join(text_parts),
                    })

        elif role == "assistant":
            if isinstance(content, str):
                openai_messages.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                openai_messages.append(entry)

    # Build payload
    result: dict[str, Any] = {
        "model": normalize_model_name(body.get("model", "")),
        "messages": openai_messages,
    }

    if "max_tokens" in body:
        result["max_tokens"] = body["max_tokens"]

    # tools
    if "tools" in body:
        openai_tools = []
        for tool in body["tools"]:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        result["tools"] = openai_tools

    # tool_choice
    tc = body.get("tool_choice")
    if tc:
        if isinstance(tc, dict):
            tc_type = tc.get("type")
            if tc_type == "any":
                result["tool_choice"] = "required"
            elif tc_type == "auto":
                result["tool_choice"] = "auto"
            elif tc_type == "tool":
                result["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc.get("name", "")},
                }
        elif isinstance(tc, str):
            if tc == "any":
                result["tool_choice"] = "required"
            else:
                result["tool_choice"] = tc

    return result


# ---------------------------------------------------------------------------
# 响应转换：OpenAI → Anthropic（非流式）
# ---------------------------------------------------------------------------

def openai_response_to_anthropic(
    data: dict[str, Any],
    model: str = "",
) -> dict[str, Any]:
    """将 OpenAI Chat Completions 响应转换为 Anthropic Messages 格式。"""
    choice = data.get("choices", [{}])[0]
    message_data = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "")

    # stop_reason mapping
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
    }
    stop_reason = stop_reason_map.get(finish_reason, finish_reason)

    # content blocks
    content: list[dict[str, Any]] = []
    text = message_data.get("content")
    if text:
        content.append({"type": "text", "text": text})

    for tc in message_data.get("tool_calls", []):
        func = tc.get("function", {})
        try:
            tool_input = json.loads(func.get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_input = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": func.get("name", ""),
            "input": tool_input,
        })

    # usage
    usage_data = data.get("usage", {})
    usage = {
        "input_tokens": usage_data.get("prompt_tokens", 0),
        "output_tokens": usage_data.get("completion_tokens", 0),
    }

    return {
        "id": data.get("id", ""),
        "type": "message",
        "role": "assistant",
        "model": model or data.get("model", ""),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# 流式 SSE 转换：OpenAI → Anthropic
# ---------------------------------------------------------------------------

class StreamState:
    """OpenAI SSE → Anthropic SSE 转换的状态机。"""

    def __init__(self, model: str = ""):
        self.model = model
        self.block_index: int = -1
        self.current_block_type: str = ""
        self.message_started: bool = False
        self.input_tokens: int = 0
        self.output_tokens: int = 0

    def _message_start_event(self) -> dict[str, Any]:
        """生成 message_start 事件。"""
        return {
            "type": "message_start",
            "message": {
                "id": "",
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
            },
        }

    def _content_block_start(self, block_type: str, **kwargs: Any) -> dict[str, Any]:
        """生成 content_block_start 事件。"""
        self.block_index += 1
        self.current_block_type = block_type
        block: dict[str, Any] = {"type": block_type}
        if block_type == "tool_use":
            block["id"] = kwargs.get("id", "")
            block["name"] = kwargs.get("name", "")
            block["input"] = {}
        elif block_type == "text":
            block["text"] = ""
        return {
            "type": "content_block_start",
            "index": self.block_index,
            "content_block": block,
        }

    def _content_block_stop(self) -> dict[str, Any]:
        """生成 content_block_stop 事件。"""
        return {
            "type": "content_block_stop",
            "index": self.block_index,
        }


def openai_sse_to_anthropic_events(
    openai_lines: Iterator[str],
    model: str = "",
) -> Iterator[tuple[str, str]]:
    """将 OpenAI SSE 行流转换为 Anthropic SSE 事件流。

    Yields:
        (event_type, json_data) 元组。
    """
    state = StreamState(model=model)
    text_block_open = False

    for line in openai_lines:
        if not line.startswith("data: "):
            continue

        data_str = line[6:]
        if data_str == "[DONE]":
            # Close any open block
            if text_block_open or state.current_block_type:
                yield ("content_block_stop",
                       json.dumps(state._content_block_stop()))
                text_block_open = False

            # message_delta with stop_reason
            yield ("message_delta", json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": state.output_tokens},
            }))
            yield ("message_stop", json.dumps({"type": "message_stop"}))
            return

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        # Usage
        if data.get("usage"):
            usage = data["usage"]
            state.input_tokens = usage.get("prompt_tokens", state.input_tokens)
            state.output_tokens = usage.get("completion_tokens", state.output_tokens)

        choices = data.get("choices", [])
        if not choices:
            # May be a usage-only chunk
            if not state.message_started and data.get("usage"):
                state.message_started = True
                yield ("message_start",
                       json.dumps(state._message_start_event()))
            continue

        delta = choices[0].get("delta", {})
        finish_reason = choices[0].get("finish_reason")

        # Ensure message_start
        if not state.message_started:
            state.message_started = True
            yield ("message_start",
                   json.dumps(state._message_start_event()))

        # Text content
        content = delta.get("content")
        if content is not None:
            if not text_block_open:
                yield ("content_block_start",
                       json.dumps(state._content_block_start("text")))
                text_block_open = True
            yield ("content_block_delta", json.dumps({
                "type": "content_block_delta",
                "index": state.block_index,
                "delta": {"type": "text_delta", "text": content},
            }))

        # Tool calls
        for tc_delta in delta.get("tool_calls", []):
            # Close text block if open
            if text_block_open:
                yield ("content_block_stop",
                       json.dumps(state._content_block_stop()))
                text_block_open = False

            func = tc_delta.get("function", {})
            if tc_delta.get("id"):
                # New tool call → start block
                yield ("content_block_start", json.dumps(
                    state._content_block_start(
                        "tool_use",
                        id=tc_delta["id"],
                        name=func.get("name", ""),
                    )
                ))

            args = func.get("arguments", "")
            if args:
                yield ("content_block_delta", json.dumps({
                    "type": "content_block_delta",
                    "index": state.block_index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": args,
                    },
                }))

        # Finish reason
        if finish_reason:
            # Close any open block
            if text_block_open or state.current_block_type:
                yield ("content_block_stop",
                       json.dumps(state._content_block_stop()))
                text_block_open = False
                state.current_block_type = ""

            stop_reason_map = {
                "stop": "end_turn",
                "tool_calls": "tool_use",
                "length": "max_tokens",
            }
            stop = stop_reason_map.get(finish_reason, finish_reason)
            yield ("message_delta", json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": state.output_tokens},
            }))
