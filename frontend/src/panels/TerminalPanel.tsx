import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
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

/** Methods exposed via ref for external terminal interaction (mobile input panels, etc.). */
export interface TerminalPanelHandle {
  /** Write raw data (key sequences, text) to the terminal's PTY channel. Auto-scrolls to bottom. */
  writeInput: (data: string) => void;
  /** Focus the underlying xterm textarea (triggers system keyboard on mobile). */
  focusTerminal: () => void;
  /** Scroll terminal to the bottom. */
  scrollToBottom: () => void;
}

/** execCommand('copy') fallback for non-secure contexts */
function execCopy(text: string) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}

const TerminalPanel = forwardRef<TerminalPanelHandle, Props>(function TerminalPanel({ sessionId, terminalId: initialId, workspaceId, nodeId, rpc, onTerminalCreated, onTerminalExited }, ref) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const termIdRef = useRef<string | null>(initialId ?? null);
  const chRef = useRef<number>(0);

  const [expired, setExpired] = useState(false);
  const [connected, setConnected] = useState(false);
  const [ctrlVPaste, setCtrlVPaste] = useState(true);
  const [resizeLocked, setResizeLocked] = useState(false);

  // Ref so the effect's key handler can read current toggle state
  const ctrlVPasteRef = useRef(ctrlVPaste);
  ctrlVPasteRef.current = ctrlVPaste;

  // Expose init() via ref so handleRecreate can call it
  const initRef = useRef<(() => void) | null>(null);

  // Expose writeInput / focusTerminal / scrollToBottom to parent via ref
  useImperativeHandle(ref, () => ({
    writeInput(data: string) {
      const ch = chRef.current;
      if (ch > 0 && rpc) {
        const encoder = new TextEncoder();
        rpc.sendBinaryToChannel(ch, encoder.encode(data));
      }
      // Auto-scroll to bottom when sending input
      termRef.current?.scrollToBottom();
    },
    focusTerminal() {
      termRef.current?.focus();
    },
    scrollToBottom() {
      termRef.current?.scrollToBottom();
    },
  }), [rpc]);

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
        scrollbarSliderBackground: "rgba(121, 121, 121, 0.4)",
        scrollbarSliderHoverBackground: "rgba(121, 121, 121, 0.7)",
        scrollbarSliderActiveBackground: "rgba(121, 121, 121, 0.7)",
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
    // Whether this client is the primary (controls PTY size)
    let isPrimary = true;
    // Whether fitAddon is paused (non-primary clients pause fit to keep PTY-synced size)
    let fitPaused = false;

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
          // 非主客户端：暂停 fitAddon，保持 PTY 同步尺寸不被 fit 覆盖
          if (!isPrimary && !fitPaused) {
            fitPaused = true;
          }
        }
        return;
      }

      if (msgType === "resize_owner") {
        const ownerId = msg.client_id as string;
        const locked = msg.locked as boolean;
        const wasPrimary = isPrimary;
        isPrimary = ownerId === rpc!.clientId;
        setResizeLocked(locked && isPrimary);
        // 成为主客户端 → 恢复 fitAddon，重新 fit 到容器尺寸
        if (isPrimary && !wasPrimary && fitPaused) {
          fitPaused = false;
          if (fitRef.current) {
            fitRef.current.fit();
          }
        }
        return;
      }
    }

    // Track the session_id used for the current channel (needed for disconnect)
    let channelSessionId = "";

    async function openTerminalChannel(termSessionId: string) {
      if (!active || !rpc) return;
      inputMuted = true;

      try {
        const result = await rpc.call<{ ch: number }>("session.connect", { session_id: termSessionId });
        ch = result.ch;
        channelSessionId = termSessionId;
        if (!active) {
          rpc.cleanupChannelHandlers(ch);
          rpc.call("session.disconnect", { session_id: termSessionId, ch }).catch(() => {});
          return;
        }
        chRef.current = ch;
        rpc.onBinaryChannel(ch, handleBinaryData);
        rpc.onChannel(ch, handleJsonMessage);
      } catch {
        // Session connect failed
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
        rpc.cleanupChannelHandlers(ch);
        rpc.call("session.disconnect", { session_id: channelSessionId, ch }).catch(() => {});
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

    // Clipboard helper: writeText with execCommand fallback for non-secure contexts
    function copyToClipboard(text: string) {
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).catch(() => execCopy(text));
      } else {
        execCopy(text);
      }
    }

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

    // Intercept Ctrl+C (copy when selection) and Ctrl+V (paste toggle)
    term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
      if (e.type !== "keydown") return true;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "c") {
        const sel = term.getSelection();
        if (sel) {
          copyToClipboard(sel);
          term.clearSelection();
          return false; // consumed — don't send \x03
        }
        return true; // no selection → send SIGINT
      }
      if (mod && e.key === "v") {
        if (ctrlVPasteRef.current) {
          return false; // let browser fire paste event
        }
        return true; // send raw Ctrl+V to terminal
      }
      return true;
    });

    // Handle paste via browser event (clipboardData works without Secure Context)
    const onPaste = (e: ClipboardEvent) => {
      const text = e.clipboardData?.getData("text/plain");
      if (text && ch > 0 && rpc) {
        const encoder = new TextEncoder();
        rpc.sendBinaryToChannel(ch, encoder.encode(text));
      }
      e.preventDefault();
    };
    container.addEventListener("paste", onPaste);

    // Resize handling — fit the terminal to container, then report to server.
    // Non-primary clients pause fit to keep PTY-synced size.
    const resizeObserver = new ResizeObserver(() => {
      if (fitPaused) return;
      if (fitRef.current) {
        fitRef.current.fit();
      }
    });

    // After fitAddon.fit() resizes the terminal, report new dimensions to server
    const resizeDisposable = term.onResize(({ rows: r, cols: c }) => {
      sendResize(r, c);
    });
    resizeObserver.observe(container);

    // Touch scrolling for mobile — with inertia (momentum)
    let touchStartY = 0;
    let touchAccum = 0;
    let isTouchScrolling = false;
    let velocity = 0;
    let lastTouchTime = 0;
    let inertiaRaf = 0;
    const TOUCH_THRESHOLD = 5; // px before we start scrolling
    const FRICTION = 0.92; // deceleration factor per frame
    const MIN_VELOCITY = 0.5; // px/frame below which we stop

    function getLineHeight() {
      const rowsEl = term.element?.querySelector(".xterm-rows");
      return rowsEl
        ? rowsEl.getBoundingClientRect().height / term.rows || 16
        : 16;
    }

    function stopInertia() {
      if (inertiaRaf) {
        cancelAnimationFrame(inertiaRaf);
        inertiaRaf = 0;
      }
    }

    function startInertia() {
      stopInertia();
      const lineHeight = getLineHeight();
      let accum = 0;
      function tick() {
        velocity *= FRICTION;
        if (Math.abs(velocity) < MIN_VELOCITY) {
          inertiaRaf = 0;
          return;
        }
        accum += velocity;
        const lines = Math.trunc(accum / lineHeight);
        if (lines !== 0) {
          term.scrollLines(lines);
          accum -= lines * lineHeight;
        }
        inertiaRaf = requestAnimationFrame(tick);
      }
      inertiaRaf = requestAnimationFrame(tick);
    }

    const onTouchStart = (e: TouchEvent) => {
      stopInertia();
      touchStartY = e.touches[0]!.clientY;
      lastTouchTime = performance.now();
      touchAccum = 0;
      velocity = 0;
      isTouchScrolling = false;
    };

    const onTouchMove = (e: TouchEvent) => {
      const currentY = e.touches[0]!.clientY;
      const deltaY = touchStartY - currentY;
      touchStartY = currentY;

      if (!isTouchScrolling && Math.abs(deltaY) < TOUCH_THRESHOLD) return;
      isTouchScrolling = true;

      const now = performance.now();
      const dt = now - lastTouchTime;
      lastTouchTime = now;

      // Track velocity (px per 16ms frame)
      if (dt > 0) {
        velocity = deltaY * (16 / dt);
      }

      const lineHeight = getLineHeight();
      touchAccum += deltaY;
      const lines = Math.trunc(touchAccum / lineHeight);
      if (lines !== 0) {
        term.scrollLines(lines);
        touchAccum -= lines * lineHeight;
      }
      e.preventDefault();
    };

    const onTouchEnd = () => {
      if (isTouchScrolling && Math.abs(velocity) > MIN_VELOCITY) {
        startInertia();
      }
      isTouchScrolling = false;
      touchAccum = 0;
    };

    container.addEventListener("touchstart", onTouchStart, { passive: true });
    container.addEventListener("touchmove", onTouchMove, { passive: false });
    container.addEventListener("touchend", onTouchEnd, { passive: true });

    init();

    return () => {
      active = false;
      initRef.current = null;
      inputDisposable.dispose();
      resizeDisposable.dispose();
      resizeObserver.disconnect();
      stopInertia();
      container.removeEventListener("paste", onPaste);
      container.removeEventListener("touchstart", onTouchStart);
      container.removeEventListener("touchmove", onTouchMove);
      container.removeEventListener("touchend", onTouchEnd);
      unsubClosed();
      // Close channel on cleanup
      if (ch > 0 && rpc) {
        rpc.cleanupChannelHandlers(ch);
        rpc.call("session.disconnect", { session_id: channelSessionId, ch }).catch(() => {});
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

  // Clipboard helper (same logic as in effect, but for menu clicks)
  const copyTermSelection = useCallback(() => {
    const term = termRef.current;
    if (!term) return;
    const selection = term.getSelection();
    if (!selection) return;
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(selection).catch(() => execCopy(selection));
    } else {
      execCopy(selection);
    }
    term.clearSelection();
  }, []);

  const pasteToTerminal = useCallback((text: string) => {
    const ch = chRef.current;
    if (ch > 0 && rpc && text) {
      const encoder = new TextEncoder();
      rpc.sendBinaryToChannel(ch, encoder.encode(text));
    }
  }, [rpc]);

  const menuItems: ContextMenuItem[] = [
    {
      label: "Copy",
      shortcut: "Ctrl+C",
      onClick: copyTermSelection,
    },
    {
      label: "Paste",
      shortcut: "Ctrl+V",
      onClick: () => {
        if (navigator.clipboard?.readText) {
          navigator.clipboard.readText().then(pasteToTerminal).catch(() => {});
        }
      },
    },
    { label: "", separator: true },
    {
      label: "Ctrl+V Paste",
      checked: ctrlVPaste,
      onClick: () => setCtrlVPaste((v) => !v),
    },
    { label: "", separator: true },
    {
      label: "Clear Terminal",
      onClick: () => {
        termRef.current?.clear();
      },
    },
    { label: "", separator: true },
    {
      label: "Auto (follow input)",
      checked: !resizeLocked,
      onClick: () => {
        const ch = chRef.current;
        if (ch > 0 && rpc) {
          rpc.sendToChannel(ch, { type: "claim_resize", lock: false });
        }
      },
    },
    {
      label: "Follow Me",
      checked: resizeLocked,
      onClick: () => {
        const ch = chRef.current;
        if (ch > 0 && rpc) {
          rpc.sendToChannel(ch, { type: "claim_resize", lock: true });
        }
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
});

export default TerminalPanel;
