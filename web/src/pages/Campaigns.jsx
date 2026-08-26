import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import "./campaigns.css";

// Campaigns — the half of the job that creates conversations.
//
// Everything else in LinkChat works on people who have already replied. This is
// where they come from: looking through a search, asking people to connect,
// taking back the requests nobody answered, and working out who said yes.
//
// TWO RULES ON THIS SCREEN, AND THEY ARE THE WHOLE DESIGN.
//
// Practice does everything except the outward action and tells you what it
// would have done. Nothing here picks "Do it" for you.
//
// And every one of these answers to the daily ceiling in YOUR CRM - the same
// one Gather answers to. There is no second set of counts. LinkedIn counts the
// account, not the program, so two programs each keeping their own tally is how
// an account goes over a limit both of them believed they were inside.

const ORDER = ["search", "connect", "withdraw", "accept-sync"];

export default function Campaigns() {
  const [lanes, setLanes] = useState(null);
  const [camps, setCamps] = useState([]);
  const [busy, setBusy] = useState("");
  const [said, setSaid] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const d = await api.lanes();
      setLanes(d.lanes || {});
      setErr("");
    } catch (e) { setErr(String(e.message || e)); }
    try {
      const c = await api.campaigns();
      setCamps(c.campaigns || []);
    } catch { /* the lanes still work without a campaign list */ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, [load]);

  const run = async (name, mode) => {
    setBusy(name + mode);
    setSaid(null);
    setErr("");
    try {
      const r = await api.runLane(name, { mode, max: mode === "commit" ? 10 : null });
      setSaid({ name, mode, result: r });
      await load();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy("");
    }
  };

  if (lanes === null) {
    return <div className="cp-wrap"><div className="cp-empty">Looking…</div></div>;
  }

  return (
    <div className="cp-wrap">
      <div className="cp-head">
        <h1>Campaigns</h1>
        <p className="cp-sub">
          Where conversations come from. Everything else in LinkChat works on people
          who have already replied — this is the part that reaches out. Each of these
          answers to the daily limit in your own CRM, the same one Gather answers to.
        </p>
      </div>

      {err ? <div className="cp-err">{err}</div> : null}

      <div className="cp-lanes">
        {ORDER.filter((n) => lanes[n]).map((name) => {
          const l = lanes[name];
          return (
            <div className={"cp-lane" + (l.allowed ? "" : " cp-blocked")} key={name}>
              <div className="cp-lane-h">
                <span className={"cp-dot " + (l.allowed ? "cp-green" : "cp-grey")} />
                {l.label}
              </div>
              <p className="cp-what">{l.what}</p>
              <div className="cp-why">
                {l.allowed
                  ? <span className="cp-muted">{l.why || "your ceiling allows this"}</span>
                  : <span className="cp-no">{l.why || "your daily ceiling says no for now"}</span>}
              </div>
              <div className="cp-actions">
                <button className="btn" disabled={!!busy}
                        onClick={() => run(name, "probe")}>
                  {busy === name + "probe" ? "Working…" : "Practice"}
                </button>
                <button className="btn primary" disabled={!!busy || !l.allowed}
                        onClick={() => run(name, "commit")}>
                  {busy === name + "commit" ? "Working…" : "Do it"}
                </button>
              </div>
            </div>
          );
        })}
      </div>

      {said ? (
        <div className="cp-said">
          <div className="cp-said-h">
            {said.name} · {said.mode === "commit" ? "done" : "practice, nothing sent"}
          </div>
          <pre>{JSON.stringify(said.result, null, 1).slice(0, 1400)}</pre>
        </div>
      ) : null}

      <div className="cp-camps">
        <div className="cp-said-h">Your pushes</div>
        {camps.length === 0 ? (
          <p className="cp-muted">
            None yet. Look through a saved search and the people it finds land in a
            queue here, ready for connection requests.
          </p>
        ) : (
          <table className="cp-table">
            <thead>
              <tr><th>Name</th><th>People</th><th>Queued</th>
                  <th>Asked</th><th>Said yes</th><th></th></tr>
            </thead>
            <tbody>
              {camps.map((c) => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{c.people ?? "—"}</td>
                  <td>{c.queued ?? "—"}</td>
                  <td>{c.requested ?? "—"}</td>
                  <td>{c.accepted ?? "—"}</td>
                  <td className="cp-muted">{c.status || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
