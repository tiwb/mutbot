import { useEffect, useRef, useState } from "react";

interface AgentStatusBarProps {
  isBusy: boolean;
}

export default function AgentStatusBar({ isBusy }: AgentStatusBarProps) {
  const [elapsed, setElapsed] = useState(0);
  const startTimeRef = useRef(0);

  useEffect(() => {
    if (isBusy) {
      startTimeRef.current = Date.now();
      setElapsed(0);
      const timer = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startTimeRef.current) / 1000));
      }, 1000);
      return () => clearInterval(timer);
    }
  }, [isBusy]);

  if (!isBusy) return null;

  return (
    <div className="agent-status-bar">
      <span className="status-spinner" />
      <span>Working {elapsed > 0 ? `${elapsed}s` : ""}</span>
    </div>
  );
}
