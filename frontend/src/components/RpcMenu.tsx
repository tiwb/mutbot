/**
 * RpcMenu — 通过 WorkspaceRpc 获取后端菜单数据并渲染的菜单组件。
 *
 * 支持两种模式：
 * 1. 下拉模式（trigger）：点击触发按钮展开下拉菜单
 * 2. 上下文菜单模式（position + onClose）：在指定位置显示右键菜单
 *
 * 用法（下拉模式）：
 *   <RpcMenu
 *     rpc={workspaceRpc}
 *     category="SessionPanel/Add"
 *     trigger={<button>+</button>}
 *     onResult={handleMenuResult}
 *   />
 *
 * 用法（上下文菜单模式）：
 *   <RpcMenu
 *     rpc={workspaceRpc}
 *     category="Tab/Context"
 *     context={{ session_id: "...", session_status: "active" }}
 *     position={{ x: 100, y: 200 }}
 *     onClose={() => setMenu(null)}
 *     onResult={handleMenuResult}
 *     onClientAction={handleClientAction}
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
  shortcut?: string;
  client_action?: string;
  data?: Record<string, unknown>;
}

/** menu.execute 返回的结果 */
export interface MenuExecResult {
  action: string;
  data: Record<string, unknown>;
  error?: string;
}

type RpcMenuProps = {
  /** WorkspaceRpc 实例 */
  rpc: WorkspaceRpc | null;
  /** 菜单 category（对应后端 display_category） */
  category: string;
  /** 传递给 menu.query 的上下文（如 session_id, session_status） */
  context?: Record<string, unknown>;
  /** 执行结果回调 */
  onResult?: (result: MenuExecResult) => void;
  /** 前端直接处理的 client_action 回调 */
  onClientAction?: (action: string, data: Record<string, unknown>) => void;
} & (
  | { trigger: React.ReactElement; position?: never; onClose?: never }
  | { trigger?: never; position: { x: number; y: number }; onClose: () => void }
);

/** 从 order 字段提取 group 名（"group:index" → "group"） */
function getGroup(order: string): string {
  const idx = order.indexOf(":");
  return idx >= 0 ? order.slice(0, idx) : order;
}

export default function RpcMenu(props: RpcMenuProps) {
  const { rpc, category, context, onResult, onClientAction } = props;
  const isContextMenu = "position" in props && !!props.position;

  const [open, setOpen] = useState(isContextMenu);
  const [items, setItems] = useState<RpcMenuItem[]>([]);
  const [loading, setLoading] = useState(false);
  const btnRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuPos, setMenuPos] = useState(
    isContextMenu ? { top: props.position!.y, left: props.position!.x } : { top: 0, left: 0 },
  );

  const close = useCallback(() => {
    setOpen(false);
    if (isContextMenu && props.onClose) {
      props.onClose();
    }
  }, [isContextMenu, props]);

  // 获取菜单项
  const fetchItems = useCallback(async () => {
    if (!rpc) return;
    setLoading(true);
    try {
      const result = await rpc.call<RpcMenuItem[]>("menu.query", { category, context: context || {} });
      setItems(result.filter((it) => it.visible));
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [rpc, category, context]);

  // 上下文菜单模式：立即获取数据
  useEffect(() => {
    if (isContextMenu) {
      fetchItems();
    }
  }, [isContextMenu, fetchItems]);

  // 计算下拉模式菜单位置
  const updateMenuPos = useCallback(() => {
    if (!btnRef.current) return;
    const rect = btnRef.current.getBoundingClientRect();
    setMenuPos({ top: rect.bottom + 2, left: rect.right - 140 });
  }, []);

  // 上下文菜单模式：调整位置防止溢出
  useEffect(() => {
    if (!isContextMenu || !open || !menuRef.current) return;
    const rect = menuRef.current.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let x = props.position!.x;
    let y = props.position!.y;
    if (x + rect.width > vw) x = vw - rect.width - 4;
    if (y + rect.height > vh) y = vh - rect.height - 4;
    if (x < 0) x = 4;
    if (y < 0) y = 4;
    menuRef.current.style.left = `${x}px`;
    menuRef.current.style.top = `${y}px`;
  }, [isContextMenu, open, items, props]);

  // 点击外部关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: PointerEvent) => {
      const target = e.target as Node;
      if (btnRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [open, close]);

  // Escape 关闭
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, close]);

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
      if (!item.enabled) return;
      close();

      // 如果有 client_action，交由前端处理
      if (item.client_action && onClientAction) {
        onClientAction(item.client_action, item.data || {});
        return;
      }

      // 否则走 RPC execute
      if (!rpc) return;
      try {
        const result = await rpc.call<MenuExecResult>("menu.execute", {
          menu_id: item.id,
          params: { ...(context || {}), ...(item.data || {}) },
        });
        onResult?.(result);
      } catch {
        // 静默处理
      }
    },
    [rpc, context, onResult, onClientAction, close],
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
          <span className="rpc-menu-icon">{item.icon ? getIconForType(item.icon) : null}</span>
          <span className="rpc-menu-label">{item.name}</span>
          {item.shortcut && <span className="rpc-menu-shortcut">{item.shortcut}</span>}
        </button>,
      );
    }
    return elements;
  };

  // 上下文菜单模式
  if (isContextMenu) {
    if (!open) return null;
    return createPortal(
      <div ref={menuRef} className="rpc-menu" style={{ top: menuPos.top, left: menuPos.left }}>
        {renderItems()}
      </div>,
      document.body,
    );
  }

  // 下拉模式
  return (
    <>
      <div ref={btnRef} style={{ display: "inline-flex" }} onPointerDown={handleToggle}>
        {props.trigger}
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
