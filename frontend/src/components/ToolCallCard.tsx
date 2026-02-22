import { useState } from "react";

export interface ToolGroupData {
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  startTime: number;
  endTime?: number;
}

interface Props {
  data: ToolGroupData;
}

export default function ToolCallCard({ data }: Props) {
  const [expanded, setExpanded] = useState(false);
  const isRunning = data.endTime === undefined;
  const duration =
    data.endTime !== undefined ? data.endTime - data.startTime : undefined;

  return (
    <div
      className={`tool-card ${isRunning ? "running" : data.isError ? "error" : "success"}`}
    >
      <div className="tool-card-header" onClick={() => setExpanded((v) => !v)}>
        <span className="tool-card-status">
          {isRunning ? "\u21bb" : data.isError ? "\u2717" : "\u2713"}
        </span>
        <span className="tool-card-name">{data.toolName}</span>
        {!expanded && (
          <span className="tool-card-args-preview">
            {formatArgsPreview(data.arguments)}
          </span>
        )}
        <span className="tool-card-meta">
          {duration !== undefined && (
            <span className="tool-card-duration">
              {duration < 1000
                ? `${duration}ms`
                : `${(duration / 1000).toFixed(1)}s`}
            </span>
          )}
          <span className="tool-card-chevron">
            {expanded ? "\u25be" : "\u25b8"}
          </span>
        </span>
      </div>
      {expanded && (
        <div className="tool-card-body">
          <div className="tool-card-section">
            <div className="tool-card-label">Arguments</div>
            <pre className="tool-card-pre">
              {JSON.stringify(data.arguments, null, 2)}
            </pre>
          </div>
          {data.result !== undefined && (
            <div className="tool-card-section">
              <div
                className={`tool-card-label ${data.isError ? "error" : ""}`}
              >
                {data.isError ? "Error" : "Result"}
              </div>
              <pre className="tool-card-pre">{data.result}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function formatArgsPreview(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return "()";
  const preview = entries
    .map(([k, v]) => {
      const val =
        typeof v === "string"
          ? v.length > 30
            ? `"${v.slice(0, 27)}..."`
            : `"${v}"`
          : JSON.stringify(v);
      return `${k}=${val}`;
    })
    .join(", ");
  if (preview.length > 80) return `(${preview.slice(0, 77)}...)`;
  return `(${preview})`;
}
