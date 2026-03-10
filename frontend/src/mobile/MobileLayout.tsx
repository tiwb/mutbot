import { useState, useCallback, useEffect, useRef, Suspense, lazy } from "react";
import MobileDrawer from "./MobileDrawer";
import AgentPanel from "../panels/AgentPanel";
import RpcMenu, { type MenuExecResult } from "../components/RpcMenu";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import type { Session } from "../lib/types";
import WelcomePage from "../components/WelcomePage";
import { getSessionIcon } from "../components/SessionIcons";

const TerminalPanel = lazy(() => import("../panels/TerminalPanel"));

interface Props {
  sessions: Session[];
  activeSessionId: string | null;
  workspaceId: string | null;
  rpc: WorkspaceRpc | null;
  connected: boolean;
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
  connected,
  onSelectSession,
  onCreateSession,
  onDeleteSessions,
  onRenameSession,
  onHeaderAction,
  onChangeIcon,
  onMenuResult,
}: Props) {
  const [drawerOpen, setDrawerOpen] = useState(false);

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
          {connected ? (
            <span className="mobile-status-dot connected" />
          ) : (
            <span className="mobile-status-dot disconnected" />
          )}
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
          <Suspense fallback={<div className="panel-loading">Loading...</div>}>
            <TerminalPanel
              sessionId={activeSession.id}
              terminalId={activeSession.config?.terminal_id as string | undefined}
              workspaceId={workspaceId ?? ""}
              rpc={rpc}
            />
          </Suspense>
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
        connected={connected}
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
