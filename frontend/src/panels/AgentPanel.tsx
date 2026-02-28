import { useCallback, useEffect, useRef, useState } from "react";
import { ReconnectingWebSocket } from "../lib/websocket";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import { rlog, setLogSocket } from "../lib/remote-log";
import MessageList, { type ChatMessage, type AgentDisplay } from "../components/MessageList";
import ChatInput from "../components/ChatInput";
import AgentStatusBar from "../components/AgentStatusBar";
import ModelSelector from "../components/ModelSelector";
import type { ToolGroupData } from "../components/ToolCallCard";

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
      // 回填最后一条 assistant text 消息的时间元数据
      const ts = data.timestamp as string | undefined;
      const dur = data.duration_seconds as number | undefined;
      const model = data.model as string | undefined;
      if (ts) {
        setMessages((prev) => {
          for (let i = prev.length - 1; i >= 0; i--) {
            const m = prev[i]!;
            if (m.role === "assistant" && m.type === "text") {
              const updated = [...prev];
              updated[i] = { id: m.id, role: "assistant" as const, type: "text" as const, content: m.content, timestamp: ts, durationSeconds: dur, model };
              return updated;
            }
            if (m.role === "user") break;
          }
          return prev;
        });
      }
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
      const timestamp = data.timestamp as string | undefined;
      const turnId = data.turn_id as string | undefined;
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "user" as const,
          type: "text" as const,
          content: text,
          timestamp,
          turnId,
        },
      ]);
    } else if (eventType === "agent_status") {
      const status = data.status as AgentStatus;
      setAgentStatus(status);
    } else if (eventType === "agent_cancelled") {
      setAgentStatus("idle");
      pendingTextRef.current = "";
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
              rpc.call<{
                session_id: string;
                messages: { role: string; content: string; tool_calls?: { id: string; name: string; arguments: Record<string, unknown> }[]; tool_results?: { tool_call_id: string; content: string; is_error: boolean }[] }[];
                total_tokens: number;
                context_used: number;
                context_window: number;
                turn_timestamps?: { user_timestamps: string[]; agent_timestamp: string; duration_seconds: number; model?: string }[];
                agent_display?: { name: string; avatar?: string };
              }>("session.messages", { session_id: sessionId }).then((result) => {
                if (result.agent_display) {
                  setAgentDisplayBase(result.agent_display);
                }
                if (result.messages && result.messages.length > 0) {
                  if (DEBUG) rlog.debug("Restoring", result.messages.length, "messages from history");
                  const restored: ChatMessage[] = [];
                  const toolResultMap = new Map<string, { content: string; isError: boolean }>();
                  for (const msg of result.messages) {
                    if (msg.role === "user" && msg.tool_results) {
                      for (const tr of msg.tool_results) {
                        toolResultMap.set(tr.tool_call_id, { content: tr.content, isError: tr.is_error });
                      }
                    }
                  }

                  // 按 turn 分配时间戳：每轮 turn 的用户消息和 agent 响应
                  const turns = result.turn_timestamps ?? [];
                  let turnIdx = 0;
                  let userMsgInTurn = 0;

                  for (const msg of result.messages) {
                    if (msg.role === "user" && !msg.tool_results) {
                      const turn = turns[turnIdx];
                      const ts = turn?.user_timestamps?.[userMsgInTurn];
                      restored.push({
                        id: crypto.randomUUID(),
                        role: "user",
                        type: "text",
                        content: msg.content,
                        timestamp: ts,
                      });
                      userMsgInTurn++;
                      // 当前 turn 的用户消息已全部匹配，推进到下一个 turn
                      if (turn && userMsgInTurn >= turn.user_timestamps.length) {
                        turnIdx++;
                        userMsgInTurn = 0;
                      }
                    } else if (msg.role === "assistant") {
                      if (msg.content) {
                        restored.push({
                          id: crypto.randomUUID(),
                          role: "assistant",
                          type: "text",
                          content: msg.content,
                        });
                      }
                      if (msg.tool_calls) {
                        for (const tc of msg.tool_calls) {
                          const result = toolResultMap.get(tc.id);
                          const toolData: ToolGroupData = {
                            toolCallId: tc.id,
                            toolName: tc.name,
                            arguments: tc.arguments,
                            startTime: 0,
                            endTime: 0,
                            ...(result ? { result: result.content, isError: result.isError } : {}),
                          };
                          restored.push({
                            id: crypto.randomUUID(),
                            role: "assistant",
                            type: "tool_group",
                            data: toolData,
                          });
                        }
                      }
                      // 如果这是该 turn 最后的 assistant 消息（下一条是 user 或结束）
                      // 回填 timestamp 和 duration
                    } else if (msg.role === "user" && msg.tool_results) {
                      // tool_results 消息不显示，跳过
                    }
                  }

                  // 回填 agent 时间戳：对每个 turn，找最后一条 assistant text 消息
                  if (turns.length > 0) {
                    let ti = 0;
                    let userCount = 0;
                    for (let i = 0; i < restored.length; i++) {
                      const m = restored[i]!;
                      if (m.role === "user" && m.type === "text") {
                        userCount++;
                        const turn = turns[ti];
                        if (turn && userCount >= turn.user_timestamps.length) {
                          let lastAssistant = -1;
                          for (let j = i + 1; j < restored.length; j++) {
                            const rj = restored[j]!;
                            if (rj.role === "user") break;
                            if (rj.role === "assistant" && rj.type === "text") {
                              lastAssistant = j;
                            }
                          }
                          if (lastAssistant >= 0) {
                            const am = restored[lastAssistant]!;
                            if (am.role === "assistant" && am.type === "text") {
                              restored[lastAssistant] = {
                                id: am.id, role: "assistant" as const, type: "text" as const,
                                content: am.content,
                                timestamp: turn.agent_timestamp,
                                durationSeconds: turn.duration_seconds,
                                model: turn.model,
                              };
                            }
                          }
                          ti++;
                          userCount = 0;
                        }
                      }
                    }
                  }

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
        {tokenUsage && <TokenUsageDisplay usage={tokenUsage} />}
        {DEBUG && <span style={{ marginLeft: "auto", opacity: 0.5, fontSize: "0.8em" }}>msgs: {messages.length}</span>}
      </div>
      <MessageList messages={messages} rpc={rpc ?? null} agentDisplay={agentDisplay} onSessionLink={onSessionLink} scrollToBottomSignal={scrollSignal} />
      <AgentStatusBar isBusy={agentStatus !== "idle"} />
      <ChatInput onSend={handleSend} onCancel={handleCancel} disabled={!connected} isBusy={agentStatus !== "idle"} />
    </div>
  );
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
