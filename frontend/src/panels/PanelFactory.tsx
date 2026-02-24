import React, { Suspense } from "react";
import type { TabNode } from "flexlayout-react";
import {
  PANEL_AGENT_CHAT,
  PANEL_TERMINAL,
  PANEL_CODE_EDITOR,
  PANEL_LOG,
} from "../lib/layout";
import type { WorkspaceRpc } from "../lib/workspace-rpc";
import AgentPanel from "./AgentPanel";

const TerminalPanel = React.lazy(() => import("./TerminalPanel"));
const CodeEditorPanel = React.lazy(() => import("./CodeEditorPanel"));
const LogPanel = React.lazy(() => import("./LogPanel"));

export interface PanelContext {
  sessions: { id: string; title: string; type: string; status: string; config?: Record<string, unknown> | null }[];
  activeSessionId: string | null;
  workspaceId: string | null;
  rpc: WorkspaceRpc | null;
  onSelectSession: (id: string) => void;
  onUpdateTabConfig?: (nodeId: string, config: Record<string, unknown>) => void;
  onTerminalExited?: (sessionId: string) => void;
}

function Loading() {
  return <div className="panel-loading">Loading...</div>;
}

export function panelFactory(node: TabNode, ctx: PanelContext) {
  const component = node.getComponent();

  switch (component) {
    case PANEL_AGENT_CHAT: {
      const sessionId = node.getConfig()?.sessionId as string | undefined;
      if (!sessionId) return <div className="empty-state"><p>No session.</p></div>;
      return <AgentPanel sessionId={sessionId} rpc={ctx.rpc} />;
    }

    case PANEL_TERMINAL: {
      const termConfig = node.getConfig() as
        | { sessionId?: string; terminalId?: string; workspaceId?: string }
        | undefined;
      return (
        <Suspense fallback={<Loading />}>
          <TerminalPanel
            sessionId={termConfig?.sessionId}
            terminalId={termConfig?.terminalId}
            workspaceId={termConfig?.workspaceId ?? ctx.workspaceId ?? ""}
            nodeId={node.getId()}
            rpc={ctx.rpc}
            onTerminalCreated={ctx.onUpdateTabConfig}
            onTerminalExited={ctx.onTerminalExited}
          />
        </Suspense>
      );
    }

    case PANEL_CODE_EDITOR: {
      const editorConfig = node.getConfig() as
        | { filePath?: string; workspaceId?: string; language?: string }
        | undefined;
      return (
        <Suspense fallback={<Loading />}>
          <CodeEditorPanel
            filePath={editorConfig?.filePath ?? ""}
            workspaceId={editorConfig?.workspaceId ?? ctx.workspaceId ?? ""}
            language={editorConfig?.language}
            rpc={ctx.rpc}
          />
        </Suspense>
      );
    }

    case PANEL_LOG:
      return (
        <Suspense fallback={<Loading />}>
          <LogPanel rpc={ctx.rpc} />
        </Suspense>
      );

    default:
      return <div className="empty-state"><p>Unknown panel: {component}</p></div>;
  }
}
