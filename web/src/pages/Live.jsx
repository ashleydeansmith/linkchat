import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import "./live.css";

// What is happening, and what has happened.
//
// The screens this was cut out of had a Live view that streamed pictures of the
// browser as it worked. That needs a door the engine here does not have, and
// watching a robot move a mouse is not what makes somebody trust it anyway.
//
// This is the other kind of live: the event log out of YOUR CRM, newest first,
// refreshed while something is running. It is the same file everything else in
// your CRM writes to, so what shows here is what your CRM actually believes
// happened - not a second story told by the screen.
//
// It only reads.

const WORDS = {
  message_sent: "message sent",
  message_staged: "written to your outbox, not sent",
  connection_request: "connection request",
  reply_received: "they replied",
  scrape: "read a page",
};

function when(ts) {
  if (!ts) return "";
  const t = new Date(ts);
  if (isNaN(t)) return String(ts).slice(11, 16);
  const mins = Math.round((Date.now() - t.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + " min ago";
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return hrs + (hrs === 1 ? " hour ago" : " hours ago");
  return t.toISOString().slice(0, 10);
}

export default function Live() {
  const [events, setEvents] = useState(null);
  const [inbox, setInbox] = useState(null);
  const [err, setErr] = useState("");
  const timer = useRef(null);

  const load = useCallback(async () => {
    let running = false;
    try {
      const s = await api.inbox.status();
      setInbox(s);
      running = !!s?.sync?.running;
      setErr("");
    } catch (e) { setErr(String(e.message || e)); }
    try {
      const d = await api.crmEvents(80);
      setEvents(d.events || []);
    } catch { /* keep what is on screen */ }
    // Quick while something is happening, slow while it is not.
    timer.current = setTimeout(load, running ? 2000 : 10000);
  }, []);

  useEffect(() => {
    load();
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [load]);

  const syncing = !!inbox?.sync?.running;
  const last = inbox?.sync?.result;

  return (
    <div className="lv-wrap">
      <div className="lv-head">
        <h1>Live</h1>
        <p className="lv-sub">
          What LinkChat is doing now, and what your CRM has recorded. Read from your
          own event log — nothing on this screen sends or changes anything.
        </p>
      </div>

      {err ? <div className="lv-err">{err}</div> : null}

      <div className="lv-now">
        {syncing ? (
          <div className="lv-line">
            <span className="lv-dot lv-amber lv-pulse" />
            <strong>Reading your inbox now.</strong>
            {inbox?.sync?.started
              ? <span className="lv-muted"> started {String(inbox.sync.started).slice(11, 16)}</span>
              : null}
          </div>
        ) : last && last.ok === false ? (
          <div className="lv-line">
            <span className="lv-dot lv-red" />
            <strong>The last read did not finish.</strong>
            <span className="lv-muted"> {last.msg}</span>
          </div>
        ) : (
          <div className="lv-line">
            <span className={"lv-dot " + (inbox?.keeper ? "lv-green" : "lv-grey")} />
            {inbox?.keeper
              ? "Nothing running. The LinkedIn browser is open and ready."
              : "Nothing running. The browser opens by itself when you press Sync inbox."}
          </div>
        )}
      </div>

      <div className="lv-feed">
        <div className="lv-feed-h">What your CRM has recorded</div>
        {events === null ? (
          <div className="lv-empty">Reading your event log…</div>
        ) : events.length === 0 ? (
          <div className="lv-empty">
            Nothing recorded yet. Your CRM writes a line here every time something
            actually happens — the first will appear the moment you approve a message.
          </div>
        ) : (
          <ol className="lv-list">
            {events.map((e, i) => (
              <li key={i} className="lv-item">
                <span className="lv-when">{when(e.ts)}</span>
                <span className={"lv-what lv-t-" + (e.type || "").replace(/[^a-z_]/g, "")}>
                  {WORDS[e.type] || (e.type || "").replace(/_/g, " ")}
                </span>
                {/* THE PERSON'S NAME IS IN `person`. `who` is the address the
                    event was filed under, and it is empty whenever your CRM
                    managed to place the person - which is most of the time. So
                    this column read a dash on every row that HAD a name, next
                    to a line saying what had just happened to somebody. The
                    name comes first now; the address is the fallback. */}
                <span className="lv-who">{e.person || e.who || "—"}</span>
                {!e.person ? (
                  <span className="lv-nobody" title={
                    "Your CRM could not place this person, so the event is filed "
                    + "against nobody and their history will not find it."}>
                    not identified
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
