import { useState, useCallback, useEffect, useRef, Suspense, lazy } from "react";
import MobileDrawer from "./MobileDrawer";
import ShortcutGrid, {
  loadShortcutConfig, saveShortcutConfig, defaultConfig, resizeGrid,
  type ShortcutConfig,
} from "./ShortcutGrid";
import ShortcutEditDialog from "./ShortcutEditDialog";
import TerminalInput from "./TerminalInput";
import AgentPanel from "../panels/AgentPanel";
import RpcMenu, { type MenuExecResult } from "../components/RpcMenu";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import type { Session } from "../lib/types";
import type { TerminalPanelHandle } from "../panels/TerminalPanel";
import WelcomePage from "../components/WelcomePage";
import { getSessionIcon } from "../components/SessionIcons";

const TerminalPanel = lazy(() => import("../panels/TerminalPanel"));

interface Props {
  sessions: Session[];
  activeSessionId: string | null;
  workspaceId: string | null;
  rpc: WorkspaceRpc | null;
  connectionStatus: "connected" | "connecting" | "disconnected";
  onSelectSession: (id: string) => void;
  onCreateSession: (type: string) => void;
  onDeleteSessions: (ids: string[]) => void;
  onRenameSession: (id: string, newTitle: string) => void;
  onHeaderAction?: (action: string, data: Record<string, unknown>) => void;
  onChangeIcon?: (sessionId: string, position: { x: number; y: number }) => void;
  onMenuResult?: (result: MenuExecResult) => void;
}

