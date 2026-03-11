import { useCallback, useRef, useState } from "react";

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
  const inputRef = useRef<HTMLInputElement>(null);

  const handleSend = useCallback(() => {
    onSend(value + "\r");
    setValue("");
    inputRef.current?.focus();
  }, [value, onSend]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  return (
    <div className="terminal-input-bar">
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
      <button
        className="terminal-input-enter"
        onClick={handleSend}
        aria-label="Send"
      >
        ↵
      </button>
      <button
        className="terminal-input-toggle"
        onClick={onToggleShortcuts}
        aria-label={shortcutsOpen ? "Hide shortcuts" : "Show shortcuts"}
      >
        {shortcutsOpen ? "▼" : "▲"}
      </button>
    </div>
  );
}
