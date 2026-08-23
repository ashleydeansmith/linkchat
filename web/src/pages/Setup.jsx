import { useState } from "react";

// The first run, and the only question LinkChat asks: where is the CRM you
// already built. Everything else it needs is already in there.
export default function Setup({ crm, onDone, onCancel }) {
  const [path, setPath] = useState(crm?.crm || "");
  const [you, setYou] = useState(crm?.you || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/crm/choose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path.trim(), you: you.trim() }),
      });
      const d = await r.json();
      if (!r.ok) { setError(d.detail || "that folder could not be used"); return; }
      onDone();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="setup">
      <div className="setup-card">
        <h1>Point LinkChat at your CRM</h1>

        <p>
          LinkChat keeps no record about a person. Your people, your event log,
          your daily limit and your hold list all stay in the CRM you built, and
          LinkChat reads them from there.
        </p>

        <label>
          The folder your CRM is in
          <input
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="C:\Users\you\CRM"
            spellCheck={false}
          />
        </label>
        <p className="hint">
          It is the folder with <code>_engine</code> and <code>People</code>{" "}
          inside it — the one Layer 1 made.
        </p>

        <label>
          Your name
          <input
            value={you}
            onChange={(e) => setYou(e.target.value)}
            placeholder="Your name"
          />
        </label>
        <p className="hint">
          This goes on the approval line. A sequence writes a message and cannot
          approve its own work, so every message that reaches your outbox carries
          the name of the person who said it could: you.
        </p>

        {error ? <div className="setup-error">{error}</div> : null}

        <div className="setup-actions">
          {onCancel ? (
            <button className="ghost" onClick={onCancel}>Cancel</button>
          ) : null}
          <button className="primary" onClick={save} disabled={busy || !path.trim()}>
            {busy ? "Checking…" : "Use this CRM"}
          </button>
        </div>
      </div>
    </div>
  );
}
