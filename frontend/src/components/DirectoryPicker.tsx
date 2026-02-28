import { useCallback, useEffect, useState } from "react";
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

interface DirectoryPickerProps {
  appRpc: AppRpc;
  onSelect: (ws: Workspace) => void;
  onCancel: () => void;
}

export default function DirectoryPicker({
  appRpc,
  onSelect,
  onCancel,
}: DirectoryPickerProps) {
  const [currentPath, setCurrentPath] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [manualInput, setManualInput] = useState(false);
  const [inputPath, setInputPath] = useState("");
  const [wsName, setWsName] = useState("");

  /** 从路径中提取目录名作为 placeholder */
  const defaultName = currentPath
    ? currentPath.split(/[/\\]/).filter(Boolean).pop() || ""
    : "";

  const browse = useCallback(
    async (path: string) => {
      setLoading(true);
      setError("");
      try {
        const result = await appRpc.call<BrowseResult>("filesystem.browse", {
          path: path || undefined,
        });
        if (result.error) {
          setError(result.error);
        } else {
          setCurrentPath(result.path);
          setParent(result.parent);
          setEntries(result.entries);
          setInputPath(result.path);
        }
      } catch (e) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [appRpc],
  );

  useEffect(() => {
    browse("");
  }, [browse]);

  const handleNavigate = (dirName: string) => {
    const sep = currentPath.includes("\\") ? "\\" : "/";
    browse(currentPath + sep + dirName);
  };

  const handleUp = () => {
    if (parent) browse(parent);
  };

  const handleManualGo = () => {
    if (inputPath.trim()) {
      browse(inputPath.trim());
      setManualInput(false);
    }
  };

  const handleCreate = async () => {
    if (!currentPath) return;
    setCreating(true);
    setError("");
    try {
      const params: Record<string, unknown> = { project_path: currentPath };
      if (wsName.trim()) {
        params.name = wsName.trim();
      }
      const result = await appRpc.call<Workspace & { error?: string }>(
        "workspace.create",
        params,
      );
      if (result.error) {
        setError(result.error);
      } else {
        onSelect(result);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="dir-picker-overlay">
      <div className="dir-picker" onClick={(e) => e.stopPropagation()}>
        <h3 className="dir-picker-title">New Workspace</h3>

        <div className="dir-picker-name-row">
          <input
            className="dir-picker-name-input"
            type="text"
            placeholder={
              defaultName
                ? `Workspace name (default: ${defaultName})`
                : "Workspace name (optional)"
            }
            value={wsName}
            onChange={(e) => setWsName(e.target.value)}
          />
        </div>

        <div className="dir-picker-path-bar">
          {manualInput ? (
            <div className="dir-picker-input-row">
              <input
                className="dir-picker-input"
                value={inputPath}
                onChange={(e) => setInputPath(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") handleManualGo();
                  if (e.key === "Escape") setManualInput(false);
                }}
                autoFocus
              />
              <button className="dir-picker-btn-sm" onClick={handleManualGo}>
                Go
              </button>
            </div>
          ) : (
            <button
              className="dir-picker-path"
              onClick={() => setManualInput(true)}
              title="Click to enter path manually"
            >
              {currentPath || "..."}
            </button>
          )}
        </div>

        {error && <div className="dir-picker-error">{error}</div>}

        <div className="dir-picker-entries">
          {loading ? (
            <div className="dir-picker-loading">Loading...</div>
          ) : (
            <>
              {parent && (
                <button className="dir-picker-entry" onClick={handleUp}>
                  <span className="dir-picker-entry-icon">{"\u2B06"}</span>
                  <span>..</span>
                </button>
              )}
              {entries.length === 0 && !parent && (
                <div className="dir-picker-empty">No subdirectories</div>
              )}
              {entries.map((entry) => (
                <button
                  key={entry.name}
                  className="dir-picker-entry"
                  onClick={() => handleNavigate(entry.name)}
                >
                  <span className="dir-picker-entry-icon">{"\uD83D\uDCC1"}</span>
                  <span>{entry.name}</span>
                </button>
              ))}
            </>
          )}
        </div>

        <div className="dir-picker-actions">
          <button className="dir-picker-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button
            className="dir-picker-btn-primary"
            onClick={handleCreate}
            disabled={!currentPath || creating}
          >
            {creating ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
