/**
 * Workspace 级 WebSocket RPC 客户端（可靠传输 + Channel 多路复用）。
 *
 * 通过 /ws/workspace/{workspaceId} 与后端通信，提供：
 * - call(method, params) → Promise<result>  —— 请求/响应式 RPC
 * - on(event, handler)   → unsubscribe       —— 服务端事件监听
 * - openChannel / closeChannel               —— Channel 多路复用
 * - sendToChannel / sendBinaryToChannel      —— Channel 消息发送
 * - onChannel / onBinaryChannel              —— Channel 消息回调
 *
 * 可靠传输：隐式消息计数 + ACK + SendBuffer，支持断线重连后消息重发。
 */

import { getWsUrl } from "./connection";

/** RPC 调用默认超时 (ms) */
const DEFAULT_TIMEOUT = 30_000;

/** ACK 间隔 (ms) */
const ACK_INTERVAL = 5_000;

/** 高吞吐时每 N 条消息发一次 ACK */
const ACK_BATCH = 100;

/** 重连最大重试次数 */
const MAX_RETRIES = 10;

/** SendBuffer 最大条目数 */
const SEND_BUFFER_MAX = 1000;

// ---------------------------------------------------------------------------
// varint (LEB128) 编解码
// ---------------------------------------------------------------------------

function encodeVarint(n: number): Uint8Array {
  if (n === 0) return new Uint8Array([0]);
  const parts: number[] = [];
  while (n > 0) {
    let byte = n & 0x7f;
    n >>>= 7;
    if (n > 0) byte |= 0x80;
    parts.push(byte);
  }
  return new Uint8Array(parts);
}

