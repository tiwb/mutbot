import { useCallback, useEffect, useState } from "react";
import { fetchWorkspaces, createSession, fetchSessions } from "./lib/api";
import SessionListPanel from "./panels/SessionListPanel";
import AgentPanel from "./panels/AgentPanel";

interface Workspace {
  id: string;
  name: string;
  sessions: string[];
}

interface Session {
  id: string;
  workspace_id: string;
  title: string;
  status: string;
}

export default function App() {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

  // Load default workspace on mount
  useEffect(() => {
    fetchWorkspaces().then((wss) => {
      if (wss.length > 0) {
        const ws = wss[0]!;
        setWorkspace(ws);
        fetchSessions(ws.id).then(setSessions);
      }
    });
  }, []);

  const handleNewSession = useCallback(async () => {
    if (!workspace) return;
    const session = await createSession(workspace.id);
    setSessions((prev) => [...prev, session]);
    setActiveSessionId(session.id);
  }, [workspace]);

  const handleSelectSession = useCallback((id: string) => {
    setActiveSessionId(id);
  }, []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-header">
          <h1>MutBot</h1>
        </div>
        <SessionListPanel
          sessions={sessions}
          activeSessionId={activeSessionId}
          onSelect={handleSelectSession}
          onNewSession={handleNewSession}
        />
      </aside>
      <main className="main-panel">
        {activeSessionId ? (
          <AgentPanel sessionId={activeSessionId} />
        ) : (
          <div className="empty-state">
            <p>Select or create a session to begin.</p>
          </div>
        )}
      </main>
    </div>
  );
}
