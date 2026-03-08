import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import ContextMenu, { type ContextMenuItem } from "../components/ContextMenu";

interface Props {
  sessionId?: string;
  terminalId?: string;
  workspaceId: string;
  nodeId?: string;
  rpc?: WorkspaceRpc | null;
  onTerminalCreated?: (nodeId: string, config: Record<string, unknown>) => void;
  onTerminalExited?: (sessionId: string) => void;
}

export default function TerminalPanel({ sessionId, terminalId: initialId, workspaceId, nodeId, rpc, onTerminalCreated, onTerminalExited }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const termIdRef = useRef<string | null>(initialId ?? null);
  const chRef = useRef<number>(0);

  const [expired, setExpired] = useState(false);
  const [connected, setConnected] = useState(false);

  // Expose init() via ref so handleRecreate can call it
  const initRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !rpc) return;

    // Per-invocation flag — survives React StrictMode's unmount/remount
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

    // Mute terminal input during scrollback replay to prevent
    // xterm.js from echoing responses to terminal query sequences
    // (e.g. \e[6n cursor position report) back as user input.
    let inputMuted = true;
    // Guard against duplicate 0x04 signals (reader thread + alive-check fallback)
    let processExited = false;
    let ch = 0;

    // Flag to suppress recursive sendResize when server-initiated pty_resize
    // triggers xterm's onResize event
    let serverResizing = false;

    function sendResize(r: number, c: number) {
      if (serverResizing) return;
      if (ch > 0 && rpc) {
        rpc.sendToChannel(ch, { type: "resize", rows: r, cols: c });
      }
    }

    function handleBinaryData(payload: Uint8Array) {
      if (!active) return;
      if (payload.length === 0) return;
      // Binary payload = raw PTY output (no msg_type prefix)
      term.write(payload);
    }

    function handleJsonMessage(msg: Record<string, unknown>) {
      if (!active) return;
      const msgType = msg.type as string;

      if (msgType === "ready") {
        const alive = msg.alive as boolean;
        // Flush xterm's write buffer: callback fires after all queued
        // scrollback data has been fully parsed, preventing terminal
        // query responses from leaking as user input.
        term.write("", () => {
          if (!active) return;
          inputMuted = false;
          if (alive) {
            processExited = false;
            setExpired(false);
            setConnected(true);
            sendResize(
              termRef.current?.rows ?? rows,
              termRef.current?.cols ?? cols,
            );
          } else {
            processExited = true;
            setExpired(true);
          }
        });
        return;
      }

      if (msgType === "process_exit") {
        if (processExited) return;
        processExited = true;
        const exitCode = msg.exit_code as number | undefined;
        const exitInfo = exitCode !== undefined ? ` (exit code: ${exitCode})` : "";
        term.write(`\r\n\x1b[33m[Terminal process has ended${exitInfo}]\x1b[0m\r\n`);
        setExpired(true);
        if (sessionId && onTerminalExited) {
          onTerminalExited(sessionId);
        }
        return;
      }

      if (msgType === "pty_resize") {
        const ptyRows = msg.rows as number;
        const ptyCols = msg.cols as number;
        if (termRef.current && ptyRows > 0 && ptyCols > 0) {
          serverResizing = true;
          termRef.current.resize(ptyCols, ptyRows);
          serverResizing = false;
        }
        return;
      }
    }

    async function openTerminalChannel(termSessionId: string) {
      if (!active || !rpc) return;
      inputMuted = true;

      try {
        ch = await rpc.openChannel("session", { session_id: termSessionId });
        if (!active) {
          rpc.closeChannel(ch).catch(() => {});
          return;
        }
        chRef.current = ch;
        rpc.onBinaryChannel(ch, handleBinaryData);
        rpc.onChannel(ch, handleJsonMessage);
      } catch {
        // Channel open failed
        if (!processExited) {
          term.write("\r\n\x1b[31m[Failed to connect to terminal]\x1b[0m\r\n");
        }
      }
    }

    async function init() {
      if (!active || !rpc) return;
      let termId = termIdRef.current;
      if (!termId) {
        if (sessionId) {
          // Session-backed terminal: restart via session RPC to create PTY
          const result = await rpc.call<{ terminal_id: string }>("session.restart", { session_id: sessionId });
          if (!active) return;
          termId = result.terminal_id;
        } else {
          const result = await rpc.call<{ id: string }>("terminal.create", { rows, cols });
          if (!active) return;
          termId = result.id;
        }
        termIdRef.current = termId;
        if (nodeId && onTerminalCreated) {
          onTerminalCreated(nodeId, { sessionId, terminalId: termId, workspaceId });
        }
      }

      const channelSessionId = sessionId || termId!;
      await openTerminalChannel(channelSessionId);
    }

    // Expose init for external re-creation
    initRef.current = () => {
      // Close old channel
      if (ch > 0 && rpc) {
        rpc.closeChannel(ch).catch(() => {});
        ch = 0;
        chRef.current = 0;
      }
      // For session-backed terminals, session.restart handles cleanup (saves scrollback
      // then kills old PTY).  For standalone terminals, delete the old PTY now.
      if (!sessionId) {
        const oldTid = termIdRef.current;
        if (oldTid && rpc) {
          rpc.call("terminal.delete", { term_id: oldTid }).catch(() => {});
        }
      }
      // Server sends clear screen + scrollback on channel open, no need to clear here
      processExited = false;
      termIdRef.current = null;
      init();
    };

    // Listen for channel.closed (session deleted, connection reset, etc.)
    const unsubClosed = rpc.onChannelClosed((closedCh, _reason) => {
      if (closedCh === chRef.current) {
        setConnected(false);
        chRef.current = 0;
        ch = 0;
      }
    });

    // Terminal responses that xterm.js generates automatically (DA1, DA2, DSR,
    // CPR).  These must never be forwarded as user input — they are always
    // xterm's reply to a query from the PTY.  During live use the PTY handles
    // the response correctly, but during scrollback replay or re-attach the
    // response arrives at the PTY unexpectedly and appears as garbage text.
    const termResponseRe = /^\x1b\[[\?>= ]?[\d;]*[cnR]$/;

    // Terminal input → channel binary (muted during scrollback replay)
    const inputDisposable = term.onData((data) => {
      if (inputMuted) return;
      if (data.charCodeAt(0) === 0x1b && termResponseRe.test(data)) return;
      if (ch > 0 && rpc) {
        const encoder = new TextEncoder();
        const encoded = encoder.encode(data);
        rpc.sendBinaryToChannel(ch, encoded);
      }
    });

    // Resize handling — only calculate and report to server, don't resize term.
    // Server will broadcast pty_resize with the actual PTY size (min of all clients).
    const resizeObserver = new ResizeObserver(() => {
      if (fitRef.current) {
        const dims = fitRef.current.proposeDimensions();
        if (dims) {
          sendResize(dims.rows, dims.cols);
        }
      }
    });
    resizeObserver.observe(container);

    init();

    return () => {
      active = false;
      initRef.current = null;
      inputDisposable.dispose();
      resizeObserver.disconnect();
      unsubClosed();
      // Close channel on cleanup
      if (ch > 0 && rpc) {
        rpc.closeChannel(ch).catch(() => {});
      }
      term.dispose();
      termRef.current = null;
      // PTY lifecycle managed by backend session — no deletion on unmount
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initialId only used
  // for ref initialization on mount; re-running the effect on prop change would
  // destroy and recreate the terminal (handleRecreate manages recreation).
  }, [workspaceId, rpc]);

  const handleRecreate = useCallback(() => {
    setExpired(false);
    setConnected(false);
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
          const ch = chRef.current;
          if (ch > 0 && rpc && text) {
            const encoder = new TextEncoder();
            const encoded = encoder.encode(text);
            rpc.sendBinaryToChannel(ch, encoded);
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
      {(!connected || expired) && (
        <div className="terminal-expired-overlay">
          <div className="terminal-expired-content">
            {expired ? (
              <button onClick={handleRecreate}>Restart Terminal</button>
            ) : (
              <span className="terminal-connecting">Connecting...</span>
            )}
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
