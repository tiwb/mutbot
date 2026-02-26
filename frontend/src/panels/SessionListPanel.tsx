import { useState, useEffect, useCallback, useRef } from "react";
import RpcMenu, { type MenuExecResult } from "../components/RpcMenu";
import { getSessionIcon } from "../components/SessionIcons";
import type { WorkspaceRpc } from "../lib/workspace-rpc";

interface Session {
  id: string;
  title: string;
  type: string;
  kind: string;
  icon: string;
  status: string;
}

interface Props {
  sessions: Session[];
  activeSessionId: string | null;
  rpc: WorkspaceRpc | null;
  onSelect: (id: string) => void;
  onModeChange?: (collapsed: boolean) => void;
  onCloseSession?: (id: string) => void;
  onDeleteSession?: (id: string) => void;
  onRenameSession?: (id: string, newTitle: string) => void;
  onChangeIcon?: (sessionId: string, position: { x: number; y: number }) => void;
}

const STORAGE_KEY = "mutbot-sidebar-collapsed";

export default function SessionListPanel({
  sessions,
  activeSessionId,
  rpc,
  onSelect,
  onModeChange,
  onRenameSession,
  onChangeIcon,
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

  // Inline rename state
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);

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

  // Focus rename input when it appears
  useEffect(() => {
    if (renamingId && renameInputRef.current) {
      renameInputRef.current.focus();
      renameInputRef.current.select();
    }
  }, [renamingId]);

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

  const startRename = useCallback((sessionId: string) => {
    const session = sessions.find((s) => s.id === sessionId);
    if (session) {
      setRenamingId(sessionId);
      setRenameValue(session.title);
    }
  }, [sessions]);

  const commitRename = useCallback(() => {
    if (renamingId && renameValue.trim()) {
      onRenameSession?.(renamingId, renameValue.trim());
    }
    setRenamingId(null);
    setRenameValue("");
  }, [renamingId, renameValue, onRenameSession]);

  const cancelRename = useCallback(() => {
    setRenamingId(null);
    setRenameValue("");
  }, []);

  // Handle client_action from RpcMenu
  const handleClientAction = useCallback((action: string, _data: Record<string, unknown>) => {
    if (!contextMenu) return;
    if (action === "start_rename") {
      startRename(contextMenu.sessionId);
    } else if (action === "change_icon") {
      onChangeIcon?.(contextMenu.sessionId, contextMenu.position);
    }
  }, [contextMenu, startRename, onChangeIcon]);

  // Handle menu.execute results from RpcMenu
  // 状态更新由 App.tsx 的 event handler 统一处理，无需额外操作
  const handleMenuResult = useCallback((_result: MenuExecResult) => {
    // no-op: broadcast event handler 统一更新状态
  }, []);

  // Resolve context menu session for RpcMenu context
  const contextSession = contextMenu
    ? sessions.find((s) => s.id === contextMenu.sessionId)
    : null;

  // Sort: active sessions first, then ended
  const sorted = [...sessions].sort((a, b) => {
    if (a.status === "active" && b.status !== "active") return -1;
    if (a.status !== "active" && b.status === "active") return 1;
    return 0;
  });

  // Compact mode: show all sessions as icons
  if (collapsed) {
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
          {sorted.map((s) => (
            <div
              key={s.id}
              className={`session-icon-item ${s.id === activeSessionId ? "active" : ""} ${s.status === "ended" ? "ended" : ""}`}
              onClick={() => onSelect(s.id)}
              onContextMenu={(e) => handleContextMenu(e, s.id)}
              title={`${s.title} (${s.status})`}
            >
              {getSessionIcon(s.kind, 24, "#cccccc", s.icon)}
            </div>
          ))}
        </div>
        {contextMenu && (
          <RpcMenu
            rpc={rpc}
            category="SessionList/Context"
            context={{
              session_id: contextSession?.id ?? "",
              session_type: contextSession?.type ?? "",
              session_status: contextSession?.status ?? "",
            }}
            position={contextMenu.position}
            onClose={closeContextMenu}
            onResult={handleMenuResult}
            onClientAction={handleClientAction}
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
              onClick={() => { if (renamingId !== s.id) onSelect(s.id); }}
              onContextMenu={(e) => handleContextMenu(e, s.id)}
              onDoubleClick={() => startRename(s.id)}
            >
              <span className="session-type-icon">
                {getSessionIcon(s.kind, 16, "currentColor", s.icon)}
              </span>
              {renamingId === s.id ? (
                <input
                  ref={renameInputRef}
                  className="session-rename-input"
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onBlur={commitRename}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitRename();
                    else if (e.key === "Escape") cancelRename();
                  }}
                  onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <span className="session-title">{s.title}</span>
              )}
              <span className={`session-status ${s.status}`}>{s.status}</span>
            </li>
          ))}
        </ul>
      </div>
      {contextMenu && (
        <RpcMenu
          rpc={rpc}
          category="SessionList/Context"
          context={{
            session_id: contextSession?.id ?? "",
            session_type: contextSession?.type ?? "",
            session_status: contextSession?.status ?? "",
          }}
          position={contextMenu.position}
          onClose={closeContextMenu}
          onResult={handleMenuResult}
          onClientAction={handleClientAction}
        />
      )}
    </div>
  );
}
