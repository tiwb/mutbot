import { forwardRef, useCallback, useRef, useState } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import Markdown from "./Markdown";
import ToolCallCard, { type ToolGroupData } from "./ToolCallCard";
import ContextMenu, { type ContextMenuItem } from "./ContextMenu";

export type ChatMessage =
  | { id: string; role: "user"; type: "text"; content: string }
  | { id: string; role: "assistant"; type: "text"; content: string }
  | { id: string; role: "assistant"; type: "tool_group"; data: ToolGroupData }
  | { id: string; role: "assistant"; type: "error"; content: string };

interface Props {
  messages: ChatMessage[];
  onSessionLink?: (sessionId: string) => void;
  agentStatus?: "idle" | "thinking" | "tool_calling";
  toolName?: string;
}

/** Virtuoso scroller 容器，添加 className 以便 CSS 定位滚动条样式 */
const VirtuosoScroller = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  function VirtuosoScroller(props, ref) {
    return <div {...props} ref={ref} className={`virtuoso-scroller ${props.className || ""}`} />;
  },
);

export default function MessageList({ messages, onSessionLink, agentStatus, toolName }: Props) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
  } | null>(null);

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setContextMenu({ position: { x: e.clientX, y: e.clientY } });
  }, []);

  const menuItems: ContextMenuItem[] = [
    {
      label: "Copy",
      shortcut: "Ctrl+C",
      onClick: () => {
        const selection = window.getSelection()?.toString();
        if (selection) {
          navigator.clipboard.writeText(selection);
        }
      },
    },
    {
      label: "Select All",
      shortcut: "Ctrl+A",
      onClick: () => {
        if (listRef.current) {
          const range = document.createRange();
          range.selectNodeContents(listRef.current);
          const sel = window.getSelection();
          sel?.removeAllRanges();
          sel?.addRange(range);
        }
      },
    },
  ];

  return (
    <div className="message-list" ref={listRef} onContextMenu={handleContextMenu}>
      <Virtuoso
        ref={virtuosoRef}
        data={messages}
        initialTopMostItemIndex={messages.length > 0 ? messages.length - 1 : 0}
        followOutput="smooth"
        atBottomThreshold={150}
        atBottomStateChange={setAtBottom}
        itemContent={(_index, msg) => renderMessage(msg, onSessionLink)}
        components={{
          Scroller: VirtuosoScroller,
          Footer: () =>
            agentStatus === "thinking" ? (
              <AgentStatusIndicator status={agentStatus} toolName={toolName} />
            ) : null,
        }}
        style={{ height: "100%", width: "100%" }}
      />
      {!atBottom && (
        <button
          className="scroll-to-bottom"
          onClick={() => virtuosoRef.current?.scrollToIndex({ index: "LAST", align: "end", behavior: "smooth" })}
          title="滚动到底部"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      )}
      {contextMenu && (
        <ContextMenu
          items={menuItems}
          position={contextMenu.position}
          onClose={() => setContextMenu(null)}
        />
      )}
    </div>
  );
}

function renderMessage(msg: ChatMessage, onSessionLink?: (sessionId: string) => void) {
  switch (msg.type) {
    case "text":
      return (
        <div className={`message ${msg.role} text`}>
          {msg.role === "assistant" ? (
            <Markdown content={msg.content} onSessionLink={onSessionLink} />
          ) : (
            <div className="message-content">
              <pre>{msg.content}</pre>
            </div>
          )}
        </div>
      );

    case "tool_group":
      return (
        <div className="message assistant tool-group">
          <ToolCallCard data={msg.data} />
        </div>
      );

    case "error":
      return (
        <div className="message assistant error">
          <div className="tool-label">Error</div>
          <div className="message-content">
            <pre>{msg.content}</pre>
          </div>
        </div>
      );
  }
}

function AgentStatusIndicator(_props: { status: "thinking"; toolName?: string }) {
  return (
    <div className="agent-status-indicator">
      <span className="thinking-dots">
        <span /><span /><span />
        </span>
      <span>思考中...</span>
    </div>
  );
}
