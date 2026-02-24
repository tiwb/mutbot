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
  type IJsonModel,
  type ITabSetRenderValues,
  type ITabRenderValues,
} from "flexlayout-react";
import "flexlayout-react/style/dark.css";
import {
  fetchWorkspaces,
  fetchSessions,
  getSession,
  updateWorkspaceLayout,
  checkAuthStatus,
  login,
  setAuthToken,
  getAuthToken,
  stopSession,
  deleteSession,
  renameSession,
  updateSession,
} from "./lib/api";
import {
  createModel,
  PANEL_AGENT_CHAT,
  PANEL_TERMINAL,
  PANEL_CODE_EDITOR,
} from "./lib/layout";
import { panelFactory } from "./panels/PanelFactory";
import SessionListPanel from "./panels/SessionListPanel";
import ContextMenu, { type ContextMenuItem } from "./components/ContextMenu";
import RpcMenu, { type MenuExecResult } from "./components/RpcMenu";
import { WorkspaceRpc } from "./lib/workspace-rpc";

// ---------- Tab icons (inline SVG, 16px) ----------

function TabIcon({ type }: { type: string }) {
  const color = "#858585";
  const size = 16;
  switch (type) {
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

interface Workspace {
  id: string;
  name: string;
  sessions: string[];
  layout?: IJsonModel | null;
}

interface Session {
  id: string;
  workspace_id: string;
  title: string;
  type: string;
  status: string;
  config?: Record<string, unknown> | null;
}

/** Find the active tabset, or fall back to the first tabset in the model. */
function getTargetTabset(model: Model): TabSetNode | undefined {
  return model.getActiveTabset() ?? model.getFirstTabSet();
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
// Login screen
// ---------------------------------------------------------------------------

function LoginScreen({ onLogin }: { onLogin: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const result = await login(username, password);
      if (result.token) {
        setAuthToken(result.token);
        onLogin();
      } else if (result.error) {
        setError(result.error);
      }
    } catch {
      setError("Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-form" onSubmit={handleSubmit}>
        <h2>MutBot Login</h2>
        {error && <div className="login-error">{error}</div>}
        <input
          type="text"
          placeholder="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoFocus
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <button type="submit" disabled={loading}>
          {loading ? "Logging in..." : "Login"}
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Terminal close confirmation dialog
// ---------------------------------------------------------------------------

function ConfirmDialog({
  message,
  confirmLabel,
  cancelLabel,
  dismissLabel,
  onConfirm,
  onCancel,
  onDismiss,
}: {
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  dismissLabel?: string;
  onConfirm: () => void;
  onCancel: () => void;
  onDismiss?: () => void;
}) {
  return (
    <div className="confirm-overlay">
      <div className="confirm-dialog">
        <p>{message}</p>
        <div className="confirm-actions">
          <button className="confirm-btn-primary" onClick={onConfirm}>{confirmLabel}</button>
          <button className="confirm-btn-secondary" onClick={onCancel}>{cancelLabel}</button>
          {dismissLabel && onDismiss && (
            <button className="confirm-btn-secondary" onClick={onDismiss}>{dismissLabel}</button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------

export default function App() {
  const [authChecked, setAuthChecked] = useState(false);
  const [authRequired, setAuthRequired] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);

  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const modelRef = useRef<Model | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- setEditingTab is internal API
  const layoutRef = useRef<any>(null);
  const [, forceUpdate] = useState(0);

  // Pending "End Session" confirmation state
  const [pendingEndSession, setPendingEndSession] = useState<{
    nodeId?: string;   // if set, also close the tab
    sessionId: string;
  } | null>(null);

  // Tab context menu state
  const [tabContextMenu, setTabContextMenu] = useState<{
    position: { x: number; y: number };
    nodeId: string;
  } | null>(null);

  // Sidebar collapsed state
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(260);
  const sidebarResizing = useRef(false);

  // Workspace RPC 连接
  const rpcRef = useRef<WorkspaceRpc | null>(null);
  const [rpc, setRpc] = useState<WorkspaceRpc | null>(null);

  // Initialize model once workspace is available
  if (!modelRef.current) {
    modelRef.current = createModel();
  }

  // Check auth status on mount
  useEffect(() => {
    checkAuthStatus()
      .then((status) => {
        setAuthRequired(status.auth_required);
        if (!status.auth_required) {
          setAuthenticated(true);
        }
        setAuthChecked(true);
      })
      .catch(() => {
        // If we can't reach the server, proceed without auth
        setAuthChecked(true);
        setAuthenticated(true);
      });
  }, []);

  // Load default workspace once authenticated
  useEffect(() => {
    if (!authenticated) return;
    fetchWorkspaces().then((wss: Workspace[]) => {
      if (wss.length > 0) {
        const ws = wss[0]!;
        setWorkspace(ws);
        if (ws.layout) {
          try {
            modelRef.current = createModel(ws.layout);
            forceUpdate((n) => n + 1);
          } catch {
            // Fall back to default layout on parse error
          }
        }
        fetchSessions(ws.id).then(setSessions);
      }
    });
  }, [authenticated]);

  // Initialize WorkspaceRpc when workspace is available
  useEffect(() => {
    if (!workspace) return;
    const wsRpc = new WorkspaceRpc(workspace.id, {
      tokenFn: getAuthToken,
    });
    rpcRef.current = wsRpc;
    setRpc(wsRpc);
    return () => {
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

      switch (session.type) {
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
  const handleMenuResult = useCallback(
    async (result: MenuExecResult, tabsetNode?: TabSetNode) => {
      if (result.error || result.action === "error") return;

      if (result.action === "session_created") {
        const sessionId = result.data.session_id as string;
        if (!sessionId) return;
        try {
          const session: Session = await getSession(sessionId);
          setSessions((prev) => [...prev, session]);
          addTabForSession(session, tabsetNode);
        } catch {
          // 静默处理
        }
      }
    },
    [addTabForSession],
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
      const targetComponent = componentMap[session.type] || PANEL_AGENT_CHAT;

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
            renameSession(sid, newName).then(() => {
              setSessions((prev) =>
                prev.map((s) => (s.id === sid ? { ...s, title: newName } : s)),
              );
            });
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
  // End Session handling
  // ------------------------------------------------------------------

  const handleEndSessionConfirm = useCallback(() => {
    if (!pendingEndSession) return;
    const { nodeId, sessionId } = pendingEndSession;
    stopSession(sessionId).then(() => {
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, status: "ended" } : s)),
      );
    });
    if (nodeId) {
      const model = modelRef.current;
      if (model) model.doAction(Actions.deleteTab(nodeId));
    }
    setPendingEndSession(null);
  }, [pendingEndSession]);

  const handleEndSessionCancel = useCallback(() => {
    setPendingEndSession(null);
  }, []);

  // ------------------------------------------------------------------
  // Layout callbacks
  // ------------------------------------------------------------------

  const layoutSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleModelChange = useCallback(
    (model: Model) => {
      if (!workspace) return;
      // Debounce layout saves to avoid flooding the server during drag resize
      if (layoutSaveTimer.current) clearTimeout(layoutSaveTimer.current);
      layoutSaveTimer.current = setTimeout(() => {
        const json = model.toJson();
        updateWorkspaceLayout(workspace.id, json);
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
        updateSession(sessionId, { config: { terminal_id: terminalId }, status: "active" });
        setSessions((prev) =>
          prev.map((s) =>
            s.id === sessionId
              ? { ...s, status: "active", config: { ...s.config, terminal_id: terminalId } }
              : s,
          ),
        );
      }
    },
    [],
  );

  const handleTerminalExited = useCallback(
    (sessionId: string) => {
      // Sync session status to "ended" when terminal process exits
      updateSession(sessionId, { status: "ended" }).catch(() => {});
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, status: "ended" } : s)),
      );
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

  const handleEndSession = useCallback(
    (sessionId: string) => {
      const session = sessions.find((s) => s.id === sessionId);
      if (!session || session.status === "ended") return;
      // Show confirmation dialog
      setPendingEndSession({ sessionId });
    },
    [sessions],
  );

  const handleRenameSession = useCallback(
    (sessionId: string, newTitle: string) => {
      renameSession(sessionId, newTitle).then(() => {
        setSessions((prev) =>
          prev.map((s) => (s.id === sessionId ? { ...s, title: newTitle } : s)),
        );
        // Also update the flexlayout tab name if it exists
        const model = modelRef.current;
        if (model) {
          model.visitNodes((node) => {
            if (node.getType() === "tab") {
              const tabNode = node as TabNode;
              if (tabNode.getConfig()?.sessionId === sessionId) {
                model.doAction(Actions.renameTab(node.getId(), newTitle));
              }
            }
          });
        }
      });
    },
    [],
  );

  const handleDeleteSession = useCallback(
    (sessionId: string) => {
      // Soft-delete: stops + marks deleted on backend
      deleteSession(sessionId)
        .catch(() => {})
        .finally(() => {
          setSessions((prev) => prev.filter((s) => s.id !== sessionId));
          // Close any open tab for this session
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
        });
    },
    [],
  );

  // ------------------------------------------------------------------
  // Tab context menu
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
      // flexlayout-react's TabButton sets data-layout-path to "{tabsetPath}/tb{index}"
      // while the corresponding tab node's path is "{tabsetPath}/t{index}".
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

  const getTabContextMenuItems = useCallback((): ContextMenuItem[] => {
    if (!tabContextMenu) return [];
    const { nodeId } = tabContextMenu;
    const model = modelRef.current;
    if (!model) return [];

    // Find the tab node and resolve its session
    let tabNode: TabNode | null = null;
    model.visitNodes((n) => {
      if (n.getId() === nodeId && n.getType() === "tab") tabNode = n as TabNode;
    });
    const sessionId = tabNode ? resolveSessionId(tabNode, sessions) : undefined;
    const session = sessionId ? sessions.find((s) => s.id === sessionId) : undefined;

    const items: ContextMenuItem[] = [
      {
        label: "Rename",
        onClick: () => {
          if (tabNode && layoutRef.current) {
            // setEditingTab is on LayoutInternal, accessed via Layout's selfRef
            const internal = layoutRef.current.selfRef?.current;
            if (internal?.setEditingTab) {
              internal.setEditingTab(tabNode);
            }
          }
        },
      },
      {
        label: "Close",
        onClick: () => {
          model.doAction(Actions.deleteTab(nodeId));
        },
      },
      {
        label: "Close Others",
        onClick: () => {
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
        },
      },
    ];

    // Add "End Session" for active sessions
    if (session && session.status !== "ended") {
      items.push(
        { label: "", separator: true },
        {
          label: "End Session",
          onClick: () => {
            setPendingEndSession({ nodeId, sessionId: session.id });
          },
        },
      );
    }

    return items;
  }, [tabContextMenu, sessions]);

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
      const component = node.getComponent();
      // Determine session type from component
      let type: string | null = null;
      if (component === PANEL_AGENT_CHAT) type = "agent";
      else if (component === PANEL_TERMINAL) type = "terminal";
      else if (component === PANEL_CODE_EDITOR) type = "document";

      if (type) {
        renderValues.leading = <TabIcon type={type} />;
      }
    },
    [],
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
        onSelectSession: handleSelectSession,
        onUpdateTabConfig: handleUpdateTabConfig,
        onTerminalExited: handleTerminalExited,
      });
    },
    [sessions, activeSessionId, workspace, handleSelectSession, handleUpdateTabConfig, handleTerminalExited],
  );

  // Show nothing while checking auth
  if (!authChecked) {
    return null;
  }

  // Show login screen if auth is required and not yet authenticated
  if (authRequired && !authenticated) {
    return <LoginScreen onLogin={() => setAuthenticated(true)} />;
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
            onSelect={handleSelectSession}
            onModeChange={handleSidebarModeChange}
            onCloseSession={handleEndSession}
            onDeleteSession={handleDeleteSession}
            onRenameSession={handleRenameSession}
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
        </div>
      </div>
      {pendingEndSession && (
        <ConfirmDialog
          message="End this session? The process will be terminated."
          confirmLabel="End Session"
          cancelLabel="Cancel"
          onConfirm={handleEndSessionConfirm}
          onCancel={handleEndSessionCancel}
        />
      )}
      {tabContextMenu && (
        <ContextMenu
          items={getTabContextMenuItems()}
          position={tabContextMenu.position}
          onClose={() => setTabContextMenu(null)}
        />
      )}
    </div>
  );
}
