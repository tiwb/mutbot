import React, { Suspense } from "react";
import type { TabNode } from "flexlayout-react";
import {
  PANEL_SESSION_LIST,
  PANEL_AGENT_CHAT,
  PANEL_TERMINAL,
  PANEL_CODE_EDITOR,
  PANEL_LOG,
} from "../lib/layout";
import SessionListPanel from "./SessionListPanel";
import AgentPanel from "./AgentPanel";

const TerminalPanel = React.lazy(() => import("./TerminalPanel"));
const CodeEditorPanel = React.lazy(() => import("./CodeEditorPanel"));
const LogPanel = React.lazy(() => import("./LogPanel"));

export interface PanelContext {
  sessions: { id: string; title: string; status: string }[];
  activeSessionId: string | null;
  workspaceId: string | null;
  onSelectSession: (id: string) => void;
  onNewSession: () => void;
  onUpdateTabConfig?: (nodeId: string, config: Record<string, unknown>) => void;
}

function Loading() {
  return <div className="panel-loading">Loading...</div>;
}

export function panelFactory(node: TabNode, ctx: PanelContext) {
  const component = node.getComponent();

  switch (component) {
    case PANEL_SESSION_LIST:
      return (
        <SessionListPanel
          sessions={ctx.sessions}
          activeSessionId={ctx.activeSessionId}
          onSelect={ctx.onSelectSession}
          onNewSession={ctx.onNewSession}
        />
      );

    case PANEL_AGENT_CHAT: {
      const sessionId = node.getConfig()?.sessionId as string | undefined;
      if (!sessionId) return <div className="empty-state"><p>No session.</p></div>;
      return <AgentPanel sessionId={sessionId} />;
    }

    case PANEL_TERMINAL: {
      const termConfig = node.getConfig() as
        | { terminalId?: string; workspaceId?: string }
        | undefined;
      return (
        <Suspense fallback={<Loading />}>
          <TerminalPanel
            terminalId={termConfig?.terminalId}
            workspaceId={termConfig?.workspaceId ?? ctx.workspaceId ?? ""}
            nodeId={node.getId()}
            onTerminalCreated={ctx.onUpdateTabConfig}
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
          />
        </Suspense>
      );
    }

    case PANEL_LOG:
      return (
        <Suspense fallback={<Loading />}>
          <LogPanel />
        </Suspense>
      );

    default:
      return <div className="empty-state"><p>Unknown panel: {component}</p></div>;
  }
}
