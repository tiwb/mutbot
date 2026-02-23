import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";
import { createTerminal as apiCreateTerminal } from "../lib/api";

interface Props {
  terminalId?: string;
  workspaceId: string;
}

export default function TerminalPanel({ terminalId: initialId, workspaceId }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const termIdRef = useRef<string | null>(initialId ?? null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const term = new Terminal({
      theme: {
        background: "#0d1117",
        foreground: "#e4e4e4",
        cursor: "#e94560",
        selectionBackground: "rgba(233, 69, 96, 0.3)",
        black: "#1a1a2e",
        red: "#e94560",
        green: "#4ecca3",
        yellow: "#f0c040",
        blue: "#0f3460",
        magenta: "#9b59b6",
        cyan: "#00b4d8",
        white: "#e4e4e4",
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

    // Create terminal session via REST then connect WebSocket
    const rows = term.rows;
    const cols = term.cols;

    let ws: WebSocket | null = null;
    let destroyed = false;

    async function init() {
      let termId = termIdRef.current;
      if (!termId) {
        const result = await apiCreateTerminal(workspaceId, rows, cols);
        if (destroyed) return;
        termId = result.id;
        termIdRef.current = termId;
      }

      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${location.host}/ws/terminal/${termId}`;
      ws = new WebSocket(url);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      ws.onopen = () => {
        // Send initial resize
        sendResize(rows, cols);
      };

      ws.onmessage = (event) => {
        const data = event.data as ArrayBuffer;
        const bytes = new Uint8Array(data);
        if (bytes.length > 0 && bytes[0] === 0x01) {
          // PTY output
          term.write(bytes.slice(1));
        }
      };

      ws.onclose = () => {
        if (!destroyed) {
          term.write("\r\n\x1b[31m[Terminal disconnected]\x1b[0m\r\n");
        }
      };
    }

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

    // Terminal input → WebSocket
    const inputDisposable = term.onData((data) => {
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
      destroyed = true;
      inputDisposable.dispose();
      resizeObserver.disconnect();
      ws?.close();
      wsRef.current = null;
      term.dispose();
      termRef.current = null;
    };
  }, [workspaceId, initialId]);

  return <div ref={containerRef} className="terminal-panel" />;
}
