import { useEffect, useRef, useState } from "react";
import type { AppRpc } from "../lib/app-rpc";
import type { Workspace } from "../lib/types";

interface NewWorkspacePageProps {
  appRpc: AppRpc;
  initialName: string;
  onCreated: (ws: Workspace) => void;
  onCancel: () => void;
}

export default function NewWorkspacePage({
  appRpc,
  initialName,
  onCreated,
  onCancel,
}: NewWorkspacePageProps) {
  const [name, setName] = useState(initialName);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
    nameRef.current?.select();
  }, []);

  const handleCreate = async () => {
    if (!name.trim()) return;
    setCreating(true);
    setError("");
    try {
      const result = await appRpc.call<Workspace & { error?: string }>(
        "workspace.create",
        { name: name.trim() },
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
    if (e.key === "Enter" && !creating) {
      handleCreate();
    } else if (e.key === "Escape") {
      onCancel();
    }
  };

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
          onChange={(e) => setName(e.target.value)}
          placeholder="my-project"
        />

        {error && <div className="nw-error">{error}</div>}

        <div className="nw-actions">
          <button className="nw-btn-secondary" onClick={onCancel}>
            Cancel
          </button>
          <button
            className="nw-btn-primary"
            onClick={handleCreate}
            disabled={!name.trim() || creating}
          >
            {creating ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}
