import { useEffect, useRef } from "react";
import Markdown from "./Markdown";
import ToolCallCard, { type ToolGroupData } from "./ToolCallCard";

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

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="message-list">
      {messages.map((msg) => renderMessage(msg))}
      <div ref={endRef} />
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
