/**
 * RpcMenu — 通过 WorkspaceRpc 获取后端菜单数据并渲染的下拉菜单组件。
 *
 * 用法：
 *   <RpcMenu
 *     rpc={workspaceRpc}
 *     category="SessionPanel/Add"
 *     trigger={<button>+</button>}
 *     onResult={handleMenuResult}
 *   />
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { WorkspaceRpc } from "../lib/workspace-rpc";

/** 后端返回的菜单项 */
interface RpcMenuItem {
  id: string;
  name: string;
  icon: string;
  order: string;
  enabled: boolean;
  visible: boolean;
  data?: Record<string, unknown>;
}

/** menu.execute 返回的结果 */
export interface MenuExecResult {
  action: string;
  data: Record<string, unknown>;
  error?: string;
}

interface RpcMenuProps {
  /** WorkspaceRpc 实例 */
  rpc: WorkspaceRpc | null;
  /** 菜单 category（对应后端 display_category） */
  category: string;
  /** 触发按钮（渲染在原位） */
  trigger: React.ReactElement;
  /** 执行结果回调 */
  onResult?: (result: MenuExecResult) => void;
}

/** 从 order 字段提取 group 名（"group:index" → "group"） */
function getGroup(order: string): string {
  const idx = order.indexOf(":");
  return idx >= 0 ? order.slice(0, idx) : order;
}

export default function RpcMenu({ rpc, category, trigger, onResult }: RpcMenuProps) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<RpcMenuItem[]>([]);
  const [loading, setLoading] = useState(false);
  const btnRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuPos, setMenuPos] = useState({ top: 0, left: 0 });

  // 打开时获取菜单项
  const fetchItems = useCallback(async () => {
    if (!rpc) return;
    setLoading(true);
    try {
      const result = await rpc.call<RpcMenuItem[]>("menu.query", { category });
      setItems(result.filter((it) => it.visible));
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [rpc, category]);

  // 计算菜单位置
  const updateMenuPos = useCallback(() => {
    if (!btnRef.current) return;
    const rect = btnRef.current.getBoundingClientRect();
    setMenuPos({ top: rect.bottom + 2, left: rect.right - 140 });
  }, []);

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: PointerEvent) => {
      const target = e.target as Node;
      if (btnRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [open]);

  // Escape 关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open]);

  const handleToggle = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      e.preventDefault();
      if (!open) {
        updateMenuPos();
        fetchItems();
      }
      setOpen((v) => !v);
    },
    [open, updateMenuPos, fetchItems],
  );

  const handleExecute = useCallback(
    async (item: RpcMenuItem) => {
      if (!rpc || !item.enabled) return;
      setOpen(false);
      try {
        const result = await rpc.call<MenuExecResult>("menu.execute", {
          menu_id: item.id,
          params: item.data || {},
        });
        onResult?.(result);
      } catch {
        // 静默处理
      }
    },
    [rpc, onResult],
  );

  // 插入分组分隔线
  const renderItems = () => {
    if (loading) {
      return <div className="rpc-menu-loading">Loading...</div>;
    }
    if (items.length === 0) {
      return <div className="rpc-menu-empty">No items</div>;
    }

    const elements: React.ReactNode[] = [];
    let lastGroup = "";

    for (const [i, item] of items.entries()) {
      const group = getGroup(item.order);

      // 分组间分隔线
      if (i > 0 && group !== lastGroup) {
        elements.push(<div key={`sep-${i}`} className="rpc-menu-separator" />);
      }
      lastGroup = group;

      elements.push(
        <button
          key={item.id}
          className={`rpc-menu-item ${!item.enabled ? "disabled" : ""}`}
          disabled={!item.enabled}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => handleExecute(item)}
        >
          {item.icon && <span className="rpc-menu-icon">{getIconForType(item.icon)}</span>}
          <span className="rpc-menu-label">{item.name}</span>
        </button>,
      );
    }
    return elements;
  };

  return (
    <>
      <div ref={btnRef} style={{ display: "inline-flex" }} onPointerDown={handleToggle}>
        {trigger}
      </div>
      {open &&
        createPortal(
          <div ref={menuRef} className="rpc-menu" style={{ top: menuPos.top, left: menuPos.left }}>
            {renderItems()}
          </div>,
          document.body,
        )}
    </>
  );
}

// ---------------------------------------------------------------------------
// 图标映射（复用 TabIcon 风格的内联 SVG）
// ---------------------------------------------------------------------------

function getIconForType(icon: string): React.ReactNode {
  const color = "#cccccc";
  const size = 16;
  switch (icon) {
    case "agent":
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      );
    case "terminal":
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="4 17 10 11 4 5" />
          <line x1="12" y1="19" x2="20" y2="19" />
        </svg>
      );
    case "document":
      return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
        </svg>
      );
    default:
      return null;
  }
}
