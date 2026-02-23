import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
} from "flexlayout-react";
import "flexlayout-react/style/dark.css";
import {
  fetchWorkspaces,
  createSession,
  fetchSessions,
  updateWorkspaceLayout,
  checkAuthStatus,
  login,
  setAuthToken,
  stopSession,
} from "./lib/api";
import {
  createModel,
  PANEL_AGENT_CHAT,
  PANEL_TERMINAL,
  PANEL_CODE_EDITOR,
} from "./lib/layout";
import { panelFactory } from "./panels/PanelFactory";

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
// Add Session dropdown button (for tabset "+" button)
// ---------------------------------------------------------------------------

function AddSessionDropdown({
  onAdd,
}: {
  onAdd: (type: "agent" | "terminal" | "document") => void;
}) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuPos, setMenuPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });

  // Compute menu position from button rect
  const updateMenuPos = useCallback(() => {
    if (!btnRef.current) return;
    const rect = btnRef.current.getBoundingClientRect();
    setMenuPos({ top: rect.bottom + 2, left: rect.right - 120 });
  }, []);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e: PointerEvent) => {
      const target = e.target as Node;
      if (
        btnRef.current?.contains(target) ||
        menuRef.current?.contains(target)
      ) {
        return;
      }
      setOpen(false);
    };
    document.addEventListener("pointerdown", handler);
    return () => document.removeEventListener("pointerdown", handler);
  }, [open]);

  const handleToggle = useCallback(
    (e: React.PointerEvent) => {
      e.stopPropagation();
      e.preventDefault();
      if (!open) updateMenuPos();
      setOpen((v) => !v);
    },
    [open, updateMenuPos],
  );

  const handleSelect = useCallback(
    (type: "agent" | "terminal" | "document") => {
      setOpen(false);
      onAdd(type);
    },
    [onAdd],
  );

  return (
    <>
      <button
        ref={btnRef}
        className="add-session-btn"
        title="New Session"
        onPointerDown={handleToggle}
      >
        +
      </button>
      {open &&
        createPortal(
          <div
            ref={menuRef}
            className="add-session-menu"
            style={{ top: menuPos.top, left: menuPos.left }}
          >
            <button onPointerDown={(e) => e.stopPropagation()} onClick={() => handleSelect("agent")}>
              Agent
            </button>
            <button onPointerDown={(e) => e.stopPropagation()} onClick={() => handleSelect("document")}>
              Document
            </button>
            <button onPointerDown={(e) => e.stopPropagation()} onClick={() => handleSelect("terminal")}>
              Terminal
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Terminal close confirmation dialog
// ---------------------------------------------------------------------------

function ConfirmDialog({
  message,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
}: {
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="confirm-overlay">
      <div className="confirm-dialog">
        <p>{message}</p>
        <div className="confirm-actions">
          <button className="confirm-btn-primary" onClick={onConfirm}>{confirmLabel}</button>
          <button className="confirm-btn-secondary" onClick={onCancel}>{cancelLabel}</button>
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
  const [, forceUpdate] = useState(0);

  // Pending close confirmation state
  const [pendingClose, setPendingClose] = useState<{
    nodeId: string;
    sessionId: string;
  } | null>(null);

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

  const handleCreateSession = useCallback(
    async (type: "agent" | "terminal" | "document", tabsetNode?: TabSetNode) => {
      if (!workspace) return;
      const config = type === "document"
        ? { file_path: `untitled-${Date.now()}.md` }
        : undefined;
      const session: Session = await createSession(workspace.id, type, config);
      setSessions((prev) => [...prev, session]);
      addTabForSession(session, tabsetNode);
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
      const targetComponent = componentMap[session.type] || PANEL_AGENT_CHAT;

      // Check if tab for this session already exists
      let existingNodeId: string | null = null;
      model.visitNodes((node) => {
        if (node.getType() === "tab") {
          const tabNode = node as TabNode;
          if (
            tabNode.getComponent() === targetComponent &&
            tabNode.getConfig()?.sessionId === id
          ) {
            existingNodeId = node.getId();
          }
        }
      });

      if (existingNodeId) {
        model.doAction(Actions.selectTab(existingNodeId));
      } else {
        // For ended terminal sessions, create new PTY with same shell_command
        if (session.type === "terminal" && session.status === "ended" && workspace) {
          const shellCommand = (session.config?.shell_command as string) || "cmd.exe";
          createSession(workspace.id, "terminal", { shell_command: shellCommand }).then(
            (newSession: Session) => {
              setSessions((prev) => [...prev, newSession]);
              addTabForSession(newSession);
            },
          );
          return;
        }
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

      // Intercept tab close actions
      if (action.type === Actions.DELETE_TAB) {
        const nodeId = action.data?.node;
        if (!nodeId) return action;

        let tabNode: TabNode | null = null;
        model.visitNodes((node) => {
          if (node.getId() === nodeId && node.getType() === "tab") {
            tabNode = node as TabNode;
          }
        });

        if (!tabNode) return action;
        const config = (tabNode as TabNode).getConfig();
        const component = (tabNode as TabNode).getComponent();
        const sessionId = config?.sessionId as string | undefined;

        if (!sessionId) return action; // Not a session tab, allow close

        if (component === PANEL_TERMINAL) {
          // Terminal: show confirmation dialog
          setPendingClose({ nodeId, sessionId });
          return undefined; // Block the close action
        }

        if (component === PANEL_AGENT_CHAT) {
          // Agent: auto-end session on close
          stopSession(sessionId).then(() => {
            setSessions((prev) =>
              prev.map((s) => (s.id === sessionId ? { ...s, status: "ended" } : s)),
            );
          });
          return action; // Allow close
        }

        // Document: just close the tab, session stays active
        return action;
      }

      return action;
    },
    [],
  );

  const handleTerminalCloseConfirm = useCallback(() => {
    if (!pendingClose) return;
    const { nodeId, sessionId } = pendingClose;

    // End session + close tab
    stopSession(sessionId).then(() => {
      setSessions((prev) =>
        prev.map((s) => (s.id === sessionId ? { ...s, status: "ended" } : s)),
      );
    });

    const model = modelRef.current;
    if (model) {
      model.doAction(Actions.deleteTab(nodeId));
    }
    setPendingClose(null);
  }, [pendingClose]);

  const handleTerminalCloseCancel = useCallback(() => {
    if (!pendingClose) return;
    const { nodeId } = pendingClose;

    // Just close the tab, keep session active
    const model = modelRef.current;
    if (model) {
      model.doAction(Actions.deleteTab(nodeId));
    }
    setPendingClose(null);
  }, [pendingClose]);

  // ------------------------------------------------------------------
  // Layout callbacks
  // ------------------------------------------------------------------

  const handleModelChange = useCallback(
    (model: Model) => {
      if (!workspace) return;
      const json = model.toJson();
      updateWorkspaceLayout(workspace.id, json);
    },
    [workspace],
  );

  const handleUpdateTabConfig = useCallback(
    (nodeId: string, config: Record<string, unknown>) => {
      const model = modelRef.current;
      if (!model) return;
      model.doAction(Actions.updateNodeAttributes(nodeId, { config }));
    },
    [],
  );

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

      renderValues.stickyButtons.push(
        <AddSessionDropdown
          key="add-session"
          onAdd={(type) =>
            handleCreateSession(type, tabSetNode as TabSetNode)
          }
        />,
      );
    },
    [handleCreateSession],
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
      });
    },
    [sessions, activeSessionId, workspace, handleSelectSession, handleUpdateTabConfig],
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
        {model && (
          <Layout
            model={model}
            factory={factory}
            onModelChange={handleModelChange}
            onRenderTabSet={onRenderTabSet}
            onAction={handleAction}
          />
        )}
      </div>
      {pendingClose && (
        <ConfirmDialog
          message="End terminal session?"
          confirmLabel="End Session"
          cancelLabel="Close Panel Only"
          onConfirm={handleTerminalCloseConfirm}
          onCancel={handleTerminalCloseCancel}
        />
      )}
    </div>
  );
}
