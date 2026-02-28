import { useCallback, useEffect, useRef, useState } from "react";
import {
  Layout,
  type Model,
  Actions,
  DockLocation,
  type Action,
  type TabNode,
  type TabSetNode,
  type BorderNode,
  type ITabSetRenderValues,
  type ITabRenderValues,
} from "flexlayout-react";
import "flexlayout-react/style/dark.css";
import {
  createModel,
  PANEL_AGENT_CHAT,
  PANEL_TERMINAL,
  PANEL_CODE_EDITOR,
} from "./lib/layout";
import { panelFactory } from "./panels/PanelFactory";
import SessionListPanel from "./panels/SessionListPanel";
import RpcMenu, { type MenuExecResult } from "./components/RpcMenu";
import { WorkspaceRpc } from "./lib/workspace-rpc";
import { AppRpc } from "./lib/app-rpc";
import { isRemote } from "./lib/connection";
import { getSessionIcon } from "./components/SessionIcons";
import IconPicker from "./components/IconPicker";
import WelcomePage from "./components/WelcomePage";
import WorkspaceSelector from "./components/WorkspaceSelector";
import type { Workspace, Session } from "./lib/types";

// ---------- Helpers ----------

/** Find the active tabset, or fall back to the first tabset in the model. */
function getTargetTabset(model: Model): TabSetNode | undefined {
  return model.getActiveTabset() ?? model.getFirstTabSet();
}

/** Check if the model contains any tab nodes. */
function modelHasTabs(model: Model): boolean {
  let found = false;
  model.visitNodes((node) => {
    if (!found && node.getType() === "tab") found = true;
  });
  return found;
}

/** Resolve the sessionId for a tab node, with terminalId fallback. */
function resolveSessionId(
  tabNode: TabNode,
  sessions: { id: string; config?: Record<string, unknown> | null }[],
): string | undefined {
  const config = tabNode.getConfig();
  let sessionId = config?.sessionId as string | undefined;
  if (!sessionId && config?.terminalId) {
    const match = sessions.find(
      (s) => s.config?.terminal_id === config.terminalId,
    );
    if (match) sessionId = match.id;
  }
  return sessionId;
}

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------

