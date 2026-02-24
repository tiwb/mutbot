import { useCallback, useEffect, useRef, useState } from "react";
import { ReconnectingWebSocket } from "../lib/websocket";
import { getAuthToken } from "../lib/api";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import { rlog, setLogSocket } from "../lib/remote-log";
import MessageList, { type ChatMessage } from "../components/MessageList";
import ChatInput from "../components/ChatInput";
import type { ToolGroupData } from "../components/ToolCallCard";

const DEBUG = true;

// Session-level message cache — survives session switching (in-memory only)
const messageCache = new Map<string, ChatMessage[]>();
const pendingTextCache = new Map<string, string>();

interface Props {
  sessionId: string;
  rpc?: WorkspaceRpc | null;
}

export default function AgentPanel({ sessionId, rpc }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>(
    () => messageCache.get(sessionId) ?? [],
  );
  const [connected, setConnected] = useState(false);
  const [connectionCount, setConnectionCount] = useState(0);
  const wsRef = useRef<ReconnectingWebSocket | null>(null);
  const pendingTextRef = useRef(pendingTextCache.get(sessionId) ?? "");
  const messagesRef = useRef<ChatMessage[]>(messages);
  const toolCallMapRef = useRef<Map<string, string>>(new Map());
  const replayedRef = useRef<Set<string>>(new Set());
  const processedEventIds = useRef<Set<string>>(new Set());

  // Keep messagesRef in sync with state (for cleanup to read latest)
  messagesRef.current = messages;

  function handleEvent(data: Record<string, unknown>) {
    const eventType = data.type as string;

    // Dedup by event_id (backend assigns unique IDs to all persisted events)
    const eventId = data.event_id as string | undefined;
    if (eventId) {
      if (processedEventIds.current.has(eventId)) {
        if (DEBUG) rlog.debug("skip duplicate event_id", eventId, eventType);
        return;
      }
      processedEventIds.current.add(eventId);
    }

    if (DEBUG) {
      if (eventType === "text_delta") {
        rlog.debug("evt text_delta", `"${(data.text as string).slice(0, 40)}"`);
      } else if (eventType !== "connection_count") {
        rlog.debug("evt", eventType, JSON.stringify(data).slice(0, 150));
      }
    }

    if (eventType === "text_delta") {
      const text = data.text as string;
      pendingTextRef.current += text;
      const snapshot = pendingTextRef.current;
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === "assistant" && last.type === "text") {
          const updated = [...prev];
          updated[updated.length - 1] = { ...last, content: snapshot };
          return updated;
        }
        return [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant" as const,
            type: "text" as const,
            content: snapshot,
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
      if (DEBUG) rlog.debug("response_done: pendingText was", pendingTextRef.current.length, "chars");
      pendingTextRef.current = "";
    } else if (eventType === "turn_done") {
      pendingTextRef.current = "";
      if (DEBUG) {
        setMessages((prev) => {
          rlog.debug("turn_done: total msgs =", prev.length);
          return prev;
        });
      }
    } else if (eventType === "agent_done") {
      if (DEBUG) rlog.info("agent_done: agent thread finished");
    } else if (eventType === "error") {
      rlog.error("agent error:", data.error);
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
    } else if (eventType === "user_message") {
      const text = data.text as string;
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "user" as const,
          type: "text" as const,
          content: text,
        },
      ]);
    } else if (eventType === "connection_count") {
      setConnectionCount(data.count as number);
    } else {
      if (DEBUG) rlog.debug("unhandled event:", eventType);
    }
  }

  useEffect(() => {
    // Restore from cache (or keep initialState from useState)
    const cached = messageCache.get(sessionId);
    const cachedPending = pendingTextCache.get(sessionId) ?? "";
    if (DEBUG) rlog.debug("init session", sessionId, "cached msgs =", cached?.length ?? 0);
    setMessages(cached ?? []);
    pendingTextRef.current = cachedPending;
    toolCallMapRef.current.clear();
    processedEventIds.current.clear();

    // Track whether this session was already replayed from cache
    const hadCache = !!cached && cached.length > 0;

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${location.host}/ws/session/${sessionId}`;

    const ws = new ReconnectingWebSocket(
      url,
      (data) => handleEvent(data),
      {
        onOpen: () => {
          if (DEBUG) rlog.debug("WS open");
          setConnected(true);

          // If no cached messages, load history from server
          if (!hadCache && !replayedRef.current.has(sessionId)) {
            replayedRef.current.add(sessionId);
            if (rpc) {
              rpc.call<{ session_id: string; events: Record<string, unknown>[] }>("session.events", { session_id: sessionId }).then((result) => {
                if (result.events && result.events.length > 0) {
                  if (DEBUG) rlog.debug("Replaying", result.events.length, "events from history");
                  for (const event of result.events) {
                    handleEvent(event);
                  }
                }
              }).catch((err) => {
                if (DEBUG) rlog.error("Failed to fetch session events:", String(err));
              });
            }
          }
        },
        onClose: () => {
          if (DEBUG) rlog.debug("WS close");
          setConnected(false);
        },
        tokenFn: getAuthToken,
      },
    );
    wsRef.current = ws;
    setLogSocket(ws, sessionId);

    return () => {
      if (DEBUG) rlog.debug("cleanup session", sessionId, "saving", messagesRef.current.length, "msgs");
      // Save current session state to cache before switching
      messageCache.set(sessionId, messagesRef.current);
      pendingTextCache.set(sessionId, pendingTextRef.current);
      setLogSocket(null);
      ws.close();
    };
  }, [sessionId]);

  const handleSend = useCallback((text: string) => {
    if (!text.trim()) return;
    if (DEBUG) rlog.debug("send:", text.slice(0, 80));
    pendingTextRef.current = "";
    wsRef.current?.send({ type: "message", text });
  }, []);

  return (
    <div className="agent-panel">
      <div className="agent-header">
        <span className={`status-dot ${connected ? "connected" : ""}`} />
        <span>Session {sessionId.slice(0, 8)}</span>
        {connectionCount > 1 && (
          <span
            style={{ marginLeft: 8, opacity: 0.6, fontSize: "0.8em" }}
            title={`${connectionCount} clients connected`}
          >
            ({connectionCount})
          </span>
        )}
        {DEBUG && <span style={{ marginLeft: "auto", opacity: 0.5, fontSize: "0.8em" }}>msgs: {messages.length}</span>}
      </div>
      <MessageList messages={messages} />
      <ChatInput onSend={handleSend} disabled={!connected} />
    </div>
  );
}
