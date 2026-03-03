import { useEffect, useCallback, useState } from "react";

export interface ViewSchema {
  title?: string;
  components: ComponentSchema[];
  actions?: ActionSchema[];
}

export interface ComponentSchema {
  type: string;
  id: string;
  [key: string]: unknown;
}

export interface ActionSchema {
  type: string;
  label: string;
  primary?: boolean;
  [key: string]: unknown;
}

export interface ToolGroupData {
  toolCallId: string;
  toolName: string;
  input: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  isCancelled?: boolean;
  /** Active UI view pushed by backend UIContext */
  uiView?: ViewSchema | null;
  /** Final UI view (read-only snapshot after close) */
  uiFinalView?: ViewSchema | null;
}

interface Props {
  data: ToolGroupData;
  onUIEvent?: (toolCallId: string, event: UIEventPayload) => void;
}

export interface UIEventPayload {
  type: string;
  data: Record<string, unknown>;
  source?: string;
}

export default function ToolCallCard({ data, onUIEvent }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [modalArg, setModalArg] = useState<{ key: string; value: string } | null>(null);
  const isRunning = data.result === undefined;
  const isCancelled = !!data.isCancelled;
  const hasUI = data.uiView || data.uiFinalView;
  const hasArgs = Object.keys(data.input).length > 0;

  const cardClass = isRunning
    ? "running"
    : isCancelled
      ? "cancelled"
      : data.isError
        ? "error"
        : "success";

  const statusIcon = isRunning
    ? "\u25cf"
    : isCancelled
      ? "\u2298"
      : data.isError
        ? "\u2717"
        : "\u2713";

  const handleModalClose = useCallback(() => setModalArg(null), []);

  return (
    <div className={`tool-card ${cardClass}`}>
      <div className="tool-card-header" onClick={() => setExpanded((v) => !v)}>
        <span className="tool-card-status">
          {statusIcon}
        </span>
        <span className="tool-card-name">{data.toolName}</span>
        {!expanded && !hasUI && (
          <span className="tool-card-args-preview">
            {formatArgsPreview(data.input)}
          </span>
        )}
        <span className="tool-card-meta">
          <span className="tool-card-chevron">
            {expanded ? "\u25be" : "\u25b8"}
          </span>
        </span>
      </div>
      {hasUI ? (
        <div className="tool-card-body tool-card-ui">
          <ViewRenderer
            view={(data.uiView ?? data.uiFinalView)!}
            mode={data.uiView ? "connected" : "readonly"}
            onEvent={(e) => onUIEvent?.(data.toolCallId, e)}
          />
        </div>
      ) : expanded ? (
        <div className="tool-card-body">
          {hasArgs && (
            <div className="tool-card-section">
              <div className="tool-card-label">Arguments</div>
              <div className="tool-card-args-list">
                {Object.entries(data.input).map(([k, v]) => (
                  <ArgRow key={k} name={k} value={v} onExpand={setModalArg} />
                ))}
              </div>
            </div>
          )}
          {data.result !== undefined && (
            <div className="tool-card-section">
              <div
                className={`tool-card-label ${data.isError ? "error" : ""}`}
              >
                {data.isError ? "Error" : "Result"}
              </div>
              <pre className="tool-card-pre">{data.result}</pre>
            </div>
          )}
        </div>
      ) : null}
      {modalArg && <ArgModal name={modalArg.key} value={modalArg.value} onClose={handleModalClose} />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ArgRow — 单个参数行
// ---------------------------------------------------------------------------

const ARG_TRUNCATE_LEN = 120;

function formatValue(v: unknown): string {
  if (typeof v === "string") return v;
  return JSON.stringify(v, null, 2);
}

function ArgRow({ name, value, onExpand }: {
  name: string;
  value: unknown;
  onExpand: (arg: { key: string; value: string }) => void;
}) {
  const formatted = formatValue(value);
  const isLong = formatted.length > ARG_TRUNCATE_LEN;

  return (
    <div className="tool-arg-row">
      <span className="tool-arg-key">{name}</span>
      <span className="tool-arg-value">
        {isLong ? formatted.slice(0, ARG_TRUNCATE_LEN) + "..." : formatted}
      </span>
      {isLong && (
        <button
          className="tool-arg-expand-btn"
          title="View full value"
          onClick={(e) => { e.stopPropagation(); onExpand({ key: name, value: formatted }); }}
        >
          {"\u2922"}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ArgModal — 长参数值弹框
// ---------------------------------------------------------------------------

function ArgModal({ name, value, onClose }: { name: string; value: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="arg-modal-overlay" onClick={onClose}>
      <div className="arg-modal" onClick={(e) => e.stopPropagation()}>
        <div className="arg-modal-header">
          <span className="arg-modal-title">{name}</span>
          <button className="arg-modal-close" onClick={onClose}>{"\u2715"}</button>
        </div>
        <pre className="arg-modal-content">{value}</pre>
      </div>
    </div>
  );
}

function formatArgsPreview(args: Record<string, unknown>): string {
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  const preview = entries
    .map(([k, v]) => {
      const val =
        typeof v === "string"
          ? v.length > 30
            ? `"${v.slice(0, 27)}..."`
            : `"${v}"`
          : JSON.stringify(v);
      return `${k}=${val}`;
    })
    .join(", ");
  if (preview.length > 80) return `(${preview.slice(0, 77)}...)`;
  return `(${preview})`;
}


// ---------------------------------------------------------------------------
// ViewRenderer — 核心视图渲染器（Task 6 完善，此处最小实现）
// ---------------------------------------------------------------------------

interface ViewRendererProps {
  view: ViewSchema;
  mode: "connected" | "local" | "readonly";
  onEvent?: (event: UIEventPayload) => void;
}

function ViewRenderer({ view, mode, onEvent }: ViewRendererProps) {
  const [formValues, setFormValues] = useState<Record<string, unknown>>(() => {
    const init: Record<string, unknown> = {};
    for (const comp of view.components) {
      if (comp.value !== undefined) init[comp.id] = comp.value;
      else if (comp.defaultValue !== undefined) init[comp.id] = comp.defaultValue;
    }
    return init;
  });

  // Reset formValues when view changes (wizard multi-step switching)
  useEffect(() => {
    const init: Record<string, unknown> = {};
    for (const comp of view.components) {
      if (comp.value !== undefined) init[comp.id] = comp.value;
      else if (comp.defaultValue !== undefined) init[comp.id] = comp.defaultValue;
    }
    setFormValues(init);
  }, [view]);

  function handleChange(id: string, value: unknown) {
    setFormValues((prev) => ({ ...prev, [id]: value }));
  }

  function handleAction(actionType: string) {
    if (mode === "readonly") return;
    if (actionType === "submit") {
      onEvent?.({ type: "submit", data: { ...formValues } });
    } else if (actionType === "cancel") {
      onEvent?.({ type: "cancel", data: {} });
    } else {
      onEvent?.({ type: "action", data: { action: actionType, ...formValues } });
    }
  }

  function evaluateVisibility(comp: ComponentSchema): boolean {
    const cond = comp.visible_when as Record<string, unknown[]> | undefined;
    if (!cond) return true;
    return Object.entries(cond).every(
      ([field, allowed]) => Array.isArray(allowed) && allowed.includes(formValues[field]),
    );
  }

  function handleAutoSubmit(id: string, val: unknown) {
    if (mode === "readonly") return;
    const merged = { ...formValues, [id]: val };
    onEvent?.({ type: "submit", data: merged });
  }

  return (
    <div className="ui-view">
      {view.title && <div className="ui-view-title">{view.title}</div>}
      <div className="ui-view-components">
        {view.components
          .filter((comp) => evaluateVisibility(comp))
          .map((comp) => (
            <UIComponent
              key={comp.id}
              schema={comp}
              mode={mode}
              value={formValues[comp.id]}
              onChange={(v) => handleChange(comp.id, v)}
              onAutoSubmit={handleAutoSubmit}
            />
          ))}
      </div>
      {view.actions && view.actions.length > 0 && (
        <div className="ui-view-actions">
          {view.actions.map((action, i) => (
            <button
              key={action.type + i}
              className={`ui-action-btn ${action.primary ? "primary" : ""}`}
              onClick={() => handleAction(action.type)}
              disabled={mode === "readonly"}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// UIComponent — 组件分发器（Task 7 完善，此处最小实现）
// ---------------------------------------------------------------------------

interface UIComponentProps {
  schema: ComponentSchema;
  mode: "connected" | "local" | "readonly";
  value: unknown;
  onChange: (value: unknown) => void;
  onAutoSubmit?: (id: string, value: unknown) => void;
}

/** Secret text input with Show/Hide toggle. */
function SecretInput({ value, placeholder, onChange, disabled }: {
  value: string;
  placeholder: string;
  onChange: (v: string) => void;
  disabled: boolean;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="ui-secret-wrap">
      <input
        className="ui-text-input"
        type={visible ? "text" : "password"}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
      />
      <button
        type="button"
        className="ui-secret-toggle"
        onClick={() => setVisible((v) => !v)}
        tabIndex={-1}
      >
        {visible ? "Hide" : "Show"}
      </button>
    </div>
  );
}

function UIComponent({ schema, mode, value, onChange, onAutoSubmit }: UIComponentProps) {
  const disabled = mode === "readonly";

  switch (schema.type) {
    case "text":
      return (
        <div className="ui-field">
          {schema.label ? <label className="ui-label">{String(schema.label)}</label> : null}
          {schema.secret ? (
            <SecretInput
              value={(value as string) ?? ""}
              placeholder={String(schema.placeholder ?? "")}
              onChange={onChange as (v: string) => void}
              disabled={disabled}
            />
          ) : (
            <input
              className="ui-text-input"
              type="text"
              value={(value as string) ?? ""}
              placeholder={String(schema.placeholder ?? "")}
              onChange={(e) => onChange(e.target.value)}
              disabled={disabled}
            />
          )}
        </div>
      );

    case "select": {
      const options = (schema.options ?? []) as { value: string; label: string }[];
      const layout = schema.layout === "vertical" ? "vertical" : "horizontal";
      const isMultiple = !!schema.multiple;
      const scrollable = schema.scrollable ? " scrollable" : "";

      if (isMultiple) {
        const selected = Array.isArray(value) ? (value as string[]) : [];
        const toggle = (v: string) => {
          if (disabled) return;
          const next = selected.includes(v)
            ? selected.filter((s) => s !== v)
            : [...selected, v];
          onChange(next);
        };
        return (
          <div className="ui-field">
            {schema.label ? <label className="ui-label">{String(schema.label)}</label> : null}
            <div className={`ui-select-cards ${layout}${scrollable}`}>
              {options.map((opt) => (
                <button
                  key={opt.value}
                  className={`ui-select-card ${selected.includes(opt.value) ? "selected" : ""}`}
                  onClick={() => toggle(opt.value)}
                  disabled={disabled}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>
        );
      }

      return (
        <div className="ui-field">
          {schema.label ? <label className="ui-label">{String(schema.label)}</label> : null}
          <div className={`ui-select-cards ${layout}${scrollable}`}>
            {options.map((opt) => (
              <button
                key={opt.value}
                className={`ui-select-card ${value === opt.value ? "selected" : ""}`}
                onClick={() => {
                  if (disabled) return;
                  onChange(opt.value);
                  if (schema.auto_submit) onAutoSubmit?.(schema.id, opt.value);
                }}
                disabled={disabled}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      );
    }

    case "button":
      return (
        <button
          className="ui-action-btn"
          onClick={() => !disabled && onChange(schema.id)}
          disabled={disabled}
        >
          {String(schema.label ?? schema.id)}
        </button>
      );

    case "toggle":
      return (
        <div className="ui-field ui-toggle-field">
          <label className="ui-toggle-label">
            <input
              type="checkbox"
              checked={!!value}
              onChange={(e) => onChange(e.target.checked)}
              disabled={disabled}
            />
            <span>{String(schema.label ?? "")}</span>
          </label>
        </div>
      );

    case "hint":
      return (
        <div className="ui-hint">{String(schema.text)}</div>
      );

    case "badge": {
      const variant = (schema.variant as string) ?? "info";
      return (
        <span className={`ui-badge ${variant}`}>{String(schema.text)}</span>
      );
    }

    case "spinner":
      return (
        <div className="ui-spinner">
          <span className="ui-spinner-dot" />
          {schema.text ? <span>{String(schema.text)}</span> : null}
        </div>
      );

    case "copyable": {
      const text = (schema.text as string) ?? "";
      return (
        <div className="ui-copyable">
          <code>{text}</code>
          <button
            className="ui-copy-btn"
            onClick={() => navigator.clipboard.writeText(text)}
            title="Copy"
          >
            {"\u2398"}
          </button>
        </div>
      );
    }

    case "link":
      return (
        <a
          className="ui-link"
          href={schema.url as string}
          target="_blank"
          rel="noopener noreferrer"
        >
          {String(schema.label ?? schema.url)}
        </a>
      );

    case "button_group": {
      const options = (schema.options ?? []) as { value: string; label: string }[];
      return (
        <div className="ui-field">
          {schema.label ? <label className="ui-label">{String(schema.label)}</label> : null}
          <div className="ui-button-group">
            {options.map((opt) => (
              <button
                key={opt.value}
                className={`ui-btn-group-item ${value === opt.value ? "selected" : ""}`}
                onClick={() => !disabled && onChange(opt.value)}
                disabled={disabled}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      );
    }

    default:
      return (
        <div className="ui-unknown">
          Unknown component: {schema.type}
        </div>
      );
  }
}
