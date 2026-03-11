import { useState } from "react";
import type { ShortcutSlot } from "./ShortcutGrid";

/** Preset shortcut categories for the edit dialog. */
const PRESETS: { category: string; items: ShortcutSlot[] }[] = [
  {
    category: "Ctrl 组合",
    items: [
      { label: "Ctrl+C", sequence: "\x03" },
      { label: "Ctrl+D", sequence: "\x04" },
      { label: "Ctrl+Z", sequence: "\x1a" },
      { label: "Ctrl+L", sequence: "\x0c" },
      { label: "Ctrl+A", sequence: "\x01" },
      { label: "Ctrl+E", sequence: "\x05" },
      { label: "Ctrl+U", sequence: "\x15" },
      { label: "Ctrl+K", sequence: "\x0b" },
      { label: "Ctrl+W", sequence: "\x17" },
      { label: "Ctrl+R", sequence: "\x12" },
    ],
  },
  {
    category: "方向键",
    items: [
      { label: "↑", sequence: "\x1b[A" },
      { label: "↓", sequence: "\x1b[B" },
      { label: "→", sequence: "\x1b[C" },
      { label: "←", sequence: "\x1b[D" },
    ],
  },
  {
    category: "功能键",
    items: [
      { label: "Esc", sequence: "\x1b" },
      { label: "Tab", sequence: "\t" },
      { label: "Enter", sequence: "\r" },
      { label: "Back", sequence: "\x7f" },
      { label: "Space", sequence: " " },
      { label: "Del", sequence: "\x1b[3~" },
      { label: "Home", sequence: "\x1b[H" },
      { label: "End", sequence: "\x1b[F" },
      { label: "PgUp", sequence: "\x1b[5~" },
      { label: "PgDn", sequence: "\x1b[6~" },
    ],
  },
];

interface Props {
  /** Current slot value (null if empty) */
  current: ShortcutSlot | null;
  /** Called with new slot value, or null to clear */
  onSelect: (slot: ShortcutSlot | null) => void;
  onClose: () => void;
}

export default function ShortcutEditDialog({ current, onSelect, onClose }: Props) {
  const [customMode, setCustomMode] = useState(false);
  const [customLabel, setCustomLabel] = useState("");
  const [customSeq, setCustomSeq] = useState("");

  const handlePresetSelect = (slot: ShortcutSlot) => {
    onSelect(slot);
  };

  const handleCustomSave = () => {
    if (customLabel.trim() && customSeq) {
      onSelect({ label: customLabel.trim(), sequence: customSeq });
    }
  };

  const handleClear = () => {
    onSelect(null);
  };

  return (
    <div className="shortcut-edit-overlay" onClick={onClose}>
      <div className="shortcut-edit-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="shortcut-edit-header">
          <span>编辑快捷键</span>
          {current && (
            <span className="shortcut-edit-current">当前: {current.label}</span>
          )}
        </div>

        {!customMode ? (
          <div className="shortcut-edit-presets">
            {PRESETS.map((group) => (
              <div key={group.category} className="shortcut-edit-group">
                <div className="shortcut-edit-group-label">{group.category}</div>
                <div className="shortcut-edit-group-items">
                  {group.items.map((item) => (
                    <button
                      key={item.label}
                      className="shortcut-edit-preset-btn"
                      onClick={() => handlePresetSelect(item)}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
              </div>
            ))}
            <div className="shortcut-edit-group">
              <button
                className="shortcut-edit-preset-btn custom-btn"
                onClick={() => setCustomMode(true)}
              >
                自定义...
              </button>
            </div>
          </div>
        ) : (
          <div className="shortcut-edit-custom">
            <label>
              显示名称
              <input
                type="text"
                value={customLabel}
                onChange={(e) => setCustomLabel(e.target.value)}
                placeholder="e.g. Ctrl+P"
                autoFocus
              />
            </label>
            <label>
              按键序列
              <input
                type="text"
                value={customSeq}
                onChange={(e) => setCustomSeq(e.target.value)}
                placeholder="e.g. \x10"
              />
              <span className="shortcut-edit-hint">
                支持转义: \x1b (ESC), \r (Enter), \t (Tab), \x03 (Ctrl+C) 等
              </span>
            </label>
            <div className="shortcut-edit-custom-actions">
              <button onClick={() => setCustomMode(false)}>返回预设</button>
              <button
                className="primary"
                onClick={handleCustomSave}
                disabled={!customLabel.trim() || !customSeq}
              >
                确定
              </button>
            </div>
          </div>
        )}

        <div className="shortcut-edit-footer">
          <button className="shortcut-edit-clear" onClick={handleClear}>
            清空此格
          </button>
          <button onClick={onClose}>取消</button>
        </div>
      </div>
    </div>
  );
}
