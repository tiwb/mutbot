import { useState, useCallback } from "react";
import type { ShortcutSlot } from "./ShortcutGrid";

// ── Format conversion ────────────────────────────────────────────────

/** Convert runtime bytes to display text: \x03 → "\\x03", \t → "\\t" */
function runtimeToDisplay(s: string): string {
  let result = "";
  for (let i = 0; i < s.length; i++) {
    const code = s.charCodeAt(i);
    if (code === 0x09) result += "\\t";
    else if (code === 0x0d) result += "\\r";
    else if (code === 0x0a) result += "\\n";
    else if (code < 0x20 || code === 0x7f) {
      result += "\\x" + code.toString(16).padStart(2, "0");
    } else {
      result += s[i];
    }
  }
  return result;
}

/** Convert display text to runtime bytes: "\\x03" → \x03, "\\t" → \t */
function displayToRuntime(s: string): string {
  let result = "";
  let i = 0;
  while (i < s.length) {
    if (s[i] === "\\" && i + 1 < s.length) {
      const next = s[i + 1];
      if (next === "x" && i + 3 < s.length) {
        const hex = s.substring(i + 2, i + 4);
        const code = parseInt(hex, 16);
        if (!isNaN(code)) {
          result += String.fromCharCode(code);
          i += 4;
          continue;
        }
      }
      if (next === "t") { result += "\t"; i += 2; continue; }
      if (next === "r") { result += "\r"; i += 2; continue; }
      if (next === "n") { result += "\n"; i += 2; continue; }
      if (next === "\\") { result += "\\"; i += 2; continue; }
    }
    result += s[i];
    i++;
  }
  return result;
}

// ── Escape sequence mapping ──────────────────────────────────────────

interface BaseKey {
  id: string;
  label: string;
  /** Display-format sequence */
  seq: string;
  type: "letter" | "digit" | "func" | "special";
}

/** Map modifier set + base key → display-format sequence and label. */
function buildSequence(
  base: BaseKey,
  modifiers: { ctrl: boolean; alt: boolean; shift: boolean },
): { label: string; sequence: string } {
  const { ctrl, alt, shift } = modifiers;

  // Shift-only combos
  if (shift && !ctrl && !alt) {
    if (base.id === "Tab") return { label: "S+Tab", sequence: "\\x1b[Z" };
    if (base.type === "letter")
      return { label: `S+${base.id}`, sequence: base.id.toUpperCase() };
  }

  // Ctrl+letter
  if (ctrl && !alt && base.type === "letter") {
    const code = base.id.toUpperCase().charCodeAt(0) - 64;
    const hex = code.toString(16).padStart(2, "0");
    return { label: `Ctrl+${base.id.toUpperCase()}`, sequence: `\\x${hex}` };
  }

  // Ctrl+special chars
  if (ctrl && !alt) {
    if (base.id === "[") return { label: "Ctrl+[", sequence: "\\x1b" };
    if (base.id === "\\") return { label: "Ctrl+\\", sequence: "\\x1c" };
    if (base.id === "]") return { label: "Ctrl+]", sequence: "\\x1d" };
  }

  // Alt combos
  if (alt && !ctrl) {
    if (base.type === "letter" || base.type === "digit" || base.id.length === 1) {
      const ch = shift && base.type === "letter" ? base.id.toUpperCase() : base.id.toLowerCase();
      return { label: `Alt+${base.id.toUpperCase()}`, sequence: `\\x1b${ch}` };
    }
  }

  // No modifier — use base key's own sequence
  if (!ctrl && !alt && !shift) {
    return { label: base.label, sequence: base.seq };
  }

  // Unsupported combo fallback
  return { label: base.label, sequence: base.seq };
}

const LETTERS: BaseKey[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").map((c) => ({
  id: c, label: c, seq: c.toLowerCase(), type: "letter",
}));

