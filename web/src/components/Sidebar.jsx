// Four destinations, and a settings button, in the order the job happens:
// where things stand, the conversations, what to say next, and what is happening
// right now.
//
// "Find people" was here and is not any more. It ran the four jobs from Session
// 2 of your CRM, which most people had not built yet, so it showed a paragraph
// explaining why it was empty. A tab that is usually empty teaches somebody the
// program is half-finished. The screen is kept in _retired/ rather than deleted;
// connect, withdraw and searching are being built properly into Campaigns.
const NAV = [
  { key: "home",  label: "Home",          ic: "⌂" },
  { key: "inbox", label: "Conversations", ic: "💬" },
  { key: "flows", label: "Sequences",     ic: "⤷" },
  { key: "live",  label: "Live",          ic: "◉" },
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
