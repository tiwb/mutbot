import { useCallback } from "react";

/** A single shortcut key slot in the 4x4 grid. */
export interface ShortcutSlot {
  /** Display label (e.g. "Esc", "Ctrl+C", "↑") */
  label: string;
  /** Raw byte sequence to send to the terminal */
  sequence: string;
}

/** 4x4 grid = 16 slots. Index 12 (bottom-left) is reserved for ⚙/保存. */
export type ShortcutLayout = (ShortcutSlot | null)[];

const STORAGE_KEY = "mutbot-terminal-shortcuts";

/** Index of the ⚙ edit button (bottom-left corner of 4x4 grid). */
export const EDIT_BUTTON_INDEX = 12;

/** Default 4x4 layout:
 * ┌─────┬─────┬─────┬─────┐
 * │ Esc │ Tab │Back │ Del │
 * ├─────┼─────┼─────┼─────┤
 * │Ct+C │Ct+D │Ct+Z │Ct+L │
 * ├─────┼─────┼─────┼─────┤
 * │Ct+A │Ct+E │  ↑  │Enter│
 * ├─────┼─────┼─────┼─────┤
 * │ ⚙  │  ←  │  ↓  │  →  │
 * └─────┴─────┴─────┴─────┘
 */
export const DEFAULT_LAYOUT: ShortcutLayout = [
  { label: "Esc",    sequence: "\x1b" },
  { label: "Tab",    sequence: "\t" },
  { label: "Back",   sequence: "\x7f" },
  { label: "Del",    sequence: "\x1b[3~" },
  { label: "Ctrl+C", sequence: "\x03" },
  { label: "Ctrl+D", sequence: "\x04" },
  { label: "Ctrl+Z", sequence: "\x1a" },
  { label: "Ctrl+L", sequence: "\x0c" },
  { label: "Ctrl+A", sequence: "\x01" },
  { label: "Ctrl+E", sequence: "\x05" },
  { label: "↑",      sequence: "\x1b[A" },
  { label: "Enter",  sequence: "\r" },
  null, // index 12 = ⚙ edit button (reserved)
  { label: "←",      sequence: "\x1b[D" },
  { label: "↓",      sequence: "\x1b[B" },
  { label: "→",      sequence: "\x1b[C" },
];

/** Load layout from localStorage, falling back to default. */
export function loadShortcutLayout(): ShortcutLayout {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const parsed = JSON.parse(saved) as ShortcutLayout;
      if (Array.isArray(parsed) && parsed.length === 16) {
        // Ensure edit button slot stays null
        parsed[EDIT_BUTTON_INDEX] = null;
        return parsed;
      }
    }
  } catch { /* ignore */ }
  return [...DEFAULT_LAYOUT];
}

/** Persist layout to localStorage. */
export function saveShortcutLayout(layout: ShortcutLayout) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
  } catch { /* ignore */ }
}

interface Props {
  layout: ShortcutLayout;
  /** When true, clicking a slot opens the edit dialog instead of sending the key. */
  editing?: boolean;
  /** Called when a shortcut button is pressed (normal mode). */
  onKey: (sequence: string) => void;
  /** Called when a slot is clicked in edit mode, with the slot index. */
  onEditSlot?: (index: number) => void;
  /** Called when the ⚙ button is clicked (enter edit) or 保存 is clicked (save edit). */
  onEditToggle?: () => void;
}

export default function ShortcutGrid({ layout, editing, onKey, onEditSlot, onEditToggle }: Props) {
  const handlePress = useCallback(
    (slot: ShortcutSlot | null, index: number) => {
      if (index === EDIT_BUTTON_INDEX) {
        onEditToggle?.();
        return;
      }
      if (editing) {
        onEditSlot?.(index);
        return;
      }
      if (!slot) return;
      navigator.vibrate?.(30);
      onKey(slot.sequence);
    },
    [editing, onKey, onEditSlot, onEditToggle],
  );

  return (
    <div className={`shortcut-grid ${editing ? "editing" : ""}`}>
      {layout.map((slot, i) => {
        if (i === EDIT_BUTTON_INDEX) {
          return (
            <button
              key={i}
              className={`shortcut-grid-btn edit-btn ${editing ? "save-mode" : ""}`}
              onClick={() => handlePress(null, i)}
            >
              {editing ? "保存" : "⚙"}
            </button>
          );
        }
        return (
          <button
            key={i}
            className={`shortcut-grid-btn ${!slot ? "empty" : ""} ${editing ? "editable" : ""}`}
            onClick={() => handlePress(slot, i)}
            disabled={!editing && !slot}
          >
            {slot?.label ?? ""}
          </button>
        );
      })}
    </div>
  );
}