export default function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const modelRef = useRef<Model | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- setEditingTab is internal API
  const layoutRef = useRef<any>(null);
  const [, forceUpdate] = useState(0);

  // 是否有打开的 tab（用于欢迎页和自动打开逻辑）
  const [hasOpenTabs, setHasOpenTabs] = useState(false);
  const hasOpenTabsRef = useRef(false);

  // Tab context menu state
  const [tabContextMenu, setTabContextMenu] = useState<{
    position: { x: number; y: number };
    nodeId: string;
  } | null>(null);

  // Icon picker state (shared by tab and session list context menus)
  const [iconPicker, setIconPicker] = useState<{
    position: { x: number; y: number };
    sessionId: string;
  } | null>(null);

  // Sidebar collapsed state
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(260);
  const sidebarResizing = useRef(false);

  // Toast notification state
  const [toast, setToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((message: string, duration = 5000) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast(message);
    toastTimerRef.current = setTimeout(() => setToast(null), duration);
  }, []);

  // Workspace RPC 连接
  const rpcRef = useRef<WorkspaceRpc | null>(null);
  const [rpc, setRpc] = useState<WorkspaceRpc | null>(null);

  // App RPC 连接（/ws/app，用于工作区列表和创建）
  const appRpcRef = useRef<AppRpc | null>(null);
  const [appRpc, setAppRpc] = useState<AppRpc | null>(null);

  // Initialize model once workspace is available
  if (!modelRef.current) {
    modelRef.current = createModel();
  }

  // Remote mode: connection status tracking
  const [remoteConnected, setRemoteConnected] = useState<boolean | null>(null);

  // 连接 /ws/app 获取工作区列表 + hash 路由
  useEffect(() => {
    const rpcInst = new AppRpc({
      onOpen: () => {
        setRemoteConnected(true);
        rpcInst
          .call<Workspace[]>("workspace.list")
          .then((wss) => {
            setWorkspaces(wss);

            const wsName = location.hash.replace(/^#\/?/, "");
            if (wsName) {
              const target = wss.find((w) => w.name === wsName);
              if (target) {
                setWorkspace(target);
                if (target.layout) {
                  try {
                    modelRef.current = createModel(target.layout);
                    forceUpdate((n) => n + 1);
                  } catch {
                    // fallback to default layout
                  }
                }
              } else {
                // hash 指向不存在的 workspace，清空 hash
                location.hash = "";
              }
            }
          })
          .catch(() => {});
      },
      onClose: () => {
        setRemoteConnected((prev) => prev === null ? false : prev);
      },
    });
    appRpcRef.current = rpcInst;
    setAppRpc(rpcInst);

    // 远程模式：版本校验（直接访问 /v<version>/ 时检查版本一致性）
    if (isRemote()) {
      const urlVersion = location.pathname.match(/^\/v([^/]+)\//)?.[1];
      rpcInst.on("welcome", (data) => {
        const serverVersion = (data as { version?: string }).version;
        if (urlVersion && serverVersion && serverVersion !== urlVersion) {
          // 版本不匹配，重定向到根路径让 Landing Page 重新匹配
          location.href = `/${location.hash}`;
        }
      });
    }

    return () => {
      rpcInst.close();
      appRpcRef.current = null;
      setAppRpc(null);
    };
  }, []);

  // hash 变化监听
  useEffect(() => {
    const onHashChange = () => {
      const wsName = location.hash.replace(/^#\/?/, "");
      if (!wsName) {
        setWorkspace(null);
        return;
      }
      const target = workspaces.find((w) => w.name === wsName);
      if (target) {
        setWorkspace(target);
        if (target.layout) {
          try {
            modelRef.current = createModel(target.layout);
            forceUpdate((n) => n + 1);
          } catch {
            // fallback to default layout
          }
        }
      } else {
        // hash 指向不存在的 workspace，清空 hash
        location.hash = "";
        setWorkspace(null);
      }
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, [workspaces]);

  // Initialize WorkspaceRpc when workspace is available
  useEffect(() => {
    if (!workspace) return;
    const wsRpc = new WorkspaceRpc(workspace.id, {
      onOpen: () => {
        // 连接建立后通过 RPC 获取 session 列表
        wsRpc.call<Session[]>("session.list", { workspace_id: workspace.id }).then(setSessions).catch(() => {});
      },
    });
    rpcRef.current = wsRpc;
    setRpc(wsRpc);

    // 事件监听：session_created / session_updated / session_deleted
    const unsubs = [
      wsRpc.on("session_created", (data) => {
        const session = data as unknown as Session;
        if (session?.id) {
          setSessions((prev) => {
            if (prev.some((s) => s.id === session.id)) return prev;
            return [...prev, session];
          });
        }
      }),
      wsRpc.on("session_updated", (data) => {
        const session = data as unknown as Session;
        if (session?.id) {
          setSessions((prev) =>
            prev.map((s) => (s.id === session.id ? { ...s, ...session } : s)),
          );
          // 同步更新 flexlayout 中的 tab 名称（跨客户端重命名）
          if (session.title) {
            const model = modelRef.current;
            if (model) {
              model.visitNodes((node) => {
                if (node.getType() === "tab") {
                  const tabNode = node as TabNode;
                  if (tabNode.getConfig()?.sessionId === session.id && tabNode.getName() !== session.title) {
                    model.doAction(Actions.renameTab(node.getId(), session.title));
                  }
                }
              });
            }
          }
        }
      }),
      wsRpc.on("session_deleted", (data) => {
        const sessionId = data.session_id as string;
        if (sessionId) {
          setSessions((prev) => prev.filter((s) => s.id !== sessionId));
          // 关闭已删除 session 的 tab
          const model = modelRef.current;
          if (model) {
            model.visitNodes((node) => {
              if (node.getType() === "tab") {
                const tabNode = node as TabNode;
                if (tabNode.getConfig()?.sessionId === sessionId) {
                  model.doAction(Actions.deleteTab(node.getId()));
                }
              }
            });
          }
        }
      }),
      wsRpc.on("open_session", async (data) => {
        const sessionId = data.session_id as string;
        if (!sessionId) return;
        try {
          const session: Session = await wsRpc.call("session.get", { session_id: sessionId });
          addTabForSession(session);
        } catch {
          // 静默处理
        }
      }),
      wsRpc.on("config_changed", (data) => {
        if (data.reason === "file_changed") {
          showToast("Configuration updated. New sessions will use the latest settings.");
        }
      }),
    ];

    return () => {
      unsubs.forEach((fn) => fn());
      wsRpc.close();
      rpcRef.current = null;
      setRpc(null);
    };
  }, [workspace]);

  // ------------------------------------------------------------------
  // Session creation helpers
  // ------------------------------------------------------------------

  const addTabForSession = useCallback(
    (session: Session, tabsetNode?: TabSetNode) => {
      const model = modelRef.current;
      if (!model) return;
      const tabset = tabsetNode ?? getTargetTabset(model);
      if (!tabset) return;

      let component: string;
      let tabConfig: Record<string, unknown>;

      switch (session.kind) {
        case "terminal":
          component = PANEL_TERMINAL;
          tabConfig = {
            sessionId: session.id,
            terminalId: session.config?.terminal_id as string | undefined,
            workspaceId: session.workspace_id,
          };
          break;
        case "document":
          component = PANEL_CODE_EDITOR;
          tabConfig = {
            sessionId: session.id,
            filePath: session.config?.file_path as string | undefined,
            workspaceId: session.workspace_id,
          };
          break;
        default: // "agent"
          component = PANEL_AGENT_CHAT;
          tabConfig = { sessionId: session.id };
          break;
      }

      model.doAction(
        Actions.addNode(
          {
            type: "tab",
            name: session.title,
            component,
            config: tabConfig,
          },
          tabset.getId(),
          DockLocation.CENTER,
          -1,
          true,
        ),
      );
      setActiveSessionId(session.id);
    },
    [],
  );

  // Handle RpcMenu result (from menu.execute)
  // 状态更新由 event handler 统一处理，这里只做发起者特有的 UI 操作（打开 tab）
  const handleMenuResult = useCallback(
    async (result: MenuExecResult, tabsetNode?: TabSetNode) => {
      if (result.error || result.action === "error") return;

      if (result.action === "session_created") {
        const sessionId = result.data.session_id as string;
        if (!sessionId || !rpcRef.current) return;
        try {
          const session: Session = await rpcRef.current.call("session.get", { session_id: sessionId });
          addTabForSession(session, tabsetNode);
        } catch {
          // 静默处理
        }
      }
    },
    [addTabForSession],
  );

  // ------------------------------------------------------------------
  // Header menu actions (SessionList/Header)
  // ------------------------------------------------------------------

  const handleHeaderAction = useCallback(
    async (action: string, _data: Record<string, unknown>) => {
      if (action === "run_setup_wizard") {
        if (!rpcRef.current || !workspace) return;
        try {
          const session: Session = await rpcRef.current.call("session.create", {
            workspace_id: workspace.id,
            type: "mutbot.builtins.guide.GuideSession",
            config: { initial_message: "__setup__", force_setup: true },
          });
          addTabForSession(session);
        } catch { /* silent */ }
      } else if (action === "close_workspace") {
        location.hash = "";
      }
    },
    [workspace, addTabForSession],
  );

  // ------------------------------------------------------------------
  // Welcome page: create session directly (bypasses menu system)
  // ------------------------------------------------------------------

  const handleCreateSession = useCallback(
    async (sessionType: string) => {
      if (!rpcRef.current || !workspace) return;
      try {
        const session: Session = await rpcRef.current.call("session.create", {
          workspace_id: workspace.id,
          type: sessionType,
        });
        addTabForSession(session);
      } catch { /* silent */ }
    },
    [workspace, addTabForSession],
  );

  // ------------------------------------------------------------------
  // Session selection from list
  // ------------------------------------------------------------------

  const handleSelectSession = useCallback(
    (id: string) => {
      setActiveSessionId(id);
      const model = modelRef.current;
      if (!model) return;

      const session = sessions.find((s) => s.id === id);
      if (!session) return;

      // Determine which panel component to look for
      const componentMap: Record<string, string> = {
        agent: PANEL_AGENT_CHAT,
        terminal: PANEL_TERMINAL,
        document: PANEL_CODE_EDITOR,
      };
      const targetComponent = componentMap[session.kind] || PANEL_AGENT_CHAT;

      // Check if tab for this session already exists
      let existingNodeId: string | null = null;
      model.visitNodes((node) => {
        if (node.getType() === "tab") {
          const tabNode = node as TabNode;
          const config = tabNode.getConfig();
          if (tabNode.getComponent() === targetComponent) {
            // Primary match: by sessionId
            if (config?.sessionId === id) {
              existingNodeId = node.getId();
            }
            // Fallback for Terminal: match by terminalId
            else if (
              !existingNodeId &&
              targetComponent === PANEL_TERMINAL &&
              session.config?.terminal_id &&
              config?.terminalId === session.config.terminal_id
            ) {
              existingNodeId = node.getId();
              // Patch the missing sessionId into the tab config
              model.doAction(Actions.updateNodeAttributes(node.getId(), {
                config: { ...config, sessionId: id },
              }));
            }
          }
        }
      });

      if (existingNodeId) {
        model.doAction(Actions.selectTab(existingNodeId));
      } else {
        addTabForSession(session);
      }
    },
    [sessions, workspace, addTabForSession],
  );

  // ------------------------------------------------------------------
  // Tab close handling
  // ------------------------------------------------------------------

  const handleAction = useCallback(
    (action: Action): Action | undefined => {
      const model = modelRef.current;
      if (!model) return action;

      // Tab 切换同步 activeSessionId
      if (action.type === Actions.SELECT_TAB) {
        const nodeId = (action as any).data?.tabNode;
        if (nodeId) {
          let tabNode: TabNode | null = null;
          model.visitNodes((node) => {
            if (node.getId() === nodeId && node.getType() === "tab") {
              tabNode = node as TabNode;
            }
          });
          if (tabNode) {
            const sessionId = resolveSessionId(tabNode, sessions);
            if (sessionId) setActiveSessionId(sessionId);
          }
        }
        return action;
      }

      // 跨 tabset 切换焦点同步 activeSessionId
      if (action.type === Actions.SET_ACTIVE_TABSET) {
        const tabsetId = (action as any).data?.tabsetNode;
        if (tabsetId) {
          let foundTabset: TabSetNode | null = null;
          model.visitNodes((node) => {
            if (node.getId() === tabsetId && node.getType() === "tabset") {
              foundTabset = node as TabSetNode;
            }
          });
          const tsNode = foundTabset as TabSetNode | null;
          if (tsNode) {
            const selectedNode = tsNode.getSelectedNode();
            if (selectedNode && selectedNode.getType() === "tab") {
              const sessionId = resolveSessionId(selectedNode as TabNode, sessions);
              if (sessionId) setActiveSessionId(sessionId);
            }
          }
        }
        return action;
      }

      // Intercept tab rename to sync with session title
      if (action.type === Actions.RENAME_TAB) {
        const nodeId = (action as any).data?.node;
        const newName = (action as any).data?.text;
        if (nodeId && newName) {
          let tabNode: TabNode | null = null;
          model.visitNodes((node) => {
            if (node.getId() === nodeId && node.getType() === "tab") {
              tabNode = node as TabNode;
            }
          });
          const sessionId = tabNode ? resolveSessionId(tabNode, sessions) : undefined;
          if (sessionId) {
            const sid = sessionId;
            rpcRef.current?.call("session.update", { session_id: sid, title: newName });
          }
        }
        return action;
      }

      // Tab close = just close the panel, session stays active
      if (action.type === Actions.DELETE_TAB) {
        return action;
      }

      return action;
    },
    [sessions],
  );

  // ------------------------------------------------------------------
  // Window blur/focus: clear activeSessionId when window loses focus
  // ------------------------------------------------------------------

  useEffect(() => {
    const handleBlur = () => {
      setActiveSessionId(null);
    };
    const handleFocus = () => {
      // 恢复焦点时，从当前 active tab 恢复 activeSessionId
      const model = modelRef.current;
      if (!model) return;
      const activeTabset = model.getActiveTabset();
      if (!activeTabset) return;
      const selectedNode = activeTabset.getSelectedNode();
      if (selectedNode && selectedNode.getType() === "tab") {
        const sessionId = resolveSessionId(selectedNode as TabNode, sessions);
        if (sessionId) setActiveSessionId(sessionId);
      }
    };
    window.addEventListener("blur", handleBlur);
    window.addEventListener("focus", handleFocus);
    return () => {
      window.removeEventListener("blur", handleBlur);
      window.removeEventListener("focus", handleFocus);
    };
  }, [sessions]);

  // ------------------------------------------------------------------
  // Layout callbacks
  // ------------------------------------------------------------------

  const layoutSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleModelChange = useCallback(
    (model: Model) => {
      // 更新 hasOpenTabs 状态
      const hasTabs = modelHasTabs(model);
      if (hasTabs !== hasOpenTabsRef.current) {
        hasOpenTabsRef.current = hasTabs;
        setHasOpenTabs(hasTabs);
      }

      if (!workspace) return;
      // Debounce layout saves to avoid flooding the server during drag resize
      if (layoutSaveTimer.current) clearTimeout(layoutSaveTimer.current);
      layoutSaveTimer.current = setTimeout(() => {
        const json = model.toJson();
        rpcRef.current?.call("workspace.update", { workspace_id: workspace.id, layout: json });
      }, 300);
    },
    [workspace],
  );

  const handleUpdateTabConfig = useCallback(
    (nodeId: string, config: Record<string, unknown>) => {
      const model = modelRef.current;
      if (!model) return;
      model.doAction(Actions.updateNodeAttributes(nodeId, { config }));
      // Sync terminal_id to backend session when PTY is recreated
      const sessionId = config.sessionId as string | undefined;
      const terminalId = config.terminalId as string | undefined;
      if (sessionId && terminalId) {
        rpcRef.current?.call("session.update", {
          session_id: sessionId,
          config: { terminal_id: terminalId },
          status: "running",
        });
      }
    },
    [],
  );

  const handleTerminalExited = useCallback(
    (sessionId: string) => {
      // Sync session status to "stopped" when terminal process exits
      rpcRef.current?.call("session.update", { session_id: sessionId, status: "stopped" }).catch(() => {});
    },
    [],
  );

  // ------------------------------------------------------------------
  // Sidebar collapse/expand
  // ------------------------------------------------------------------

  const handleSidebarModeChange = useCallback(
    (collapsed: boolean) => {
      setSidebarCollapsed(collapsed);
    },
    [],
  );

  // Sidebar resize by dragging
  const handleSidebarResizeStart = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      sidebarResizing.current = true;
      const startX = e.clientX;
      const startWidth = sidebarWidth;

      const onMouseMove = (ev: MouseEvent) => {
        const newWidth = Math.max(150, Math.min(600, startWidth + ev.clientX - startX));
        setSidebarWidth(newWidth);
      };

      const onMouseUp = () => {
        sidebarResizing.current = false;
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      };

      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [sidebarWidth],
  );

  // ------------------------------------------------------------------
  // Session close/delete from context menu
  // ------------------------------------------------------------------

  const handleRenameSession = useCallback(
    (sessionId: string, newTitle: string) => {
      rpcRef.current?.call("session.update", { session_id: sessionId, title: newTitle });
    },
    [],
  );

  const handleReorderSessions = useCallback(
    (sessionIds: string[]) => {
      setSessions((prev) => {
        const map = new Map(prev.map((s) => [s.id, s]));
        return sessionIds.map((id) => map.get(id)).filter(Boolean) as Session[];
      });
    },
    [],
  );

  const handleDeleteSessions = useCallback(
    (sessionIds: string[]) => {
      if (sessionIds.length === 1) {
        rpcRef.current?.call("session.delete", { session_id: sessionIds[0] }).catch(() => {});
      } else if (sessionIds.length > 1) {
        rpcRef.current?.call("session.delete_batch", { session_ids: sessionIds }).catch(() => {});
      }
    },
    [],
  );

  // ------------------------------------------------------------------
  // Tab context menu (RpcMenu context mode)
  // ------------------------------------------------------------------

  const handleLayoutContextMenu = useCallback(
    (e: React.MouseEvent) => {
      // Walk up from click target to find a tab button
      let el = e.target as HTMLElement | null;
      while (el && !el.classList.contains("flexlayout__tab_button")) {
        if (el.classList.contains("flexlayout__layout")) break;
        el = el.parentElement;
      }
      if (!el || !el.classList.contains("flexlayout__tab_button")) return;

      const model = modelRef.current;
      if (!model) return;

      // Resolve the actual tab node ID from the DOM element.
      let matchedNodeId: string | null = null;
      const layoutPath = el.getAttribute("data-layout-path");
      if (layoutPath) {
        const tabPath = layoutPath.replace(/\/tb(\d+)$/, "/t$1");
        model.visitNodes((node) => {
          if (node.getType() === "tab" && node.getPath() === tabPath) {
            matchedNodeId = node.getId();
          }
        });
      }

      if (!matchedNodeId) return;

      e.preventDefault();
      setTabContextMenu({
        position: { x: e.clientX, y: e.clientY },
        nodeId: matchedNodeId,
      });
    },
    [],
  );

  // Resolve tab context menu session info
  const tabContextSession = (() => {
    if (!tabContextMenu) return null;
    const model = modelRef.current;
    if (!model) return null;
    let tabNode: TabNode | null = null;
    model.visitNodes((n) => {
      if (n.getId() === tabContextMenu.nodeId && n.getType() === "tab") tabNode = n as TabNode;
    });
    const sessionId = tabNode ? resolveSessionId(tabNode, sessions) : undefined;
    const session = sessionId ? sessions.find((s) => s.id === sessionId) : undefined;
    return { tabNode, session };
  })();

  const handleTabClientAction = useCallback(
    (action: string, _data: Record<string, unknown>) => {
      if (!tabContextMenu) return;
      const { nodeId } = tabContextMenu;
      const model = modelRef.current;
      if (!model) return;

      if (action === "start_rename") {
        let tabNode: TabNode | null = null;
        model.visitNodes((n) => {
          if (n.getId() === nodeId && n.getType() === "tab") tabNode = n as TabNode;
        });
        if (tabNode && layoutRef.current) {
          const internal = layoutRef.current.selfRef?.current;
          if (internal?.setEditingTab) {
            internal.setEditingTab(tabNode);
          }
        }
      } else if (action === "close_tab") {
        model.doAction(Actions.deleteTab(nodeId));
      } else if (action === "close_others") {
        let parentId: string | null = null;
        model.visitNodes((node) => {
          if (node.getId() === nodeId && node.getType() === "tab") {
            parentId = node.getParent()?.getId() ?? null;
          }
        });
        if (!parentId) return;
        const toClose: string[] = [];
        model.visitNodes((node) => {
          if (
            node.getType() === "tab" &&
            node.getParent()?.getId() === parentId &&
            node.getId() !== nodeId
          ) {
            toClose.push(node.getId());
          }
        });
        for (const id of toClose) {
          model.doAction(Actions.deleteTab(id));
        }
      } else if (action === "change_icon") {
        if (tabContextSession?.session) {
          setIconPicker({
            position: tabContextMenu.position,
            sessionId: tabContextSession.session.id,
          });
        }
      } else if (action === "close_all") {
        let parentId: string | null = null;
        model.visitNodes((node) => {
          if (node.getId() === nodeId && node.getType() === "tab") {
            parentId = node.getParent()?.getId() ?? null;
          }
        });
        if (!parentId) return;
        const toClose: string[] = [];
        model.visitNodes((node) => {
          if (node.getType() === "tab" && node.getParent()?.getId() === parentId) {
            toClose.push(node.getId());
          }
        });
        for (const id of toClose) {
          model.doAction(Actions.deleteTab(id));
        }
      }
    },
    [tabContextMenu, tabContextSession],
  );

  const handleTabMenuResult = useCallback(
    (_result: MenuExecResult) => {
      // 状态更新由 event handler 统一处理
    },
    [],
  );

  // --- Icon Picker handlers ---

  const handleIconSelect = useCallback(
    async (iconName: string) => {
      if (!iconPicker || !rpc) return;
      try {
        await rpc.call("session.update", {
          session_id: iconPicker.sessionId,
          config: { icon: iconName },
        });
      } catch { /* silent */ }
      setIconPicker(null);
    },
    [iconPicker, rpc],
  );

  const handleIconReset = useCallback(async () => {
    if (!iconPicker || !rpc) return;
    try {
      await rpc.call("session.update", {
        session_id: iconPicker.sessionId,
        config: { icon: null },
      });
    } catch { /* silent */ }
    setIconPicker(null);
  }, [iconPicker, rpc]);

  const handleIconPickerClose = useCallback(() => {
    setIconPicker(null);
  }, []);

  // ------------------------------------------------------------------
  // Tabset "+" button rendering
  // ------------------------------------------------------------------

  const onRenderTabSet = useCallback(
    (
      tabSetNode: TabSetNode | BorderNode,
      renderValues: ITabSetRenderValues,
    ) => {
      // Don't add "+" button to border panels (Session list border)
      if (tabSetNode.getType() === "border") return;

      const tsNode = tabSetNode as TabSetNode;
      renderValues.stickyButtons.push(
        <RpcMenu
          key="add-session"
          rpc={rpc}
          category="SessionPanel/Add"
          trigger={
            <button className="add-session-btn" title="New Session">+</button>
          }
          onResult={(result) => handleMenuResult(result, tsNode)}
        />,
      );
    },
    [rpc, handleMenuResult],
  );

  // ------------------------------------------------------------------
  // Tab icon rendering
  // ------------------------------------------------------------------

  const onRenderTab = useCallback(
    (node: TabNode, renderValues: ITabRenderValues) => {
      // 优先从 session kind 获取图标，回退到 component 类型
      const config = node.getConfig() as Record<string, unknown> | undefined;
      const sessionId = config?.sessionId as string | undefined;
      const session = sessionId ? sessions.find((s) => s.id === sessionId) : undefined;

      let kind: string | null = null;
      if (session) {
        kind = session.kind;
      } else {
        const component = node.getComponent();
        if (component === PANEL_AGENT_CHAT) kind = "agent";
        else if (component === PANEL_TERMINAL) kind = "terminal";
        else if (component === PANEL_CODE_EDITOR) kind = "document";
      }

      if (kind) {
        renderValues.leading = getSessionIcon(kind, 16, "#858585", session?.icon);
      }
    },
    [sessions],
  );

  // ------------------------------------------------------------------
  // Factory
  // ------------------------------------------------------------------

  const factory = useCallback(
    (node: TabNode) => {
      return panelFactory(node, {
        sessions,
        activeSessionId,
        workspaceId: workspace?.id ?? null,
        rpc,
        onSelectSession: handleSelectSession,
        onUpdateTabConfig: handleUpdateTabConfig,
        onTerminalExited: handleTerminalExited,
      });
    },
    [sessions, activeSessionId, workspace, rpc, handleSelectSession, handleUpdateTabConfig, handleTerminalExited],
  );

  // 无工作区 → 显示工作区选择器（或远程降级页）
  if (!workspace) {
    // 远程模式连接失败 → 降级提示
    if (isRemote() && remoteConnected === false) {
      return (
        <div className="remote-fallback">
          <div className="remote-fallback-card">
            <h1>MutBot</h1>
            <p className="remote-fallback-subtitle">Define Your AI</p>
            <div className="remote-fallback-warning">
              Could not connect to local MutBot at localhost:8741.
              <br />
              Make sure MutBot is running, then try again.
            </div>
            <div className="remote-fallback-actions">
              <button onClick={() => location.reload()}>Retry</button>
              <a href="http://localhost:8741/">Open localhost:8741</a>
              <a href="/">Back to mutbot.ai</a>
            </div>
          </div>
        </div>
      );
    }

    return (
      <WorkspaceSelector
        workspaces={workspaces}
        appRpc={appRpc}
        onSelect={(ws) => {
          location.hash = ws.name;
          setWorkspace(ws);
        }}
        onCreated={(ws) => {
          setWorkspaces((prev) => [...prev, ws]);
          location.hash = ws.name;
          setWorkspace(ws);
        }}
        onRemoved={(wsId) => {
          setWorkspaces((prev) => prev.filter((w) => w.id !== wsId));
        }}
      />
    );
  }

  const model = modelRef.current;

  return (
    <div className="app-root">
      <div className="app-layout">
        <div
          className={`sidebar ${sidebarCollapsed ? "collapsed" : ""}`}
          style={sidebarCollapsed ? undefined : { width: sidebarWidth }}
        >
          <SessionListPanel
            sessions={sessions}
            activeSessionId={activeSessionId}
            rpc={rpc}
            onSelect={handleSelectSession}
            onModeChange={handleSidebarModeChange}
            onDeleteSessions={handleDeleteSessions}
            onRenameSession={handleRenameSession}
            onReorderSessions={handleReorderSessions}
            onChangeIcon={(sessionId, position) => setIconPicker({ sessionId, position })}
            onHeaderAction={handleHeaderAction}
            onMenuResult={handleMenuResult}
          />
        </div>
        {!sidebarCollapsed && (
          <div className="sidebar-resize-handle" onMouseDown={handleSidebarResizeStart} />
        )}
        <div className="main-content" onContextMenu={handleLayoutContextMenu}>
          {model && (
            <Layout
              ref={layoutRef}
              model={model}
              factory={factory}
              onModelChange={handleModelChange}
              onRenderTabSet={onRenderTabSet}
              onRenderTab={onRenderTab}
              onAction={handleAction}
              realtimeResize={true}
            />
          )}
          {!hasOpenTabs && (
            <WelcomePage rpc={rpc} onCreateSession={handleCreateSession} />
          )}
        </div>
      </div>
      {tabContextMenu && (
        <RpcMenu
          rpc={rpc}
          category="Tab/Context"
          context={{
            session_id: tabContextSession?.session?.id ?? "",
            session_type: tabContextSession?.session?.type ?? "",
            session_status: tabContextSession?.session?.status ?? "",
          }}
          position={tabContextMenu.position}
          onClose={() => setTabContextMenu(null)}
          onResult={handleTabMenuResult}
          onClientAction={handleTabClientAction}
        />
      )}
      {iconPicker && (
        <IconPicker
          position={iconPicker.position}
          onSelect={handleIconSelect}
          onReset={handleIconReset}
          onClose={handleIconPickerClose}
        />
      )}
      {toast && (
        <div className="toast-notification" onClick={() => setToast(null)}>
          {toast}
        </div>
      )}
    </div>
  );
}
