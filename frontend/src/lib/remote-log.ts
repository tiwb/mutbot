/**
 * Remote logging — sends frontend logs through WebSocket to the backend.
 *
 * Usage:
 *   import { rlog, setLogSocket } from "../lib/remote-log";
 *   setLogSocket(ws);          // bind to current WebSocket
 *   rlog.debug("event", data); // logs to console AND sends to backend
 */

import type { ReconnectingWebSocket } from "./websocket";

let _ws: ReconnectingWebSocket | null = null;
let _sessionId: string = "";

export function setLogSocket(ws: ReconnectingWebSocket | null, sessionId?: string) {
  _ws = ws;
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
  // Forward to backend via WebSocket
  if (_ws) {
    try {
      _ws.send({ type: "log", level, message, ts: Date.now() });
    } catch {
      // WS not ready — ignore
    }
  }
}

export const rlog = {
  debug: (...args: unknown[]) => send("debug", args),
  info: (...args: unknown[]) => send("info", args),
  warn: (...args: unknown[]) => send("warn", args),
  error: (...args: unknown[]) => send("error", args),
};
