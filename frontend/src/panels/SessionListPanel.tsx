interface Session {
  id: string;
  title: string;
  status: string;
}

interface Props {
  sessions: Session[];
  activeSessionId: string | null;
  onSelect: (id: string) => void;
  onNewSession: () => void;
}

export default function SessionListPanel({
  sessions,
  activeSessionId,
  onSelect,
  onNewSession,
}: Props) {
  return (
    <div className="session-list">
      <button className="btn-new-session" onClick={onNewSession}>
        + New Session
      </button>
      <ul>
        {sessions.map((s) => (
          <li
            key={s.id}
            className={`session-item ${s.id === activeSessionId ? "active" : ""} ${s.status === "ended" ? "ended" : ""}`}
            onClick={() => onSelect(s.id)}
          >
            <span className="session-title">{s.title}</span>
            <span className={`session-status ${s.status}`}>{s.status}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
