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

    function sendResize(r: number, c: number) {
      if (ch > 0 && rpc) {
        const buf = new ArrayBuffer(5);
        const view = new DataView(buf);
        view.setUint8(0, 0x02);
        view.setUint16(1, r, false);
        view.setUint16(3, c, false);
        rpc.sendBinaryToChannel(ch, buf);
      }
    }

    function handleBinaryData(payload: Uint8Array) {
      if (!active) return;
      if (payload.length === 0) return;

      if (payload[0] === 0x04) {
        // Terminal process exited — idempotent guard
        if (processExited) return;
        processExited = true;
        let exitInfo = "";
        if (payload.length >= 5) {
          const dv = new DataView(payload.buffer, payload.byteOffset + 1, 4);
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

      if (payload[0] === 0x03) {
        // Scrollback replay complete — use requestAnimationFrame to defer
        // unmuting until after xterm.js has fully processed pending writes.
        requestAnimationFrame(() => {
          inputMuted = false;
          setConnected(true);
          sendResize(
            termRef.current?.rows ?? rows,
            termRef.current?.cols ?? cols,
          );
        });
        return;
      }

      if (payload[0] === 0x01) {
        term.write(payload.slice(1));
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
          // Session-backed terminal: restart via session RPC to restore scrollback
          const result = await rpc.call<{ terminal_id: string }>("session.restart", { session_id: sessionId });
          if (!active) return;
          termId = result.terminal_id;
        } else {
          const result = await rpc.call<{ id: string }>("terminal.create", { rows, cols });
          if (!active) return;
          termId = result.id;
        }
        termIdRef.current = termId;
        // Persist terminal ID into the flexlayout tab config
        if (nodeId && onTerminalCreated) {
          onTerminalCreated(nodeId, { sessionId, terminalId: termId, workspaceId });
        }
      }
      // Open channel using the session_id that owns this terminal
      // For session-backed terminals, use the session_id prop
      // For standalone terminals, use the termId as a workaround (the channel target is "session")
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
      // Clear terminal screen for fresh start
      term.write("\x1b[2J\x1b[H");
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

    // Terminal input → channel binary (muted during scrollback replay)
    const inputDisposable = term.onData((data) => {
      if (inputMuted) return;
      if (ch > 0 && rpc) {
        const encoder = new TextEncoder();
        const encoded = encoder.encode(data);
        const buf = new Uint8Array(1 + encoded.length);
        buf[0] = 0x00;
        buf.set(encoded, 1);
        rpc.sendBinaryToChannel(ch, buf);
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
            const buf = new Uint8Array(1 + encoded.length);
            buf[0] = 0x00;
            buf.set(encoded, 1);
            rpc.sendBinaryToChannel(ch, buf);
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