export default function MobileLayout({
  sessions,
  activeSessionId,
  workspaceId,
  rpc,
  connectionStatus,
  onSelectSession,
  onCreateSession,
  onDeleteSessions,
  onRenameSession,
  onHeaderAction,
  onChangeIcon,
  onMenuResult,
}: Props) {
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Mobile terminal state
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [shortcutConfig, setShortcutConfig] = useState<ShortcutConfig>(loadShortcutConfig);
  const [shortcutEditing, setShortcutEditing] = useState(false);
  const [editingSlotIndex, setEditingSlotIndex] = useState<number | null>(null);
  const [settingsMenuOpen, setSettingsMenuOpen] = useState(false);
  const [gridSizeDialogOpen, setGridSizeDialogOpen] = useState(false);
  const settingsMenuRef = useRef<HTMLDivElement>(null);
  const termWrapperRef = useRef<HTMLDivElement>(null);
  const termPanelRef = useRef<TerminalPanelHandle>(null);

  const STORAGE_KEY = "mutbot-mobile-active-session";

  // Restore or auto-select session on refresh
  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      let restored = false;
      try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved && sessions.some((s) => s.id === saved)) {
          onSelectSession(saved);
          restored = true;
        }
      } catch { /* ignore */ }
      if (!restored) {
        onSelectSession(sessions[0]!.id);
      }
    }
  }, [activeSessionId, sessions, onSelectSession]);

  // Persist active session id
  useEffect(() => {
    if (activeSessionId) {
      try { localStorage.setItem(STORAGE_KEY, activeSessionId); } catch { /* ignore */ }
    }
  }, [activeSessionId]);

  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? null;

  const handleSelectSession = useCallback(
    (id: string) => {
      onSelectSession(id);
      setDrawerOpen(false);
    },
    [onSelectSession],
  );

  // --- Long-press / context menu for top bar session tabs ---
  const [tabContextMenu, setTabContextMenu] = useState<{
    sessionId: string;
    position: { x: number; y: number };
  } | null>(null);

  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const touchStartPos = useRef<{ x: number; y: number } | null>(null);
  const longPressFired = useRef(false);

  const findSessionId = (target: EventTarget): string | null => {
    const el = (target as HTMLElement).closest?.("[data-session-id]");
    return el?.getAttribute("data-session-id") ?? null;
  };

  const openTabMenu = useCallback(
    (sessionId: string, x: number, y: number) => {
      onSelectSession(sessionId);
      setTabContextMenu({ sessionId, position: { x, y } });
    },
    [onSelectSession],
  );

  const handleTabsTouchStart = useCallback(
    (e: React.TouchEvent) => {
      const sessionId = findSessionId(e.target);
      if (!sessionId) return;
      const touch = e.touches[0]!;
      touchStartPos.current = { x: touch.clientX, y: touch.clientY };
      longPressFired.current = false;
      longPressTimer.current = setTimeout(() => {
        longPressFired.current = true;
        navigator.vibrate?.(50);
        openTabMenu(sessionId, touch.clientX, touch.clientY);
      }, 300);
    },
    [openTabMenu],
  );

  const handleTabsTouchMove = useCallback((e: React.TouchEvent) => {
    if (!touchStartPos.current || !longPressTimer.current) return;
    const touch = e.touches[0]!;
    const dx = touch.clientX - touchStartPos.current.x;
    const dy = touch.clientY - touchStartPos.current.y;
    if (dx * dx + dy * dy > 100) { // 10px threshold
      clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  }, []);

  const handleTabsTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      if (longPressTimer.current) {
        clearTimeout(longPressTimer.current);
        longPressTimer.current = null;
      }
      if (longPressFired.current) {
        // Long press already handled, suppress click
        e.preventDefault();
        return;
      }
      // Short tap — switch tab
      const sessionId = findSessionId(e.target);
      if (sessionId) {
        onSelectSession(sessionId);
      }
    },
    [onSelectSession],
  );

  const handleTabContextMenu = useCallback(
    (e: React.MouseEvent) => {
      const sessionId = findSessionId(e.target);
      if (!sessionId) return;
      e.preventDefault();
      openTabMenu(sessionId, e.clientX, e.clientY);
    },
    [openTabMenu],
  );

  const handleTabMenuClientAction = useCallback(
    (action: string, _data: Record<string, unknown>) => {
      if (!tabContextMenu) return;
      if (action === "start_rename") {
        const session = sessions.find((s) => s.id === tabContextMenu.sessionId);
        const newTitle = prompt("Rename session", session?.title ?? "");
        if (newTitle?.trim()) {
          onRenameSession(tabContextMenu.sessionId, newTitle.trim());
        }
      } else if (action === "change_icon") {
        onChangeIcon?.(tabContextMenu.sessionId, tabContextMenu.position);
      }
    },
    [tabContextMenu, sessions, onRenameSession, onChangeIcon],
  );

  // Resolve context session for RpcMenu
  const tabContextSession = tabContextMenu
    ? sessions.find((s) => s.id === tabContextMenu.sessionId)
    : null;

  // Send shortcut key sequence to terminal
  const handleShortcutKey = useCallback((sequence: string) => {
    termPanelRef.current?.writeInput(sequence);
  }, []);

  // Send text from input bar to terminal
  const handleTerminalInput = useCallback((text: string) => {
    termPanelRef.current?.writeInput(text);
  }, []);

  // Toggle shortcuts panel
  const handleToggleShortcuts = useCallback(() => {
    setShortcutsOpen((v) => !v);
  }, []);

  // Shortcut editing — ⚙ button shows settings menu (non-edit) or saves (edit)
  const handleSettingsClick = useCallback(() => {
    setSettingsMenuOpen(true);
  }, []);

  const handleSaveClick = useCallback(() => {
    setShortcutEditing(false);
    setEditingSlotIndex(null);
    saveShortcutConfig(shortcutConfig);
  }, [shortcutConfig]);

  // Close settings menu on outside click
  useEffect(() => {
    if (!settingsMenuOpen) return;
    const handler = (e: MouseEvent | TouchEvent) => {
      if (settingsMenuRef.current && !settingsMenuRef.current.contains(e.target as Node)) {
        setSettingsMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    document.addEventListener("touchstart", handler);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("touchstart", handler);
    };
  }, [settingsMenuOpen]);

  const handleStartEdit = useCallback(() => {
    setSettingsMenuOpen(false);
    setShortcutEditing(true);
    setShortcutsOpen(true);
  }, []);

  const handleResetDefault = useCallback(() => {
    setSettingsMenuOpen(false);
    if (confirm("确定恢复默认快捷键布局？")) {
      const cfg = defaultConfig();
      setShortcutConfig(cfg);
      saveShortcutConfig(cfg);
    }
  }, []);

  const handleOpenGridSize = useCallback(() => {
    setSettingsMenuOpen(false);
    setGridSizeDialogOpen(true);
  }, []);

  const handleGridSizeChange = useCallback((newRows: number, newCols: number) => {
    setShortcutConfig((prev) => {
      const cfg = resizeGrid(prev, newRows, newCols);
      saveShortcutConfig(cfg);
      return cfg;
    });
    setGridSizeDialogOpen(false);
  }, []);

  const handleEditSlot = useCallback((index: number) => {
    setEditingSlotIndex(index);
  }, []);

  const handleSwapSlots = useCallback((a: number, b: number) => {
    setShortcutConfig((prev) => {
      const next = { ...prev, slots: [...prev.slots] };
      const tmp = next.slots[a] ?? null;
      next.slots[a] = next.slots[b] ?? null;
      next.slots[b] = tmp;
      return next;
    });
  }, []);

  const handleEditSlotSelect = useCallback((slot: import("./ShortcutGrid").ShortcutSlot | null) => {
    if (editingSlotIndex === null) return;
    setShortcutConfig((prev) => {
      const next = { ...prev, slots: [...prev.slots] };
      next.slots[editingSlotIndex] = slot;
      return next;
    });
    setEditingSlotIndex(null);
  }, [editingSlotIndex]);

  return (
    <div className="mobile-root">
      {/* Top bar */}
      <div className="mobile-topbar">
        <button
          className="mobile-hamburger"
          onClick={() => setDrawerOpen((v) => !v)}
          aria-label="Toggle menu"
        >
          <svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor">
            <rect y="3" width="20" height="2" rx="1" />
            <rect y="9" width="20" height="2" rx="1" />
            <rect y="15" width="20" height="2" rx="1" />
          </svg>
        </button>
        {sessions.length > 0 ? (
          <div
            className="mobile-session-tabs"
            onTouchStart={handleTabsTouchStart}
            onTouchMove={handleTabsTouchMove}
            onTouchEnd={handleTabsTouchEnd}
            onTouchCancel={handleTabsTouchEnd}
            onContextMenu={handleTabContextMenu}
          >
            {sessions.map((s) => {
              const isActive = s.id === activeSessionId;
              return (
                <button
                  key={s.id}
                  className={`mobile-session-tab ${isActive ? "active" : ""}`}
                  data-session-id={s.id}
                  onClick={() => onSelectSession(s.id)}
                  title={s.title || "Untitled"}
                >
                  {getSessionIcon(s.kind, 16, isActive ? "#cccccc" : "#858585", s.icon)}
                  {isActive && (
                    <span className="mobile-session-tab-name">
                      {s.title || "Untitled"}
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        ) : (
          <div className="mobile-topbar-title">
            <span>MutBot</span>
          </div>
        )}
        <div className="mobile-topbar-status">
          <span className={`mobile-status-dot ${connectionStatus}`} />
        </div>
      </div>

      {/* Full-screen panel */}
      <div className="mobile-panel-container">
        {activeSession && activeSession.kind === "agent" ? (
          <AgentPanel
            sessionId={activeSession.id}
            rpc={rpc}
            onSessionLink={onSelectSession}
          />
        ) : activeSession && activeSession.kind === "terminal" ? (
          <div
            ref={termWrapperRef}
            className="mobile-terminal-wrapper"
          >
            <Suspense fallback={<div className="panel-loading">Loading...</div>}>
              <TerminalPanel
                ref={termPanelRef}
                sessionId={activeSession.id}
                terminalId={activeSession.config?.terminal_id as string | undefined}
                workspaceId={workspaceId ?? ""}
                rpc={rpc}
              />
            </Suspense>
            <TerminalInput
              onSend={handleTerminalInput}
              shortcutsOpen={shortcutsOpen}
              onToggleShortcuts={handleToggleShortcuts}
            />
            {shortcutsOpen && (
              <div className="mobile-terminal-input-panel">
                <ShortcutGrid
                  layout={shortcutConfig.slots}
                  rows={shortcutConfig.rows}
                  cols={shortcutConfig.cols}
                  editing={shortcutEditing}
                  onKey={handleShortcutKey}
                  onEditSlot={handleEditSlot}
                  onSettingsClick={handleSettingsClick}
                  onSaveClick={handleSaveClick}
                  onSwapSlots={handleSwapSlots}
                />
                {settingsMenuOpen && (
                  <div ref={settingsMenuRef} className="shortcut-settings-menu">
                    <button className="shortcut-settings-option" onClick={handleStartEdit}>
                      编辑快捷键
                    </button>
                    <button className="shortcut-settings-option" onClick={handleOpenGridSize}>
                      网格大小
                    </button>
                    <button className="shortcut-settings-option" onClick={handleResetDefault}>
                      恢复默认
                    </button>
                  </div>
                )}
              </div>
            )}
            {editingSlotIndex !== null && (
              <ShortcutEditDialog
                current={shortcutConfig.slots[editingSlotIndex] ?? null}
                onSelect={handleEditSlotSelect}
                onClose={() => setEditingSlotIndex(null)}
              />
            )}
            {gridSizeDialogOpen && (
              <GridSizeDialog
                rows={shortcutConfig.rows}
                cols={shortcutConfig.cols}
                onConfirm={handleGridSizeChange}
                onClose={() => setGridSizeDialogOpen(false)}
              />
            )}
          </div>
        ) : activeSession ? (
          <div className="mobile-unsupported-panel">
            <p>该面板类型暂不支持移动端显示</p>
          </div>
        ) : (
          <WelcomePage rpc={rpc} onCreateSession={onCreateSession} />
        )}
      </div>

      {/* Drawer */}
      <MobileDrawer
        open={drawerOpen}
        sessions={sessions}
        activeSessionId={activeSessionId}
        rpc={rpc}
        connectionStatus={connectionStatus}
        onSelect={handleSelectSession}
        onClose={() => setDrawerOpen(false)}
        onCreateSession={onCreateSession}
        onDeleteSessions={onDeleteSessions}
        onRenameSession={onRenameSession}
        onHeaderAction={onHeaderAction}
        onChangeIcon={onChangeIcon}
        onMenuResult={onMenuResult}
      />

      {/* Top bar tab context menu (long-press / right-click) */}
      {tabContextMenu && (
        <RpcMenu
          rpc={rpc}
          category="SessionList/Context"
          context={{
            session_id: tabContextSession?.id ?? "",
            session_ids: [tabContextMenu.sessionId],
            session_type: tabContextSession?.type ?? "",
            session_status: tabContextSession?.status ?? "",
          }}
          position={tabContextMenu.position}
          onClose={() => setTabContextMenu(null)}
          onResult={onMenuResult}
          onClientAction={handleTabMenuClientAction}
        />
      )}
    </div>
  );
}

/** Grid size configuration dialog. */
function GridSizeDialog({ rows, cols, onConfirm, onClose }: {
  rows: number;
  cols: number;
  onConfirm: (rows: number, cols: number) => void;
  onClose: () => void;
}) {
  const [r, setR] = useState(rows);
  const [c, setC] = useState(cols);

  return (
    <div className="shortcut-edit-overlay" onClick={onClose}>
      <div className="shortcut-edit-dialog grid-size-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="shortcut-edit-header">
          <span>网格大小</span>
        </div>
        <div className="grid-size-controls">
          <label>
            <span>行数</span>
            <div className="grid-size-stepper">
              <button onClick={() => setR((v) => Math.max(1, v - 1))} disabled={r <= 1}>−</button>
              <span>{r}</span>
              <button onClick={() => setR((v) => v + 1)}>+</button>
            </div>
          </label>
          <label>
            <span>列数</span>
            <div className="grid-size-stepper">
              <button onClick={() => setC((v) => Math.max(1, v - 1))} disabled={c <= 1}>−</button>
              <span>{c}</span>
              <button onClick={() => setC((v) => v + 1)}>+</button>
            </div>
          </label>
        </div>
        <div className="shortcut-edit-footer">
          <button onClick={onClose}>取消</button>
          <button className="primary" onClick={() => onConfirm(r, c)}>确定</button>
        </div>
      </div>
    </div>
  );
}
