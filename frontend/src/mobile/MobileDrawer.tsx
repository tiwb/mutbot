import { useCallback } from "react";
import SessionListPanel from "../panels/SessionListPanel";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import type { Session } from "../lib/types";
import type { MenuExecResult } from "../components/RpcMenu";

interface Props {
  open: boolean;
  sessions: Session[];
  activeSessionId: string | null;
  rpc: WorkspaceRpc | null;
  connectionStatus: "connected" | "connecting" | "disconnected";
  onSelect: (id: string) => void;
  onClose: () => void;
  onCreateSession: (type: string) => void;
  onDeleteSessions: (ids: string[]) => void;
  onRenameSession: (id: string, newTitle: string) => void;
  onHeaderAction?: (action: string, data: Record<string, unknown>) => void;
  onChangeIcon?: (sessionId: string, position: { x: number; y: number }) => void;
  onMenuResult?: (result: MenuExecResult) => void;
}

export default function MobileDrawer({
  open,
  sessions,
  activeSessionId,
  rpc,
  connectionStatus,
  onSelect,
  onClose,
  onDeleteSessions,
  onRenameSession,
  onHeaderAction,
  onChangeIcon,
  onMenuResult,
}: Props) {
  const handleOverlayClick = useCallback(
    (e: React.MouseEvent) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose],
  );

  return (
    <div
      className={`mobile-drawer-overlay ${open ? "open" : ""}`}
      onClick={handleOverlayClick}
    >
      <div className={`mobile-drawer ${open ? "open" : ""}`}>
        <SessionListPanel
          sessions={sessions}
          activeSessionId={activeSessionId}
          rpc={rpc}
          connectionStatus={connectionStatus}
          onSelect={onSelect}
          onDeleteSessions={onDeleteSessions}
          onRenameSession={onRenameSession}
          onHeaderAction={onHeaderAction}
          onChangeIcon={onChangeIcon}
          onMenuResult={onMenuResult}
          forceExpanded
          onToggleOverride={onClose}
        />
      </div>
    </div>
  );
}
