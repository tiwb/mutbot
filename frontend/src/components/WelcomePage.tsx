import { useCallback } from "react";
import { renderLucideIcon } from "./SessionIcons";
import type { RpcClient } from "../lib/types";

interface Props {
  rpc: RpcClient | null;
  onCreateSession?: (sessionType: string) => void;
}

export default function WelcomePage({ rpc, onCreateSession }: Props) {
  const handleClick = useCallback(
    (sessionType: string) => {
      if (!rpc) return;
      onCreateSession?.(sessionType);
    },
    [rpc, onCreateSession],
  );

  return (
    <div className="welcome-page">
      <div className="welcome-watermark">MutBot</div>
      <div className="welcome-actions">
        <button
          className="welcome-action-link"
          onClick={() => handleClick("mutbot.builtins.guide.GuideSession")}
        >
          {renderLucideIcon("message-square", 16, "currentColor")}
          <span>Agent</span>
          <span className="welcome-action-desc">— Start an AI agent session</span>
        </button>
        <button
          className="welcome-action-link"
          onClick={() => handleClick("mutbot.session.TerminalSession")}
        >
          {renderLucideIcon("terminal", 16, "currentColor")}
          <span>Terminal</span>
          <span className="welcome-action-desc">— Open a command-line terminal</span>
        </button>
      </div>
    </div>
  );
}
