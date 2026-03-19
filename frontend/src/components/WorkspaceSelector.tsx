import { useEffect, useRef, useState } from "react";
import type { AppRpc } from "../lib/app-rpc";
import type { Workspace } from "../lib/types";
import RpcMenu, { type MenuExecResult } from "./RpcMenu";
import { useMobileDetect } from "../lib/useMobileDetect";

const MAX_VISIBLE = 5;

interface WorkspaceSelectorProps {
  workspaces: Workspace[];
  appRpc: AppRpc | null;
  onSelect: (ws: Workspace) => void;
  onNewWorkspace: () => void;
  onRemoved: (wsId: string) => void;
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// WorkspaceSearchDialog — 全量工作区搜索弹窗
// ---------------------------------------------------------------------------

function WorkspaceSearchDialog({
  workspaces,
  appRpc,
  onSelect,
  onClose,
  onRemoved,
}: {
  workspaces: Workspace[];
  appRpc: AppRpc | null;
  onSelect: (ws: Workspace) => void;
  onClose: () => void;
  onRemoved: (wsId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
    ws: Workspace;
  } | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const filtered = workspaces.filter((ws) => {
    if (!query) return true;
    const q = query.toLowerCase();
    return ws.name.toLowerCase().includes(q);
  });

  const handleMenuResult = (result: MenuExecResult) => {
    if (result.action === "workspace_removed") {
      const wsId = result.data.workspace_id as string;
      if (wsId) onRemoved(wsId);
    }
  };

  return (
    <div className="ws-search-overlay" onClick={(e) => {
      if (e.target === e.currentTarget) onClose();
    }}>
      <div className="ws-search-dialog">
        <div className="ws-search-input-row">
          <SearchIcon />
          <input
            ref={inputRef}
            className="ws-search-input"
            type="text"
            placeholder="Search workspaces..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") onClose();
            }}
          />
        </div>
        <ul className="ws-search-list">
          {filtered.length === 0 ? (
            <li className="ws-search-empty">No matching workspaces</li>
          ) : (
            filtered.map((ws) => (
              <li key={ws.id}>
                <button
                  className="ws-search-item"
                  onClick={() => onSelect(ws)}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setContextMenu({ position: { x: e.clientX, y: e.clientY }, ws });
                  }}
                >
                  <span className="ws-search-item-name">{ws.name}</span>
                </button>
              </li>
            ))
          )}
        </ul>
      </div>

      {contextMenu && appRpc && (
        <RpcMenu
          rpc={appRpc}
          category="WorkspaceSelector/Context"
          context={{ workspace_id: contextMenu.ws.id }}
          position={contextMenu.position}
          onClose={() => setContextMenu(null)}
          onResult={handleMenuResult}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// WorkspaceSelector
// ---------------------------------------------------------------------------

export default function WorkspaceSelector({
  workspaces,
  appRpc,
  onSelect,
  onNewWorkspace,
  onRemoved,
}: WorkspaceSelectorProps) {
  const isMobile = useMobileDetect();
  const [showSearch, setShowSearch] = useState(false);
  const [contextMenu, setContextMenu] = useState<{
    position: { x: number; y: number };
    ws: Workspace;
  } | null>(null);

  const handleMenuResult = (result: MenuExecResult) => {
    if (result.action === "workspace_removed") {
      const wsId = result.data.workspace_id as string;
      if (wsId) onRemoved(wsId);
    }
  };

  const visible = workspaces.slice(0, MAX_VISIBLE);
  const hasMore = workspaces.length > MAX_VISIBLE;

  return (
    <div className="ws-selector">
      <div className="ws-selector-inner">
        <div className="ws-selector-brand">
          <h1 className="ws-selector-title">MutBot</h1>
          <p className="ws-selector-tagline">Define Your AI</p>
        </div>
        <div className="ws-selector-heading-row">
          <h2 className="ws-selector-heading">Workspaces</h2>
          <button
            className={`ws-selector-new-btn${isMobile ? " ws-selector-new-btn-mobile" : ""}`}
            onClick={onNewWorkspace}
            disabled={!appRpc}
            title="New Workspace"
          >
            <PlusIcon />
            {isMobile && <span>New</span>}
          </button>
        </div>
        {workspaces.length === 0 ? (
          <p className="ws-selector-empty">
            No workspaces yet —{" "}
            <a
              className="ws-selector-create-link"
              href="#"
              onClick={(e) => { e.preventDefault(); onNewWorkspace(); }}
            >
              create one
            </a>
          </p>
        ) : (
          <>
            <ul className="ws-selector-list">
              {visible.map((ws) => (
                <li key={ws.id}>
                  <button
                    className="ws-selector-item"
                    onClick={() => onSelect(ws)}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      setContextMenu({ position: { x: e.clientX, y: e.clientY }, ws });
                    }}
                  >
                    <span className="ws-selector-item-name">{ws.name}</span>
                  </button>
                </li>
              ))}
            </ul>
            {hasMore && (
              <button
                className="ws-selector-more"
                onClick={() => setShowSearch(true)}
              >
                More...
              </button>
            )}
          </>
        )}
      </div>

      {showSearch && (
        <WorkspaceSearchDialog
          workspaces={workspaces}
          appRpc={appRpc}
          onSelect={(ws) => {
            setShowSearch(false);
            onSelect(ws);
          }}
          onClose={() => setShowSearch(false)}
          onRemoved={onRemoved}
        />
      )}

      {contextMenu && appRpc && (
        <RpcMenu
          rpc={appRpc}
          category="WorkspaceSelector/Context"
          context={{ workspace_id: contextMenu.ws.id }}
          position={contextMenu.position}
          onClose={() => setContextMenu(null)}
          onResult={handleMenuResult}
        />
      )}
    </div>
  );
}
