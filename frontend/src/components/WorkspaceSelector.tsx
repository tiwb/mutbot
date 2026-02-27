import { useEffect, useRef, useState } from "react";
import type { AppRpc } from "../lib/app-rpc";
import type { Workspace } from "../lib/types";
import DirectoryPicker from "./DirectoryPicker";

const MAX_VISIBLE = 5;

interface WorkspaceSelectorProps {
  workspaces: Workspace[];
  appRpc: AppRpc | null;
  onSelect: (ws: Workspace) => void;
  onCreated: (ws: Workspace) => void;
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
  onSelect,
  onClose,
}: {
  workspaces: Workspace[];
  onSelect: (ws: Workspace) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const filtered = workspaces.filter((ws) => {
    if (!query) return true;
    const q = query.toLowerCase();
    return (
      ws.name.toLowerCase().includes(q) ||
      ws.project_path.toLowerCase().includes(q)
    );
  });

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
                >
                  <span className="ws-search-item-name">{ws.name}</span>
                  <span className="ws-search-item-path">
                    {shortenPath(ws.project_path)}
                  </span>
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
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
  onCreated,
}: WorkspaceSelectorProps) {
  const [showDirPicker, setShowDirPicker] = useState(false);
  const [showSearch, setShowSearch] = useState(false);

  const handleCreate = (ws: Workspace) => {
    setShowDirPicker(false);
    onCreated(ws);
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
            className="ws-selector-new-btn"
            onClick={() => setShowDirPicker(true)}
            disabled={!appRpc}
            title="New Workspace"
          >
            <PlusIcon />
            <span>New</span>
          </button>
        </div>
        {workspaces.length === 0 ? (
          <p className="ws-selector-empty">
            No workspaces yet — create one to get started
          </p>
        ) : (
          <>
            <ul className="ws-selector-list">
              {visible.map((ws) => (
                <li key={ws.id}>
                  <button
                    className="ws-selector-item"
                    onClick={() => onSelect(ws)}
                  >
                    <span className="ws-selector-item-name">{ws.name}</span>
                    <span className="ws-selector-item-path">
                      {shortenPath(ws.project_path)}
                    </span>
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

      {showDirPicker && appRpc && (
        <DirectoryPicker
          appRpc={appRpc}
          onSelect={handleCreate}
          onCancel={() => setShowDirPicker(false)}
        />
      )}

      {showSearch && (
        <WorkspaceSearchDialog
          workspaces={workspaces}
          onSelect={(ws) => {
            setShowSearch(false);
            onSelect(ws);
          }}
          onClose={() => setShowSearch(false)}
        />
      )}
    </div>
  );
}

function shortenPath(path: string): string {
  // 替换 home 目录为 ~
  const home =
    typeof navigator !== "undefined" &&
    navigator.userAgent.includes("Windows")
      ? path.replace(/^[A-Z]:\\Users\\[^\\]+/, "~")
      : path.replace(/^\/home\/[^/]+/, "~").replace(/^\/Users\/[^/]+/, "~");
  return home;
}
