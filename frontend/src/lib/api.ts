const BASE = "";

export async function fetchWorkspaces() {
  const res = await fetch(`${BASE}/api/workspaces`);
  return res.json();
}

export async function createWorkspace(name: string, projectPath: string) {
  const res = await fetch(`${BASE}/api/workspaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, project_path: projectPath }),
  });
  return res.json();
}

export async function fetchSessions(workspaceId: string) {
  const res = await fetch(`${BASE}/api/workspaces/${workspaceId}/sessions`);
  return res.json();
}

export async function createSession(workspaceId: string) {
  const res = await fetch(`${BASE}/api/workspaces/${workspaceId}/sessions`, {
    method: "POST",
  });
  return res.json();
}

export async function stopSession(sessionId: string) {
  const res = await fetch(`${BASE}/api/sessions/${sessionId}`, {
    method: "DELETE",
  });
  return res.json();
}
