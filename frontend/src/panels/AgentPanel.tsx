import { useCallback, useEffect, useRef, useState } from "react";
import { ReconnectingWebSocket } from "../lib/websocket";
import MessageList, { type ChatMessage } from "../components/MessageList";
import ChatInput from "../components/ChatInput";
import type { ToolGroupData } from "../components/ToolCallCard";

interface Props {
  sessionId: string;
}

export default function AgentPanel({ sessionId }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<ReconnectingWebSocket | null>(null);
  const pendingTextRef = useRef("");
  // Map tool_call_id → message id for matching exec_start with exec_end
  const toolCallMapRef = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    setMessages([]);
    pendingTextRef.current = "";
    toolCallMapRef.current.clear();

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${location.host}/ws/session/${sessionId}`;

    const ws = new ReconnectingWebSocket(
      url,
      (data) => handleEvent(data),
      {
        onOpen: () => setConnected(true),
        onClose: () => setConnected(false),
      },
    );
    wsRef.current = ws;
    return () => ws.close();
  }, [sessionId]);

  function handleEvent(data: Record<string, unknown>) {
    const eventType = data.type as string;

    if (eventType === "text_delta") {
      pendingTextRef.current += data.text as string;
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === "assistant" && last.type === "text") {
          const updated = [...prev];
          updated[updated.length - 1] = {
            ...last,
            content: pendingTextRef.current,
          };
          return updated;
        }
        return [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant" as const,
            type: "text" as const,
            content: pendingTextRef.current,
          },
        ];
      });
    } else if (eventType === "tool_exec_start") {
      const tc = data.tool_call as
        | { id: string; name: string; arguments: Record<string, unknown> }
        | undefined;
      if (tc) {
        const msgId = crypto.randomUUID();
        toolCallMapRef.current.set(tc.id, msgId);
        const toolData: ToolGroupData = {
          toolCallId: tc.id,
          toolName: tc.name,
          arguments: tc.arguments,
          startTime: Date.now(),
        };
        setMessages((prev) => [
          ...prev,
          {
            id: msgId,
            role: "assistant" as const,
            type: "tool_group" as const,
            data: toolData,
          },
        ]);
      }
    } else if (eventType === "tool_exec_end") {
      const tr = data.tool_result as
        | { tool_call_id: string; content: string; is_error: boolean }
        | undefined;
      if (tr) {
        const msgId = toolCallMapRef.current.get(tr.tool_call_id);
        if (msgId) {
          toolCallMapRef.current.delete(tr.tool_call_id);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === msgId && m.type === "tool_group"
                ? {
                    ...m,
                    data: {
                      ...m.data,
                      result: tr.content,
                      isError: tr.is_error,
                      endTime: Date.now(),
                    },
                  }
                : m,
            ),
          );
        }
      }
    } else if (eventType === "response_done") {
      // Reset pending text between LLM responses (e.g. between tool call rounds)
      pendingTextRef.current = "";
    } else if (eventType === "turn_done") {
      pendingTextRef.current = "";
    } else if (eventType === "error") {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant" as const,
          type: "error" as const,
          content: data.error as string,
        },
      ]);
    } else if (eventType === "present") {
      const content = data.content as
        | { type: string; body: string; source?: string; metadata?: Record<string, unknown> }
        | undefined;
      if (content?.body) {
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant" as const,
            type: "text" as const,
            content: content.body,
          },
        ]);
      }
    }
  }

  const handleSend = useCallback((text: string) => {
    if (!text.trim()) return;
    setMessages((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        role: "user" as const,
        type: "text" as const,
        content: text,
      },
    ]);
    pendingTextRef.current = "";
    wsRef.current?.send({ type: "message", text });
  }, []);

  return (
    <div className="agent-panel">
      <div className="agent-header">
        <span className={`status-dot ${connected ? "connected" : ""}`} />
        <span>Session {sessionId.slice(0, 8)}</span>
      </div>
      <MessageList messages={messages} />
      <ChatInput onSend={handleSend} disabled={!connected} />
    </div>
  );
}
