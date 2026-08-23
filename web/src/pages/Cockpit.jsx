import { useCallback, useEffect, useState } from "react";
import "./cockpit.css";

// Waiting for you.
//
// This is the step the whole program exists for, and until now it had no button.
// A sequence writes a message. It cannot approve its own work, because the
// reasoning that wrote it is the reasoning that would have to find the fault in
// it. So it waits here for you.
//
// Approving is the decision, and then LinkChat carries it: the message is written
// into your outbox so you keep the words, and then it goes into the conversation.
// The reply comes back into Conversations, and the sequence picks up from there.
// That loop closing is the point - a message you have to paste in by hand teaches
// nobody anything and nobody keeps doing it.

export default function Cockpit() {
  const [waiting, setWaiting] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState(null);
  const [done, setDone] = useState([]);

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/crm/waiting");
      if (!r.ok) throw new Error("could not read what is waiting");
      const d = await r.json();
      setWaiting(d.waiting || []);
      setError("");
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, [load]);

  const approve = async (item) => {
    setBusyId(item.id);
    setError("");
    try {
      const body = (item.payload && item.payload.body) || item.summary || "";
      const r = await fetch("/api/crm/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          item_id: item.id,
          to: item.to || item.recipient || "",
          identifier: item.identifier || (item.payload && item.payload.identifier) || "",
          thread_urn: item.thread_urn || (item.payload && item.payload.thread_urn) || null,
          body,
        }),
      });
      const d = await r.json();
      if (!r.ok) { setError(d.detail || "that could not be approved"); return; }
      setDone((prev) => [{ id: item.id, to: item.to, where: d.staged,
                           sent: d.sent, why: d.why }, ...prev]);
      await load();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusyId(null);
    }
  };

  if (loading) return <div className="ck-wrap"><div className="ck-empty">Looking…</div></div>;

  return (
    <div className="ck-wrap">
      <div className="ck-intro">
        <strong>Waiting for you.</strong> A sequence wrote each of these and is not
        allowed to approve its own work. Read it. If you approve it, LinkChat sends
        it and the reply comes back into Conversations. If you do not, nothing
        happens — nothing here goes anywhere on its own.
      </div>

      {error ? <div className="ck-error">{error}</div> : null}

      {waiting.length === 0 ? (
        <div className="ck-empty">
          Nothing is waiting for you. When a sequence writes a message, it appears
          here for you to read before anything happens to it.
        </div>
      ) : (
        <div className="ck-list">
          {waiting.map((item) => (
            <div className="ck-card" key={item.id}>
              <div className="ck-to">
                {item.to || item.recipient || "Someone in your CRM"}
              </div>
              <div className="ck-body">
                {(item.payload && item.payload.body) || item.summary}
              </div>
              <div className="ck-foot">
                <span className="ck-by">written by a sequence</span>
                <button
                  className="ck-approve"
                  disabled={busyId === item.id}
                  onClick={() => approve(item)}
                >
                  {busyId === item.id ? "Sending…" : "Approve and send"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {done.length > 0 ? (
        <div className="ck-done">
          <div className="ck-done-head">Just now</div>
          {done.map((d) => (
            <div className="ck-done-row" key={d.id}>
              <strong>{d.to}</strong> —{" "}
              {d.sent
                ? "sent. Their reply will show up in Conversations."
                : <>not sent. {d.why} It is saved in your outbox either way.</>}
              <div className="ck-path">{d.where}</div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