const DIGITS: BaseKey[] = "0123456789".split("").map((c) => ({
  id: c, label: c, seq: c, type: "digit",
}));

const SPECIAL_KEYS: BaseKey[] = [
  { id: "Esc", label: "Esc", seq: "\\x1b", type: "special" },
  { id: "Tab", label: "Tab", seq: "\\t", type: "special" },
  { id: "Enter", label: "Enter", seq: "\\r", type: "special" },
  { id: "Space", label: "Space", seq: " ", type: "special" },
  { id: "Back", label: "Back", seq: "\\x7f", type: "special" },
  { id: "Del", label: "Del", seq: "\\x1b[3~", type: "special" },
  { id: "↑", label: "↑", seq: "\\x1b[A", type: "special" },
  { id: "↓", label: "↓", seq: "\\x1b[B", type: "special" },
  { id: "←", label: "←", seq: "\\x1b[D", type: "special" },
  { id: "→", label: "→", seq: "\\x1b[C", type: "special" },
  { id: "Home", label: "Home", seq: "\\x1b[H", type: "special" },
  { id: "End", label: "End", seq: "\\x1b[F", type: "special" },
  { id: "PgUp", label: "PgUp", seq: "\\x1b[5~", type: "special" },
  { id: "PgDn", label: "PgDn", seq: "\\x1b[6~", type: "special" },
  { id: "Ins", label: "Ins", seq: "\\x1b[2~", type: "special" },
];

const F_KEYS: BaseKey[] = [
  { id: "F1", label: "F1", seq: "\\x1bOP", type: "func" },
  { id: "F2", label: "F2", seq: "\\x1bOQ", type: "func" },
  { id: "F3", label: "F3", seq: "\\x1bOR", type: "func" },
  { id: "F4", label: "F4", seq: "\\x1bOS", type: "func" },
  { id: "F5", label: "F5", seq: "\\x1b[15~", type: "func" },
  { id: "F6", label: "F6", seq: "\\x1b[17~", type: "func" },
  { id: "F7", label: "F7", seq: "\\x1b[18~", type: "func" },
  { id: "F8", label: "F8", seq: "\\x1b[19~", type: "func" },
  { id: "F9", label: "F9", seq: "\\x1b[20~", type: "func" },
  { id: "F10", label: "F10", seq: "\\x1b[21~", type: "func" },
  { id: "F11", label: "F11", seq: "\\x1b[23~", type: "func" },
  { id: "F12", label: "F12", seq: "\\x1b[24~", type: "func" },
];

// ── Delete last sequence token ───────────────────────────────────────

