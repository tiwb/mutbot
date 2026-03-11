"""MCP 最小类型定义。

只定义实际用到的核心类型，不照搬官方 SDK。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# MCP 协议版本
PROTOCOL_VERSION = "2025-03-26"


@dataclass
class ToolDef:
    """MCP tool 定义。"""
    name: str
    description: str = ""
    inputSchema: dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
    })

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.inputSchema,
        }


@dataclass
class ResourceDef:
    """MCP resource 定义。"""
    uri: str
    name: str
    description: str = ""
    mimeType: str = "text/plain"

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mimeType,
        }


@dataclass
class ResourceContent:
    """MCP resource 内容。"""
    uri: str
    text: str | None = None
    blob: str | None = None  # base64 encoded
    mimeType: str = "text/plain"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"uri": self.uri, "mimeType": self.mimeType}
        if self.text is not None:
            d["text"] = self.text
        if self.blob is not None:
            d["blob"] = self.blob
        return d


@dataclass
class PromptDef:
    """MCP prompt 定义。"""
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "arguments": self.arguments,
        }


@dataclass
class PromptMessage:
    """MCP prompt 消息。"""
    role: str  # "user" | "assistant"
    content: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass
class ToolResult:
    """MCP tool 调用结果。"""
    content: list[dict[str, Any]] = field(default_factory=list)
    isError: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"content": self.content}
        if self.isError:
            d["isError"] = True
        return d

    @classmethod
    def text(cls, text: str) -> ToolResult:
        """快捷创建文本结果。"""
        return cls(content=[{"type": "text", "text": text}])

    @classmethod
    def error(cls, message: str) -> ToolResult:
        """快捷创建错误结果。"""
        return cls(content=[{"type": "text", "text": message}], isError=True)


@dataclass
class ServerCapabilities:
    """MCP server 能力声明。"""
    tools: dict[str, Any] | None = None
    resources: dict[str, Any] | None = None
    prompts: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.tools is not None:
            d["tools"] = self.tools
        if self.resources is not None:
            d["resources"] = self.resources
        if self.prompts is not None:
            d["prompts"] = self.prompts
        if self.logging is not None:
            d["logging"] = self.logging
        return d
