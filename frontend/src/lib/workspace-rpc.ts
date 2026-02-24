/**
 * Workspace 级 WebSocket RPC 客户端。
 *
 * 通过 /ws/workspace/{workspaceId} 与后端通信，提供：
 * - call(method, params) → Promise<result>  —— 请求/响应式 RPC
 * - on(event, handler)   → unsubscribe       —— 服务端事件监听
 */

import { ReconnectingWebSocket } from "./websocket";

/** RPC 调用默认超时 (ms) */
const DEFAULT_TIMEOUT = 30_000;

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

export class WorkspaceRpc {
  private ws: ReconnectingWebSocket;
  private pending = new Map<string, PendingCall>();
  private eventHandlers = new Map<string, Set<EventHandler>>();
  private nextId = 1;

  constructor(
    workspaceId: string,
    opts?: {
      tokenFn?: () => string | null;
      onOpen?: () => void;
      onClose?: () => void;
    },
  ) {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${location.host}/ws/workspace/${workspaceId}`;

    this.ws = new ReconnectingWebSocket(url, (msg) => this.handleMessage(msg), {
      tokenFn: opts?.tokenFn,
      onOpen: opts?.onOpen,
      onClose: () => {
        // Reject all pending calls on disconnect
        for (const [, pending] of this.pending) {
          clearTimeout(pending.timer);
          pending.reject(new RpcError(-1, "WebSocket disconnected"));
        }
        this.pending.clear();
        opts?.onClose?.();
      },
    });
  }

  /**
   * 发起 RPC 调用，返回 Promise。
   *
   * @param method - RPC 方法名，如 "menu.query"
   * @param params - 参数对象
   * @param timeout - 超时时间 (ms)，默认 30s
   */
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

      this.ws.send({ type: "rpc", id, method, params });
    });
  }

  /**
   * 监听服务端推送事件。
   *
   * @returns 取消订阅函数
   */
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
    this.ws.close();
  }

  // -----------------------------------------------------------------------
  // 内部消息处理
  // -----------------------------------------------------------------------

  private handleMessage(msg: Record<string, unknown>) {
    const type = msg.type as string;

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
      const data = (msg.data as Record<string, unknown>) || {};
      const handlers = this.eventHandlers.get(eventName);
      if (handlers) {
        for (const handler of handlers) {
          try {
            handler(data);
          } catch {
            // ignore handler errors
          }
        }
      }
    }
  }
}
