/**
 * RpcMenu — 通过 WorkspaceRpc 获取后端菜单数据并渲染的菜单组件。
 *
 * 支持两种模式：
 * 1. 下拉模式（trigger）：点击触发按钮展开下拉菜单
 * 2. 上下文菜单模式（position + onClose）：在指定位置显示右键菜单
 *
 * 支持子菜单：后端菜单项包含 submenu_category 时，hover 自动展开子菜单。
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
import type { RpcClient } from "../lib/types";
import { renderLucideIcon } from "./SessionIcons";

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
  submenu_category?: string;
}

/** menu.execute 返回的结果 */
export interface MenuExecResult {
  action: string;
  data: Record<string, unknown>;
  error?: string;
}

type RpcMenuProps = {
  /** RPC 客户端实例（AppRpc 或 WorkspaceRpc） */
  rpc: RpcClient | null;
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

/** 子菜单面板组件 */
function SubMenu({
  rpc,
  category,
  parentRect,
  onExecute,
}: {
  rpc: RpcClient;
  category: string;
  parentRect: DOMRect;
  onExecute: (item: RpcMenuItem) => void;
}) {
  const [items, setItems] = useState<RpcMenuItem[]>([]);
  const [loading, setLoading] = useState(true);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    rpc.call<RpcMenuItem[]>("menu.query", { category, context: {} }).then((result) => {
      if (!cancelled) {
        setItems(result.filter((it) => it.visible));
        setLoading(false);
      }
    }).catch(() => {
      if (!cancelled) { setItems([]); setLoading(false); }
    });
    return () => { cancelled = true; };
  }, [rpc, category]);

  // 定位：父项右侧，溢出时调整
  useEffect(() => {
    if (!ref.current) return;
    const el = ref.current;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let x = parentRect.right;
    let y = parentRect.top;
    const rect = el.getBoundingClientRect();
    if (x + rect.width > vw) x = parentRect.left - rect.width;
    if (y + rect.height > vh) y = vh - rect.height - 4;
    if (x < 0) x = 4;
    if (y < 0) y = 4;
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
  }, [parentRect, items]);

  return createPortal(
    <div ref={ref} className="rpc-menu rpc-submenu" style={{ top: parentRect.top, left: parentRect.right }}>
      {loading ? (
        <div className="rpc-menu-loading">Loading...</div>
      ) : items.length === 0 ? (
        <div className="rpc-menu-empty">No items</div>
      ) : (
        items.map((item) => (
          <button
            key={item.id}
            className={`rpc-menu-item ${!item.enabled ? "disabled" : ""}`}
            disabled={!item.enabled}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => onExecute(item)}
          >
            <span className="rpc-menu-icon">{item.icon ? renderLucideIcon(item.icon, 16, "#cccccc") : null}</span>
            <span className="rpc-menu-label">{item.name}</span>
            {item.shortcut && <span className="rpc-menu-shortcut">{item.shortcut}</span>}
          </button>
        ))
      )}
    </div>,
    document.body,
  );
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

  // 子菜单状态
  const [hoveredSubmenu, setHoveredSubmenu] = useState<{
    category: string;
    rect: DOMRect;
  } | null>(null);
  const submenuTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const close = useCallback(() => {
    setOpen(false);
    setHoveredSubmenu(null);
    if (submenuTimerRef.current) clearTimeout(submenuTimerRef.current);
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
      // 不关闭如果点击在子菜单内
      const submenuEls = document.querySelectorAll(".rpc-submenu");
      for (const el of submenuEls) {
        if (el.contains(target)) return;
      }
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

  // 子菜单 hover 处理
  const handleItemMouseEnter = useCallback(
    (item: RpcMenuItem, e: React.MouseEvent) => {
      if (submenuTimerRef.current) clearTimeout(submenuTimerRef.current);
      if (item.submenu_category) {
        const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
        submenuTimerRef.current = setTimeout(() => {
          setHoveredSubmenu({ category: item.submenu_category!, rect });
        }, 150);
      } else {
        setHoveredSubmenu(null);
      }
    },
    [],
  );

  const handleItemMouseLeave = useCallback(() => {
    if (submenuTimerRef.current) clearTimeout(submenuTimerRef.current);
    // 延迟关闭，给用户移动到子菜单的时间
    submenuTimerRef.current = setTimeout(() => {
      setHoveredSubmenu(null);
    }, 300);
  }, []);

  const handleSubmenuMouseEnter = useCallback(() => {
    // 鼠标进入子菜单，取消关闭计时
    if (submenuTimerRef.current) clearTimeout(submenuTimerRef.current);
  }, []);

  const handleSubmenuMouseLeave = useCallback(() => {
    submenuTimerRef.current = setTimeout(() => {
      setHoveredSubmenu(null);
    }, 200);
  }, []);

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

      const hasSubmenu = !!item.submenu_category;

      elements.push(
        <button
          key={item.id}
          className={`rpc-menu-item ${!item.enabled ? "disabled" : ""} ${hasSubmenu && hoveredSubmenu?.category === item.submenu_category ? "submenu-open" : ""}`}
          disabled={!item.enabled}
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => { if (!hasSubmenu) handleExecute(item); }}
          onMouseEnter={(e) => handleItemMouseEnter(item, e)}
          onMouseLeave={handleItemMouseLeave}
        >
          <span className="rpc-menu-icon">{item.icon ? renderLucideIcon(item.icon, 16, "#cccccc") : null}</span>
          <span className="rpc-menu-label">{item.name}</span>
          {item.shortcut && <span className="rpc-menu-shortcut">{item.shortcut}</span>}
          {hasSubmenu && <span className="rpc-menu-submenu-arrow">{"\u25b8"}</span>}
        </button>,
      );
    }
    return elements;
  };

  const menuContent = (
    <>
      {renderItems()}
      {hoveredSubmenu && rpc && (
        <div onMouseEnter={handleSubmenuMouseEnter} onMouseLeave={handleSubmenuMouseLeave}>
          <SubMenu
            rpc={rpc}
            category={hoveredSubmenu.category}
            parentRect={hoveredSubmenu.rect}
            onExecute={handleExecute}
          />
        </div>
      )}
    </>
  );

  // 上下文菜单模式
  if (isContextMenu) {
    if (!open) return null;
    return createPortal(
      <div ref={menuRef} className="rpc-menu" style={{ top: menuPos.top, left: menuPos.left }}>
        {menuContent}
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
            {menuContent}
          </div>,
          document.body,
        )}
    </>
  );
}
