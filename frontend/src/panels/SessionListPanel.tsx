interface Session {
  id: string;
  title: string;
  type: string;
  status: string;
}

interface Props {
  sessions: Session[];
  activeSessionId: string | null;
  onSelect: (id: string) => void;
}

const TYPE_ICONS: Record<string, string> = {
  agent: "\u{1F916}",    // robot
  terminal: ">_",
  document: "\u{1F4C4}", // page
};

export default function SessionListPanel({
  sessions,
  activeSessionId,
  onSelect,
}: Props) {
  // Sort: active sessions first, then ended (grayed out)
  const sorted = [...sessions].sort((a, b) => {
    if (a.status === "active" && b.status !== "active") return -1;
    if (a.status !== "active" && b.status === "active") return 1;
    return 0;
  });

  return (
    <div className="session-list-container">
      <div className="sidebar-header">
        <h1>MutBot</h1>
      </div>
      <div className="session-list">
        <ul>
          {sorted.map((s) => (
            <li
              key={s.id}
              className={`session-item ${s.id === activeSessionId ? "active" : ""} ${s.status === "ended" ? "ended" : ""}`}
              onClick={() => onSelect(s.id)}
            >
              <span className="session-type-icon">
                {TYPE_ICONS[s.type] || "?"}
              </span>
              <span className="session-title">{s.title}</span>
              <span className={`session-status ${s.status}`}>{s.status}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
