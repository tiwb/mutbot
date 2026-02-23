import { useCallback, useEffect, useRef, useState } from "react";
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
}

export default function MessageList({ messages }: Props) {
  const endRef = useRef<HTMLDivElement>(null);
  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
  } | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

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
        const el = endRef.current?.parentElement;
        if (el) {
          const range = document.createRange();
          range.selectNodeContents(el);
          const sel = window.getSelection();
          sel?.removeAllRanges();
          sel?.addRange(range);
        }
      },
    },
  ];

  return (
    <div className="message-list" onContextMenu={handleContextMenu}>
      {messages.map((msg) => renderMessage(msg))}
      <div ref={endRef} />
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

function renderMessage(msg: ChatMessage) {
  switch (msg.type) {
    case "text":
      return (
        <div key={msg.id} className={`message ${msg.role} text`}>
          {msg.role === "assistant" ? (
            <Markdown content={msg.content} />
          ) : (
            <div className="message-content">
              <pre>{msg.content}</pre>
            </div>
          )}
        </div>
      );

    case "tool_group":
      return (
        <div key={msg.id} className="message assistant tool-group">
          <ToolCallCard data={msg.data} />
        </div>
      );

    case "error":
      return (
        <div key={msg.id} className="message assistant error">
          <div className="tool-label">Error</div>
          <div className="message-content">
            <pre>{msg.content}</pre>
          </div>
        </div>
      );
  }
}
