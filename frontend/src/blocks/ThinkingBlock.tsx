import { useState } from "react";

export default function ThinkingBlock({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="thinking-block">
      <div
        className="thinking-header"
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="thinking-icon">
          {expanded ? "\u25be" : "\u25b8"}
        </span>
        <span>Thinking</span>
      </div>
      {expanded && <pre className="thinking-content">{content}</pre>}
    </div>
  );
}
