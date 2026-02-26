import { useCallback, useRef, useState } from "react";

interface Props {
  onSend: (text: string) => void;
  onCancel?: () => void;
  disabled?: boolean;
  isBusy?: boolean;
}

export default function ChatInput({ onSend, onCancel, disabled, isBusy }: Props) {
  const [text, setText] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setText("");
    textareaRef.current?.focus();
  }, [text, onSend]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  return (
    <div className="chat-input">
      <textarea
        ref={textareaRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Type a message... (Ctrl+Enter to send)"
        disabled={disabled}
        rows={3}
      />
      <div className="chat-input-buttons">
        <button onClick={handleSend} disabled={disabled || !text.trim()}>
          Send
        </button>
        <button
          className="stop-button"
          onClick={onCancel}
          disabled={disabled || !isBusy}
          title="Stop agent"
        >
          Stop
        </button>
      </div>
    </div>
  );
}
