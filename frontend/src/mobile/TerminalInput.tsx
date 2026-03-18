import { useCallback, useEffect, useRef, useState } from "react";
import { CornerDownLeft, ChevronUp, ChevronDown, Send, History } from "lucide-react";
import { rlog } from "../lib/remote-log";

const INPUT_MODE_KEY = "mutbot-terminal-input-mode";
type InputMode = "single" | "multi";

function loadInputMode(): InputMode {
  try {
    const v = localStorage.getItem(INPUT_MODE_KEY);
    if (v === "multi") return "multi";
  } catch { /* ignore */ }
  return "single";
}

// --- Input History (localStorage) ---

const HISTORY_KEY = "terminalInput.history";
const HISTORY_MAX = 50;

interface HistoryEntry {
  text: string;
  timestamp: number;
}

function loadHistory(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    if (raw) return JSON.parse(raw) as HistoryEntry[];
  } catch { /* ignore */ }
  return [];
}

function saveHistory(entries: HistoryEntry[]): void {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(entries));
  } catch { /* ignore */ }
}

function pushHistory(text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  let entries = loadHistory();
  entries = entries.filter((e) => e.text !== trimmed);
  entries.unshift({ text: trimmed, timestamp: Date.now() });
  if (entries.length > HISTORY_MAX) entries.length = HISTORY_MAX;
  saveHistory(entries);
}

function removeHistoryEntry(index: number): HistoryEntry[] {
  const entries = loadHistory();
  entries.splice(index, 1);
  saveHistory(entries);
  return entries;
}

function clearHistory(): HistoryEntry[] {
  saveHistory([]);
  return [];
}

// --- Component ---

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
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyEntries, setHistoryEntries] = useState<HistoryEntry[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressFired = useRef(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const historyRef = useRef<HTMLDivElement>(null);

  // IME composing guard
  const isComposing = useRef(false);

  const handleSend = useCallback(() => {
    if (isComposing.current) return;
    const text = value;
    if (!text) return;
    rlog.info("terminal-send", { len: text.length, head: text.slice(0, 50) });
    pushHistory(text);
    onSend(text + "\r");
    setValue("");
    textareaRef.current?.focus();
  }, [value, onSend]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (isComposing.current) return;
      if (e.key === "Enter") {
        if (mode === "single") {
          e.preventDefault();
          handleSend();
        }
      }
    },
    [mode, handleSend],
  );

  // Auto-grow textarea
  const autoGrow = useCallback((ta: HTMLTextAreaElement) => {
    ta.style.height = "0";
    ta.style.height = ta.scrollHeight + "px";
  }, []);

  const handleChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    autoGrow(e.target);
  }, [autoGrow]);

  // Reset textarea height when value is cleared (after send)
  useEffect(() => {
    if (textareaRef.current && value === "") {
      textareaRef.current.style.height = "auto";
    }
  }, [value]);

  // Switch mode
  const switchMode = useCallback((newMode: InputMode) => {
    setMode(newMode);
    setMenuOpen(false);
    try { localStorage.setItem(INPUT_MODE_KEY, newMode); } catch { /* ignore */ }
  }, []);

  // Long press on send button → mode menu
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

  // Open history panel (from mode menu)
  const handleOpenHistory = useCallback(() => {
    setMenuOpen(false);
    const entries = loadHistory();
    setHistoryEntries(entries);
    setHistoryOpen(true);
  }, []);

  // History panel actions
  const handleHistorySelect = useCallback((text: string) => {
    setValue(text);
    setHistoryOpen(false);
    setTimeout(() => {
      if (textareaRef.current) {
        autoGrow(textareaRef.current);
        textareaRef.current.focus();
      }
    }, 0);
  }, [autoGrow]);

  const handleHistoryDelete = useCallback((index: number) => {
    navigator.vibrate?.(30);
    const updated = removeHistoryEntry(index);
    setHistoryEntries(updated);
    if (updated.length === 0) setHistoryOpen(false);
  }, []);

  const handleHistoryClear = useCallback(() => {
    clearHistory();
    setHistoryEntries([]);
    setHistoryOpen(false);
  }, []);

  // Close menus on outside click
  useEffect(() => {
    if (!menuOpen && !historyOpen) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      if (menuOpen && menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
      if (historyOpen && historyRef.current && !historyRef.current.contains(e.target as Node)) {
        setHistoryOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [menuOpen, historyOpen]);

  const placeholder = mode === "single"
    ? "Enter to send"
    : "Tap button to send";

  return (
    <div className="terminal-input-bar">
      <div className="terminal-input-textarea-wrapper">
        <textarea
          ref={textareaRef}
          className="terminal-input-field terminal-input-textarea"
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onCompositionStart={() => { isComposing.current = true; }}
          onCompositionEnd={() => { isComposing.current = false; }}
          placeholder={placeholder}
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          rows={1}
        />
        {historyOpen && historyEntries.length > 0 && (
          <div ref={historyRef} className="terminal-input-history-panel">
            <div className="terminal-input-history-header">
              <span>Input History</span>
              <button
                className="terminal-input-history-clear"
                onClick={handleHistoryClear}
              >
                Clear
              </button>
            </div>
            <div className="terminal-input-history-list">
              {historyEntries.map((entry, i) => (
                <HistoryItem
                  key={entry.timestamp}
                  entry={entry}
                  onSelect={() => handleHistorySelect(entry.text)}
                  onDelete={() => handleHistoryDelete(i)}
                />
              ))}
            </div>
          </div>
        )}
      </div>
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
              <span>Single Line</span>
            </button>
            <button
              className={`terminal-input-mode-option ${mode === "multi" ? "active" : ""}`}
              onClick={() => switchMode("multi")}
            >
              <Send size={14} />
              <span>Multi Line</span>
            </button>
            <div className="terminal-input-mode-divider" />
            <button
              className="terminal-input-mode-option"
              onClick={handleOpenHistory}
            >
              <History size={14} />
              <span>Input History</span>
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

// --- HistoryItem sub-component (long press to delete) ---

function HistoryItem({
  entry,
  onSelect,
  onDelete,
}: {
  entry: HistoryEntry;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firedRef = useRef(false);

  const handlePointerDown = useCallback(() => {
    firedRef.current = false;
    timerRef.current = setTimeout(() => {
      firedRef.current = true;
      onDelete();
    }, 500);
  }, [onDelete]);

  const handlePointerUp = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const handleClick = useCallback(() => {
    if (firedRef.current) {
      firedRef.current = false;
      return;
    }
    onSelect();
  }, [onSelect]);

  const display = entry.text.length > 60
    ? entry.text.slice(0, 57) + "..."
    : entry.text;

  return (
    <button
      className="terminal-input-history-item"
      onClick={handleClick}
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerUp}
      onPointerCancel={handlePointerUp}
    >
      {display}
    </button>
  );
}
