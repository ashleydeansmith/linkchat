// Three destinations, and a settings button. They are the three parts of the job:
// finding people, talking to them, and deciding what to say next.
//
// Three is still short on purpose. A rail with nine items on a program this size
// is how a small tool reads as a complicated one.
const NAV = [
  { key: "inbox", label: "Conversations", ic: "💬" },
  { key: "flows", label: "Sequences", ic: "⤷" },
  { key: "find",  label: "Find people",  ic: "🔍" },
];

export default function Sidebar({ route, onNavigate, onOpenSetup }) {
  return (
    <aside className="side">
      <div className="brand">
        <svg className="mark" width="26" height="26" viewBox="0 0 64 64" aria-hidden="true">
          <defs>
            <linearGradient id="lcTeal" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0" stopColor="#0B6E6C" />
              <stop offset="1" stopColor="#27D3C4" />
            </linearGradient>
          </defs>
          <rect x="10" y="16" width="44" height="30" rx="15" fill="none"
                stroke="url(#lcTeal)" strokeWidth="7" />
          <circle cx="24" cy="31" r="3.4" fill="url(#lcTeal)" />
          <circle cx="34" cy="31" r="3.4" fill="url(#lcTeal)" />
          <circle cx="44" cy="31" r="3.4" fill="url(#lcTeal)" />
        </svg>
        LinkChat
      </div>

      {NAV.map((n) => (
        <button
          key={n.key}
          className={"nav" + (route === n.key ? " active" : "")}
          onClick={() => onNavigate(n.key)}
        >
          <span className="ic">{n.ic}</span>
          {n.label}
        </button>
      ))}

      <div style={{ marginTop: "auto" }}>
        <button className="nav" onClick={onOpenSetup}>
          <span className="ic">⚙</span>
          Your CRM
        </button>
      </div>
    </aside>
  );
}
