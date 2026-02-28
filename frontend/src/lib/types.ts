import type { IJsonModel } from "flexlayout-react";

/** AppRpc / WorkspaceRpc 共用的 RPC 调用接口 */
export interface RpcClient {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
}

export interface Workspace {
  id: string;
  name: string;
  project_path: string;
  sessions: string[];
  layout?: IJsonModel | null;
  created_at?: string;
  updated_at?: string;
  last_accessed_at?: string;
}

export interface Session {
  id: string;
  workspace_id: string;
  title: string;
  type: string;
  kind: string;
  icon: string;
  status: string;
  config?: Record<string, unknown> | null;
}
