import { useEffect, useState } from "react";
import { api } from "../api.js";

// The live view: what LinkChat is doing right now, on every screen.
//
// WHY IT EXISTS
// The program used to have a status line at the top. It came over from the
// program LinkChat was cut out of, was never rendered by the shell, and asked
// the engine for doors the engine does not answer on - so it showed nothing
// and was archived on 2026-08-25. The guide meanwhile told members to watch a
// line at the top for the word "running", which no screen drew.
//
// This is that line, rebuilt against the doors the engine actually has:
//   /api/inbox/status  -> is the LinkedIn browser up, is a sync running
//   /api/crm/state     -> how much of today's shared ceiling is left
//   /api/crm/waiting   -> how many messages are waiting for you
//
// It only reads. Nothing here starts a browser, syncs, or sends.

function timeOnly(s) {
  if (!s) return "";
  const m = String(s).match(/(\d{2}:\d{2}):\d{2}/);
  return m ? m[1] : String(s);
}

export default function LiveBar() {
  const [inbox, setInbox] = useState(null);
  const [crm, setCrm] = useState(null);
  const [waiting, setWaiting] = useState(null);
  const [reachable, setReachable] = useState(true);

  useEffect(() => {
    let stop = false;
    let timer = null;

    const tick = async () => {
      try {
        const s = await api.inbox.status();
        if (stop) return;
        setInbox(s);
        setReachable(true);
        // Two calls that do not change second to second are only refreshed
        // when nothing is running, so a sync is not competing with them.
        if (!s?.sync?.running) {
          try { setCrm(await api.crmState()); } catch { /* leave the last one */ }
          try {
            const w = await api.crmWaiting();
            setWaiting((w?.waiting || []).filter((x) => !x.approved).length);
          } catch { /* leave the last one */ }
        }
        // Fast while something is happening, slow while it is not.
        timer = setTimeout(tick, s?.sync?.running ? 2000 : 8000);
      } catch {
        if (stop) return;
        setReachable(false);
        timer = setTimeout(tick, 5000);
      }
    };

    tick();
    return () => { stop = true; if (timer) clearTimeout(timer); };
  }, []);

  if (!reachable) {
    return (
      <div className="livebar livebar-warn">
        <span className="lb-dot lb-red" />
        <span><strong>The engine is not answering.</strong> Close LinkChat and open it again.</span>
      </div>
    );
  }

  const keeper = !!inbox?.keeper;
  const syncing = !!inbox?.sync?.running;
  const last = inbox?.sync?.result;
  const cap = crm?.cap;
  const used = crm?.used || 0;
  const left = cap == null ? null : Math.max(cap - used, 0);

  return (
    <div className="livebar">
      {/* Is the LinkedIn browser up? This is the one the guide points at. */}
      <span className="lb-item" title={keeper
        ? "LinkChat has a LinkedIn browser open and can read and send."
        : "No LinkedIn browser yet. It opens by itself the first time you press Sync."}>
        <span className={"lb-dot " + (keeper ? "lb-green" : "lb-grey")} />
        LinkedIn browser: <strong>{keeper ? "running" : "not started"}</strong>
      </span>

      <span className="lb-sep">·</span>

      {/* What is happening right now. */}
      <span className="lb-item">
        {syncing ? (
          <>
            <span className="lb-dot lb-amber lb-pulse" />
            Reading your inbox…{inbox?.sync?.started
              ? <span className="lb-muted"> started {timeOnly(inbox.sync.started)}</span>
              : null}
          </>
        ) : last && last.ok === false ? (
          <>
            <span className="lb-dot lb-red" />
            Last read did not finish
          </>
        ) : (
          <>
            <span className="lb-dot lb-grey" />
            Not reading
          </>
        )}
      </span>

      <span className="lb-sep">·</span>

      {/* Today's shared ceiling. */}
      <span className="lb-item" title="The daily limit lives in your CRM and is shared with Gather.">
        Today: <strong>{used}</strong>{cap == null ? " sent" : ` of ${cap}`}
        {left === 0 ? <span className="lb-muted"> — none left</span> : null}
      </span>

      {waiting ? (
        <>
          <span className="lb-sep">·</span>
          <span className="lb-item lb-waiting">
            <strong>{waiting}</strong> waiting for you
          </span>
        </>
      ) : null}

      {inbox?.conversations != null ? (
        <>
          <span className="lb-sep">·</span>
          <span className="lb-item lb-muted">
            {inbox.conversations} conversation{inbox.conversations === 1 ? "" : "s"} on this computer
          </span>
        </>
      ) : null}
    </div>
  );
}
