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
// Workspace API (REST — 仅保留 WS 连接前必需的接口)
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
