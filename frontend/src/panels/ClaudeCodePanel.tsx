import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import Markdown from "../components/Markdown";
import "./claude-code.css";

/** Claude Code CLI stdout 消息 */
interface CCEvent {
  type: string;
  subtype?: string;
  [key: string]: unknown;
}

/** 前端显示的消息条目 */
type CCMessage =
  | { kind: "system"; text: string }
  | { kind: "assistant_text"; id: string; content: string; streaming: boolean }
  | { kind: "tool_use"; id: string; name: string; input: Record<string, unknown>; collapsed: boolean }
  | { kind: "tool_result"; toolUseId: string; content: string; isError: boolean }
  | { kind: "permission"; requestId: string; toolName: string; input: Record<string, unknown>; resolved: boolean }
  | { kind: "result"; duration_ms?: number; num_turns?: number; cost?: string }
  | { kind: "status"; text: string }
  | { kind: "error"; text: string };

interface Props {
  sessionId: string;
  rpc?: WorkspaceRpc | null;
}

export default function ClaudeCodePanel({ sessionId, rpc }: Props) {
  const [messages, setMessages] = useState<CCMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const [processAlive, setProcessAlive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [inputText, setInputText] = useState("");

  const chRef = useRef(0);
  const messagesRef = useRef(messages);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // 流式文本累积器：content_block index → text
  const streamBuffers = useRef<Map<number, string>>(new Map());
  // 当前 assistant 消息 ID
  const currentAssistantId = useRef<string | null>(null);
  // 自动滚动到底部
  const autoScrollRef = useRef(true);

  messagesRef.current = messages;

  // 滚动到底部
  const scrollToBottom = useCallback(() => {
    if (scrollRef.current && autoScrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  // 处理后端广播的 claude_code_event
  const handleEvent = useCallback((data: Record<string, unknown>) => {
    const eventType = data.type as string;

    if (eventType === "ready") {
      setProcessAlive(data.alive as boolean);
      return;
    }

    if (eventType === "error") {
      addMessage({ kind: "error", text: data.message as string });
      return;
    }

    if (eventType !== "claude_code_event") return;

    const event = data.event as CCEvent;
    if (!event) return;

    handleCCEvent(event);
  }, []);

  function handleCCEvent(event: CCEvent) {
    const t = event.type;

    if (t === "system") {
      if (event.subtype === "init") {
        setProcessAlive(true);
        setBusy(false);
        const ver = event.claude_code_version as string || "";
        const model = event.model as string || "";
        addMessage({ kind: "system", text: `Claude Code ${ver}${model ? ` (${model})` : ""} initialized` });
      } else if (event.subtype === "status") {
        const status = event.status as string | null;
        if (status) {
          addMessage({ kind: "status", text: status });
        }
      }
    } else if (t === "stream_event") {
      handleStreamEvent(event.event as Record<string, unknown>);
    } else if (t === "assistant") {
      // 完整 assistant 消息 — 替换流式中间状态
      finalizeAssistantMessage(event);
      setBusy(false);
    } else if (t === "user") {
      // 用户消息 — 可能包含 tool_result
      const message = event.message as Record<string, unknown> | undefined;
      const content = message?.content as Array<Record<string, unknown>> | undefined;
      if (content) {
        for (const block of content) {
          if (block.type === "tool_result") {
            const resultContent = block.content as string | Array<Record<string, unknown>> | undefined;
            let text = "";
            if (typeof resultContent === "string") {
              text = resultContent;
            } else if (Array.isArray(resultContent)) {
              text = resultContent.map(c => (c.text as string) || "").join("\n");
            }
            addMessage({
              kind: "tool_result",
              toolUseId: block.tool_use_id as string || "",
              content: text.slice(0, 5000),  // 截断过长输出
              isError: block.is_error as boolean || false,
            });
          }
        }
      }
    } else if (t === "result") {
      setBusy(false);
      const cost = event.cost_usd != null ? `$${(event.cost_usd as number).toFixed(4)}` : undefined;
      addMessage({
        kind: "result",
        duration_ms: event.duration_ms as number | undefined,
        num_turns: event.num_turns as number | undefined,
        cost,
      });
    } else if (t === "control_request") {
      const req = event.request as Record<string, unknown> | undefined;
      if (req?.subtype === "can_use_tool") {
        addMessage({
          kind: "permission",
          requestId: event.request_id as string,
          toolName: req.tool_name as string,
          input: req.input as Record<string, unknown> || {},
          resolved: false,
        });
      }
    } else if (t === "process_exited") {
      setProcessAlive(false);
      setBusy(false);
      addMessage({
        kind: "system",
        text: `Process exited (code: ${event.exit_code ?? "unknown"})`,
      });
    }
  }

  function handleStreamEvent(event: Record<string, unknown>) {
    if (!event) return;
    const eventType = event.type as string;

    if (eventType === "content_block_start") {
      const index = event.index as number;
      const block = event.content_block as Record<string, unknown> | undefined;
      if (block?.type === "text") {
        streamBuffers.current.set(index, "");
        // 创建或更新 assistant 消息
        ensureAssistantMessage();
      } else if (block?.type === "tool_use") {
        addMessage({
          kind: "tool_use",
          id: block.id as string,
          name: block.name as string,
          input: {},
          collapsed: true,
        });
      }
    } else if (eventType === "content_block_delta") {
      const index = event.index as number;
      const delta = event.delta as Record<string, unknown> | undefined;
      if (delta?.type === "text_delta") {
        const text = delta.text as string;
        const buf = streamBuffers.current.get(index) ?? "";
        streamBuffers.current.set(index, buf + text);
        updateAssistantContent();
      } else if (delta?.type === "input_json_delta") {
        // tool input streaming — 暂不处理增量，等 content_block_stop
      }
    } else if (eventType === "content_block_stop") {
      // block 完成
    } else if (eventType === "message_start") {
      setBusy(true);
      streamBuffers.current.clear();
      currentAssistantId.current = null;
    } else if (eventType === "message_stop") {
      // 等 assistant 完整消息
    }
  }

  function ensureAssistantMessage() {
    if (currentAssistantId.current) return;
    const id = `cc-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
    currentAssistantId.current = id;
    addMessage({ kind: "assistant_text", id, content: "", streaming: true });
  }

  function updateAssistantContent() {
    const id = currentAssistantId.current;
    if (!id) return;
    // 合并所有 text buffers
    let combined = "";
    const sorted = [...streamBuffers.current.entries()].sort((a, b) => a[0] - b[0]);
    for (const [, text] of sorted) {
      combined += text;
    }
    setMessages(prev =>
      prev.map(m =>
        m.kind === "assistant_text" && m.id === id
          ? { ...m, content: combined }
          : m
      )
    );
  }

  function finalizeAssistantMessage(event: CCEvent) {
    const id = currentAssistantId.current;
    const message = event.message as Record<string, unknown> | undefined;
    const content = message?.content as Array<Record<string, unknown>> | undefined;

    if (!content) return;

    // 提取完整文本
    let fullText = "";
    for (const block of content) {
      if (block.type === "text") {
        fullText += (block.text as string) || "";
      }
    }

    if (id) {
      // 更新现有流式消息为完整状态
      setMessages(prev =>
        prev.map(m =>
          m.kind === "assistant_text" && m.id === id
            ? { ...m, content: fullText, streaming: false }
            : m
        )
      );
    } else if (fullText) {
      // 没有流式消息（罕见），直接添加
      addMessage({
        kind: "assistant_text",
        id: `cc-${Date.now()}`,
        content: fullText,
        streaming: false,
      });
    }

    // 处理 tool_use blocks（完整消息中的）
    for (const block of content) {
      if (block.type === "tool_use") {
        // 检查是否已存在（从 stream_event 添加的）
        const exists = messagesRef.current.some(
          m => m.kind === "tool_use" && m.id === (block.id as string)
        );
        if (!exists) {
          addMessage({
            kind: "tool_use",
            id: block.id as string,
            name: block.name as string,
            input: block.input as Record<string, unknown> || {},
            collapsed: true,
          });
        }
      }
    }

    streamBuffers.current.clear();
    currentAssistantId.current = null;
  }

  function addMessage(msg: CCMessage) {
    setMessages(prev => [...prev, msg]);
  }

  // 连接 session
  useEffect(() => {
    if (!rpc) return;

    setMessages([]);
    setConnected(false);
    setProcessAlive(false);
    setBusy(false);
    streamBuffers.current.clear();
    currentAssistantId.current = null;

    let ch = 0;

    rpc.call<{ ch: number }>("session.connect", { session_id: sessionId })
      .then(({ ch: channelId }) => {
        ch = channelId;
        chRef.current = ch;
        rpc!.onChannel(ch, handleEvent);
        setConnected(true);
      })
      .catch(() => setConnected(false));

    const unsubClosed = rpc.onChannelClosed((closedCh) => {
      if (closedCh === chRef.current) {
        setConnected(false);
        chRef.current = 0;
      }
    });

    return () => {
      unsubClosed();
      if (ch > 0) {
        rpc!.cleanupChannelHandlers(ch);
        rpc!.call("session.disconnect", { session_id: sessionId, ch }).catch(() => {});
      }
    };
  }, [sessionId, rpc, handleEvent]);

  // 发送消息
  const handleSend = useCallback(() => {
    const text = inputText.trim();
    if (!text || !rpc || chRef.current <= 0) return;

    // 本地显示用户消息
    addMessage({ kind: "system", text: `> ${text}` });
    setInputText("");
    setBusy(true);

    rpc.sendToChannel(chRef.current, { type: "user_message", text });
    inputRef.current?.focus();
  }, [inputText, rpc]);

  // 中断
  const handleInterrupt = useCallback(() => {
    if (rpc && chRef.current > 0) {
      rpc.sendToChannel(chRef.current, { type: "interrupt" });
    }
  }, [rpc]);

  // 权限回复
  const handlePermission = useCallback((requestId: string, behavior: string) => {
    if (rpc && chRef.current > 0) {
      rpc.sendToChannel(chRef.current, {
        type: "permission_response",
        request_id: requestId,
        behavior,
      });
      // 标记已处理
      setMessages(prev =>
        prev.map(m =>
          m.kind === "permission" && m.requestId === requestId
            ? { ...m, resolved: true }
            : m
        )
      );
    }
  }, [rpc]);

  // 键盘事件
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  // 滚动检测（是否在底部）
  const handleScroll = useCallback(() => {
    if (!scrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = scrollRef.current;
    autoScrollRef.current = scrollHeight - scrollTop - clientHeight < 100;
  }, []);

  return (
    <div className="cc-panel">
      {/* 状态栏 */}
      <div className="cc-header">
        <span className="cc-status-dot" data-alive={processAlive} />
        <span className="cc-header-label">Claude Code</span>
        <span className="cc-session-id">{sessionId.slice(0, 8)}</span>
        {!connected && <span className="cc-disconnected">disconnected</span>}
      </div>

      {/* 消息区 */}
      <div className="cc-messages" ref={scrollRef} onScroll={handleScroll}>
        {messages.map((msg, i) => (
          <CCMessageRow key={i} msg={msg} onPermission={handlePermission} />
        ))}
        {busy && <div className="cc-thinking">Thinking...</div>}
      </div>

      {/* 输入区 */}
      <div className="cc-input-area">
        <textarea
          ref={inputRef}
          value={inputText}
          onChange={e => setInputText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={processAlive ? "Type a message..." : "Process not running"}
          disabled={!connected || !processAlive}
          rows={1}
        />
        <div className="cc-input-buttons">
          {busy ? (
            <button className="cc-btn cc-btn-stop" onClick={handleInterrupt}>Stop</button>
          ) : (
            <button
              className="cc-btn cc-btn-send"
              onClick={handleSend}
              disabled={!inputText.trim() || !processAlive}
            >
              Send
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 消息行渲染
// ---------------------------------------------------------------------------

function CCMessageRow({ msg, onPermission }: {
  msg: CCMessage;
  onPermission: (requestId: string, behavior: string) => void;
}) {
  switch (msg.kind) {
    case "system":
      return <div className="cc-msg cc-msg-system">{msg.text}</div>;

    case "assistant_text":
      return (
        <div className="cc-msg cc-msg-assistant">
          <div className="cc-terminal-md">
            <Markdown content={msg.content || " "} />
          </div>
        </div>
      );

    case "tool_use":
      return <ToolUseBlock msg={msg} />;

    case "tool_result":
      return (
        <div className={`cc-msg cc-msg-tool-result ${msg.isError ? "cc-error" : ""}`}>
          <pre>{msg.content}</pre>
        </div>
      );

    case "permission":
      return <PermissionBlock msg={msg} onPermission={onPermission} />;

    case "result":
      return (
        <div className="cc-msg cc-msg-result">
          {msg.duration_ms != null && <span>Duration: {(msg.duration_ms / 1000).toFixed(1)}s</span>}
          {msg.num_turns != null && <span> | Turns: {msg.num_turns}</span>}
          {msg.cost && <span> | Cost: {msg.cost}</span>}
        </div>
      );

    case "status":
      return <div className="cc-msg cc-msg-status">{msg.text}</div>;

    case "error":
      return <div className="cc-msg cc-msg-error">{msg.text}</div>;

    default:
      return null;
  }
}

function ToolUseBlock({ msg }: { msg: CCMessage & { kind: "tool_use" } }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="cc-msg cc-msg-tool">
      <div className="cc-tool-header" onClick={() => setExpanded(!expanded)}>
        <span className="cc-tool-chevron">{expanded ? "▼" : "▶"}</span>
        <span className="cc-tool-name">{msg.name}</span>
        <span className="cc-tool-id">{msg.id.slice(0, 8)}</span>
      </div>
      {expanded && (
        <pre className="cc-tool-input">{JSON.stringify(msg.input, null, 2)}</pre>
      )}
    </div>
  );
}

function PermissionBlock({ msg, onPermission }: {
  msg: CCMessage & { kind: "permission" };
  onPermission: (requestId: string, behavior: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="cc-msg cc-msg-permission">
      <div className="cc-permission-header">
        <span className="cc-permission-icon">⚠</span>
        <span className="cc-permission-tool">{msg.toolName}</span>
        {!msg.resolved && (
          <div className="cc-permission-actions">
            <button
              className="cc-btn cc-btn-allow"
              onClick={() => onPermission(msg.requestId, "allow")}
            >
              Allow
            </button>
            <button
              className="cc-btn cc-btn-deny"
              onClick={() => onPermission(msg.requestId, "deny")}
            >
              Deny
            </button>
          </div>
        )}
        {msg.resolved && <span className="cc-permission-resolved">resolved</span>}
      </div>
      <div
        className="cc-tool-header"
        onClick={() => setExpanded(!expanded)}
        style={{ paddingLeft: "24px" }}
      >
        <span className="cc-tool-chevron">{expanded ? "▼" : "▶"}</span>
        <span style={{ opacity: 0.7 }}>Input</span>
      </div>
      {expanded && (
        <pre className="cc-tool-input">{JSON.stringify(msg.input, null, 2)}</pre>
      )}
    </div>
  );
}
