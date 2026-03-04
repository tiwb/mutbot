import { useCallback, useEffect, useRef, useState } from "react";
import { ReconnectingWebSocket } from "../lib/websocket";
import { getWsUrl } from "../lib/connection";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import { rlog, setLogSocket } from "../lib/remote-log";
import MessageList, { type ChatMessage, type AgentDisplay } from "../components/MessageList";
import ChatInput from "../components/ChatInput";
import AgentStatusBar from "../components/AgentStatusBar";
import ModelSelector from "../components/ModelSelector";
import RpcMenu from "../components/RpcMenu";
import type { ToolGroupData, UIEventPayload } from "../components/ToolCallCard";

const DEBUG = false;

// Session-level message cache — survives session switching (in-memory only)
const messageCache = new Map<string, ChatMessage[]>();
const pendingTextCache = new Map<string, string>();

type AgentStatus = "idle" | "thinking" | "tool_calling";

interface TokenUsage {
  contextUsed: number;
  contextWindow: number | null;
  contextPercent: number | null;
  sessionTotalTokens: number;
  model: string;
}

interface Props {
  sessionId: string;
  rpc?: WorkspaceRpc | null;
  onSessionLink?: (sessionId: string) => void;
}

export default function AgentPanel({ sessionId, rpc, onSessionLink }: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>(
    () => messageCache.get(sessionId) ?? [],
  );
  const [connected, setConnected] = useState(false);
  const [connectionCount, setConnectionCount] = useState(0);
  const [agentStatus, setAgentStatus] = useState<AgentStatus>("idle");
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [currentModel, setCurrentModel] = useState("");
  const [agentDisplayBase, setAgentDisplayBase] = useState<AgentDisplay>({ name: "Agent" });
  const [scrollSignal, setScrollSignal] = useState(0);

  // Agent 显示名称优先使用模型名
  const agentDisplay: AgentDisplay = {
    ...agentDisplayBase,
    name: currentModel || agentDisplayBase.name,
  };
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

    if (eventType === "turn_start") {
      const msgId = data.id as string;
      const turnId = data.turn_id as string;
      const timestamp = data.timestamp as string;
      setMessages((prev) => [
        ...prev,
        { id: msgId, type: "turn_start" as const, turnId, timestamp },
      ]);
    } else if (eventType === "user_message") {
      const text = data.text as string;
      const rawTs = data.timestamp as number | undefined;
      const timestamp = rawTs ? new Date(rawTs * 1000).toISOString() : undefined;
      const sender = data.sender as string | undefined;
      const msgId = data.id as string | undefined;
      const model = data.model as string | undefined;
      if (model) setCurrentModel(model);
      setMessages((prev) => [
        ...prev,
        {
          id: msgId ?? crypto.randomUUID(),
          role: "user" as const,
          type: "text" as const,
          content: text,
          timestamp,
          sender,
        },
      ]);
    } else if (eventType === "response_start") {
      // response_start 创建 assistant 消息卡片（替代首个 text_delta 的角色）
      const resp = data.response as { message?: { id?: string; model?: string; timestamp?: number } } | undefined;
      if (resp?.message) {
        const msgId = resp.message.id ?? crypto.randomUUID();
        const model = resp.message.model;
        const timestamp = resp.message.timestamp
          ? new Date(resp.message.timestamp * 1000).toISOString()
          : undefined;
        if (model) setCurrentModel(model);
        pendingTextRef.current = "";
        setMessages((prev) => [
          ...prev,
          {
            id: msgId,
            role: "assistant" as const,
            type: "text" as const,
            content: "",
            timestamp,
            model,
          },
        ]);
      }
    } else if (eventType === "text_delta") {
      const text = data.text as string;
      pendingTextRef.current += text;
      const snapshot = pendingTextRef.current;

      // 始终更新最后一条 assistant text（卡片已由 response_start 创建）
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.type === "text" && last.role === "assistant") {
          const updated = [...prev];
          updated[updated.length - 1] = { ...last, content: snapshot };
          return updated;
        }
        return prev;
      });
    } else if (eventType === "response_done") {
      if (DEBUG) rlog.debug("response_done: pendingText was", pendingTextRef.current.length, "chars");
      pendingTextRef.current = "";
      // 从 response.message.duration（秒）回填 durationMs
      const resp = data.response as { message?: { duration?: number } } | undefined;
      const durationMs = resp?.message?.duration ? Math.round(resp.message.duration * 1000) : undefined;
      if (durationMs != null) {
        setMessages((prev) => {
          for (let i = prev.length - 1; i >= 0; i--) {
            const m = prev[i]!;
            if (m.type === "text" && m.role === "assistant") {
              const updated = [...prev];
              updated[i] = { ...m, durationMs };
              return updated;
            }
            if (m.type === "text" && m.role === "user") break;
          }
          return prev;
        });
      }
    } else if (eventType === "tool_exec_start") {
      const tc = data.tool_call as
        | { id: string; name: string; input: Record<string, unknown> }
        | undefined;
      if (tc) {
        const msgId = crypto.randomUUID();
        toolCallMapRef.current.set(tc.id, msgId);
        const toolData: ToolGroupData = {
          toolCallId: tc.id,
          toolName: tc.name,
          input: tc.input,
        };
        const rawTs = data.timestamp as number | undefined;
        const timestamp = rawTs ? new Date(rawTs * 1000).toISOString() : undefined;
        setMessages((prev) => [
          ...prev,
          {
            id: msgId,
            role: "assistant" as const,
            type: "tool_group" as const,
            data: toolData,
            timestamp,
          },
        ]);
      }
    } else if (eventType === "tool_exec_end") {
      const tc = data.tool_call as
        | { id: string; result: string; is_error: boolean; duration: number }
        | undefined;
      if (tc) {
        const msgId = toolCallMapRef.current.get(tc.id);
        const durationMs = tc.duration ? Math.round(tc.duration * 1000) : undefined;
        if (msgId) {
          toolCallMapRef.current.delete(tc.id);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === msgId && m.type === "tool_group"
                ? {
                    ...m,
                    durationMs,
                    data: {
                      ...m.data,
                      result: tc.result,
                      isError: tc.is_error,
                    },
                  }
                : m,
            ),
          );
        }
      }
    } else if (eventType === "ui_view") {
      // 后端 UIContext 推送视图 → 更新对应 ToolCallCard 的 uiView
      const contextId = data.context_id as string;
      const view = data.view as ToolGroupData["uiView"];
      if (contextId && view) {
        setMessages((prev) =>
          prev.map((m) =>
            m.type === "tool_group" && m.data?.toolCallId === contextId
              ? { ...m, data: { ...m.data, uiView: view } }
              : m,
          ),
        );
      }
    } else if (eventType === "ui_close") {
      // 后端 UIContext 关闭 → 清除 uiView，可选设置 uiFinalView
      const contextId = data.context_id as string;
      const finalView = data.final_view as ToolGroupData["uiFinalView"] | undefined;
      if (contextId) {
        setMessages((prev) =>
          prev.map((m) =>
            m.type === "tool_group" && m.data?.toolCallId === contextId
              ? { ...m, data: { ...m.data, uiView: null, uiFinalView: finalView ?? null } }
              : m,
          ),
        );
      }
    } else if (eventType === "turn_done") {
      pendingTextRef.current = "";
      const msgId = data.id as string;
      const turnId = data.turn_id as string;
      const timestamp = data.timestamp as string;
      const durationSeconds = data.duration_seconds as number;
      setMessages((prev) => [
        ...prev,
        { id: msgId, type: "turn_done" as const, turnId, timestamp, durationSeconds },
      ]);
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
      const msgId = (data.id as string) ?? crypto.randomUUID();
      const timestamp = data.timestamp as string | undefined;
      const model = data.model as string | undefined;
      setMessages((prev) => [
        ...prev,
        {
          id: msgId,
          role: "assistant" as const,
          type: "error" as const,
          content: data.error as string,
          timestamp,
          model,
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
    } else if (eventType === "agent_status") {
      const status = data.status as AgentStatus;
      setAgentStatus(status);
    } else if (eventType === "agent_cancelled") {
      setAgentStatus("idle");
      pendingTextRef.current = "";
      // 标记所有未完成的工具为已取消
      if (toolCallMapRef.current.size > 0) {
        const pending = new Map(toolCallMapRef.current);
        toolCallMapRef.current.clear();
        setMessages((prev) =>
          prev.map((m) => {
            if (m.type === "tool_group" && pending.has(m.data?.toolCallId)) {
              return { ...m, data: { ...m.data!, result: "(cancelled)", isCancelled: true } };
            }
            return m;
          }),
        );
      }
    } else if (eventType === "token_usage") {
      setTokenUsage({
        contextUsed: data.context_used as number,
        contextWindow: data.context_window as number | null,
        contextPercent: data.context_percent as number | null,
        sessionTotalTokens: data.session_total_tokens as number,
        model: data.model as string,
      });
      if (data.model) setCurrentModel(data.model as string);
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
    setAgentStatus("idle");
    setTokenUsage(null);
    setCurrentModel("");

    // Track whether this session was already replayed from cache
    const hadCache = !!cached && cached.length > 0;

    const url = getWsUrl(`/ws/session/${sessionId}`);

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
              rpc.call<{
                session_id: string;
                messages: {
                  role: string;
                  blocks: { type: string; text?: string; id?: string; name?: string;
                    input?: Record<string, unknown>; status?: string; result?: string;
                    is_error?: boolean; duration?: number; turn_id?: string }[];
                  id?: string; timestamp?: number; duration?: number;
                  model?: string; sender?: string;
                  input_tokens?: number; output_tokens?: number;
                }[];
                total_tokens: number;
                context_used: number;
                context_window: number;
                agent_display?: { name: string; avatar?: string };
              }>("session.messages", { session_id: sessionId }).then((result) => {
                if (result.agent_display) {
                  setAgentDisplayBase(result.agent_display);
                }
                if (result.messages && result.messages.length > 0) {
                  if (DEBUG) rlog.debug("Restoring", result.messages.length, "messages from history");
                  const restored = restoreChatMessages(result.messages);
                  setMessages(restored);
                }
                if (result.total_tokens || result.context_used) {
                  const cw = result.context_window || null;
                  const cu = result.context_used || 0;
                  const cp = cw && cu ? Math.round(cu / cw * 1000) / 10 : null;
                  setTokenUsage({
                    contextUsed: cu,
                    contextWindow: cw,
                    contextPercent: cp,
                    sessionTotalTokens: result.total_tokens || 0,
                    model: "",
                  });
                }
              }).catch((err) => {
                if (DEBUG) rlog.error("Failed to fetch session messages:", String(err));
              });
            }
          }
        },
        onClose: () => {
          if (DEBUG) rlog.debug("WS close");
          setConnected(false);
        },
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

  // 加载初始 model 并订阅 session_updated 事件
  useEffect(() => {
    if (!rpc) return;
    rpc.call<{ model?: string }>("session.get", { session_id: sessionId })
      .then((s) => { if (s.model) setCurrentModel(s.model); })
      .catch(() => {});
    const unsub = rpc.on("session_updated", (data) => {
      const s = data as { id?: string; model?: string };
      if (s.id === sessionId && s.model !== undefined) {
        setCurrentModel(s.model);
      }
    });
    return unsub;
  }, [sessionId, rpc]);

  const handleSend = useCallback((text: string) => {
    if (!text.trim()) return;
    if (DEBUG) rlog.debug("send:", text.slice(0, 80));
    pendingTextRef.current = "";
    wsRef.current?.send({ type: "message", text });
    // 乐观更新：立即切 thinking 状态
    setAgentStatus("thinking");
    setScrollSignal((s) => s + 1);
  }, []);

  const handleCancel = useCallback(() => {
    wsRef.current?.send({ type: "cancel" });
  }, []);

  const handleUIEvent = useCallback((toolCallId: string, event: UIEventPayload) => {
    wsRef.current?.send({
      type: "ui_event",
      context_id: toolCallId,
      event_type: event.type,
      data: event.data,
      source: event.source,
    });
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
        {rpc && <ModelSelector sessionId={sessionId} currentModel={currentModel} rpc={rpc} />}
        <div style={{ flex: 1 }} />
        {tokenUsage && <TokenUsageDisplay usage={tokenUsage} />}
        {DEBUG && <span style={{ opacity: 0.5, fontSize: "0.8em" }}>msgs: {messages.length}</span>}
        {rpc && (
          <RpcMenu
            rpc={rpc}
            category="AgentPanel/Header"
            context={{ session_id: sessionId }}
            trigger={<button className="agent-menu-btn" title="Menu">⋮</button>}
            onClientAction={(action) => {
              if (action === "run_setup") {
                rpc.call("session.run_setup", { session_id: sessionId }).catch(() => {});
              }
            }}
          />
        )}
      </div>
      <MessageList messages={messages} rpc={rpc ?? null} agentDisplay={agentDisplay} isStreaming={agentStatus !== "idle"} onSessionLink={onSessionLink} scrollToBottomSignal={scrollSignal} onUIEvent={handleUIEvent} onSetupLLM={() => { rpc?.call("session.run_setup", { session_id: sessionId }).catch(() => {}); }} />
      <AgentStatusBar isBusy={agentStatus !== "idle"} />
      <ChatInput onSend={handleSend} onCancel={handleCancel} disabled={!connected} isBusy={agentStatus !== "idle"} />
    </div>
  );
}

/** 从后端 Message[] blocks 格式展开为前端 flat ChatMessage[] */
function restoreChatMessages(msgs: {
  role: string;
  blocks: { type: string; text?: string; id?: string; name?: string;
    input?: Record<string, unknown>; status?: string; result?: string;
    is_error?: boolean; duration?: number; turn_id?: string }[];
  id?: string; timestamp?: number; duration?: number;
  model?: string; sender?: string;
  input_tokens?: number; output_tokens?: number;
}[]): ChatMessage[] {
  const restored: ChatMessage[] = [];
  for (const msg of msgs) {
    const ts = msg.timestamp ? new Date(msg.timestamp * 1000).toISOString() : undefined;
    const durationMs = msg.duration ? Math.round(msg.duration * 1000) : undefined;

    for (const block of msg.blocks) {
      switch (block.type) {
        case "turn_start":
          restored.push({
            id: msg.id ?? crypto.randomUUID(),
            type: "turn_start",
            turnId: block.turn_id ?? "",
            timestamp: ts ?? "",
          });
          break;
        case "text":
          if (msg.role === "user") {
            restored.push({
              id: msg.id ?? crypto.randomUUID(),
              role: "user",
              type: "text",
              content: block.text ?? "",
              timestamp: ts,
              sender: msg.sender,
            });
          } else if (msg.role === "assistant") {
            restored.push({
              id: msg.id ?? crypto.randomUUID(),
              role: "assistant",
              type: "text",
              content: block.text ?? "",
              timestamp: ts,
              durationMs,
              model: msg.model,
            });
          }
          break;
        case "tool_use":
          restored.push({
            id: crypto.randomUUID(),
            role: "assistant",
            type: "tool_group",
            timestamp: ts,
            durationMs: block.duration ? Math.round(block.duration * 1000) : undefined,
            model: msg.model,
            data: {
              toolCallId: block.id ?? "",
              toolName: block.name ?? "",
              input: block.input ?? {},
              result: block.result,
              isError: block.is_error,
            },
          });
          break;
        case "turn_end":
          restored.push({
            id: crypto.randomUUID(),
            type: "turn_done",
            turnId: block.turn_id ?? "",
            timestamp: ts ?? "",
            durationSeconds: block.duration ?? 0,
          });
          break;
      }
    }
  }
  return restored;
}

function formatTokenCount(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`;
  return `${tokens}`;
}

function TokenUsageDisplay({ usage }: { usage: TokenUsage }) {
  const percentColor =
    usage.contextPercent == null ? undefined
    : usage.contextPercent > 80 ? "var(--error)"
    : usage.contextPercent > 50 ? "var(--warning)"
    : "var(--success)";

  const sizeText = formatTokenCount(usage.contextUsed);

  return (
    <span className="token-usage">
      <span title={`${usage.contextUsed.toLocaleString()} / ${usage.contextWindow?.toLocaleString() ?? "?"} tokens`}>
        Context: {sizeText}
        {usage.contextPercent != null && (
          <span style={percentColor ? { color: percentColor } : undefined}> ({usage.contextPercent}%)</span>
        )}
      </span>
      <span className="token-usage-sep">|</span>
      <span title={`Session total: ${usage.sessionTotalTokens.toLocaleString()} tokens`}>
        Session: {formatTokenCount(usage.sessionTotalTokens)}
      </span>
    </span>
  );
}
