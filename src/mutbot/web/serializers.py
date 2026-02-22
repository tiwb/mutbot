"""Serialize mutagent dataclasses to JSON-safe dicts for WebSocket transport."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mutagent.messages import (
    Content,
    Message,
    Response,
    StreamEvent,
    ToolCall,
    ToolResult,
)


def serialize_tool_call(tc: ToolCall) -> dict[str, Any]:
    return {"id": tc.id, "name": tc.name, "arguments": tc.arguments}


def serialize_tool_result(tr: ToolResult) -> dict[str, Any]:
    return {
        "tool_call_id": tr.tool_call_id,
        "content": tr.content,
        "is_error": tr.is_error,
    }


def serialize_message(msg: Message) -> dict[str, Any]:
    d: dict[str, Any] = {"role": msg.role, "content": msg.content}
    if msg.tool_calls:
        d["tool_calls"] = [serialize_tool_call(tc) for tc in msg.tool_calls]
    if msg.tool_results:
        d["tool_results"] = [serialize_tool_result(tr) for tr in msg.tool_results]
    return d


def serialize_response(resp: Response) -> dict[str, Any]:
    return {
        "message": serialize_message(resp.message),
        "stop_reason": resp.stop_reason,
        "usage": resp.usage,
    }


def serialize_content(content: Content) -> dict[str, Any]:
    d: dict[str, Any] = {"type": content.type, "body": content.body}
    if content.target:
        d["target"] = content.target
    if content.source:
        d["source"] = content.source
    if content.metadata:
        d["metadata"] = content.metadata
    return d


def serialize_stream_event(event: StreamEvent) -> dict[str, Any]:
    """Convert a StreamEvent to a JSON-serializable dict."""
    d: dict[str, Any] = {"type": event.type}

    if event.type == "text_delta":
        d["text"] = event.text
    elif event.type in ("tool_use_start", "tool_exec_start"):
        if event.tool_call:
            d["tool_call"] = serialize_tool_call(event.tool_call)
    elif event.type == "tool_use_delta":
        d["tool_json_delta"] = event.tool_json_delta
    elif event.type == "tool_exec_end":
        if event.tool_result:
            d["tool_result"] = serialize_tool_result(event.tool_result)
    elif event.type == "response_done":
        if event.response:
            d["response"] = serialize_response(event.response)
    elif event.type == "error":
        d["error"] = event.error
    # turn_done, tool_use_end: just type is enough

    return d
