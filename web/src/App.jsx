import { useEffect, useState } from "react";
import Sidebar from "./components/Sidebar.jsx";
import LiveBar from "./components/LiveBar.jsx";
import Inbox from "./pages/Inbox.jsx";
import Flows from "./pages/Flows.jsx";
import Find from "./pages/Find.jsx";
import Setup from "./pages/Setup.jsx";

// One call answers everything the shell needs to know: is there a CRM, how much
// of it is built, and how much of today's shared allowance is left.
function useCRM() {
  const [crm, setCRM] = useState(null);
  const [loading, setLoading] = useState(true);

  const refresh = () =>
    fetch("/api/crm/state")
      .then((r) => r.json())
      .then((d) => { setCRM(d); setLoading(false); })
      .catch(() => setLoading(false));

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 15000);
    return () => clearInterval(t);
  }, []);

  return { crm, loading, refresh };
}

export default function App() {
  const [route, setRoute] = useState("inbox");
  const [showSetup, setShowSetup] = useState(false);
  const { crm, loading, refresh } = useCRM();

  if (loading) return <div className="boot">Opening…</div>;

  // No CRM chosen yet. This is the first run and nothing else, so it gets the
  // whole window rather than a warning strip somebody can work around.
  if (!crm?.connected || showSetup) {
    return (
      <Setup
        crm={crm}
        onDone={() => { setShowSetup(false); refresh(); }}
        onCancel={crm?.connected ? () => setShowSetup(false) : null}
      />
    );
  }

  return (
    <div className="app">
      <Sidebar
        route={route}
        onNavigate={setRoute}
        onOpenSetup={() => setShowSetup(true)}
      />
      <main className="main">
        <LiveBar />
        <Banner crm={crm} />
        {route === "inbox" ? <Inbox /> : route === "find" ? <Find /> : <Flows />}
      </main>
    </div>
  );
}

// Says one true sentence about what this copy of LinkChat can do today, and
// names the layer that would change it. A member part-way through the CRM build
// sees why a button is off rather than a program that appears broken.
function Banner({ crm }) {
  const left = crm.cap == null ? null : Math.max(crm.cap - (crm.used || 0), 0);

  // Conversations failing to load draws exactly the same picture as an inbox with
  // nothing in it, which is the worst way for a fault to behave: on a call it
  // reads as "LinkedIn has no messages for you" rather than "this half did not
  // start". So it is said, first, above everything else.
  if (crm.conversations_fault) {
    return (
      <div className="banner warn">
        <strong>Conversations did not start.</strong> The inbox below will look
        empty whatever is in your LinkedIn, because this half of the program is
        not running. Nothing you do here will fix it and nothing is lost —{" "}
        send this line to Ashley: <code>{crm.conversations_fault}</code>
      </div>
    );
  }

  if (crm.reading_only) {
    // What is missing, not why importing it failed. The member has done nothing
    // wrong by not having finished Layer 6 yet, and a Python error name tells
    // them something crashed.
    const missing = Object.values(crm.missing || {})
      .map((m) => String(m).split(" - ")[0].trim())
      .filter(Boolean)
      .join(", ");
    return (
      <div className="banner warn">
        <strong>Reading only — you can see everything.</strong> Writing messages
        turns on when you install Layer 6 of your CRM. Nothing is broken and there
        is nothing to fix here: it turns on by itself.
        {missing ? <span className="muted"> Still to install: {missing}.</span> : null}
      </div>
    );
  }

  return (
    <div className="banner">
      <strong>{crm.people}</strong> people ·{" "}
      {left == null
        ? "no daily limit found"
        : <><strong>{left}</strong> of {crm.cap} left today, shared with Gather</>}
      {/* This line used to say nothing here sends, which stopped being true the
          day approving started sending. A banner that describes a different
          program from the one underneath it is worse than no banner: it is the
          one sentence somebody quotes back when a message goes out. */}
      <span className="muted"> · a message goes when you press Send or approve one; a copy is written to your outbox first</span>
    </div>
  );
}
