import { useEffect, useState, useCallback } from "react";
import { api } from "./api.js";

// One poller for the engine status — the single EngineStatus object (§4.0) that both the
// header pill and Home render from, so the UI can never show two contradictory states.
export function useStatus(intervalMs = 5000) {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.status());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, intervalMs);
    return () => clearInterval(t);
  }, [refresh, intervalMs]);

  return { status, error, refresh };
}

// ---------------------------------------------------------------------------
// deriveStatus — THE single source of run/connection truth (LF-001/009/013/014/016).
// The raw /status object carries every input; the bug was each screen re-deriving
// "running"/"next" from a DIFFERENT subset of them (some ignoring the background
// runner, some ignoring the window) so the UI contradicted itself. This folds all
// three real gates — engine state + automatic sending (daemon) + working window —
// into one object. Every surface renders from THIS and nothing else.
//
// Hard rule encoded here: a surface may only claim "running", or promise a "next"
// auto-fire, when automatic sending can actually make it happen (engine armed AND
// the runner on AND inside the window). Otherwise it is honest that nothing fires
// on its own.
// isConnected — THE single connection read-model (LF-B1-001). "Connected" means an
// authenticated LinkedIn SESSION exists (li_at cookie), NOT merely that the browser
// process is running. Home, the connect step and Settings all call this one function,
// so "am I connected to LinkedIn?" can never give three disagreeing answers again.
// Accepts either the /status object or the /api/browser object (both carry the fields).
export function isConnected(src) {
  return Boolean(src?.has_session && !src?.needs_login);
}

// isBrowserRunning — THE single browser-running read-model (LF-WL-001). "Is the keeper
// browser process up?" was answered from TWO backend fields on different polls — the
// /api/status `browser` string and the /api/browser `running` boolean — so Home, Live,
// the header lock and Settings could disagree at the same instant. Both fields derive
// from the SAME keeper probe on the server; this folds them into one truth so every
// surface renders the identical answer. Accepts either object shape (or both, OR-ed by
// the caller) so a not-yet-loaded /api/browser poll can fall back to the shared status.
export function isBrowserRunning(src) {
  if (!src) return false;
  if (typeof src.running === "boolean") return src.running;   // /api/browser (authoritative keeper)
  const b = src.browser;                                       // /api/status string form of the same probe
  return b === "connected" || (typeof b === "string" && b.startsWith("locked"));
}

// connectionPhase — the THREE distinct connect states (LF-WL-003), collapsing the silent
// "dead middle" where a running keeper with no LinkedIn session read identically to a
// cold, never-started one. Every connection surface renders from this so "browser up but
// not signed in" gets its own state + finish-signing-in nudge, distinct from both ends.
//   "signed-in"  — an authenticated LinkedIn session (li_at) exists
//   "signing-in" — the keeper browser is running but no session yet (finish signing in)
//   "cold"       — no browser, no session (never started)
// Accepts either the /api/status or the /api/browser object (both carry the fields).
export function connectionPhase(src) {
  if (isConnected(src)) return "signed-in";
  if (isBrowserRunning(src)) return "signing-in";
  return "cold";
}

// The ONE canonical engine-state word, used on every surface (LF-B1-010). The state was
// previously named four ways — Resting / Idle / Off / Everything paused. This is the
// single map; render THROUGH it everywhere so a user never has to recognise synonyms.
export const ENGINE_LABEL = { off: "Resting", rehearsal: "Practice", live: "Live" };
export function engineLabel(state) {
  return ENGINE_LABEL[state] || ENGINE_LABEL.off;
}

export function deriveStatus(status) {
  const state = status?.state ?? "off";              // off | rehearsal | live
  const off = state === "off";
  const live = state === "live";
  const practice = state === "rehearsal";
  const autoOn = !!status?.daemon;                   // "Automatic sending" (background runner)
  const inWindow = !!status?.in_window;
  const busy = !!status?.busy;
  // browser-running via the ONE shared read-model (LF-WL-001), never re-derived inline.
  const browserRunning = isBrowserRunning(status);
  // Connection is SESSION-based now, not process-based (LF-B1-001).
  const connected = isConnected(status);
  const nextAction = status?.next_action || "";
  // The runner can only fire on its own when the engine is armed AND automatic sending is on.
  const canAutoFire = !off && autoOn;
  // A "next" time is only real (auto-firing) when the runner can fire AND we're in the window.
  const willAutoFire = canAutoFire && inWindow && !!nextAction;
  // "Running" = actually doing, or about to do, something on its own right now.
  const running = busy || willAutoFire;
  return {
    raw: status || {}, state, off, live, practice,
    autoOn, inWindow, busy, connected, browserRunning, nextAction,
    canAutoFire, willAutoFire, running,
  };
}

// runReadout — the ONE combined "will it actually run?" answer (LF-B1-004). Folds the
// engine state + automatic sending + the working window into a single yes/no + why-not,
// so the four scattered on/off concepts resolve to one honest sentence. Reused by Home
// and the Automation tab; the campaign card renders its own campaign-scoped version.
export function runReadout(d) {
  if (!d) return { ok: false, label: "—", why: "" };
  if (d.off)
    return { ok: false, label: "Nothing will send", why: "The engine is Resting. Set it to Practice or Live on Home." };
  if (!d.autoOn)
    return { ok: false, label: "Nothing sends on its own", why: "Automatic sending is off — steps run only when you press Run. Turn it on in Automation." };
  if (!d.inWindow)
    return { ok: false, label: "Waiting for your sending window", why: "Automatic sending is on, but you're outside your working hours — nothing goes out until then." };
  if (d.practice)
    return { ok: true, label: "Rehearsing (Practice)", why: "Scheduled actions fire on time, but as a dry run — nothing is actually sent." };
  return { ok: true, label: "Live — it will send", why: "Engine Live, automatic sending on, inside your window. Scheduled actions fire within your caps." };
}

// statusLabel — the ONE pill/summary string, derived from deriveStatus so the topbar,
// Home and campaign screens can never phrase run-state three different (contradicting)
// ways. Never advertises an auto "next" the current state won't deliver (LF-016).
export function statusLabel(d) {
  const s = d.raw || {};
  const base = s.state_label || (d.off ? "Resting" : d.live ? "Live" : "Practice");
  if (d.off) return base;
  if (d.busy) return `${base} · working now…`;
  if (d.live) {
    if (d.willAutoFire) return `Live · next: ${d.nextAction}`;
    if (d.nextAction && !d.autoOn) return "Live · automatic sending off — runs when you press Run";
    if (d.nextAction && !d.inWindow) return "Live · waiting for your sending window";
    return "Live · armed";
  }
  // practice
  if (d.willAutoFire) return `Practice · next: ${d.nextAction} (rehearsal)`;
  return base;
}
