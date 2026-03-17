import { useCallback, useRef, useState } from "react";
import { Settings } from "lucide-react";

/** A single shortcut key slot in the grid. */
export interface ShortcutSlot {
  /** Display label (e.g. "Esc", "Ctrl+C", "↑") */
  label: string;
  /** Raw byte sequence to send to the terminal */
  sequence: string;
}

export type ShortcutLayout = (ShortcutSlot | null)[];

/** Stored format includes grid dimensions. */
export interface ShortcutConfig {
  rows: number;
  cols: number;
  slots: ShortcutLayout;
}

const STORAGE_KEY = "mutbot-terminal-shortcuts";
export const DEFAULT_ROWS = 4;
export const DEFAULT_COLS = 4;

/** Default 4x4 layout:
 * ┌───────┬───────┬───────┬───────┐
 * │  Esc  │  Tab  │ S+Tab │ Back  │
 * ├───────┼───────┼───────┼───────┤
 * │ Ct+T  │ Ct+D  │ Ct+O  │  Del  │
 * ├───────┼───────┼───────┼───────┤
 * │ Ct+B  │ Ct+C  │   ↑   │ Enter │
 * ├───────┼───────┼───────┼───────┤
 * │   ⚙   │   ←   │   ↓   │   →   │
 * └───────┴───────┴───────┴───────┘
 */
export const DEFAULT_SLOTS: ShortcutLayout = [
  { label: "Esc",    sequence: "\x1b" },
  { label: "Tab",    sequence: "\t" },
  { label: "S+Tab",  sequence: "\x1b[Z" },
  { label: "Back",   sequence: "\x7f" },
  { label: "Ctrl+T", sequence: "\x14" },
  { label: "Ctrl+D", sequence: "\x04" },
  { label: "Ctrl+O", sequence: "\x0f" },
  { label: "Del",    sequence: "\x1b[3~" },
  { label: "Ctrl+B", sequence: "\x02" },
  { label: "Ctrl+C", sequence: "\x03" },
  { label: "↑",      sequence: "\x1b[A" },
  { label: "Enter",  sequence: "\r" },
  null, // ⚙ edit button (reserved, position = (rows-1)*cols)
  { label: "←",      sequence: "\x1b[D" },
  { label: "↓",      sequence: "\x1b[B" },
  { label: "→",      sequence: "\x1b[C" },
];

/** Compute the ⚙ button index (last row, first col). */
export function editButtonIndex(rows: number, cols: number): number {
  return (rows - 1) * cols;
}

/** Build a default config. */
export function defaultConfig(): ShortcutConfig {
  return { rows: DEFAULT_ROWS, cols: DEFAULT_COLS, slots: [...DEFAULT_SLOTS] };
}

/** Load config from localStorage, falling back to default. */
export function loadShortcutConfig(): ShortcutConfig {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const parsed = JSON.parse(saved);
      if (parsed && typeof parsed.rows === "number" && typeof parsed.cols === "number" && Array.isArray(parsed.slots)) {
        const cfg: ShortcutConfig = {
          rows: parsed.rows,
          cols: parsed.cols,
          slots: parsed.slots,
        };
        const total = cfg.rows * cfg.cols;
        // Ensure correct length
        if (cfg.slots.length !== total) {
          cfg.slots = cfg.slots.slice(0, total);
          while (cfg.slots.length < total) cfg.slots.push(null);
        }
        // Ensure edit button slot is null
        cfg.slots[editButtonIndex(cfg.rows, cfg.cols)] = null;
        return cfg;
      }
    }
  } catch { /* ignore */ }
  return defaultConfig();
}

/** Persist config to localStorage. */
export function saveShortcutConfig(config: ShortcutConfig) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  } catch { /* ignore */ }
}

/** Resize grid: preserve existing slots, fill new ones with null. */
export function resizeGrid(config: ShortcutConfig, newRows: number, newCols: number): ShortcutConfig {
  const oldEditIdx = editButtonIndex(config.rows, config.cols);
  const newTotal = newRows * newCols;
  const newEditIdx = editButtonIndex(newRows, newCols);

  // Collect all valid shortcuts (excluding ⚙ slot)
  const validSlots: (ShortcutSlot | null)[] = [];
  for (let i = 0; i < config.slots.length; i++) {
    if (i !== oldEditIdx) validSlots.push(config.slots[i] ?? null);
  }

  // Build new slots array
  const newSlots: ShortcutLayout = [];
  let srcIdx = 0;
  for (let i = 0; i < newTotal; i++) {
    if (i === newEditIdx) {
      newSlots.push(null); // ⚙ reserved
    } else if (srcIdx < validSlots.length) {
      newSlots.push(validSlots[srcIdx++] ?? null);
    } else {
      newSlots.push(null);
    }
  }

  return { rows: newRows, cols: newCols, slots: newSlots };
}

interface Props {
  layout: ShortcutLayout;
  rows: number;
  cols: number;
  /** When true, clicking a slot opens the edit dialog instead of sending the key. */
  editing?: boolean;
  /** Called when a shortcut button is pressed (normal mode). */
  onKey: (sequence: string) => void;
  /** Called when a slot is clicked in edit mode, with the slot index. */
  onEditSlot?: (index: number) => void;
  /** Called when the ⚙ button is clicked (non-edit mode) to show settings menu. */
  onSettingsClick?: () => void;
  /** Called when 保存 is clicked (edit mode) to save and exit. */
  onSaveClick?: () => void;
  /** Called when two slots are swapped via drag in edit mode. */
  onSwapSlots?: (a: number, b: number) => void;
}

