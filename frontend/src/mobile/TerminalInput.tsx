import { useCallback, useEffect, useRef, useState } from "react";
import { CornerDownLeft, ChevronUp, ChevronDown, Send } from "lucide-react";

const INPUT_MODE_KEY = "mutbot-terminal-input-mode";
type InputMode = "single" | "multi";

function loadInputMode(): InputMode {
  try {
    const v = localStorage.getItem(INPUT_MODE_KEY);
    if (v === "multi") return "multi";
  } catch { /* ignore */ }
  return "single";
}

interface Props {
  /** Send text to terminal. Always appends \r. */
  onSend: (text: string) => void;
  /** Whether the shortcut panel is expanded */
  shortcutsOpen: boolean;
  /** Toggle shortcut panel */
  onToggleShortcuts: () => void;
}

export default function TerminalInput({ onSend, shortcutsOpen, onToggleShortcuts }: Props) {
  const [value, setValue] = useState("");
  const [mode, setMode] = useState<InputMode>(loadInputMode);
  const [menuOpen, setMenuOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressFired = useRef(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const handleSend = useCallback(() => {
    onSend(value + "\r");
    setValue("");
    if (mode === "single") {
      inputRef.current?.focus();
    } else {
      textareaRef.current?.focus();
    }
  }, [value, onSend, mode]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        if (mode === "single") {
          e.preventDefault();
          handleSend();
        }
        // multi mode: let Enter insert newline (default behavior)
      }
    },
    [mode, handleSend],
  );

  // Auto-grow textarea
  const handleTextareaChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    const ta = e.target;
    ta.style.height = "auto";
    ta.style.height = ta.scrollHeight + "px";
  }, []);

  // Reset textarea height when value is cleared
  useEffect(() => {
    if (mode === "multi" && textareaRef.current && value === "") {
      textareaRef.current.style.height = "auto";
    }
  }, [value, mode]);

  // Switch mode
  const switchMode = useCallback((newMode: InputMode) => {
    setMode(newMode);
    setMenuOpen(false);
    try { localStorage.setItem(INPUT_MODE_KEY, newMode); } catch { /* ignore */ }
  }, []);

  // Long press on send button
  const handleSendPointerDown = useCallback(() => {
    longPressFired.current = false;
    longPressTimer.current = setTimeout(() => {
      longPressFired.current = true;
      navigator.vibrate?.(30);
      setMenuOpen(true);
    }, 500);
  }, []);

  const handleSendPointerUp = useCallback(() => {
    if (longPressTimer.current) {
      clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  }, []);

  const handleSendClick = useCallback(() => {
    if (longPressFired.current) {
      longPressFired.current = false;
      return;
    }
    handleSend();
  }, [handleSend]);

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [menuOpen]);

  return (
    <div className="terminal-input-bar">
      {mode === "single" ? (
        <input
          ref={inputRef}
          className="terminal-input-field"
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Enter command..."
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
      ) : (
        <textarea
          ref={textareaRef}
          className="terminal-input-field terminal-input-textarea"
          value={value}
          onChange={handleTextareaChange}
          onKeyDown={handleKeyDown}
          placeholder="Enter command..."
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          rows={1}
        />
      )}
      <div className="terminal-input-send-wrapper">
        <button
          className="terminal-input-enter"
          onClick={handleSendClick}
          onPointerDown={handleSendPointerDown}
          onPointerUp={handleSendPointerUp}
          onPointerCancel={handleSendPointerUp}
          aria-label="Send"
        >
          {mode === "single"
            ? <CornerDownLeft size={18} />
            : <Send size={18} />
          }
        </button>
        {menuOpen && (
          <div ref={menuRef} className="terminal-input-mode-menu">
            <button
              className={`terminal-input-mode-option ${mode === "single" ? "active" : ""}`}
              onClick={() => switchMode("single")}
            >
              <CornerDownLeft size={14} />
              <span>单行输入</span>
            </button>
            <button
              className={`terminal-input-mode-option ${mode === "multi" ? "active" : ""}`}
              onClick={() => switchMode("multi")}
            >
              <Send size={14} />
              <span>多行输入</span>
            </button>
          </div>
        )}
      </div>
      <button
        className="terminal-input-toggle"
        onClick={onToggleShortcuts}
        aria-label={shortcutsOpen ? "Hide shortcuts" : "Show shortcuts"}
      >
        {shortcutsOpen
          ? <ChevronDown size={18} />
          : <ChevronUp size={18} />
        }
      </button>
    </div>
  );
}
