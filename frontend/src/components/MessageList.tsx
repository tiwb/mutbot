import { forwardRef, useCallback, useEffect, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import Markdown from "./Markdown";
import ToolCallCard, { type ToolGroupData } from "./ToolCallCard";
import RpcMenu from "./RpcMenu";
import CodeBlock from "./CodeBlock";

export type ChatMessage =
  | { id: string; role: "user"; type: "text"; content: string }
  | { id: string; role: "assistant"; type: "text"; content: string }
  | { id: string; role: "assistant"; type: "tool_group"; data: ToolGroupData }
  | { id: string; role: "assistant"; type: "error"; content: string };

type MarkdownMode = "rendered" | "source";

const MARKDOWN_MODE_KEY = "mutbot-markdown-display-mode";

function loadMarkdownMode(): MarkdownMode {
  try {
    const v = localStorage.getItem(MARKDOWN_MODE_KEY);
    if (v === "source") return "source";
  } catch { /* ignore */ }
  return "rendered";
}

interface Props {
  messages: ChatMessage[];
  rpc: WorkspaceRpc | null;
  onSessionLink?: (sessionId: string) => void;
  scrollToBottomSignal?: number;
}

/** Virtuoso scroller 容器，添加 className 以便 CSS 定位滚动条样式 */
const VirtuosoScroller = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  function VirtuosoScroller(props, ref) {
    return <div {...props} ref={ref} className={`virtuoso-scroller ${props.className || ""}`} />;
  },
);

export default function MessageList({ messages, rpc, onSessionLink, scrollToBottomSignal }: Props) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [markdownMode, setMarkdownMode] = useState<MarkdownMode>(loadMarkdownMode);
  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
    msgId: string | null;
  } | null>(null);

  // Scroll to bottom when send signal changes
  useEffect(() => {
    if (scrollToBottomSignal && scrollToBottomSignal > 0) {
      setAtBottom(true);
      requestAnimationFrame(() => {
        virtuosoRef.current?.scrollToIndex({ index: "LAST", align: "end", behavior: "smooth" });
      });
    }
  }, [scrollToBottomSignal]);

  // 从 messages 数组中查找消息
  const findMessage = useCallback(
    (id: string) => messages.find((m) => m.id === id) ?? null,
    [messages],
  );

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    // 从 target 向上查找 .message[data-msg-id]
    let el = e.target as HTMLElement | null;
    while (el && !el.hasAttribute("data-msg-id")) {
      if (el === e.currentTarget) { el = null; break; }
      el = el.parentElement;
    }
    const msgId = el?.getAttribute("data-msg-id") ?? null;
    setContextMenu({ position: { x: e.clientX, y: e.clientY }, msgId });
  }, []);

  // 构建 RpcMenu context
  const menuContext = (() => {
    if (!contextMenu) return {};
    const msg = contextMenu.msgId ? findMessage(contextMenu.msgId) : null;
    return {
      message_role: msg?.role ?? "",
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

  return (
    <div className="message-list" ref={listRef} onContextMenu={handleContextMenu}>
      <Virtuoso
        ref={virtuosoRef}
        data={messages}
        initialTopMostItemIndex={messages.length > 0 ? messages.length - 1 : 0}
        followOutput={(isAtBottom: boolean) => (isAtBottom ? "smooth" : false)}
        atBottomThreshold={150}
        atBottomStateChange={setAtBottom}
        itemContent={(_index, msg) => renderMessage(msg, markdownMode, onSessionLink)}
        components={{
          Scroller: VirtuosoScroller,
        }}
        style={{ height: "100%", width: "100%" }}
      />
      {!atBottom && (
        <button
          className="scroll-to-bottom"
          onClick={() => virtuosoRef.current?.scrollToIndex({ index: "LAST", align: "end", behavior: "smooth" })}
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

function renderMessage(
  msg: ChatMessage,
  markdownMode: MarkdownMode,
  onSessionLink?: (sessionId: string) => void,
) {
  switch (msg.type) {
    case "text":
      return (
        <div className={`message ${msg.role} text`} data-msg-id={msg.id}>
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
        <div className="message assistant tool-group" data-msg-id={msg.id}>
          <ToolCallCard data={msg.data} />
        </div>
      );

    case "error":
      return (
        <div className="message assistant error" data-msg-id={msg.id}>
          <div className="tool-label">Error</div>
          <div className="message-content">
            <pre>{msg.content}</pre>
          </div>
        </div>
      );
  }
}
