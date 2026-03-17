import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import { UnicodeGraphemesAddon } from "@xterm/addon-unicode-graphemes";
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
  // 服务端滚动条状态（始终保留最新数据，不因 offset=0 而清空）
  const [scrollState, setScrollState] = useState<{ offset: number; total: number; visible: number } | null>(null);
  const scrollFadeRef = useRef<number>(0);
  // 滚动条临时可见（滚动操作后 2 秒内）
  const [scrollbarFlash, setScrollbarFlash] = useState(false);
  // 滚动条拖拽中
  const scrollbarDraggingRef = useRef(false);
  // follow_me 客户端 ID（null = Auto 模式）
  const [followMe, setFollowMe] = useState<string | null>(null);

  // Ref so the effect's key handler can read current toggle state
  const ctrlVPasteRef = useRef(ctrlVPaste);
  ctrlVPasteRef.current = ctrlVPaste;

  // Expose init() via ref so handleRecreate can call it
  const initRef = useRef<(() => void) | null>(null);
  // Expose sendScrollToBottom for imperative handle
  const sendScrollToBottomRef = useRef<(() => void) | null>(null);
  // Expose sendScrollTo / sendScroll for scrollbar interaction
  const sendScrollToRef = useRef<((offset: number) => void) | null>(null);
  const sendScrollRef = useRef<((lines: number) => void) | null>(null);

  // Expose writeInput / focusTerminal / scrollToBottom to parent via ref
  useImperativeHandle(ref, () => ({
    writeInput(data: string) {
      const ch = chRef.current;
      if (ch > 0 && rpc) {
        const encoder = new TextEncoder();
        rpc.sendBinaryToChannel(ch, encoder.encode(data));
      }
      // Auto-scroll to bottom when sending input
      sendScrollToBottomRef.current?.();
    },
    focusTerminal() {
      termRef.current?.focus();
    },
    scrollToBottom() {
      sendScrollToBottomRef.current?.();
    },
  }), [rpc]);

  // ── xterm instance lifecycle (mount-only) ──
  // xterm 实例只在组件挂载时创建、卸载时销毁。
  // WS 断连不影响 xterm，终端内容保持冻结显示。
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const term = new Terminal({
      allowProposedApi: true,
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
      scrollback: 0,  // 禁用 xterm 内部 scrollback，由服务端 pyte 管理
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon());
    term.loadAddon(new UnicodeGraphemesAddon());
    // UnicodeGraphemesAddon 会自动设置 activeVersion = "15-graphemes"
    term.open(container);
    fitAddon.fit();

    termRef.current = term;
    fitRef.current = fitAddon;

    return () => {
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
  }, []);

  // ── channel lifecycle (reconnects when rpc changes) ──
  // rpc 变化时（断连 → null → 重连 → 新实例），重建 channel 但不动 xterm。
  // 断连期间终端内容冻结，重连后服务端发送 snapshot 恢复。
  useEffect(() => {
    const container = containerRef.current;
    if (!termRef.current || !container || !rpc) return;
    // Non-null assertion safe: checked above, xterm lifecycle effect guarantees
    // termRef.current is stable until component unmount.
    const term = termRef.current;

    // Per-invocation flag — survives React StrictMode's unmount/remount
    let active = true;

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

    function sendScroll(lines: number) {
      if (ch > 0 && rpc && lines !== 0) {
        rpc.sendToChannel(ch, { type: "scroll", lines });
      }
    }

    function sendScrollToBottom() {
      if (ch > 0 && rpc) {
        rpc.sendToChannel(ch, { type: "scroll_to_bottom" });
      }
    }

    function sendScrollTo(offset: number) {
      if (ch > 0 && rpc) {
        rpc.sendToChannel(ch, { type: "scroll_to", offset });
      }
    }

    sendScrollToBottomRef.current = sendScrollToBottom;
    sendScrollToRef.current = sendScrollTo;
    sendScrollRef.current = sendScroll;

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
            // 仅注册尺寸，不触发 PTY resize（避免多客户端重连时互相抢占）
            const r = termRef.current?.rows ?? rows;
            const c = termRef.current?.cols ?? cols;
            if (ch > 0 && rpc) {
              rpc.sendToChannel(ch, { type: "register_size", rows: r, cols: c });
            }
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
        const r = msg.rows as number;
        const c = msg.cols as number;
        if (termRef.current && r > 0 && c > 0) {
          serverResizing = true;
          termRef.current.resize(c, r);
          serverResizing = false;
        }
        return;
      }

      if (msgType === "resize_owner") {
        const fm = msg.follow_me as string | null;
        setFollowMe(fm);
        return;
      }

      if (msgType === "scroll_state") {
        const offset = msg.offset as number;
        const total = msg.total as number;
        const visible = msg.visible as number;
        // 拖拽期间不更新 React state，避免 re-render 干扰 DOM 操作
        if (!scrollbarDraggingRef.current && total > 0) {
          setScrollState({ offset, total, visible });
        }
        // 闪现滚动条：滚动操作后 2 秒可见
        if (!scrollbarDraggingRef.current) {
          setScrollbarFlash(true);
          clearTimeout(scrollFadeRef.current);
          scrollFadeRef.current = window.setTimeout(() => {
            setScrollbarFlash(false);
          }, 2000);
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
    // If PTY size differs from container fit size, skip fit (PTY controlled by another client).
    const resizeObserver = new ResizeObserver(() => {
      if (fitRef.current) {
        // 检查 fitAddon 的 proposeDimensions：如果与当前 PTY 尺寸一致，正常 fit；
        // 如果 PTY 尺寸被其他客户端控制（不一致），仍然 fit 以发送 resize 上报
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

    // Long-press to trigger context menu on mobile (500ms, 10px threshold)
    let longPressTimer = 0;
    let longPressFired = false;
    let longPressStartX = 0;
    let longPressStartY = 0;
    const LONG_PRESS_MS = 500;
    const LONG_PRESS_MOVE_THRESHOLD = 10;

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
          sendScroll(-lines);  // 惯性：与触摸同向，取反
          accum -= lines * lineHeight;
        }
        inertiaRaf = requestAnimationFrame(tick);
      }
      inertiaRaf = requestAnimationFrame(tick);
    }

    function cancelLongPress() {
      if (longPressTimer) {
        clearTimeout(longPressTimer);
        longPressTimer = 0;
      }
    }

    const onTouchStart = (e: TouchEvent) => {
      stopInertia();
      touchStartY = e.touches[0]!.clientY;
      lastTouchTime = performance.now();
      touchAccum = 0;
      velocity = 0;
      isTouchScrolling = false;

      // Long-press detection
      const touch = e.touches[0]!;
      longPressStartX = touch.clientX;
      longPressStartY = touch.clientY;
      longPressFired = false;
      cancelLongPress();
      longPressTimer = window.setTimeout(() => {
        longPressTimer = 0;
        longPressFired = true;
        navigator.vibrate?.(50);
        setContextMenu({ position: { x: longPressStartX, y: longPressStartY } });
      }, LONG_PRESS_MS);
    };

    const onTouchMove = (e: TouchEvent) => {
      const currentY = e.touches[0]!.clientY;
      const deltaY = touchStartY - currentY;
      touchStartY = currentY;

      // Cancel long-press if finger moved beyond threshold
      if (longPressTimer) {
        const touch = e.touches[0]!;
        const dx = touch.clientX - longPressStartX;
        const dy = touch.clientY - longPressStartY;
        if (Math.sqrt(dx * dx + dy * dy) > LONG_PRESS_MOVE_THRESHOLD) {
          cancelLongPress();
        }
      }

      if (!isTouchScrolling && Math.abs(deltaY) < TOUCH_THRESHOLD) return;
      isTouchScrolling = true;
      // 只在滚动时阻止默认行为（防止页面滚动），不影响 tap 聚焦和 IME 输入
      e.preventDefault();

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
        sendScroll(-lines);  // 触摸：上滑 deltaY>0 期望向下滚，后端 lines>0=向上，取反
        touchAccum -= lines * lineHeight;
      }
    };

    const onTouchEnd = (e: TouchEvent) => {
      cancelLongPress();
      // Suppress click/tap after long-press triggered the context menu
      if (longPressFired) {
        e.preventDefault();
        longPressFired = false;
        isTouchScrolling = false;
        touchAccum = 0;
        return;
      }
      if (isTouchScrolling && Math.abs(velocity) > MIN_VELOCITY) {
        startInertia();
      }
      isTouchScrolling = false;
      touchAccum = 0;
    };

    container.addEventListener("touchstart", onTouchStart, { passive: true });
    container.addEventListener("touchmove", onTouchMove, { passive: false });
    container.addEventListener("touchend", onTouchEnd, { passive: false });

    // PC 端鼠标滚轮 → 服务端滚动
    // 使用捕获阶段拦截，在 xterm.js 处理之前阻止事件
    // （xterm.js 在鼠标模式下会把 wheel 转成鼠标序列发给 PTY）
    let wheelAccum = 0;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const lineHeight = getLineHeight();
      wheelAccum += e.deltaY;
      const lines = Math.trunc(wheelAccum / lineHeight);
      if (lines !== 0) {
        sendScroll(-lines);  // wheel deltaY>0=向下，后端 lines>0=向上，取反
        wheelAccum -= lines * lineHeight;
      }
    };
    container.addEventListener("wheel", onWheel, { passive: false, capture: true });

    init();

    return () => {
      active = false;
      initRef.current = null;
      sendScrollToBottomRef.current = null;
      sendScrollToRef.current = null;
      sendScrollRef.current = null;
      inputDisposable.dispose();
      resizeDisposable.dispose();
      resizeObserver.disconnect();
      stopInertia();
      cancelLongPress();
      container.removeEventListener("paste", onPaste);
      container.removeEventListener("touchstart", onTouchStart);
      container.removeEventListener("touchmove", onTouchMove);
      container.removeEventListener("touchend", onTouchEnd);
      container.removeEventListener("wheel", onWheel, { capture: true });
      unsubClosed();
      // Close channel on cleanup
      if (ch > 0 && rpc) {
        rpc.cleanupChannelHandlers(ch);
        rpc.call("session.disconnect", { session_id: channelSessionId, ch }).catch(() => {});
      }
      // xterm 实例不在此处 dispose — 由 xterm lifecycle effect 管理
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initialId only used
  // for ref initialization on mount; re-running the effect on prop change would
  // destroy and recreate the terminal (handleRecreate manages recreation).
  }, [rpc]);

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
        const ch = chRef.current;
        if (ch > 0 && rpc) {
          rpc.sendToChannel(ch, { type: "clear_scrollback" });
        }
      },
    },
    { label: "", separator: true },
    {
      label: "Auto Resize",
      checked: followMe === null,
      onClick: () => {
        const ch = chRef.current;
        if (ch > 0 && rpc) {
          rpc.sendToChannel(ch, { type: "set_resize_mode", mode: "auto" });
        }
      },
    },
    {
      label: "Pin Resize to Me",
      checked: followMe === rpc?.clientId,
      onClick: () => {
        const ch = chRef.current;
        if (ch > 0 && rpc) {
          rpc.sendToChannel(ch, { type: "set_resize_mode", mode: "follow_me" });
        }
      },
    },
  ];

  // 滚动条：thumb 位置和大小
  const hasScrollbar = scrollState && scrollState.total > scrollState.visible;
  const scrollbar = hasScrollbar ? (() => {
    const { offset, total, visible } = scrollState;
    const thumbSize = Math.max(visible / total * 100, 5); // 可见区域占总内容的比例
    // maxOffset = total - visible = 历史行数，与后端 len(screen.history.top) 一致
    const maxOffset = total - visible;
    // offset=0 → 底部(live), offset=maxOffset → 顶部
    const thumbTop = maxOffset > 0
      ? (1 - offset / maxOffset) * (100 - thumbSize)
      : 100 - thumbSize;
    return { thumbSize, thumbTop, maxOffset };
  })() : null;

  // 滚动条拖拽处理（直接 DOM 操作，避免 React re-render）
  const trackRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLDivElement>(null);

  const handleThumbPointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const track = trackRef.current;
    const thumb = thumbRef.current;
    if (!track || !thumb || !scrollState) return;

    scrollbarDraggingRef.current = true;
    setScrollbarFlash(true);
    clearTimeout(scrollFadeRef.current);

    const trackRect = track.getBoundingClientRect();
    const startY = e.clientY;
    // 从当前 DOM 读取 thumb 位置（比 React state 更准确）
    const currentTop = parseFloat(thumb.style.top) || 0;
    const thumbSize = parseFloat(thumb.style.height) || 5;
    const { total } = scrollState;
    // maxOffset = 历史行数，与后端一致
    const maxOffset = total - scrollState.visible;
    const pointerId = e.pointerId;

    thumb.setPointerCapture(pointerId);

    // 节流服务端请求
    let lastSendTime = 0;
    let pendingOffset = -1;
    let sendTimerId = 0;

    const flushToServer = (offset: number) => {
      sendScrollToRef.current?.(offset);
      lastSendTime = performance.now();
      pendingOffset = -1;
    };

    const onPointerMove = (ev: PointerEvent) => {
      const deltaY = ev.clientY - startY;
      const deltaPct = (deltaY / trackRect.height) * 100;
      const newThumbTop = Math.max(0, Math.min(100 - thumbSize, currentTop + deltaPct));

      // 直接操作 DOM — 即时跟手，无 React re-render
      thumb.style.top = `${newThumbTop}%`;

      // 映射回 offset（maxOffset = 历史行数）
      const ratio = maxOffset > 0 ? 1 - newThumbTop / (100 - thumbSize) : 0;
      const newOffset = Math.round(ratio * maxOffset);

      // 节流发送到服务端（每 50ms）
      const now = performance.now();
      if (now - lastSendTime >= 50) {
        flushToServer(newOffset);
      } else {
        pendingOffset = newOffset;
        if (!sendTimerId) {
          const delay = 50 - (now - lastSendTime);
          sendTimerId = window.setTimeout(() => {
            sendTimerId = 0;
            if (pendingOffset >= 0) flushToServer(pendingOffset);
          }, delay);
        }
      }
    };

    const onPointerUp = () => {
      scrollbarDraggingRef.current = false;
      thumb.releasePointerCapture(pointerId);
      thumb.removeEventListener("pointermove", onPointerMove);
      thumb.removeEventListener("pointerup", onPointerUp);
      if (sendTimerId) clearTimeout(sendTimerId);
      // 发送最终位置
      if (pendingOffset >= 0) flushToServer(pendingOffset);
      // 拖拽结束后启动 2 秒隐藏
      clearTimeout(scrollFadeRef.current);
      scrollFadeRef.current = window.setTimeout(() => {
        setScrollbarFlash(false);
      }, 2000);
    };

    thumb.addEventListener("pointermove", onPointerMove);
    thumb.addEventListener("pointerup", onPointerUp);
  }, [scrollState]);

  const handleTrackClick = useCallback((e: React.MouseEvent) => {
    // 点击轨道（非滑块）→ 翻页
    const track = trackRef.current;
    const thumb = thumbRef.current;
    if (!track || !thumb || !scrollState) return;
    // 忽略点击在 thumb 上的事件
    if (e.target === thumb) return;
    const trackRect = track.getBoundingClientRect();
    const thumbRect = thumb.getBoundingClientRect();
    const thumbCenter = thumbRect.top + thumbRect.height / 2 - trackRect.top;
    const clickY = e.clientY - trackRect.top;
    // 点击在 thumb 上方 → 向上翻页（offset 增大），点击在下方 → 向下翻页
    const pageLines = scrollState.visible;
    if (clickY < thumbCenter) {
      sendScrollRef.current?.(pageLines);
    } else {
      sendScrollRef.current?.(-pageLines);
    }
  }, [scrollState]);

  return (
    <div ref={containerRef} className="terminal-panel" onContextMenu={handleContextMenu}>
      {scrollbar && (
        <div
          ref={trackRef}
          className={`terminal-scrollbar-track${scrollbarFlash || scrollbarDraggingRef.current ? " active" : ""}`}
          onClick={handleTrackClick}
        >
          <div
            ref={thumbRef}
            className="terminal-scrollbar-thumb"
            style={{
              top: `${scrollbar.thumbTop}%`,
              height: `${scrollbar.thumbSize}%`,
            }}
            onPointerDown={handleThumbPointerDown}
          />
        </div>
      )}
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
