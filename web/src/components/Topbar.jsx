import { useEffect, useState } from "react";
import { api } from "../api.js";
import { toggleTheme, isDark } from "../theme.js";
import { deriveStatus, statusLabel, isBrowserRunning } from "../useStatus.js";

// Status pill + browser lock + conditional Stop + overflow — the §4.0 header.
// The pill renders the ONE derived status (useStatus.deriveStatus); it cannot
// contradict Home because both read the same fold of engine + runner + window.
const PILL_CLASS = { off: "resting", rehearsal: "practice", live: "live" };
const DOT = { off: "var(--ink-3)", rehearsal: "var(--amber)", live: "#fff" };

export default function Topbar({ status, onRefresh, onOpenPalette, route }) {
  const state = status?.state ?? "off";
  const showStop = state === "live" || status?.busy;
  const [browser, setBrowser] = useState(null);
  const [lic, setLic] = useState(null);

  useEffect(() => {
    const load = () => api.browser().then(setBrowser).catch(() => {});
    load();
    const t = setInterval(load, 6000);
    return () => clearInterval(t);
  }, []);

  // Beta trial chip — rendered IN the topbar flow (not a floating overlay) so it can
  // never cover the controls. Inert/absent unless the licence layer reports a trial.
  useEffect(() => {
    api.licence().then(setLic).catch(() => {});
  }, []);
  const trialDays =
    lic?.state === "trial" && typeof lic.days_left === "number"
      ? Math.max(0, Math.ceil(lic.days_left))
      : null;

  async function stopEverything() {
    await api.pause();
    onRefresh?.();
  }
  // ONE browser-running read (LF-WL-001): the keeper poll, falling back to the shared
  // status probe so the lock is engageable the instant the browser is really running —
  // it no longer sits disabled ("start it on Home") while the keeper reports running.
  const running = isBrowserRunning(browser) || isBrowserRunning(status);
  async function toggleLock() {
    if (!running) return;
    try { setBrowser(await api.browserAction(browser?.locked ? "unlock" : "lock")); } catch { /* ignore */ }
  }
  // ONE derived label (never re-phrases run-state independently of Home). On the Home
  // route the pill defers to Home's authoritative status block: it shows only the plain
  // state word, so the same fact isn't stated three times on one screen (LF-009).
  const derived = status ? deriveStatus(status) : null;
  const onHome = route === "home";
  const label = !status
    ? "Connecting…"
    : onHome
      ? (status.state_label || "")
      : statusLabel(derived);

  const locked = browser?.locked;
  const [dark, setDark] = useState(isDark());
  useEffect(() => {
    const h = () => setDark(isDark());
    window.addEventListener("lf-theme", h);
    return () => window.removeEventListener("lf-theme", h);
  }, []);

  return (
    <div className="topbar">
      <span className={"pill " + PILL_CLASS[state]}>
        <span className="dot" style={{ background: DOT[state] }} />
        {label}
      </span>
      <span className="grow" />
      {lic?.state === "trial" && (
        <span
          title="Beta trial"
          style={{
            padding: "4px 10px", borderRadius: 999, fontSize: 11.5, fontWeight: 600,
            whiteSpace: "nowrap", alignSelf: "center",
            background: "var(--amber-bg, rgba(245,180,60,0.14))",
            color: "var(--amber, #f5b43c)", border: "1px solid var(--amber, #f5b43c)",
          }}
        >
          {trialDays === null ? "Trial" : trialDays <= 1 ? "Trial — last day" : `Trial — ${trialDays} days left`}
        </span>
      )}
      {/* Browser lock — always visible; greys out when the keeper isn't running */}
      <button
        className={"icon-btn" + (locked ? " locked" : "")}
        onClick={toggleLock}
        disabled={!running}
        title={
          !running ? "Browser not running — start it on Home to lock"
            : locked ? "Browser LOCKED — your input is frozen. Click to unlock."
              : "Lock the browser — freeze your input while a lane runs"
        }
      >
        {locked ? "🔒" : "🔓"}
      </button>
      {showStop && (
        <button className="btn stop" onClick={stopEverything}>■ Stop everything</button>
      )}
      <span className="icon-btn" title="Command palette (Ctrl/⌘ K)" onClick={onOpenPalette}>⌕</span>
      <button
        className="icon-btn"
        title={dark ? "Switch to light mode" : "Switch to dark mode"}
        onClick={() => setDark(toggleTheme())}
      >
        {dark ? (
          /* in dark mode → show a SUN (click to go light) */
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
          </svg>
        ) : (
          /* in light mode → show a MOON (click to go dark) */
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
            <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
          </svg>
        )}
      </button>
    </div>
  );
}