/** Remove the last escape-sequence token from a display-format string. */
function deleteLastToken(s: string): string {
  if (!s) return s;
  // Priority: longest match first
  const patterns = [
    /\\x1b\[\d+~$/,       // CSI func key: \x1b[3~
    /\\x1bO[A-Za-z]$/,    // SS3 func key: \x1bOP
    /\\x1b\[[A-Za-z]$/,   // CSI cursor/Shift+Tab: \x1b[A, \x1b[Z
    /\\x1b.$/,            // Alt combo: \x1bb
    /\\x[0-9a-fA-F]{2}$/, // single byte: \x01
    /\\[trn]$/,           // special escape: \t \r \n
    /.$/,                 // single char
  ];
  for (const p of patterns) {
    const m = s.match(p);
    if (m) return s.slice(0, s.length - m[0].length);
  }
  return s.slice(0, -1);
}

// ── Component ────────────────────────────────────────────────────────

interface Props {
  current: ShortcutSlot | null;
  onSelect: (slot: ShortcutSlot | null) => void;
  onClose: () => void;
}

export default function ShortcutEditDialog({ current, onSelect, onClose }: Props) {
  const [label, setLabel] = useState(current?.label ?? "");
  const [sequence, setSequence] = useState(
    current ? runtimeToDisplay(current.sequence) : "",
  );
  const [ctrl, setCtrl] = useState(false);
  const [alt, setAlt] = useState(false);
  const [shift, setShift] = useState(false);

  const handleBaseKey = useCallback(
    (base: BaseKey) => {
      const result = buildSequence(base, { ctrl, alt, shift });
      setSequence((prev) => prev + result.sequence);
      if (!label) setLabel(result.label);
      setCtrl(false);
      setAlt(false);
      setShift(false);
    },
    [ctrl, alt, shift, label],
  );

  const handleDelete = useCallback(() => {
    setSequence((prev) => deleteLastToken(prev));
  }, []);

  const handleSave = () => {
    if (label.trim() && sequence) {
      onSelect({ label: label.trim(), sequence: displayToRuntime(sequence) });
    }
  };

  const handleClear = () => {
    onSelect(null);
  };

  return (
    <div className="shortcut-edit-overlay" onClick={onClose}>
      <div className="shortcut-edit-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="shortcut-edit-header">
          <span>Edit Shortcut</span>
          {current && (
            <span className="shortcut-edit-current">Current: {current.label}</span>
          )}
        </div>

        <div className="shortcut-edit-fields">
          <label>
            Display Name
            <input
              type="text"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="e.g. Ctrl+O"
            />
          </label>
          <label>
            Key Sequence
            <div className="shortcut-edit-seq-row">
              <input
                type="text"
                value={sequence}
                onChange={(e) => setSequence(e.target.value)}
                placeholder="e.g. \x0f"
              />
              <button
                className="shortcut-edit-backspace"
                onClick={handleDelete}
                disabled={!sequence}
                title="Delete last key"
              >
                ⌫
              </button>
            </div>
          </label>
        </div>

        <div className="shortcut-edit-composer">
          <div className="shortcut-edit-modifiers">
            <button
              className={`shortcut-mod-btn ${ctrl ? "active" : ""}`}
              onClick={() => setCtrl((v) => !v)}
            >
              Ctrl
            </button>
            <button
              className={`shortcut-mod-btn ${alt ? "active" : ""}`}
              onClick={() => setAlt((v) => !v)}
            >
              Alt
            </button>
            <button
              className={`shortcut-mod-btn ${shift ? "active" : ""}`}
              onClick={() => setShift((v) => !v)}
            >
              Shift
            </button>
          </div>

          <div className="shortcut-edit-keys">
            <div className="shortcut-key-row">
              {LETTERS.slice(0, 13).map((k) => (
                <button key={k.id} className="shortcut-key-btn" onClick={() => handleBaseKey(k)}>
                  {k.label}
                </button>
              ))}
            </div>
            <div className="shortcut-key-row">
              {LETTERS.slice(13).map((k) => (
                <button key={k.id} className="shortcut-key-btn" onClick={() => handleBaseKey(k)}>
                  {k.label}
                </button>
              ))}
            </div>
            <div className="shortcut-key-row">
              {DIGITS.map((k) => (
                <button key={k.id} className="shortcut-key-btn" onClick={() => handleBaseKey(k)}>
                  {k.label}
                </button>
              ))}
            </div>
            <div className="shortcut-key-separator" />
            <div className="shortcut-key-row">
              {SPECIAL_KEYS.map((k) => (
                <button key={k.id} className="shortcut-key-btn special" onClick={() => handleBaseKey(k)}>
                  {k.label}
                </button>
              ))}
            </div>
            <div className="shortcut-key-row">
              {F_KEYS.map((k) => (
                <button key={k.id} className="shortcut-key-btn func" onClick={() => handleBaseKey(k)}>
                  {k.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="shortcut-edit-footer">
          <button className="shortcut-edit-clear" onClick={handleClear}>
            Clear Slot
          </button>
          <button onClick={onClose}>Cancel</button>
          <button
            className="primary"
            onClick={handleSave}
            disabled={!label.trim() || !sequence}
          >
            OK
          </button>
        </div>
      </div>
    </div>
  );
}
