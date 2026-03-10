import { useCallback, useEffect, useRef, useState } from "react";
import type { AppRpc } from "../lib/app-rpc";
import type { Workspace } from "../lib/types";

interface DirEntry {
  name: string;
  type: string;
}

interface BrowseResult {
  path: string;
  parent: string | null;
  entries: DirEntry[];
  error?: string;
}

interface NewWorkspacePageProps {
  appRpc: AppRpc;
  initialName: string;
  cwd: string;
  onCreated: (ws: Workspace) => void;
  onCancel: () => void;
}

function FolderIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
    </svg>
  );
}

function UpIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M12 19V5M5 12l7-7 7 7"/>
    </svg>
  );
}

/** 拼接路径，自动检测分隔符 */
function joinPath(base: string, name: string): string {
  if (!base) return name;
  const sep = base.includes("\\") ? "\\" : "/";
  const trimmed = base.replace(/[/\\]+$/, "");
  return `${trimmed}${sep}${name}`;
}

/** 从路径末段提取目录名 */
function pathBasename(path: string): string {
  return path.split(/[/\\]/).filter(Boolean).pop() || "";
}

/** 获取路径的父目录 */
function pathDirname(path: string): string | null {
  const parts = path.replace(/[/\\]+$/, "").split(/[/\\]/);
  if (parts.length <= 1) return null;
  parts.pop();
  const sep = path.includes("\\") ? "\\" : "/";
  let result = parts.join(sep);
  // Windows 驱动器根: "D:" → "D:\\"
  if (/^[A-Za-z]:$/.test(result)) result += sep;
  return result || null;
}