function decodeVarint(data: Uint8Array, offset = 0): [number, number] {
  let result = 0;
  let shift = 0;
  let pos = offset;
  while (pos < data.length) {
    const byte = data[pos]!;
    result |= (byte & 0x7f) << shift;
    pos++;
    if (!(byte & 0x80)) return [result, pos - offset];
    shift += 7;
  }
  throw new Error("truncated varint");
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** 等待中的 RPC 请求 */
interface PendingCall {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

/** RPC 错误 */
export class RpcError extends Error {
  constructor(
    public code: number,
    message: string,
  ) {
    super(message);
    this.name = "RpcError";
  }
}

type EventHandler = (data: Record<string, unknown>) => void;
type BinaryHandler = (data: Uint8Array) => void;
type ChannelClosedHandler = (ch: number, reason: string) => void;

/** SendBuffer 条目：JSON 或 Binary */
type BufferEntry =
  | { type: "json"; data: Record<string, unknown> }
  | { type: "binary"; data: ArrayBuffer };

// ---------------------------------------------------------------------------
// WorkspaceRpc
// ---------------------------------------------------------------------------

export class WorkspaceRpc {
  private ws: WebSocket | null = null;
  private baseUrl: string;
  private tokenFn?: () => string | null;
  private onOpenCb?: () => void;
  private onCloseCb?: () => void;

  // RPC
  private pending = new Map<string, PendingCall>();
  private eventHandlers = new Map<string, Set<EventHandler>>();
  private nextId = 1;

  // 可靠传输
  private clientId = crypto.randomUUID();
  private recvCount = 0;
  private recvSinceLastAck = 0;
  private sendBuffer: BufferEntry[] = [];
  private totalSent = 0;
  private peerAck = 0;
  private ackTimer: ReturnType<typeof setInterval> | null = null;

  // 重连
  private retryCount = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;

  // Channel 多路复用
  private channelJsonHandlers = new Map<number, EventHandler>();
  private channelBinaryHandlers = new Map<number, BinaryHandler>();
  private channelClosedHandlers = new Set<ChannelClosedHandler>();

  constructor(
    workspaceId: string,
    opts?: {
      tokenFn?: () => string | null;
      onOpen?: () => void;
      onClose?: () => void;
    },
  ) {
    this.baseUrl = getWsUrl(`/ws/workspace/${workspaceId}`);
    this.tokenFn = opts?.tokenFn;
    this.onOpenCb = opts?.onOpen;
    this.onCloseCb = opts?.onClose;
    this.connect();
  }

  // -----------------------------------------------------------------------
  // 连接管理
  // -----------------------------------------------------------------------

  private buildUrl(): string {
    let url = this.baseUrl;
    const sep = url.includes("?") ? "&" : "?";
    url += `${sep}client_id=${encodeURIComponent(this.clientId)}`;
    if (this.totalSent > 0 || this.recvCount > 0) {
      url += `&last_seq=${this.recvCount}`;
    }
    if (this.tokenFn) {
      const token = this.tokenFn();
      if (token) url += `&token=${encodeURIComponent(token)}`;
    }
    return url;
  }

  private connect() {
    if (this.closed) return;
    const url = this.buildUrl();
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      // 不在这里调用 onOpen — 等 welcome 消息确认后再通知
    };

    ws.onmessage = (evt) => {
      if (typeof evt.data === "string") {
        this.handleTextFrame(evt.data);
      } else {
        this.handleBinaryFrame(evt.data as ArrayBuffer);
      }
    };

    ws.onclose = () => {
      this.stopAckTimer();
      this.onCloseCb?.();
      if (!this.closed && this.retryCount < MAX_RETRIES) {
        const delay = Math.min(1000 * 2 ** this.retryCount, 30000);
        this.retryCount++;
        this.retryTimer = setTimeout(() => this.connect(), delay);
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }

  // -----------------------------------------------------------------------
  // RPC
  // -----------------------------------------------------------------------

  call<T = unknown>(
    method: string,
    params: Record<string, unknown> = {},
    timeout = DEFAULT_TIMEOUT,
  ): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const id = String(this.nextId++);

      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new RpcError(-2, `RPC timeout: ${method} (${timeout}ms)`));
      }, timeout);

      this.pending.set(id, {
        resolve: resolve as (result: unknown) => void,
        reject,
        timer,
      });

      this.sendJson({ type: "rpc", id, method, params });
    });
  }

  /** 监听服务端推送事件。 */
  on(event: string, handler: EventHandler): () => void {
    let handlers = this.eventHandlers.get(event);
    if (!handlers) {
      handlers = new Set();
      this.eventHandlers.set(event, handlers);
    }
    handlers.add(handler);
    return () => handlers!.delete(handler);
  }

  /** 关闭连接 */
  close() {
    this.closed = true;
    this.stopAckTimer();
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.ws?.close();
  }

  // -----------------------------------------------------------------------
  // Channel API
  // -----------------------------------------------------------------------

  /** 打开 Channel，返回 channel ID。 */
  async openChannel(
    target: string,
    params: Record<string, unknown> = {},
  ): Promise<number> {
    const result = await this.call<{ ch: number }>("channel.open", {
      target,
      ...params,
    });
    return result.ch;
  }

  /** 关闭 Channel。 */
  async closeChannel(ch: number): Promise<void> {
    this.channelJsonHandlers.delete(ch);
    this.channelBinaryHandlers.delete(ch);
    await this.call("channel.close", { ch });
  }

  /** 向 Channel 发送 JSON 消息（自动注入 ch 字段）。 */
  sendToChannel(ch: number, data: Record<string, unknown>) {
    this.sendJson({ ch, ...data });
  }

  /** 向 Channel 发送 Binary 消息（自动添加 varint 前缀）。 */
  sendBinaryToChannel(ch: number, data: ArrayBuffer | Uint8Array) {
    const prefix = encodeVarint(ch);
    const payload = data instanceof ArrayBuffer ? new Uint8Array(data) : data;
    const frame = new Uint8Array(prefix.length + payload.length);
    frame.set(prefix, 0);
    frame.set(payload, prefix.length);
    this.sendBinary(frame.buffer);
  }

  /** 注册 Channel JSON 消息回调。 */
  onChannel(ch: number, handler: EventHandler): () => void {
    this.channelJsonHandlers.set(ch, handler);
    return () => this.channelJsonHandlers.delete(ch);
  }

  /** 注册 Channel Binary 消息回调。 */
  onBinaryChannel(ch: number, handler: BinaryHandler): () => void {
    this.channelBinaryHandlers.set(ch, handler);
    return () => this.channelBinaryHandlers.delete(ch);
  }

  /** 监听 channel.closed 被动关闭事件。 */
  onChannelClosed(handler: ChannelClosedHandler): () => void {
    this.channelClosedHandlers.add(handler);
    return () => this.channelClosedHandlers.delete(handler);
  }

  // -----------------------------------------------------------------------
  // 发送（经过 SendBuffer）
  // -----------------------------------------------------------------------

  private sendJson(data: Record<string, unknown>) {
    const entry: BufferEntry = { type: "json", data };
    this.appendToSendBuffer(entry);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  private sendBinary(data: ArrayBuffer) {
    const entry: BufferEntry = { type: "binary", data };
    this.appendToSendBuffer(entry);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(data);
    }
  }

  private appendToSendBuffer(entry: BufferEntry) {
    if (this.sendBuffer.length >= SEND_BUFFER_MAX) {
      // 溢出：丢弃最旧的已确认消息之后、继续追加
      // 实际场景中不应发生；如溢出则截断
      this.sendBuffer.splice(0, this.sendBuffer.length - SEND_BUFFER_MAX + 1);
    }
    this.sendBuffer.push(entry);
    this.totalSent++;
  }

  // -----------------------------------------------------------------------
  // ACK
  // -----------------------------------------------------------------------

  private sendAck() {
    this.recvSinceLastAck = 0;
    if (this.ws?.readyState === WebSocket.OPEN) {
      // ACK 是控制消息，不进入 sendBuffer
      this.ws.send(JSON.stringify({ type: "ack", ack: this.recvCount }));
    }
  }

  private startAckTimer() {
    this.stopAckTimer();
    this.ackTimer = setInterval(() => this.sendAck(), ACK_INTERVAL);
  }

  private stopAckTimer() {
    if (this.ackTimer) {
      clearInterval(this.ackTimer);
      this.ackTimer = null;
    }
  }

  private onContentReceived() {
    this.recvCount++;
    this.recvSinceLastAck++;
    if (this.recvSinceLastAck >= ACK_BATCH) {
      this.sendAck();
    }
  }

  // -----------------------------------------------------------------------
  // 收到 server ACK → 清理 SendBuffer
  // -----------------------------------------------------------------------

  private onPeerAck(n: number) {
    if (n < this.peerAck || n > this.totalSent) return;
    const discard = n - this.peerAck;
    this.sendBuffer.splice(0, discard);
    this.peerAck = n;
  }

  // -----------------------------------------------------------------------
  // Text Frame 处理
  // -----------------------------------------------------------------------

  private handleTextFrame(raw: string) {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    const type = msg.type as string;

    // --- 控制消息（不计入 recvCount） ---

    if (type === "welcome") {
      this.handleWelcome(msg);
      return;
    }

    if (type === "ack") {
      this.onPeerAck(msg.ack as number);
      return;
    }

    // --- 内容消息（计入 recvCount） ---
    this.onContentReceived();

    // Channel 路由：ch > 0 → 转发到 channel handler
    const ch = msg.ch as number | undefined;
    if (ch !== undefined && ch > 0) {
      const handler = this.channelJsonHandlers.get(ch);
      if (handler) {
        try {
          handler(msg);
        } catch {
          // ignore
        }
      }
      return;
    }

    // Workspace 级消息路由
    if (type === "rpc_result") {
      const pending = this.pending.get(msg.id as string);
      if (pending) {
        this.pending.delete(msg.id as string);
        clearTimeout(pending.timer);
        pending.resolve(msg.result);
      }
      return;
    }

    if (type === "rpc_error") {
      const pending = this.pending.get(msg.id as string);
      if (pending) {
        this.pending.delete(msg.id as string);
        clearTimeout(pending.timer);
        const err = msg.error as { code: number; message: string };
        pending.reject(new RpcError(err.code, err.message));
      }
      return;
    }

    if (type === "event") {
      const eventName = msg.event as string;

      // channel.closed 被动关闭 → 清理本地 channel 状态并通知
      if (eventName === "channel.closed") {
        const closedCh = msg.closed_ch as number;
        const reason = (msg.reason as string) || "unknown";
        this.channelJsonHandlers.delete(closedCh);
        this.channelBinaryHandlers.delete(closedCh);
        for (const handler of this.channelClosedHandlers) {
          try {
            handler(closedCh, reason);
          } catch {
            // ignore
          }
        }
        return;
      }

      const data = (msg.data as Record<string, unknown>) || {};
      const handlers = this.eventHandlers.get(eventName);
      if (handlers) {
        for (const handler of handlers) {
          try {
            handler(data);
          } catch {
            // ignore
          }
        }
      }
    }
  }

  // -----------------------------------------------------------------------
  // Binary Frame 处理
  // -----------------------------------------------------------------------

  private handleBinaryFrame(data: ArrayBuffer) {
    this.onContentReceived();

    const bytes = new Uint8Array(data);
    if (bytes.length === 0) return;

    const [ch, consumed] = decodeVarint(bytes);
    const payload = bytes.subarray(consumed);

    const handler = this.channelBinaryHandlers.get(ch);
    if (handler) {
      try {
        handler(payload);
      } catch {
        // ignore
      }
    }
  }

  // -----------------------------------------------------------------------
  // Welcome 处理（连接建立/重连确认）
  // -----------------------------------------------------------------------

  private handleWelcome(msg: Record<string, unknown>) {
    const resumed = msg.resumed as boolean;
    this.retryCount = 0;

    if (resumed) {
      // 重连成功：replay server 未收到的消息
      const serverLastSeq = msg.last_seq as number;
      this.replayFromBuffer(serverLastSeq);
    } else {
      // 全新连接：重置所有状态
      this.resetState();
    }

    this.startAckTimer();
    this.onOpenCb?.();
  }

  /** Replay sendBuffer 中 server 未收到的消息。 */
  private replayFromBuffer(serverLastSeq: number) {
    const skip = serverLastSeq - this.peerAck;
    if (skip < 0 || skip > this.sendBuffer.length) {
      // 无法恢复 → 完全重置
      this.resetState();
      return;
    }
    // 清理 server 已收到的部分
    this.sendBuffer.splice(0, skip);
    this.peerAck = serverLastSeq;

    // 重发 server 未收到的消息
    for (const entry of this.sendBuffer) {
      if (this.ws?.readyState === WebSocket.OPEN) {
        if (entry.type === "json") {
          this.ws.send(JSON.stringify(entry.data));
        } else {
          this.ws.send(entry.data);
        }
      }
    }
  }

  /** 完全重置（resumed=false 或恢复失败）。 */
  private resetState() {
    this.sendBuffer = [];
    this.totalSent = 0;
    this.peerAck = 0;
    this.recvCount = 0;
    this.recvSinceLastAck = 0;

    // 拒绝所有 pending RPC
    for (const [, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(new RpcError(-1, "WebSocket connection reset"));
    }
    this.pending.clear();

    // 清空所有 channel（面板需要重新 openChannel）
    const closedChannels = [...this.channelJsonHandlers.keys(), ...this.channelBinaryHandlers.keys()];
    this.channelJsonHandlers.clear();
    this.channelBinaryHandlers.clear();
    for (const ch of new Set(closedChannels)) {
      for (const handler of this.channelClosedHandlers) {
        try {
          handler(ch, "connection_reset");
        } catch {
          // ignore
        }
      }
    }
  }
}

// Re-export for convenience
export { encodeVarint, decodeVarint };
