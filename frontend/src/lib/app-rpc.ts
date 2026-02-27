/**
 * App 级 WebSocket RPC 客户端。
 *
 * 通过 /ws/app 与后端通信，提供工作区列表、创建工作区、目录浏览等全局操作。
 * 与 WorkspaceRpc 共享相同的 JSON-RPC 消息格式。
 */

import { ReconnectingWebSocket } from "./websocket";

const DEFAULT_TIMEOUT = 30_000;

interface PendingCall {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class RpcError extends Error {
  constructor(
    public code: number,
    message: string,
  ) {
    super(message);
    this.name = "RpcError";
  }
}

export class AppRpc {
  private ws: ReconnectingWebSocket;
  private pending = new Map<string, PendingCall>();
  private nextId = 1;

  constructor(opts?: {
    tokenFn?: () => string | null;
    onOpen?: () => void;
    onClose?: () => void;
  }) {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${location.host}/ws/app`;

    this.ws = new ReconnectingWebSocket(url, (msg) => this.handleMessage(msg), {
      tokenFn: opts?.tokenFn,
      onOpen: opts?.onOpen,
      onClose: () => {
        for (const [, pending] of this.pending) {
          clearTimeout(pending.timer);
          pending.reject(new RpcError(-1, "WebSocket disconnected"));
        }
        this.pending.clear();
        opts?.onClose?.();
      },
    });
  }

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

  close() {
    this.ws.close();
  }

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
    }
  }
}
