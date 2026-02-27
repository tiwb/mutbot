/**
 * IconPicker — 图标选择器弹出面板。
 *
 * 搜索 + VirtuosoGrid 虚拟滚动网格浏览 Lucide 图标。
 * Portal 挂载，点击外部关闭。
 */

import { forwardRef, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { VirtuosoGrid } from "react-virtuoso";
import { icons } from "lucide-react";

/** PascalCase → kebab-case: "MessageSquare" → "message-square" */
function pascalToKebab(name: string): string {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();
}

/** 所有图标名（PascalCase） */
const ALL_ICON_NAMES = Object.keys(icons);

/** 搜索用小写名数组 */
const ALL_ICON_NAMES_LOWER = ALL_ICON_NAMES.map((n) => n.toLowerCase());

interface IconPickerProps {
  /** 触发位置 */
  position: { x: number; y: number };
  /** 选择图标后回调（kebab-case 名） */
  onSelect: (iconName: string) => void;
  /** 重置为默认图标 */
  onReset: () => void;
  /** 关闭选择器 */
  onClose: () => void;
}

/** VirtuosoGrid item 容器 */
const GridItem = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  function GridItem(props, ref) {
    return <div {...props} ref={ref} className="icon-picker-cell" />;
  },
);

/** VirtuosoGrid list 容器 */
const GridList = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  function GridList(props, ref) {
    return <div {...props} ref={ref} className="icon-picker-grid" />;
  },
);

export default function IconPicker({ position, onSelect, onReset, onClose }: IconPickerProps) {
  const [search, setSearch] = useState("");
  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // 过滤图标
  const filtered = useMemo(() => {
    if (!search.trim()) return ALL_ICON_NAMES;
    const q = search.trim().toLowerCase();
    return ALL_ICON_NAMES.filter((_, i) => ALL_ICON_NAMES_LOWER[i]!.includes(q));
  }, [search]);

  // 自动聚焦搜索框
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // 调整面板位置防溢出
  useEffect(() => {
    if (!panelRef.current) return;
    const rect = panelRef.current.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let x = position.x;
    let y = position.y;
    if (x + rect.width > vw) x = vw - rect.width - 4;
    if (y + rect.height > vh) y = vh - rect.height - 4;
    if (x < 0) x = 4;
    if (y < 0) y = 4;
    panelRef.current.style.left = `${x}px`;
    panelRef.current.style.top = `${y}px`;
  }, [position, filtered]);

  // 点击外部关闭
  useEffect(() => {
    const handler = (e: PointerEvent) => {
      if (panelRef.current?.contains(e.target as Node)) return;
      onClose();
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [onClose]);

  // Escape 关闭
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  const handleSelect = useCallback(
    (pascalName: string) => {
      onSelect(pascalToKebab(pascalName));
    },
    [onSelect],
  );

  return createPortal(
    <div
      ref={panelRef}
      className="icon-picker"
      style={{ top: position.y, left: position.x }}
    >
      <div className="icon-picker-search">
        <input
          ref={inputRef}
          type="text"
          placeholder="Search icons..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <div className="icon-picker-body">
        {filtered.length === 0 ? (
          <div className="icon-picker-empty">No matching icons</div>
        ) : (
          <VirtuosoGrid
            totalCount={filtered.length}
            components={{
              Item: GridItem,
              List: GridList,
            }}
            itemContent={(index) => {
              const name = filtered[index]!;
              const Icon = icons[name as keyof typeof icons];
              return (
                <button
                  className="icon-picker-btn"
                  title={pascalToKebab(name)}
                  onClick={() => handleSelect(name)}
                >
                  <Icon size={20} />
                </button>
              );
            }}
            style={{ height: "100%", width: "100%" }}
          />
        )}
      </div>
      <div className="icon-picker-footer">
        <button className="icon-picker-reset" onClick={onReset}>
          Reset to default
        </button>
      </div>
    </div>,
    document.body,
  );
}
