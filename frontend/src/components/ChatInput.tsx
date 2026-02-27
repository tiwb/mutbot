import { useCallback, useEffect, useRef, useState } from "react";

type SendMode = "enter" | "ctrl-enter";

interface Props {
  onSend: (text: string) => void;
  onCancel?: () => void;
  disabled?: boolean;
  isBusy?: boolean;
}

function loadSendMode(): SendMode {
  const v = localStorage.getItem("chatInput.sendMode");
  return v === "ctrl-enter" ? "ctrl-enter" : "enter";
}

export default function ChatInput({ onSend, onCancel, disabled, isBusy }: Props) {
  const [text, setText] = useState("");
  const [sendMode, setSendMode] = useState<SendMode>(loadSendMode);
  const [menuOpen, setMenuOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [focused, setFocused] = useState(false);

  // 自适应高度
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const lineHeight = 20;
    const maxHeight = lineHeight * 8;
    ta.style.height = `${Math.min(ta.scrollHeight, maxHeight)}px`;
    ta.style.overflowY = ta.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [text]);

  const handleSend = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText("");
    textareaRef.current?.focus();
  }, [text, onSend]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (sendMode === "enter") {
        if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
          e.preventDefault();
          handleSend();
        }
      } else {
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          handleSend();
        }
      }
    },
    [sendMode, handleSend],
  );

  const handleSetSendMode = useCallback((mode: SendMode) => {
    setSendMode(mode);
    localStorage.setItem("chatInput.sendMode", mode);
    setMenuOpen(false);
    textareaRef.current?.focus();
  }, []);

  // 点击外部关闭菜单
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen]);

  // 点击容器聚焦 textarea
  const handleContainerClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    if (target.closest("button") || target.closest(".send-mode-menu")) return;
    textareaRef.current?.focus();
  }, []);

  const placeholder = sendMode === "enter"
    ? "Type a message... (Shift+Enter for newline)"
    : "Type a message... (Ctrl+Enter to send)";

  return (
    <div
      ref={containerRef}
      className={`chat-input-container${focused ? " focused" : ""}`}
      onClick={handleContainerClick}
    >
      <textarea
        ref={textareaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
      />
      <div className="chat-input-toolbar">
        <div className="toolbar-left">
          <button className="toolbar-icon-btn" disabled title="Coming soon">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <path d="M14 4.5V14a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V2a2 2 0 0 1 2-2h5.5L14 4.5zM4 1a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V5H9.5a.5.5 0 0 1-.5-.5V1H4z"/>
            </svg>
          </button>
          <button className="toolbar-icon-btn" disabled title="Coming soon">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <path d="M6.002 5.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0z"/>
              <path d="M2.002 1a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V3a2 2 0 0 0-2-2h-12zm12 1a1 1 0 0 1 1 1v6.5l-3.777-1.947a.5.5 0 0 0-.577.093l-3.71 3.71-2.66-1.772a.5.5 0 0 0-.63.062L1.002 12V3a1 1 0 0 1 1-1h12z"/>
            </svg>
          </button>
          <button className="toolbar-icon-btn" disabled title="Coming soon">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm0 1A8 8 0 1 0 8 0a8 8 0 0 0 0 16z"/>
              <path d="M4.285 9.567a.5.5 0 0 1 .683.183A3.498 3.498 0 0 0 8 11.5a3.498 3.498 0 0 0 3.032-1.75.5.5 0 1 1 .866.5A4.498 4.498 0 0 1 8 12.5a4.498 4.498 0 0 1-3.898-2.25.5.5 0 0 1 .183-.683zM7 6.5C7 7.328 6.552 8 6 8s-1-.672-1-1.5S5.448 5 6 5s1 .672 1 1.5zm4 0c0 .828-.448 1.5-1 1.5s-1-.672-1-1.5S9.448 5 10 5s1 .672 1 1.5z"/>
            </svg>
          </button>
        </div>
        <div className="toolbar-right">
          {isBusy && (
            <button
              className="stop-btn"
              onClick={onCancel}
              disabled={disabled}
              title="Stop"
            >
              Stop
            </button>
          )}
          <div className="send-btn-group" ref={menuRef}>
            <button
              className="send-btn"
              onClick={handleSend}
              disabled={disabled || !text.trim()}
            >
              Send
            </button>
            <button
              className={`send-dropdown-btn${disabled || !text.trim() ? " dimmed" : ""}`}
              onClick={() => setMenuOpen((v) => !v)}
            >
              <svg width="10" height="6" viewBox="0 0 10 6" fill="currentColor">
                <path d="M5 0L10 6H0L5 0z"/>
              </svg>
            </button>
            {menuOpen && (
              <div className="send-mode-menu">
                <button
                  className={sendMode === "enter" ? "active" : ""}
                  onClick={() => handleSetSendMode("enter")}
                >
                  Send with Enter
                </button>
                <button
                  className={sendMode === "ctrl-enter" ? "active" : ""}
                  onClick={() => handleSetSendMode("ctrl-enter")}
                >
                  Send with Ctrl+Enter
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
