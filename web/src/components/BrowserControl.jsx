import { useEffect, useState } from "react";
import { api } from "../api.js";
import { isBrowserRunning } from "../useStatus.js";

// LinkedIn browser keeper control — Start/Stop the one persistent Chromium, and the
// input LOCK (freeze human input to the keeper window while a lane drives it, so a
// stray click can't derail a run). Mirrors the old Flet lock_btn + Start/Stop.
export default function BrowserControl({ status }) {
  const [b, setB] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.browser().then(setB).catch(() => {});
  useEffect(() => {
    load();
    const t = setInterval(load, 6000);
    return () => clearInterval(t);
  }, []);

  async function act(action) {
    setBusy(true);
    try { setB(await api.browserAction(action)); } catch { /* best-effort */ }
    setBusy(false);
    load();
  }

  if (!b) return null;
  // ONE browser-running read (LF-WL-001), shared across Home/Live/lock/Settings.
  const running = isBrowserRunning(b) || isBrowserRunning(status);
  const locked = b.locked;

  return (
    <div className="browser-ctl">
      <span className="bc-dot" style={{ background: running ? "var(--green)" : "var(--ink-3)" }} />
      <span className="bc-label">
        LinkedIn browser — {running ? "running" : "stopped"}
        {running && locked && <b style={{ color: "var(--amber)", marginLeft: 6 }}>🔒 locked{b.manual_lock ? " · manual" : ""}</b>}
      </span>
      <span className="grow" />
      <button className="btn sm" disabled={busy} onClick={() => act(running ? "stop" : "start")}>
        {busy ? "…" : running ? "Stop browser" : "Start browser"}
      </button>
      <button
        className={"btn sm" + (locked ? " primary" : "")}
        style={locked ? { background: "var(--amber)" } : undefined}
        disabled={busy || !running}
        onClick={() => act(locked ? "unlock" : "lock")}
        title={!running ? "Start the browser first" : locked ? "Unlock — let your clicks through again" : "Lock — freeze your input to the browser window while a lane runs"}
      >
        {locked ? "🔓 Unlock" : "🔒 Lock"}
      </button>
    </div>
  );
}
