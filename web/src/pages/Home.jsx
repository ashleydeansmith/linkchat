import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import "./home.css";

// Where things stand, and the one thing to do next.
//
// The first screen a member sees was Conversations, which drops somebody into a
// list before they know what the program is for or whether it is even working.
// This answers the three questions in order: is it connected, what is waiting
// for me, and what happens on its own.
//
// EVERYTHING HERE IS READ FROM DOORS THAT EXIST. Nothing on this screen invents
// a number, and nothing on it sends. The screens this program was cut out of
// had a Home that asked for six things the engine here does not answer, and it
// would have drawn a page of blanks.

function Stat({ n, label, tone }) {
  return (
    <div className={"hm-stat" + (tone ? " hm-" + tone : "")}>
      <div className="hm-n">{n}</div>
      <div className="hm-l">{label}</div>
    </div>
  );
}

export default function Home({ onNavigate }) {
  const [crm, setCrm] = useState(null);
  const [inbox, setInbox] = useState(null);
  const [waiting, setWaiting] = useState(null);
  const [suggested, setSuggested] = useState(null);
  const [flow, setFlow] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      setCrm(await api.crmState());
      setErr("");
    } catch (e) { setErr(String(e.message || e)); }
    try { setInbox(await api.inbox.status()); } catch { /* keep the last */ }
    try {
      const w = await api.crmWaiting();
      setWaiting((w?.waiting || []).filter((x) => !x.approved).length);
    } catch { /* keep the last */ }
    try {
      const r = await fetch("/api/inbox/review-queue?limit=40");
      if (r.ok) {
        const d = await r.json();
        const q = d.queue || [];
        setSuggested({
          total: q.length,
          ready: q.filter((it) => it.branch && (it.gives || [])
            .some((g) => (g.bubbles || []).length)).length,
        });
      }
    } catch { /* keep the last */ }
    try {
      const v = await api.flowsVersions();
      setFlow((v.versions || []).find((x) => x.status === "active") || null);
    } catch { /* keep the last */ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  const keeper = !!inbox?.keeper;
  const convs = inbox?.conversations ?? 0;
  const needs = inbox?.boxes?.["needs-you"] ?? 0;
  const cap = crm?.cap;
  const used = crm?.used || 0;
  const left = cap == null ? null : Math.max(cap - used, 0);
  const neverSynced = convs === 0;

  // The single next step, worked out rather than listed. A page of six things
  // you could do is a page nobody acts on.
  let next = null;
  if (!crm?.connected) {
    next = { say: "Point LinkChat at the CRM you built in Sessions 0 to 2.", go: null };
  } else if (crm?.reading_only) {
    next = { say: "This copy can read everything and send nothing until Layer 6 of "
                  + "your CRM is installed. Nothing is broken; it turns on by itself.",
             go: null };
  } else if (neverSynced) {
    next = { say: "Read your LinkedIn inbox for the first time. Press Sync inbox on "
                  + "Conversations — that is also what opens the browser, and you sign "
                  + "in once.", go: "inbox", cta: "Go to Conversations" };
  } else if (!flow) {
    next = { say: "No sequence is running yet. Start from the shape on Sequences and "
                  + "write your five messages.", go: "flows", cta: "Go to Sequences" };
  } else if (suggested?.ready) {
    next = { say: `${suggested.ready} ${suggested.ready === 1 ? "reply has" : "replies have"}`
                  + " words your sequence already agreed. Read each against what they"
                  + " actually said, then let it go.", go: "flows", cta: "Open the Cockpit" };
  } else if (needs) {
    next = { say: `${needs} ${needs === 1 ? "person is" : "people are"} waiting on a reply`
                  + " and your sequence has no words for them. That is a gap in the"
                  + " patterns, not a fault.", go: "inbox", cta: "Go to Conversations" };
  } else {
    next = { say: "Nothing is waiting on you.", go: null };
  }

  return (
    <div className="hm-wrap">
      {err ? <div className="hm-err">{err}</div> : null}

      <div className="hm-head">
        {/* Time of day read off the clock. It said "Morning" at eight in the
            evening, which is a small thing to get wrong and exactly the kind
            somebody notices first. */}
        <h1>
          {crm?.you
            ? `${(() => { const h = new Date().getHours();
                          return h < 12 ? "Morning" : h < 18 ? "Afternoon" : "Evening"; })()}, `
              + `${String(crm.you).split(" ")[0]}.`
            : "LinkChat"}
        </h1>
        <p className="hm-sub">
          Two screens over the CRM you already built. It keeps no record of its own —
          your people, your event log, your daily limit and your do-not-message list
          all stay in your CRM.
        </p>
      </div>

      <div className="hm-row">
        <Stat n={convs} label={convs === 1 ? "conversation read" : "conversations read"} />
        <Stat n={needs} label="waiting on a reply" tone={needs ? "warn" : null} />
        <Stat n={suggested?.ready ?? "—"} label="have agreed words" tone={suggested?.ready ? "go" : null} />
        <Stat n={waiting ?? 0} label="waiting for you to approve" tone={waiting ? "go" : null} />
        <Stat n={left == null ? "—" : left} label={cap == null ? "no daily limit found" : `left of ${cap} today`} />
      </div>

      <div className="hm-next">
        <div className="hm-next-h">Next</div>
        <p>{next.say}</p>
        {next.go ? (
          <button className="btn primary" onClick={() => onNavigate(next.go)}>
            {next.cta}
          </button>
        ) : null}
      </div>

      <div className="hm-cards">
        <div className="hm-card">
          <div className="hm-card-h">LinkedIn</div>
          <div className="hm-line">
            <span className={"hm-dot " + (keeper ? "hm-green" : "hm-grey")} />
            {keeper ? "browser running" : "browser not started"}
          </div>
          <p className="hm-muted">
            It opens by itself the first time you press Sync inbox, and that is when
            you sign in. Once, on this computer.
          </p>
        </div>

        <div className="hm-card">
          <div className="hm-card-h">Your sequence</div>
          <div className="hm-line">
            <span className={"hm-dot " + (flow ? "hm-green" : "hm-grey")} />
            {flow ? flow.name : "none running"}
          </div>
          <p className="hm-muted">
            {flow
              ? "It reads replies and picks the words you agreed for that kind of reply. It never sends one on its own."
              : "Start from the shape on Sequences. Every message in it is a gap for you to write; it cannot send until you have."}
          </p>
        </div>

        <div className="hm-card">
          <div className="hm-card-h">What happens on its own</div>
          <div className="hm-line"><span className="hm-dot hm-grey" />nothing</div>
          <p className="hm-muted">
            LinkChat runs when you press something. A message goes when you approve
            it, and a copy is written into your outbox first, so a send that fails
            still leaves you the words.
          </p>
        </div>
      </div>
    </div>
  );
}
