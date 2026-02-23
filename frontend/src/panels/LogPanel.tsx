import { useCallback, useEffect, useRef, useState } from "react";
import { fetchLogs } from "../lib/api";

interface LogEntry {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
}

const MAX_ENTRIES = 2000;
const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;

export default function LogPanel() {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [levelFilter, setLevelFilter] = useState<string>("DEBUG");
  const [search, setSearch] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const listRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // Load initial entries
  useEffect(() => {
    fetchLogs("", "DEBUG", 200).then((data: { entries: LogEntry[] }) => {
      setEntries(data.entries.reverse());
    });
  }, []);

  // WebSocket for real-time streaming
  useEffect(() => {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${location.host}/ws/logs`;
    const ws = new WebSocket(url);

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as { type: string } & LogEntry;
        if (data.type === "log") {
          setEntries((prev) => {
            const next = [...prev, data];
            return next.length > MAX_ENTRIES ? next.slice(-MAX_ENTRIES) : next;
          });
        }
      } catch {
        // ignore parse errors
      }
    };

    wsRef.current = ws;
    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, []);

  // Auto-scroll
  useEffect(() => {
    if (autoScroll && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, [entries, autoScroll]);

  const handleClear = useCallback(() => {
    setEntries([]);
  }, []);

  const levelIndex = LEVELS.indexOf(levelFilter as (typeof LEVELS)[number]);
  const filtered = entries.filter((e) => {
    const eLevelIdx = LEVELS.indexOf(e.level as (typeof LEVELS)[number]);
    if (eLevelIdx < levelIndex) return false;
    if (search && !e.message.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  return (
    <div className="log-panel">
      <div className="log-toolbar">
        <select value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)}>
          {LEVELS.map((l) => (
            <option key={l} value={l}>{l}</option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Search..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          className={autoScroll ? "active" : ""}
          onClick={() => setAutoScroll((v) => !v)}
          title="Auto-scroll"
        >
          Auto
        </button>
        <button onClick={handleClear}>Clear</button>
      </div>
      <div className="log-entries" ref={listRef}>
        {filtered.map((e, i) => (
          <div key={i} className="log-entry">
            <span className="log-ts">{e.timestamp} </span>
            <span className={`log-level ${e.level}`}>{e.level.padEnd(8)}</span>
            <span className="log-logger">{e.logger} - </span>
            <span className="log-msg">{e.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
