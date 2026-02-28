import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import Markdown from "./Markdown";
import ToolCallCard, { type ToolGroupData } from "./ToolCallCard";
import RpcMenu from "./RpcMenu";
import CodeBlock from "./CodeBlock";
import Avatar from "./Avatar";

export type ChatMessage =
  | { id: string; role: "user"; type: "text"; content: string; timestamp?: string; sender?: string }
  | { id: string; role: "assistant"; type: "text"; content: string; timestamp?: string; durationMs?: number; model?: string }
  | { id: string; role: "assistant"; type: "tool_group"; data: ToolGroupData; timestamp?: string; durationMs?: number; model?: string }
  | { id: string; role: "assistant"; type: "error"; content: string; timestamp?: string; model?: string }
  | { id: string; type: "turn_start"; turnId: string; timestamp: string }
  | { id: string; type: "turn_done"; turnId: string; timestamp: string; durationSeconds: number };

/** Agent 显示信息，由 AgentPanel 传入。 */
export interface AgentDisplay {
  name: string;
  avatar?: string;
}

type MarkdownMode = "rendered" | "source";

const MARKDOWN_MODE_KEY = "mutbot-markdown-display-mode";

function loadMarkdownMode(): MarkdownMode {
  try {
    const v = localStorage.getItem(MARKDOWN_MODE_KEY);
    if (v === "source") return "source";
  } catch { /* ignore */ }
  return "rendered";
}

// --- 时间格式化 ---

function formatMessageTime(isoTimestamp: string): string {
  const date = new Date(isoTimestamp);
  const now = new Date();
  const hh = date.getHours().toString().padStart(2, "0");
  const mm = date.getMinutes().toString().padStart(2, "0");
  const time = `${hh}:${mm}`;
  if (
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  ) {
    return time;
  }
  if (date.getFullYear() === now.getFullYear()) {
    return `${date.getMonth() + 1}/${date.getDate()} ${time}`;
  }
  return `${date.getFullYear()}/${date.getMonth() + 1}/${date.getDate()} ${time}`;
}

function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60000);
  const s = Math.round((ms % 60000) / 1000);
  return `${m}m ${s}s`;
}

function formatTurnDuration(seconds: number): string {
  if (seconds < 60) return `${seconds} seconds`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (s === 0) return m === 1 ? "1 minute" : `${m} minutes`;
  return m === 1 ? `1 minute ${s} seconds` : `${m} minutes ${s} seconds`;
}

/** 获取消息的有效 role（turn_start/turn_done 无 role） */
function getMsgRole(msg: ChatMessage): string | undefined {
  return "role" in msg ? msg.role : undefined;
}

// --- Props ---

interface Props {
  messages: ChatMessage[];
  rpc: WorkspaceRpc | null;
  agentDisplay: AgentDisplay;
  onSessionLink?: (sessionId: string) => void;
  scrollToBottomSignal?: number;
}

const AT_BOTTOM_THRESHOLD = 150;

