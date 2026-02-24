import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import {
  createTerminal as apiCreateTerminal,
  deleteTerminal as apiDeleteTerminal,
  getAuthToken,
} from "../lib/api";
import ContextMenu, { type ContextMenuItem } from "../components/ContextMenu";

interface Props {
  sessionId?: string;
  terminalId?: string;
  workspaceId: string;
  nodeId?: string;
  onTerminalCreated?: (nodeId: string, config: Record<string, unknown>) => void;
  onTerminalExited?: (sessionId: string) => void;
}

/** Build WS URL with optional auth token and terminal dimensions. */
function buildWsUrl(termId: string, rows?: number, cols?: number): string {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  const token = getAuthToken();
  if (token) params.set("token", token);
  if (rows) params.set("rows", String(rows));
  if (cols) params.set("cols", String(cols));
  const qs = params.toString();
  const base = `${protocol}//${location.host}/ws/terminal/${termId}`;
  return qs ? `${base}?${qs}` : base;
}

export default function TerminalPanel({ sessionId, terminalId: initialId, workspaceId, nodeId, onTerminalCreated, onTerminalExited }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const termIdRef = useRef<string | null>(initialId ?? null);

  const [expired, setExpired] = useState(false);

  // Expose init() via ref so handleRecreate can call it
  const initRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // Per-invocation flag — survives React StrictMode's unmount/remount
    // cycle without being reset by the next mount (unlike destroyedRef).
    let active = true;

    const term = new Terminal({
      theme: {
        background: "#1e1e1e",
        foreground: "#cccccc",
        cursor: "#aeafad",
        cursorAccent: "#000000",
        selectionBackground: "rgba(255, 255, 255, 0.3)",
        black: "#000000",
        red: "#cd3131",
        green: "#0dbc79",
        yellow: "#e5e510",
        blue: "#2472c8",
        magenta: "#bc3fbc",
        cyan: "#11a8cd",
        white: "#e5e5e5",
        brightBlack: "#666666",
        brightRed: "#f14c4c",
        brightGreen: "#23d18b",
        brightYellow: "#f5f543",
        brightBlue: "#3b8eea",
        brightMagenta: "#d670d6",
        brightCyan: "#29b8db",
        brightWhite: "#e5e5e5",
      },
      fontFamily: '"SF Mono", "Cascadia Code", Consolas, monospace',
      fontSize: 13,
      cursorBlink: true,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon());
    term.open(container);
    fitAddon.fit();

    termRef.current = term;
    fitRef.current = fitAddon;

    const rows = term.rows;
    const cols = term.cols;

    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let retryCount = 0;
    const maxRetries = 10;
    // Mute terminal input during scrollback replay to prevent
    // xterm.js from echoing responses to terminal query sequences
    // (e.g. \e[6n cursor position report) back as user input.
    let inputMuted = true;
    // Guard against duplicate 0x04 signals (reader thread + alive-check fallback)
    let processExited = false;

    function sendResize(r: number, c: number) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        const buf = new ArrayBuffer(5);
        const view = new DataView(buf);
        view.setUint8(0, 0x02);
        view.setUint16(1, r, false);
        view.setUint16(3, c, false);
        ws.send(buf);
      }
    }

    function connectWs(termId: string) {
      if (!active) return;

      // Mute input until scrollback replay is complete
      inputMuted = true;

      const curRows = termRef.current?.rows ?? rows;
      const curCols = termRef.current?.cols ?? cols;
      const url = buildWsUrl(termId, curRows, curCols);
      ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        if (!active) { ws?.close(); return; }
        retryCount = 0;
      };

      ws.onmessage = (event) => {
        if (!active) return;
        const data = event.data as ArrayBuffer;
        const bytes = new Uint8Array(data);
        if (bytes.length === 0) return;

        if (bytes[0] === 0x04) {
          // Terminal process exited — idempotent guard
          if (processExited) return;
          processExited = true;
          let exitInfo = "";
          if (bytes.length >= 5) {
            const dv = new DataView(data, 1, 4);
            const code = dv.getInt32(0, false);
            exitInfo = ` (exit code: ${code})`;
          }
          term.write(`\r\n\x1b[33m[Terminal process has ended${exitInfo}]\x1b[0m\r\n`);
          setExpired(true);
          // Notify parent to sync session status
          if (sessionId && onTerminalExited) {
            onTerminalExited(sessionId);
          }
          return;
        }

        if (bytes[0] === 0x03) {
          // Scrollback replay complete — delay unmute until xterm.js
          // finishes processing all pending writes (prevents DA1 response
          // leak like ^[[?1;2c being sent back to PTY as user input).
          term.write("", () => {
            inputMuted = false;
            sendResize(
              termRef.current?.rows ?? rows,
              termRef.current?.cols ?? cols,
            );
          });
          return;
        }

        if (bytes[0] === 0x01) {
          term.write(bytes.slice(1));
        }
      };

      ws.onclose = (event) => {
        if (!active) return;
        if (event.code === 4004) {
          // Terminal not found — show expired UI instead of auto-creating
          if (!processExited) {
            processExited = true;
            term.write("\r\n\x1b[33m[Terminal process has ended]\x1b[0m\r\n");
            if (sessionId && onTerminalExited) {
              onTerminalExited(sessionId);
            }
          }
          setExpired(true);
          return;
        }
        // Don't reconnect if the terminal process has already exited
        if (processExited) return;
        // Auto-reconnect with backoff
        if (retryCount < maxRetries) {
          const delay = Math.min(1000 * 2 ** retryCount, 15000);
          retryCount++;
          reconnectTimer = setTimeout(() => {
            if (active && termIdRef.current) {
              connectWs(termIdRef.current);
            }
          }, delay);
        } else {
          term.write("\r\n\x1b[31m[Terminal disconnected]\x1b[0m\r\n");
        }
      };

      ws.onerror = () => {
        ws?.close();
      };
    }

    async function init() {
      if (!active) return;
      let termId = termIdRef.current;
      if (!termId) {
        const result = await apiCreateTerminal(workspaceId, rows, cols);
        if (!active) return;
        termId = result.id;
        termIdRef.current = termId;
        // Persist terminal ID into the flexlayout tab config
        if (nodeId && onTerminalCreated) {
          onTerminalCreated(nodeId, { sessionId, terminalId: termId, workspaceId });
        }
      }
      connectWs(termId!);
    }

    // Expose init for external re-creation
    initRef.current = () => {
      // Close old WS connection (terminal process is dead)
      ws?.close();
      wsRef.current?.close();
      // Delete old dead terminal
      const oldTid = termIdRef.current;
      if (oldTid) {
        apiDeleteTerminal(oldTid).catch(() => {});
      }
      // Clear terminal screen for fresh start
      term.write("\x1b[2J\x1b[H");
      retryCount = 0;
      processExited = false;
      termIdRef.current = null;
      init();
    };

    // Terminal input → WebSocket (muted during scrollback replay)
    const inputDisposable = term.onData((data) => {
      if (inputMuted) return;
      if (ws && ws.readyState === WebSocket.OPEN) {
        const encoder = new TextEncoder();
        const encoded = encoder.encode(data);
        const buf = new Uint8Array(1 + encoded.length);
        buf[0] = 0x00;
        buf.set(encoded, 1);
        ws.send(buf.buffer);
      }
    });

    // Resize handling
    const resizeObserver = new ResizeObserver(() => {
      if (fitRef.current && termRef.current) {
        fitRef.current.fit();
        sendResize(termRef.current.rows, termRef.current.cols);
      }
    });
    resizeObserver.observe(container);

    init();

    return () => {
      active = false;
      initRef.current = null;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      inputDisposable.dispose();
      resizeObserver.disconnect();
      // Close via both local var and ref — the WS may have been
      // created after cleanup was scheduled (async race).
      ws?.close();
      wsRef.current?.close();
      wsRef.current = null;
      term.dispose();
      termRef.current = null;
      // PTY lifecycle managed by backend session — no deletion on unmount
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initialId only used
  // for ref initialization on mount; re-running the effect on prop change would
  // destroy and recreate the terminal (handleRecreate manages recreation).
  }, [workspaceId]);

  const handleRecreate = useCallback(() => {
    setExpired(false);
    initRef.current?.();
  }, []);

  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
  } | null>(null);

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setContextMenu({ position: { x: e.clientX, y: e.clientY } });
  }, []);

  const menuItems: ContextMenuItem[] = [
    {
      label: "Copy",
      shortcut: "Ctrl+C",
      onClick: () => {
        const term = termRef.current;
        if (term) {
          const selection = term.getSelection();
          if (selection) {
            navigator.clipboard.writeText(selection);
          }
        }
      },
    },
    {
      label: "Paste",
      shortcut: "Ctrl+V",
      onClick: () => {
        navigator.clipboard.readText().then((text) => {
          const ws = wsRef.current;
          if (ws && ws.readyState === WebSocket.OPEN && text) {
            const encoder = new TextEncoder();
            const encoded = encoder.encode(text);
            const buf = new Uint8Array(1 + encoded.length);
            buf[0] = 0x00;
            buf.set(encoded, 1);
            ws.send(buf.buffer);
          }
        });
      },
    },
    { label: "", separator: true },
    {
      label: "Clear Terminal",
      onClick: () => {
        termRef.current?.clear();
      },
    },
  ];

  return (
    <div ref={containerRef} className="terminal-panel" onContextMenu={handleContextMenu}>
      {expired && (
        <div className="terminal-expired-overlay">
          <div className="terminal-expired-content">
            <button onClick={handleRecreate}>Restart Terminal</button>
          </div>
        </div>
      )}
      {contextMenu && (
        <ContextMenu
          items={menuItems}
          position={contextMenu.position}
          onClose={() => setContextMenu(null)}
        />
      )}
    </div>
  );
}
