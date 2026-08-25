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
  // Conversations waiting on a reply that the sequence HAS an agreed message for.
  // Until 2026-08-25 nothing on any screen read this, so the sequence's work was
  // resolved by the engine and shown to nobody.
  const [suggested, setSuggested] = useState([]);

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
    try {
      const q = await fetch("/api/inbox/review-queue?limit=40");
      if (q.ok) {
        const d = await q.json();
        // Only the ones with an agreed message. A conversation the sequence has
        // no words for is not a decision waiting on you - it is a gap in the
        // patterns, and putting it here as an empty card would say otherwise.
        setSuggested((d.queue || []).filter(
          (it) => it.branch && (it.gives || []).some((g) => (g.bubbles || []).length)));
      }
    } catch { /* the queue is a nicety; what is already waiting still shows */ }
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

  // One press, three hands: the sequence writes it down, you release it, it goes.
  // The record ends up saying a sequence wrote it and you let it go, which is the
  // truth. Sending it as though you had typed it would not be.
  const approveSuggested = async (item, bubbles) => {
    const key = "s" + item.conv_id;
    setBusyId(key);
    setError("");
    try {
      for (const b of bubbles) {
        const r = await fetch("/api/crm/approve-suggested", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ conv_id: item.conv_id, body: b,
                                 branch: item.branch, arm: item.arm || null }),
        });
        const d = await r.json();
        if (!r.ok) { setError(d.detail || "that could not be sent"); return; }
        setDone((prev) => [{ id: key + b.slice(0, 8), to: item.participant_name,
                             where: d.staged, sent: d.sent, why: d.why }, ...prev]);
      }
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

      {suggested.length > 0 ? (
        <div className="ck-suggested">
          <div className="ck-done-head">
            Replies your sequence has words for ({suggested.length})
          </div>
          <div className="ck-intro">
            Each of these is somebody waiting on a reply whose message matched one of
            your branches. The words are the ones you agreed for that branch — nothing
            here was written just now. Read it against what they actually said before
            you let it go.
          </div>
          <div className="ck-list">
            {suggested.map((item) => {
              const g = (item.gives || []).find((x) => (x.bubbles || []).length);
              const bubbles = (g && g.bubbles) || [];
              const key = "s" + item.conv_id;
              return (
                <div className="ck-card" key={key}>
                  <div className="ck-to">
                    {item.participant_name || "Someone in your inbox"}
                    <span className="ck-branch"> · {item.branch} {item.label || ""}</span>
                  </div>
                  <div className="ck-their-last">
                    They said: {item.their_last || "(nothing stored yet)"}
                  </div>
                  <div className="ck-body">
                    {bubbles.map((b, i) => <div key={i} className="ck-bubble">{b}</div>)}
                  </div>
                  <div className="ck-foot">
                    <span className="ck-by">
                      written by your sequence · {bubbles.length} message
                      {bubbles.length === 1 ? "" : "s"}
                    </span>
                    <button
                      className="ck-approve"
                      disabled={busyId === key}
                      onClick={() => approveSuggested(item, bubbles)}
                    >
                      {busyId === key ? "Sending…" : "Approve and send"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

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
