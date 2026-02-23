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

export async function updateWorkspaceLayout(
  workspaceId: string,
  layout: unknown,
) {
  const res = await fetch(`${BASE}/api/workspaces/${workspaceId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ layout }),
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

export async function createTerminal(
  workspaceId: string,
  rows: number,
  cols: number,
) {
  const res = await fetch(
    `${BASE}/api/workspaces/${workspaceId}/terminals`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows, cols }),
    },
  );
  return res.json();
}

export async function readFile(workspaceId: string, path: string) {
  const res = await fetch(
    `${BASE}/api/workspaces/${workspaceId}/file?path=${encodeURIComponent(path)}`,
  );
  return res.json();
}

export async function fetchLogs(
  pattern = "",
  level = "DEBUG",
  limit = 200,
) {
  const params = new URLSearchParams({ pattern, level, limit: String(limit) });
  const res = await fetch(`${BASE}/api/logs?${params}`);
  return res.json();
}