export default function ShortcutGrid({
  layout, rows, cols, editing, onKey, onEditSlot, onSettingsClick, onSaveClick, onSwapSlots,
}: Props) {
  const editIdx = editButtonIndex(rows, cols);
  const gridRef = useRef<HTMLDivElement>(null);
  const dragTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dragSourceRef = useRef<number | null>(null);
  const dragTargetRef = useRef<number | null>(null);
  const [dragSourceIdx, setDragSourceIdx] = useState<number | null>(null);
  const [dragTargetIdx, setDragTargetIdx] = useState<number | null>(null);
  const dragActive = useRef(false);

  const handlePress = useCallback(
    (slot: ShortcutSlot | null, index: number) => {
      if (index === editIdx) {
        if (editing) {
          onSaveClick?.();
        } else {
          onSettingsClick?.();
        }
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
    [editIdx, editing, onKey, onEditSlot, onSettingsClick, onSaveClick],
  );

  // --- Drag to swap (editing mode only) ---

  // Touch handlers for drag — all logic inline to avoid stale closures
  const handlePointerDown = useCallback((index: number, e: React.PointerEvent) => {
    if (!editing || index === editIdx) return;
    const startX = e.clientX;
    const startY = e.clientY;

    dragTimer.current = setTimeout(() => {
      // Activate drag
      dragActive.current = true;
      dragSourceRef.current = index;
      dragTargetRef.current = null;
      setDragSourceIdx(index);
      setDragTargetIdx(null);
      navigator.vibrate?.(30);
    }, 300);

    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (dx * dx + dy * dy > 100 && dragTimer.current && !dragActive.current) {
        clearTimeout(dragTimer.current);
        dragTimer.current = null;
      }
      if (dragActive.current) {
        const grid = gridRef.current;
        if (!grid) return;
        const btns = grid.querySelectorAll<HTMLElement>(".shortcut-grid-btn");
        let idx: number | null = null;
        for (let i = 0; i < btns.length; i++) {
          const rect = btns[i]!.getBoundingClientRect();
          if (ev.clientX >= rect.left && ev.clientX <= rect.right && ev.clientY >= rect.top && ev.clientY <= rect.bottom) {
            idx = i;
            break;
          }
        }
        const editBtnIdx = editButtonIndex(rows, cols);
        const validTarget = idx !== null && idx !== editBtnIdx && idx !== dragSourceRef.current ? idx : null;
        dragTargetRef.current = validTarget;
        setDragTargetIdx(validTarget);
      }
    };

    const onUp = () => {
      if (dragTimer.current) {
        clearTimeout(dragTimer.current);
        dragTimer.current = null;
      }
      if (dragActive.current) {
        dragActive.current = false;
        const src = dragSourceRef.current;
        const tgt = dragTargetRef.current;
        dragSourceRef.current = null;
        dragTargetRef.current = null;
        setDragSourceIdx(null);
        setDragTargetIdx(null);
        if (src !== null && tgt !== null && src !== tgt) {
          onSwapSlots?.(src, tgt);
        }
      }
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.removeEventListener("pointercancel", onUp);
    };

    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    document.addEventListener("pointercancel", onUp);
  }, [editing, editIdx, rows, cols, onSwapSlots]);

  const handleClick = useCallback((slot: ShortcutSlot | null, index: number) => {
    if (dragActive.current) return;
    handlePress(slot, index);
  }, [handlePress]);

  // Build preview layout: swap source and target visually during drag
  const displayLayout = (dragSourceIdx !== null && dragTargetIdx !== null && dragSourceIdx !== dragTargetIdx)
    ? layout.map((slot, i) => {
        if (i === dragSourceIdx) return layout[dragTargetIdx] ?? null;
        if (i === dragTargetIdx) return layout[dragSourceIdx] ?? null;
        return slot;
      })
    : layout;

  return (
    <div
      ref={gridRef}
      className={`shortcut-grid ${editing ? "editing" : ""}`}
      style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}
    >
      {displayLayout.map((slot, i) => {
        const isDragSource = editing && dragSourceIdx === i;
        const isDragTarget = editing && dragTargetIdx === i;

        if (i === editIdx) {
          return (
            <button
              key={i}
              className={`shortcut-grid-btn edit-btn ${editing ? "save-mode" : ""}`}
              onClick={() => handleClick(null, i)}
            >
              {editing ? "保存" : <Settings size={16} />}
            </button>
          );
        }
        return (
          <button
            key={i}
            className={[
              "shortcut-grid-btn",
              !slot ? "empty" : "",
              editing ? "editable" : "",
              isDragSource ? "drag-source" : "",
              isDragTarget ? "drag-target" : "",
            ].filter(Boolean).join(" ")}
            onClick={() => handleClick(slot, i)}
            onPointerDown={(e) => handlePointerDown(i, e)}
            disabled={!editing && !slot}
          >
            {slot?.label ?? ""}
          </button>
        );
      })}
    </div>
  );
}