export default function MessageList({ messages, rpc, agentDisplay, onSessionLink, scrollToBottomSignal }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);
  const [markdownMode, setMarkdownMode] = useState<MarkdownMode>(loadMarkdownMode);
  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
    msgId: string | null;
  } | null>(null);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const isAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < AT_BOTTOM_THRESHOLD;
    atBottomRef.current = isAtBottom;
    setAtBottom(isAtBottom);
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (atBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  useEffect(() => {
    if (scrollToBottomSignal && scrollToBottomSignal > 0) {
      const el = scrollRef.current;
      if (!el) return;
      atBottomRef.current = true;
      setAtBottom(true);
      requestAnimationFrame(() => {
        el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
      });
    }
  }, [scrollToBottomSignal]);

  const findMessage = useCallback(
    (id: string) => messages.find((m) => m.id === id) ?? null,
    [messages],
  );

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    let el = e.target as HTMLElement | null;
    while (el && !el.hasAttribute("data-msg-id")) {
      if (el === e.currentTarget) { el = null; break; }
      el = el.parentElement;
    }
    const msgId = el?.getAttribute("data-msg-id") ?? null;
    setContextMenu({ position: { x: e.clientX, y: e.clientY }, msgId });
  }, []);

  const menuContext = (() => {
    if (!contextMenu) return {};
    const msg = contextMenu.msgId ? findMessage(contextMenu.msgId) : null;
    return {
      message_role: getMsgRole(msg!) ?? "",
      message_type: msg?.type ?? "",
      markdown_mode: markdownMode,
    };
  })();

  const handleClientAction = useCallback(
    (action: string, _data: Record<string, unknown>) => {
      if (action === "copy_selection") {
        const selection = window.getSelection()?.toString();
        if (selection) navigator.clipboard.writeText(selection);
      } else if (action === "select_all") {
        if (listRef.current) {
          const range = document.createRange();
          range.selectNodeContents(listRef.current);
          const sel = window.getSelection();
          sel?.removeAllRanges();
          sel?.addRange(range);
        }
      } else if (action === "copy_markdown") {
        const msgId = contextMenu?.msgId;
        if (msgId) {
          const msg = findMessage(msgId);
          if (msg && "content" in msg) {
            navigator.clipboard.writeText(msg.content);
          }
        }
      } else if (action === "toggle_markdown_mode") {
        setMarkdownMode((prev) => {
          const next: MarkdownMode = prev === "rendered" ? "source" : "rendered";
          try { localStorage.setItem(MARKDOWN_MODE_KEY, next); } catch { /* ignore */ }
          return next;
        });
      }
    },
    [contextMenu, findMessage],
  );

  const scrollToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, []);

  return (
    <div className="message-list" onContextMenu={handleContextMenu}>
      <div className="message-list-scroller" ref={scrollRef} onScroll={handleScroll}>
        <div ref={listRef}>
          {messages.map((msg, idx) => {
            // turn_start: 不渲染
            if (msg.type === "turn_start") return null;

            // turn_done: durationSeconds >= 60 时显示为行内文字
            if (msg.type === "turn_done") {
              if (msg.durationSeconds < 60) return null;
              return (
                <div key={msg.id} className="message-row assistant turn-done-row">
                  <div className="avatar-col" />
                  <div className="content-col">
                    <span className="turn-done-text">
                      Worked for {formatTurnDuration(msg.durationSeconds)}
                    </span>
                  </div>
                </div>
              );
            }

            const role = getMsgRole(msg)!;

            // 连续同角色合并：仅首条显示头像和名称（跳过 turn_start/turn_done）
            let showAvatar = true;
            for (let j = idx - 1; j >= 0; j--) {
              const prev = messages[j]!;
              if (prev.type === "turn_start" || prev.type === "turn_done") continue;
              showAvatar = getMsgRole(prev) !== role;
              break;
            }

            // Agent 名称：优先使用消息自身的 model 字段
            const msgModel = "model" in msg ? msg.model : undefined;
            const agentName = (role === "assistant" && msgModel) ? msgModel : agentDisplay.name;

            return (
              <div key={msg.id} className={`message-row ${role}${showAvatar ? "" : " continuation"}`}>
                {role === "assistant" ? (
                  <>
                    <div className="avatar-col">
                      {showAvatar && <Avatar name={agentName} avatar={agentDisplay.avatar} />}
                    </div>
                    <div className="content-col">
                      {showAvatar && <div className="message-sender">{agentName}</div>}
                      <div className="bubble-wrap">
                        {renderBubble(msg, markdownMode, onSessionLink)}
                        {renderMeta(msg)}
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                    <div className="content-col">
                      <div className="bubble-wrap">
                        {renderBubble(msg, markdownMode, onSessionLink)}
                        {renderMeta(msg)}
                      </div>
                    </div>
                    <div className="avatar-col">
                      {showAvatar && <Avatar name="User" />}
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </div>
      </div>
      {!atBottom && (
        <button
          className="scroll-to-bottom"
          onClick={scrollToBottom}
          title="Scroll to bottom"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      )}
      {contextMenu && (
        <RpcMenu
          rpc={rpc}
          category="MessageList/Context"
          context={menuContext}
          position={contextMenu.position}
          onClose={() => setContextMenu(null)}
          onClientAction={handleClientAction}
        />
      )}
    </div>
  );
}

/** 渲染消息气泡内容（不含 meta） */
function renderBubble(
  msg: ChatMessage,
  markdownMode: MarkdownMode,
  onSessionLink?: (sessionId: string) => void,
) {
  switch (msg.type) {
    case "text":
      return (
        <div className={`message-bubble ${msg.role} text`} data-msg-id={msg.id}>
          {msg.role === "assistant" ? (
            markdownMode === "source" ? (
              <CodeBlock code={msg.content} lang="markdown" />
            ) : (
              <Markdown content={msg.content} onSessionLink={onSessionLink} />
            )
          ) : (
            <div className="message-content">
              <pre>{msg.content}</pre>
            </div>
          )}
        </div>
      );

    case "tool_group":
      return (
        <div className="message-bubble assistant tool-group" data-msg-id={msg.id}>
          <ToolCallCard data={msg.data} />
        </div>
      );

    case "error":
      return (
        <div className="message-bubble assistant error" data-msg-id={msg.id}>
          <div className="tool-label">Error</div>
          <div className="message-content">
            <pre>{msg.content}</pre>
          </div>
        </div>
      );
  }
}

/** 渲染时间 meta（响应式，与气泡同行或换行） */
function renderMeta(msg: ChatMessage) {
  // turn_start / turn_done 无 meta
  if (msg.type === "turn_start" || msg.type === "turn_done") return null;

  // User text: 时间
  if (msg.type === "text" && msg.role === "user" && msg.timestamp) {
    return <span className="message-meta">{formatMessageTime(msg.timestamp)}</span>;
  }

  // Assistant text: durationMs >= 10s 时显示 ✻ 耗时 · 时间
  if (msg.type === "text" && msg.role === "assistant" && msg.timestamp) {
    const timeStr = formatMessageTime(msg.timestamp);
    if (msg.durationMs != null && msg.durationMs >= 10000) {
      return (
        <span className="message-meta">
          ✻ {formatDurationMs(msg.durationMs)} · {timeStr}
        </span>
      );
    }
    return <span className="message-meta">{timeStr}</span>;
  }

  // Tool group: 已完成 → 耗时 · 时间，执行中 → 时间
  if (msg.type === "tool_group" && msg.timestamp) {
    const timeStr = formatMessageTime(msg.timestamp);
    if (msg.durationMs != null) {
      return (
        <span className="message-meta">
          {formatDurationMs(msg.durationMs)} · {timeStr}
        </span>
      );
    }
    return <span className="message-meta">{timeStr}</span>;
  }

  // Error: 时间
  if (msg.type === "error" && msg.timestamp) {
    return <span className="message-meta">{formatMessageTime(msg.timestamp)}</span>;
  }

  return null;
}