export default function NewWorkspacePage({
  appRpc,
  initialName,
  cwd,
  onCreated,
  onCancel,
}: NewWorkspacePageProps) {
  const [name, setName] = useState(initialName);
  const [path, setPath] = useState(joinPath(cwd, initialName));
  const [nameEdited, setNameEdited] = useState(false);
  const [pathEdited, setPathEdited] = useState(false);
  const [showBrowser, setShowBrowser] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);

  // autofocus name input
  useEffect(() => {
    nameRef.current?.focus();
    nameRef.current?.select();
  }, []);

  // Name → Path 联动（仅当 path 未被手动编辑时）
  const handleNameChange = (value: string) => {
    setName(value);
    setNameEdited(true);
    if (!pathEdited) {
      setPath(joinPath(cwd, value));
    }
  };

  const handlePathChange = (value: string) => {
    setPath(value);
    setPathEdited(true);
  };

  const handleBrowseSelect = (selectedPath: string) => {
    setPath(selectedPath);
    setPathEdited(true);
    setShowBrowser(false);
    // 如果 name 未手动编辑，用选中目录名回填
    if (!nameEdited) {
      setName(pathBasename(selectedPath));
    }
  };

  const handleCreate = async () => {
    if (!name.trim() || !path.trim()) return;
    setCreating(true);
    setError("");
    try {
      const result = await appRpc.call<Workspace & { error?: string }>(
        "workspace.create",
        {
          project_path: path.trim(),
          name: name.trim(),
          create_dir: true,
        },
      );
      if (result.error) {
        setError(result.error);
      } else {
        onCreated(result);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !creating && !showBrowser) {
      handleCreate();
    } else if (e.key === "Escape") {
      if (showBrowser) {
        setShowBrowser(false);
      } else {
        onCancel();
      }
    }
  };

  if (showBrowser) {
    return (
      <div className="nw-page">
        <DirectoryBrowser
          appRpc={appRpc}
          initialPath={path}
          onSelect={handleBrowseSelect}
          onCancel={() => setShowBrowser(false)}
        />
      </div>
    );
  }

  return (
    <div className="nw-page" onKeyDown={handleKeyDown}>
      <div className="nw-panel">
        <h3 className="nw-title">New Workspace</h3>

        <label className="nw-label">Name</label>
        <input
          ref={nameRef}
          className="nw-input"
          type="text"
          value={name}
          onChange={(e) => handleNameChange(e.target.value)}
        />

        <label className="nw-label">Path</label>
        <div className="nw-path-row">
          <input
            className="nw-input nw-path-input"
            type="text"
            value={path}
            onChange={(e) => handlePathChange(e.target.value)}
          />
          <button
            className="nw-browse-btn"
            onClick={() => setShowBrowser(true)}
            title="Browse directories"
          >
            <FolderIcon />
          </button>
        </div>

        {error && <div className="nw-error">{error}</div>}

        <div className="nw-actions">
          <button className="nw-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button
            className="nw-btn-primary"
            onClick={handleCreate}
            disabled={!name.trim() || !path.trim() || creating}
          >
            {creating ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// DirectoryBrowser — 独立目录浏览面板
// ---------------------------------------------------------------------------

function DirectoryBrowser({
  appRpc,
  initialPath,
  onSelect,
  onCancel,
}: {
  appRpc: AppRpc;
  initialPath: string;
  onSelect: (path: string) => void;
  onCancel: () => void;
}) {
  const [currentPath, setCurrentPath] = useState("");
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [pathInput, setPathInput] = useState("");
  const [notFound, setNotFound] = useState(false);
  const pathInputRef = useRef<HTMLInputElement>(null);

  const browse = useCallback(
    async (targetPath: string) => {
      setLoading(true);
      setNotFound(false);

      // 尝试浏览 targetPath，如果不存在则向上回退到最近存在的祖先
      let tryPath = targetPath;
      while (tryPath) {
        try {
          const result = await appRpc.call<BrowseResult>("filesystem.browse", {
            path: tryPath || undefined,
          });
          if (result.error) {
            // 目录不存在，向上回退
            const parent = pathDirname(tryPath);
            if (!parent || parent === tryPath) {
              // 到根了还是失败，用默认路径
              break;
            }
            if (tryPath !== targetPath) {
              // 已经回退过了，继续向上
              tryPath = parent;
              continue;
            }
            setNotFound(true);
            tryPath = parent;
            continue;
          }
          setCurrentPath(result.path);
          setPathInput(result.path);
          setEntries(result.entries);
          setLoading(false);
          return;
        } catch {
          // RPC 错误，向上回退
          const parent = pathDirname(tryPath);
          if (!parent || parent === tryPath) break;
          if (tryPath === targetPath) setNotFound(true);
          tryPath = parent;
        }
      }

      // 所有重试失败，用空路径（服务端默认）
      try {
        const result = await appRpc.call<BrowseResult>("filesystem.browse", {});
        if (!result.error) {
          setCurrentPath(result.path);
          setPathInput(result.path);
          setEntries(result.entries);
        }
      } catch {
        // give up
      }
      setLoading(false);
    },
    [appRpc],
  );

  useEffect(() => {
    browse(initialPath);
  }, [browse, initialPath]);

  const handleNavigate = (dirName: string) => {
    browse(joinPath(currentPath, dirName));
  };

  const handleUp = () => {
    const parent = pathDirname(currentPath);
    if (parent) browse(parent);
  };

  const handlePathInputKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      browse(pathInput.trim());
    } else if (e.key === "Escape") {
      onCancel();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      onCancel();
    }
  };

  return (
    <div className="nw-panel nw-browser-panel" onKeyDown={handleKeyDown}>
      <div className="nw-browser-path-row">
        <input
          ref={pathInputRef}
          className="nw-input nw-browser-path-input"
          type="text"
          value={pathInput}
          onChange={(e) => setPathInput(e.target.value)}
          onKeyDown={handlePathInputKeyDown}
        />
        <button
          className="nw-browse-btn"
          onClick={handleUp}
          disabled={!currentPath || !pathDirname(currentPath)}
          title="Go up"
        >
          <UpIcon />
        </button>
      </div>

      <div className="nw-browser-entries">
        {loading ? (
          <div className="nw-browser-status">Loading...</div>
        ) : (
          <>
            {entries.length === 0 && (
              <div className="nw-browser-status">No subdirectories</div>
            )}
            {entries.map((entry) => (
              <button
                key={entry.name}
                className="nw-browser-entry"
                onClick={() => handleNavigate(entry.name)}
              >
                <span className="nw-browser-icon">{"\uD83D\uDCC1"}</span>
                <span>{entry.name}</span>
              </button>
            ))}
            {notFound && (
              <div className="nw-browser-not-found">Directory not found</div>
            )}
          </>
        )}
      </div>

      <div className="nw-actions">
        <button className="nw-btn-secondary" onClick={onCancel}>
          Cancel
        </button>
        <button
          className="nw-btn-primary"
          onClick={() => onSelect(currentPath)}
          disabled={!currentPath}
        >
          Select
        </button>
      </div>
    </div>
  );
}
