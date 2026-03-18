import { useEffect, useRef, useState } from "react";

export type ConnectionPhase =
  | "connected"    // 正常连接
  | "waiting"      // 等待下次 retry（倒计时中）
  | "connecting"   // 正在建连
  | "exhausted"    // retry 耗尽
  | "updating";    // 新版本可用，即将刷新

interface Props {
  phase: ConnectionPhase;
  attempt: number;
  maxRetries: number;
  delay: number;         // waiting 阶段的总等待时间 (ms)
  onRetry?: () => void;  // exhausted 时手动重试
}

export function ConnectionStatusBar({ phase, attempt, maxRetries, delay, onRetry }: Props) {
  const [countdown, setCountdown] = useState(0);
  const [visible, setVisible] = useState(false);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hadDisconnectRef = useRef(false);

  // 记录是否曾经断连过
  if (phase !== "connected") {
    hadDisconnectRef.current = true;
  }

  // 连接恢复后短暂显示再隐藏
  useEffect(() => {
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
    if (phase === "connected") {
      if (hadDisconnectRef.current) {
        // 只在曾经断连后恢复时才显示
        setVisible(true);
        hideTimerRef.current = setTimeout(() => setVisible(false), 2000);
      } else {
        setVisible(false);
      }
    } else {
      setVisible(true);
    }
    return () => {
      if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
    };
  }, [phase]);

  // waiting / updating 阶段的实时倒计时
  useEffect(() => {
    if ((phase !== "waiting" && phase !== "updating") || delay <= 0) {
      setCountdown(0);
      return;
    }
    setCountdown(Math.ceil(delay / 1000));
    const interval = setInterval(() => {
      setCountdown((prev) => {
        if (prev <= 1) {
          clearInterval(interval);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, [phase, delay]);

  if (!visible) return null;

  let className = "connection-status-bar";
  let content: React.ReactNode;

  switch (phase) {
    case "waiting":
      className += " status-disconnected";
      content = (
        <>
          <span className="status-dot red" />
          Disconnected — Reconnecting ({attempt}/{maxRetries})
          {countdown > 0 && `, retry in ${countdown}s...`}
          {onRetry && (
            <button className="status-retry-btn" onClick={onRetry}>
              Retry Now
            </button>
          )}
        </>
      );
      break;
    case "connecting":
      className += " status-connecting";
      content = (
        <>
          <span className="status-dot yellow" />
          Connecting to server... ({attempt}/{maxRetries})
        </>
      );
      break;
    case "exhausted":
      className += " status-disconnected";
      content = (
        <>
          <span className="status-dot red" />
          Cannot connect to server — {maxRetries} attempts failed
          {onRetry && (
            <button className="status-retry-btn" onClick={onRetry}>
              Retry
            </button>
          )}
        </>
      );
      break;
    case "connected":
      className += " status-restored";
      content = (
        <>
          <span className="status-dot green" />
          Connection restored
        </>
      );
      break;
    case "updating":
      className += " status-connecting";
      content = (
        <>
          <span className="status-dot yellow" />
          New version detected{countdown > 0 ? `, refreshing in ${countdown}s` : ", refreshing..."}
        </>
      );
      break;
  }

  return <div className={className}>{content}</div>;
}
