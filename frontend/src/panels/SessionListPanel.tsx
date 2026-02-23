import { useState, useEffect, useCallback } from "react";
import ContextMenu, { type ContextMenuItem } from "../components/ContextMenu";

interface Session {
  id: string;
  title: string;
  type: string;
  status: string;
}

interface Props {
  sessions: Session[];
  activeSessionId: string | null;
  onSelect: (id: string) => void;
  onModeChange?: (collapsed: boolean) => void;
  onCloseSession?: (id: string) => void;
  onDeleteSession?: (id: string) => void;
}

// ---------- Codicon-style SVG icons ----------

function ChatIcon({ size = 24, color = "currentColor" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  );
}

function TerminalIcon({ size = 24, color = "currentColor" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  );
}

function FileIcon({ size = 24, color = "currentColor" }: { size?: number; color?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}

function getSessionIcon(type: string, size = 24, color = "currentColor") {
  switch (type) {
    case "agent": return <ChatIcon size={size} color={color} />;
    case "terminal": return <TerminalIcon size={size} color={color} />;
    case "document": return <FileIcon size={size} color={color} />;
    default: return <ChatIcon size={size} color={color} />;
  }
}

const STORAGE_KEY = "mutbot-sidebar-collapsed";

export default function SessionListPanel({
  sessions,
  activeSessionId,
  onSelect,
  onModeChange,
  onCloseSession,
  onDeleteSession,
}: Props) {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === "true";
    } catch {
      return false;
    }
  });

  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
    sessionId: string;
  } | null>(null);

  const toggleMode = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(STORAGE_KEY, String(next));
      } catch { /* ignore */ }
      return next;
    });
  }, []);

  useEffect(() => {
    onModeChange?.(collapsed);
  }, [collapsed, onModeChange]);

  const handleContextMenu = useCallback((e: React.MouseEvent, sessionId: string) => {
    e.preventDefault();
    setContextMenu({
      position: { x: e.clientX, y: e.clientY },
      sessionId,
    });
  }, []);

  const closeContextMenu = useCallback(() => {
    setContextMenu(null);
  }, []);

  // Build context menu items for a session
  const getContextMenuItems = useCallback((sessionId: string): ContextMenuItem[] => {
    const session = sessions.find((s) => s.id === sessionId);
    if (!session) return [];
    return [
      {
        label: "Close Session",
        disabled: session.status === "ended",
        onClick: () => onCloseSession?.(sessionId),
      },
      { label: "", separator: true },
      {
        label: "Delete",
        onClick: () => onDeleteSession?.(sessionId),
      },
    ];
  }, [sessions, onCloseSession, onDeleteSession]);

  // Sort: active sessions first, then ended
  const sorted = [...sessions].sort((a, b) => {
    if (a.status === "active" && b.status !== "active") return -1;
    if (a.status !== "active" && b.status === "active") return 1;
    return 0;
  });

  // Compact mode: only show active session(s)
  if (collapsed) {
    const activeSessions = sorted.filter(
      (s) => s.id === activeSessionId,
    );

    return (
      <div className="session-list-container compact">
        <div className="sidebar-header compact">
          <button
            className="sidebar-toggle-btn"
            onClick={toggleMode}
            title="Expand sidebar"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
              <rect x="2" y="3" width="12" height="1.5" rx="0.5" />
              <rect x="2" y="7.25" width="12" height="1.5" rx="0.5" />
              <rect x="2" y="11.5" width="12" height="1.5" rx="0.5" />
            </svg>
          </button>
        </div>
        <div className="session-list compact">
          {activeSessions.map((s) => (
            <div
              key={s.id}
              className="session-icon-item"
              onClick={() => onSelect(s.id)}
              onContextMenu={(e) => handleContextMenu(e, s.id)}
              title={`${s.title} (${s.status})`}
            >
              {getSessionIcon(s.type, 24, "#cccccc")}
            </div>
          ))}
        </div>
        {contextMenu && (
          <ContextMenu
            items={getContextMenuItems(contextMenu.sessionId)}
            position={contextMenu.position}
            onClose={closeContextMenu}
          />
        )}
      </div>
    );
  }

  // Full mode
  return (
    <div className="session-list-container">
      <div className="sidebar-header">
        <button
          className="sidebar-toggle-btn"
          onClick={toggleMode}
          title="Collapse sidebar"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <path d="M11 2L5 8l6 6V2z" />
          </svg>
        </button>
        <h1>Sessions</h1>
      </div>
      <div className="session-list">
        <ul>
          {sorted.map((s) => (
            <li
              key={s.id}
              className={`session-item ${s.id === activeSessionId ? "active" : ""} ${s.status === "ended" ? "ended" : ""}`}
              onClick={() => onSelect(s.id)}
              onContextMenu={(e) => handleContextMenu(e, s.id)}
            >
              <span className="session-type-icon">
                {getSessionIcon(s.type, 16, "currentColor")}
              </span>
              <span className="session-title">{s.title}</span>
              <span className={`session-status ${s.status}`}>{s.status}</span>
            </li>
          ))}
        </ul>
      </div>
      {contextMenu && (
        <ContextMenu
          items={getContextMenuItems(contextMenu.sessionId)}
          position={contextMenu.position}
          onClose={closeContextMenu}
        />
      )}
    </div>
  );
}
