import { useCallback, useEffect, useRef, useState } from "react";
import {
  Layout,
  type Model,
  Actions,
  DockLocation,
  type TabNode,
  type TabSetNode,
  type IJsonModel,
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
} from "./lib/api";
import {
  createModel,
  PANEL_AGENT_CHAT,
  PANEL_TERMINAL,
  PANEL_LOG,
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
  status: string;
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

  const handleNewSession = useCallback(async () => {
    if (!workspace) return;
    const session: Session = await createSession(workspace.id);
    setSessions((prev) => [...prev, session]);
    addAgentChatTab(session.id, session.title || `Session ${session.id.slice(0, 8)}`);
  }, [workspace]);

  const handleSelectSession = useCallback(
    (id: string) => {
      setActiveSessionId(id);
      const model = modelRef.current;
      if (!model) return;

      // Check if tab for this session already exists
      let existingNodeId: string | null = null;
      model.visitNodes((node) => {
        if (node.getType() === "tab") {
          const tabNode = node as TabNode;
          if (
            tabNode.getComponent() === PANEL_AGENT_CHAT &&
            tabNode.getConfig()?.sessionId === id
          ) {
            existingNodeId = node.getId();
          }
        }
      });

      if (existingNodeId) {
        model.doAction(Actions.selectTab(existingNodeId));
      } else {
        const session = sessions.find((s) => s.id === id);
        const name = session?.title || `Session ${id.slice(0, 8)}`;
        addAgentChatTab(id, name);
      }
    },
    [sessions],
  );

  function addAgentChatTab(sessionId: string, name: string) {
    const model = modelRef.current;
    if (!model) return;
    const tabset = getTargetTabset(model);
    if (!tabset) return;
    model.doAction(
      Actions.addNode(
        {
          type: "tab",
          name,
          component: PANEL_AGENT_CHAT,
          config: { sessionId },
        },
        tabset.getId(),
        DockLocation.CENTER,
        -1,
        true,
      ),
    );
    setActiveSessionId(sessionId);
  }

  const handleAddTerminal = useCallback(() => {
    const model = modelRef.current;
    if (!model || !workspace) return;
    const tabset = getTargetTabset(model);
    if (!tabset) return;
    model.doAction(
      Actions.addNode(
        {
          type: "tab",
          name: "Terminal",
          component: PANEL_TERMINAL,
          config: { workspaceId: workspace.id },
        },
        tabset.getId(),
        DockLocation.CENTER,
        -1,
        true,
      ),
    );
  }, [workspace]);

  const handleAddLogs = useCallback(() => {
    const model = modelRef.current;
    if (!model) return;

    // Check if log tab already exists
    let existingNodeId: string | null = null;
    model.visitNodes((node) => {
      if (node.getType() === "tab") {
        const tabNode = node as TabNode;
        if (tabNode.getComponent() === PANEL_LOG) {
          existingNodeId = node.getId();
        }
      }
    });

    if (existingNodeId) {
      model.doAction(Actions.selectTab(existingNodeId));
    } else {
      const tabset = getTargetTabset(model);
      if (!tabset) return;
      model.doAction(
        Actions.addNode(
          {
            type: "tab",
            name: "Logs",
            component: PANEL_LOG,
          },
          tabset.getId(),
          DockLocation.CENTER,
          -1,
          true,
        ),
      );
    }
  }, []);

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

  const factory = useCallback(
    (node: TabNode) => {
      return panelFactory(node, {
        sessions,
        activeSessionId,
        workspaceId: workspace?.id ?? null,
        onSelectSession: handleSelectSession,
        onNewSession: handleNewSession,
        onUpdateTabConfig: handleUpdateTabConfig,
      });
    },
    [sessions, activeSessionId, workspace, handleSelectSession, handleNewSession, handleUpdateTabConfig],
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
      <div className="app-toolbar">
        <button className="toolbar-btn" onClick={handleAddTerminal}>
          Terminal
        </button>
        <button className="toolbar-btn" onClick={handleAddLogs}>
          Logs
        </button>
      </div>
      <div className="app-layout">
        {model && (
          <Layout
            model={model}
            factory={factory}
            onModelChange={handleModelChange}
          />
        )}
      </div>
    </div>
  );
}
