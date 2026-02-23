const BASE = "";

// ---------------------------------------------------------------------------
// Auth token management (localStorage backed)
// ---------------------------------------------------------------------------

const TOKEN_KEY = "mutbot_auth_token";

export function getAuthToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setAuthToken(token: string | null): void {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

/**
 * Wrapper around fetch that injects Authorization header when a token exists.
 */
async function authFetch(
  input: string,
  init?: RequestInit,
): Promise<Response> {
  const token = getAuthToken();
  const headers = new Headers(init?.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(input, { ...init, headers });
}

// ---------------------------------------------------------------------------
// Auth API
// ---------------------------------------------------------------------------

export async function checkAuthStatus(): Promise<{ auth_required: boolean }> {
  const res = await fetch(`${BASE}/api/auth/status`);
  return res.json();
}

export async function login(
  username: string,
  password: string,
): Promise<{ token?: string; error?: string }> {
  const res = await fetch(`${BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  return res.json();
}

// ---------------------------------------------------------------------------
// Workspace API
// ---------------------------------------------------------------------------

export async function fetchWorkspaces() {
  const res = await authFetch(`${BASE}/api/workspaces`);
  return res.json();
}

export async function createWorkspace(name: string, projectPath: string) {
  const res = await authFetch(`${BASE}/api/workspaces`, {
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
  const res = await authFetch(`${BASE}/api/workspaces/${workspaceId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ layout }),
  });
  return res.json();
}

// ---------------------------------------------------------------------------
// Session API
// ---------------------------------------------------------------------------

export async function fetchSessions(workspaceId: string) {
  const res = await authFetch(
    `${BASE}/api/workspaces/${workspaceId}/sessions`,
  );
  return res.json();
}

export async function createSession(
  workspaceId: string,
  type: string = "agent",
  config?: Record<string, unknown>,
  extraBody?: Record<string, unknown>,
) {
  const res = await authFetch(
    `${BASE}/api/workspaces/${workspaceId}/sessions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, config, ...extraBody }),
    },
  );
  return res.json();
}

export async function stopSession(sessionId: string) {
  const res = await authFetch(`${BASE}/api/sessions/${sessionId}`, {
    method: "DELETE",
  });
  return res.json();
}

export async function fetchSessionEvents(
  sessionId: string,
): Promise<{ session_id: string; events: Record<string, unknown>[] }> {
  const res = await authFetch(`${BASE}/api/sessions/${sessionId}/events`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Terminal API
// ---------------------------------------------------------------------------

export async function createTerminal(
  workspaceId: string,
  rows: number,
  cols: number,
) {
  const res = await authFetch(
    `${BASE}/api/workspaces/${workspaceId}/terminals`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rows, cols }),
    },
  );
  return res.json();
}

export async function fetchTerminals(workspaceId: string) {
  const res = await authFetch(
    `${BASE}/api/workspaces/${workspaceId}/terminals`,
  );
  return res.json();
}

export async function deleteTerminal(termId: string) {
  const res = await authFetch(`${BASE}/api/terminals/${termId}`, {
    method: "DELETE",
  });
  return res.json();
}

// ---------------------------------------------------------------------------
// File API
// ---------------------------------------------------------------------------

export async function readFile(workspaceId: string, path: string) {
  const res = await authFetch(
    `${BASE}/api/workspaces/${workspaceId}/file?path=${encodeURIComponent(path)}`,
  );
  return res.json();
}

// ---------------------------------------------------------------------------
// Logs API
// ---------------------------------------------------------------------------

export async function fetchLogs(
  pattern = "",
  level = "DEBUG",
  limit = 200,
) {
  const params = new URLSearchParams({ pattern, level, limit: String(limit) });
  const res = await authFetch(`${BASE}/api/logs?${params}`);
  return res.json();
}
