/**
 * Remote logging — sends frontend logs through channel to the backend.
 *
 * Usage:
 *   import { rlog, setLogChannel } from "../lib/remote-log";
 *   setLogChannel(rpc, ch);      // bind to channel
 *   rlog.debug("event", data);   // logs to console AND sends to backend
 */

import type { WorkspaceRpc } from "./workspace-rpc";

let _rpc: WorkspaceRpc | null = null;
let _ch: number = 0;
let _sessionId: string = "";

export function setLogChannel(rpc: WorkspaceRpc | null, ch?: number, sessionId?: string) {
  _rpc = rpc;
  if (ch !== undefined) _ch = ch;
  if (sessionId) _sessionId = sessionId;
}

function send(level: string, args: unknown[]) {
  const message = args
    .map((a) => (typeof a === "string" ? a : JSON.stringify(a)))
    .join(" ");
  // Always console.log locally
  const tag = `[FE:${_sessionId.slice(0, 8)}]`;
  if (level === "error") console.error(tag, ...args);
  else if (level === "warn") console.warn(tag, ...args);
  else console.log(tag, ...args);
  // Forward to backend via channel
  if (_rpc && _ch > 0) {
    try {
      _rpc.sendToChannel(_ch, { type: "log", level, message, ts: Date.now() });
    } catch {
      // channel not ready — ignore
    }
  }
}

export const rlog = {
  debug: (...args: unknown[]) => send("debug", args),
  info: (...args: unknown[]) => send("info", args),
  warn: (...args: unknown[]) => send("warn", args),
  error: (...args: unknown[]) => send("error", args),
};
