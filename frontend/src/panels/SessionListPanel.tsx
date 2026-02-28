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

// 状态显示映射
function getStatusDisplay(status: string): { text: string; className: string } | null {
  if (!status) return null; // 空状态不显示
  const known: Record<string, { text: string; className: string }> = {
    running: { text: "Running", className: "status-running" },
    stopped: { text: "Stopped", className: "status-stopped" },
  };
  return known[status] ?? { text: status, className: "status-default" };
}

interface Props {
  sessions: Session[];
  activeSessionId: string | null;
  rpc: WorkspaceRpc | null;
  onSelect: (id: string) => void;
  onModeChange?: (collapsed: boolean) => void;
  onDeleteSessions?: (ids: string[]) => void;
  onRenameSession?: (id: string, newTitle: string) => void;
  onReorderSessions?: (sessionIds: string[]) => void;
  onChangeIcon?: (sessionId: string, position: { x: number; y: number }) => void;
  onHeaderAction?: (action: string, data: Record<string, unknown>) => void;
  onMenuResult?: (result: MenuExecResult) => void;
}

const STORAGE_KEY = "mutbot-sidebar-collapsed";

export default function SessionListPanel({
  sessions,
  activeSessionId,
  rpc,
  onSelect,
  onModeChange,
  onRenameSession,
  onReorderSessions,
  onChangeIcon,
  onHeaderAction,
  onMenuResult,
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

  // 空白区域右键菜单状态
  const [blankContextMenu, setBlankContextMenu] = useState<{
    position: { x: number; y: number };
  } | null>(null);

  // Inline rename state
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const renameInputRef = useRef<HTMLInputElement>(null);

  // Drag-and-drop state
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);

  // Multi-select state
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const lastClickedRef = useRef<string | null>(null);
  const listContainerRef = useRef<HTMLDivElement>(null);

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
    // If right-clicking on an unselected item, make it the only selection
    if (!selectedIds.has(sessionId)) {
      setSelectedIds(new Set([sessionId]));
    }
    setContextMenu({
      position: { x: e.clientX, y: e.clientY },
      sessionId,
    });
  }, [selectedIds]);

  const closeContextMenu = useCallback(() => {
    setContextMenu(null);
  }, []);

  // 空白区域右键菜单
  const handleBlankContextMenu = useCallback((e: React.MouseEvent) => {
    // 仅在点击列表空白区域时触发（非 session item）
    const target = e.target as HTMLElement;
    if (target.closest(".session-item") || target.closest(".session-icon-item")) return;
    e.preventDefault();
    setBlankContextMenu({ position: { x: e.clientX, y: e.clientY } });
  }, []);

  const closeBlankContextMenu = useCallback(() => {
    setBlankContextMenu(null);
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

  // Multi-select click handler
  const handleItemClick = useCallback((e: React.MouseEvent, sessionId: string) => {
    if (e.ctrlKey || e.metaKey) {
      // Ctrl+Click: toggle selection, don't activate panel
      setSelectedIds((prev) => {
        const next = new Set(prev);
        if (next.has(sessionId)) next.delete(sessionId);
        else next.add(sessionId);
        return next;
      });
      lastClickedRef.current = sessionId;
    } else if (e.shiftKey && lastClickedRef.current) {
      // Shift+Click: range selection
      const ids = sessions.map((s) => s.id);
      const anchorIdx = ids.indexOf(lastClickedRef.current);
      const targetIdx = ids.indexOf(sessionId);
      if (anchorIdx !== -1 && targetIdx !== -1) {
        const start = Math.min(anchorIdx, targetIdx);
        const end = Math.max(anchorIdx, targetIdx);
        setSelectedIds(new Set(ids.slice(start, end + 1)));
      }
    } else {
      // Normal click: single select + activate panel
      setSelectedIds(new Set([sessionId]));
      lastClickedRef.current = sessionId;
      onSelect(sessionId);
    }
  }, [sessions, onSelect]);

  // Clean up stale selections when sessions change
  useEffect(() => {
    setSelectedIds((prev) => {
      const validIds = new Set(sessions.map((s) => s.id));
      const next = new Set([...prev].filter((id) => validIds.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [sessions]);

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

  // --- Drag and drop ---
  const handleDragStart = useCallback((e: React.DragEvent, sessionId: string) => {
    setDragId(sessionId);
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", sessionId);
  }, []);

  const handleDragOver = useCallback((e: React.DragEvent, sessionId: string) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverId(sessionId);
  }, []);

  const handleDragLeave = useCallback(() => {
    setDragOverId(null);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent, targetId: string) => {
    e.preventDefault();
    const sourceId = dragId;
    setDragId(null);
    setDragOverId(null);
    if (!sourceId || sourceId === targetId) return;

    const reordered = [...sessions];
    const fromIdx = reordered.findIndex((s) => s.id === sourceId);
    if (fromIdx === -1) return;

    const [moved] = reordered.splice(fromIdx, 1);
    if (targetId === "__tail__") {
      // Drop onto tail zone: append to end
      reordered.push(moved!);
    } else {
      const toIdx = reordered.findIndex((s) => s.id === targetId);
      if (toIdx === -1) return;
      reordered.splice(toIdx, 0, moved!);
    }
    const newIds = reordered.map((s) => s.id);
    onReorderSessions?.(newIds);
    rpc?.call("workspace.reorder_sessions", { session_ids: newIds }).catch(() => {});
  }, [dragId, sessions, rpc, onReorderSessions]);

  const handleDragEnd = useCallback(() => {
    setDragId(null);
    setDragOverId(null);
  }, []);

  // Resolve context menu session for RpcMenu context
  const contextSession = contextMenu
    ? sessions.find((s) => s.id === contextMenu.sessionId)
    : null;
  // Multi-select: pass all selected IDs to context menu
  const contextSessionIds = contextMenu
    ? (selectedIds.size > 1 ? Array.from(selectedIds) : [contextMenu.sessionId])
    : [];

  // Sort: use original order (from workspace.sessions)
  const sorted = sessions;

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
              className={`session-icon-item ${s.id === activeSessionId ? "active" : ""} ${selectedIds.size > 1 && selectedIds.has(s.id) ? "selected" : ""} ${dragOverId === s.id && dragId !== s.id ? "drag-over" : ""} ${dragId === s.id ? "dragging" : ""}`}
              onClick={(e) => handleItemClick(e, s.id)}
              onContextMenu={(e) => handleContextMenu(e, s.id)}
              title={s.title}
              draggable
              onDragStart={(e) => handleDragStart(e, s.id)}
              onDragOver={(e) => handleDragOver(e, s.id)}
              onDragLeave={handleDragLeave}
              onDrop={(e) => handleDrop(e, s.id)}
              onDragEnd={handleDragEnd}
            >
              {getSessionIcon(s.kind, 24, "#cccccc", s.icon)}
            </div>
          ))}
          {dragId && (
            <div
              className={`session-icon-item drag-tail ${dragOverId === "__tail__" ? "drag-over" : ""}`}
              onDragOver={(e) => handleDragOver(e, "__tail__")}
              onDragLeave={handleDragLeave}
              onDrop={(e) => handleDrop(e, "__tail__")}
            />
          )}
        </div>
        {contextMenu && (
          <RpcMenu
            rpc={rpc}
            category="SessionList/Context"
            context={{
              session_id: contextSession?.id ?? "",
              session_ids: contextSessionIds,
              session_type: contextSession?.type ?? "",
              session_status: contextSession?.status ?? "",
            }}
            position={contextMenu.position}
            onClose={closeContextMenu}
            onResult={handleMenuResult}
            onClientAction={handleClientAction}
          />
        )}
        {blankContextMenu && (
          <RpcMenu
            rpc={rpc}
            category="SessionList/Blank"
            position={blankContextMenu.position}
            onClose={closeBlankContextMenu}
            onResult={onMenuResult}
          />
        )}
      </div>
    );
  }

  // Full mode
  return (
    <div className="session-list-container" ref={listContainerRef}>
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
        <RpcMenu
          rpc={rpc}
          category="SessionList/Header"
          trigger={
            <button className="sidebar-menu-btn" title="Menu">
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <rect x="2" y="3" width="12" height="1.5" rx="0.5" />
                <rect x="2" y="7.25" width="12" height="1.5" rx="0.5" />
                <rect x="2" y="11.5" width="12" height="1.5" rx="0.5" />
              </svg>
            </button>
          }
          onClientAction={onHeaderAction}
        />
      </div>
      <div className="session-list" onContextMenu={handleBlankContextMenu}>
        <ul>
          {sorted.map((s) => {
            const statusDisplay = getStatusDisplay(s.status);
            return (
            <li
              key={s.id}
              className={`session-item ${s.id === activeSessionId ? "active" : ""} ${selectedIds.size > 1 && selectedIds.has(s.id) ? "selected" : ""} ${dragOverId === s.id && dragId !== s.id ? "drag-over" : ""} ${dragId === s.id ? "dragging" : ""}`}
              onClick={(e) => { if (renamingId !== s.id) handleItemClick(e, s.id); }}
              onContextMenu={(e) => handleContextMenu(e, s.id)}
              onDoubleClick={() => startRename(s.id)}
              draggable={renamingId !== s.id}
              onDragStart={(e) => handleDragStart(e, s.id)}
              onDragOver={(e) => handleDragOver(e, s.id)}
              onDragLeave={handleDragLeave}
              onDrop={(e) => handleDrop(e, s.id)}
              onDragEnd={handleDragEnd}
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
              {statusDisplay && (
                <span className={`session-status ${statusDisplay.className}`}>{statusDisplay.text}</span>
              )}
            </li>
            );
          })}
        </ul>
        {dragId && (
          <div
            className={`session-item drag-tail ${dragOverId === "__tail__" ? "drag-over" : ""}`}
            onDragOver={(e) => handleDragOver(e, "__tail__")}
            onDragLeave={handleDragLeave}
            onDrop={(e) => handleDrop(e, "__tail__")}
          />
        )}
      </div>
      {contextMenu && (
        <RpcMenu
          rpc={rpc}
          category="SessionList/Context"
          context={{
            session_id: contextSession?.id ?? "",
            session_ids: contextSessionIds,
            session_type: contextSession?.type ?? "",
            session_status: contextSession?.status ?? "",
          }}
          position={contextMenu.position}
          onClose={closeContextMenu}
          onResult={handleMenuResult}
          onClientAction={handleClientAction}
        />
      )}
      {blankContextMenu && (
        <RpcMenu
          rpc={rpc}
          category="SessionList/Blank"
          position={blankContextMenu.position}
          onClose={closeBlankContextMenu}
          onResult={onMenuResult}
        />
      )}
    </div>
  );
}
