/**
 * ModelSelector — 模型选择下拉菜单（inline dropdown）。
 *
 * 显示当前模型名称，点击展开可用模型列表进行切换。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkspaceRpc } from "../lib/workspace-rpc";

interface ModelInfo {
  name: string;
  model_id: string;
  provider_name: string;
}

interface Props {
  sessionId: string;
  currentModel: string;
  rpc: WorkspaceRpc;
}

export default function ModelSelector({ sessionId, currentModel, rpc }: Props) {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // 加载可用模型列表
  useEffect(() => {
    rpc.call<{ models: ModelInfo[]; default_model: string }>("config.models")
      .then((result) => {
        setModels(result.models);
        setDefaultModel(result.default_model);
      })
      .catch(() => {});
  }, [rpc]);

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: PointerEvent) => {
      if (ref.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [open]);

  const handleSelect = useCallback(
    (modelName: string) => {
      setOpen(false);
      if (modelName === currentModel) return;
      rpc.call("session.update", { session_id: sessionId, model: modelName })
        .catch(() => {});
    },
    [rpc, sessionId, currentModel],
  );

  const displayName = currentModel || defaultModel || "—";

  if (models.length === 0) {
    return <span className="model-selector-label">{displayName}</span>;
  }

  return (
    <div className="model-selector" ref={ref}>
      <button
        className="model-selector-label"
        onClick={() => setOpen(!open)}
        title={`Model: ${displayName}`}
      >
        {displayName} <span className="model-selector-caret">&#9662;</span>
      </button>
      {open && (
        <div className="model-selector-dropdown">
          {models.map((m) => (
            <button
              key={m.name}
              className={`model-selector-item${m.name === (currentModel || defaultModel) ? " active" : ""}`}
              onClick={() => handleSelect(m.name)}
            >
              <span className="model-selector-check">
                {m.name === (currentModel || defaultModel) ? "\u2713" : ""}
              </span>
              <span>{m.name}</span>
              {m.name !== m.model_id && (
                <span className="model-selector-hint">{m.provider_name}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
