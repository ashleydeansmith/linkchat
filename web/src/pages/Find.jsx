import { useCallback, useEffect, useState } from "react";
import "./find.css";

// Find people.
//
// These are the four jobs you built in Session 2. LinkChat does not have its own
// copy — it runs yours, out of your CRM, so everything lands in the records you
// already keep and counts against the one daily limit you already set.
//
// Every job has two modes. Practice does everything except the outward action and
// tells you what it would have done. Do it does it. Nothing here picks Do it.

const JOBS = [
  { key: "find", title: "Go out and bring people back",
    blurb: "Reads a list of people and puts them into your records. Nothing is said to anybody.",
    needsSource: true },
  { key: "ask", title: "Ask people to connect",
    blurb: "Sends connection requests. A request carries none of your words. This is the one LinkedIn counts, so it answers to your daily limit." },
  { key: "undo", title: "Take back requests nobody answered",
    blurb: "Withdraws old requests. Worth doing often: a pile of unanswered requests is what makes LinkedIn tighten your limits." },
  { key: "accepted", title: "Work out who said yes",
    blurb: "Checks who accepted, and writes it into your records." },
];

const SOURCES = [
  { key: "connections", label: "The people I am already connected to", risk: "no risk" },
  { key: "export", label: "A spreadsheet already on my computer", risk: "no risk", needsTerm: true,
    termLabel: "Where the file is" },
  { key: "search", label: "One search", risk: "uses one of your monthly searches", needsTerm: true,
    termLabel: "What to search for" },
  { key: "reactions", label: "Everyone who reacted to one post", risk: "a lot of reading in one go",
    needsTerm: true, termLabel: "The address of the post" },
];

export default function Find() {
  const [state, setState] = useState(null);
  const [job, setJob] = useState("find");
  const [source, setSource] = useState("connections");
  const [term, setTerm] = useState("");
  const [limit, setLimit] = useState(20);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    fetch("/api/gather/state").then((r) => r.json()).then(setState).catch(() => {});
  }, []);
  useEffect(load, [load]);

  const chosen = JOBS.find((j) => j.key === job);
  const chosenSource = SOURCES.find((s) => s.key === source);

  const run = async (mode) => {
    setRunning(true); setError(""); setResult(null);
    try {
      const r = await fetch("/api/gather/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job, mode,
          source: chosen?.needsSource ? source : null,
          term: chosen?.needsSource && chosenSource?.needsTerm ? term : null,
          limit: Number(limit) || null,
        }),
      });
      const d = await r.json();
      if (!r.ok) { setError(d.detail || "that could not run"); return; }
      setResult(d);
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setRunning(false);
    }
  };

  if (state && !state.installed) {
    return (
      <div className="fd-wrap">
        <div className="fd-empty">
          <h2>These are your Gather jobs</h2>
          <p>{state.why}</p>
          <p>Once Session 2 is installed in your CRM, the four jobs appear here and
             LinkChat runs the ones you already have — it does not keep its own copy.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="fd-wrap">
      <div className="fd-intro">
        <strong>These are the jobs you already built.</strong> LinkChat runs the ones
        in your CRM rather than keeping its own, so what happens here lands in the
        same records and counts against the same daily limit.
      </div>

      <div className="fd-jobs">
        {JOBS.map((j) => (
          <button key={j.key}
                  className={"fd-job" + (job === j.key ? " on" : "")}
                  onClick={() => { setJob(j.key); setResult(null); setError(""); }}>
            <span className="fd-job-t">{j.title}</span>
            <span className="fd-job-b">{j.blurb}</span>
          </button>
        ))}
      </div>

      {chosen?.needsSource && (
        <div className="fd-row">
          <label>Where to look</label>
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            {SOURCES.map((s) => (
              <option key={s.key} value={s.key}>{s.label} — {s.risk}</option>
            ))}
          </select>
          {chosenSource?.needsTerm && (
            <input placeholder={chosenSource.termLabel} value={term}
                   onChange={(e) => setTerm(e.target.value)} />
          )}
        </div>
      )}

      <div className="fd-row">
        <label>At most, how many people in one go</label>
        <input type="number" min="1" max="200" value={limit}
               onChange={(e) => setLimit(e.target.value)} style={{ width: 90 }} />
      </div>

      {error ? <div className="fd-error">{error}</div> : null}

      <div className="fd-actions">
        <button className="fd-practice" disabled={running} onClick={() => run("probe")}>
          {running ? "Running…" : "Practice — do everything except the last step"}
        </button>
        <button className="fd-commit" disabled={running} onClick={() => run("commit")}>
          Do it
        </button>
      </div>

      {result ? (
        <div className={"fd-result" + (result.ok ? "" : " bad")}>
          <div className="fd-result-head">
            {result.mode === "commit" ? "Done" : "Practice run — nothing left your computer"}
          </div>
          {result.out ? <pre>{result.out}</pre> : null}
          {result.err ? <pre className="fd-stderr">{result.err}</pre> : null}
        </div>
      ) : null}
    </div>
  );
}
