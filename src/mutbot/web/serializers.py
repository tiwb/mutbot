"""Serialize mutagent dataclasses to JSON-safe dicts for WebSocket transport."""

from __future__ import annotations

from typing import Any

from mutagent.messages import (
    Content,
    ContentBlock,
    DocumentBlock,
    ImageBlock,
    Message,
    Response,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    TurnEndBlock,
    TurnStartBlock,
)


# ---------------------------------------------------------------------------
# Block 序列化 / 反序列化
# ---------------------------------------------------------------------------

def serialize_block(block: ContentBlock) -> dict[str, Any]:
    """Serialize a ContentBlock to a JSON-safe dict."""
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        d: dict[str, Any] = {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
            "status": block.status,
        }
        if block.status == "done":
            d["result"] = block.result
            d["is_error"] = block.is_error
            d["duration"] = block.duration
        return d
    if isinstance(block, ImageBlock):
        return {"type": "image", "data": block.data, "media_type": block.media_type, "url": block.url}
    if isinstance(block, DocumentBlock):
        return {"type": "document", "data": block.data, "media_type": block.media_type}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature, "data": block.data}
    if isinstance(block, TurnStartBlock):
        return {"type": "turn_start", "turn_id": block.turn_id}
    if isinstance(block, TurnEndBlock):
        return {"type": "turn_end", "turn_id": block.turn_id, "duration": block.duration}
    return {"type": block.type}


_BLOCK_DESERIALIZERS: dict[str, type[ContentBlock]] = {
    "text": TextBlock,
    "tool_use": ToolUseBlock,
    "image": ImageBlock,
    "document": DocumentBlock,
    "thinking": ThinkingBlock,
    "turn_start": TurnStartBlock,
    "turn_end": TurnEndBlock,
}


def deserialize_block(data: dict[str, Any]) -> ContentBlock:
    """Deserialize a dict back to a ContentBlock."""
    block_type = data.get("type", "")
    cls = _BLOCK_DESERIALIZERS.get(block_type)
    if cls is None:
        return ContentBlock(type=block_type)

    if cls is TextBlock:
        return TextBlock(text=data.get("text", ""))
    if cls is ToolUseBlock:
        return ToolUseBlock(
            id=data.get("id", ""),
            name=data.get("name", ""),
            input=data.get("input", {}),
            status=data.get("status", ""),
            result=data.get("result", ""),
            is_error=data.get("is_error", False),
            duration=data.get("duration", 0),
        )
    if cls is ImageBlock:
        return ImageBlock(data=data.get("data", ""), media_type=data.get("media_type", ""), url=data.get("url", ""))
    if cls is DocumentBlock:
        return DocumentBlock(data=data.get("data", ""), media_type=data.get("media_type", ""))
    if cls is ThinkingBlock:
        return ThinkingBlock(thinking=data.get("thinking", ""), signature=data.get("signature", ""), data=data.get("data", ""))
    if cls is TurnStartBlock:
        return TurnStartBlock(turn_id=data.get("turn_id", ""))
    if cls is TurnEndBlock:
        return TurnEndBlock(turn_id=data.get("turn_id", ""), duration=data.get("duration", 0))
    return ContentBlock(type=block_type)


# ---------------------------------------------------------------------------
# Message 序列化 / 反序列化
# ---------------------------------------------------------------------------

def serialize_message(msg: Message) -> dict[str, Any]:
    """Serialize a Message to a JSON-safe dict (持久化 + WebSocket transport)."""
    d: dict[str, Any] = {
        "role": msg.role,
        "blocks": [serialize_block(b) for b in msg.blocks],
    }
    if msg.id:
        d["id"] = msg.id
    if msg.label:
        d["label"] = msg.label
    if msg.sender:
        d["sender"] = msg.sender
    if msg.model:
        d["model"] = msg.model
    if msg.timestamp:
        d["timestamp"] = msg.timestamp
    if msg.duration:
        d["duration"] = msg.duration
    if msg.input_tokens:
        d["input_tokens"] = msg.input_tokens
    if msg.output_tokens:
        d["output_tokens"] = msg.output_tokens
    return d


def deserialize_message(data: dict[str, Any]) -> Message:
    """Deserialize a dict back to a Message (持久化恢复用)."""
    return Message(
        role=data.get("role", ""),
        blocks=[deserialize_block(b) for b in data.get("blocks", [])],
        id=data.get("id", ""),
        label=data.get("label", ""),
        sender=data.get("sender", ""),
        model=data.get("model", ""),
        timestamp=data.get("timestamp", 0),
        duration=data.get("duration", 0),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
    )


# ---------------------------------------------------------------------------
# Response / Content / StreamEvent 序列化
# ---------------------------------------------------------------------------

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

    if event.type == "response_start":
        if event.response:
            d["response"] = serialize_response(event.response)
    elif event.type == "text_delta":
        d["text"] = event.text
    elif event.type in ("tool_use_start", "tool_exec_start"):
        if event.tool_call:
            d["tool_call"] = serialize_block(event.tool_call)
    elif event.type == "tool_use_delta":
        d["tool_json_delta"] = event.tool_json_delta
    elif event.type == "tool_exec_end":
        if event.tool_call:
            d["tool_call"] = serialize_block(event.tool_call)
    elif event.type == "response_done":
        if event.response:
            d["response"] = serialize_response(event.response)
    elif event.type == "turn_done":
        d["turn_id"] = event.turn_id
    elif event.type == "error":
        d["error"] = event.error

    if event.timestamp:
        d["timestamp"] = event.timestamp

    return d
