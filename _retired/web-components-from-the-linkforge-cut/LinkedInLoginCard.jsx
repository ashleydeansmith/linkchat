// LinkedInLoginCard — the SUCCESS confirmation only (LF-B1-002). The old amber "Connect
// your LinkedIn" prompt floated over the engine card / canvas on every screen with no
// dismiss; that job moved to the inline onboarding checklist on Home (OnboardingChecklist),
// which never occludes content. This component now renders ONLY the bottom-right "connected"
// snackbar (clear of all content) when a session first appears — nothing floats over a screen.
import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { isConnected } from "../useStatus.js";

export default function LinkedInLoginCard() {
  const [b, setB] = useState(null);
  const [justConnected, setJustConnected] = useState(false);
  const wasConnected = useRef(false);

  useEffect(() => {
    const load = () => api.browser().then(setB).catch(() => {});
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, []);

  const connected = isConnected(b);

  useEffect(() => {
    if (connected && !wasConnected.current) {
      setJustConnected(true);
      // 3s auto-dismiss (was 10s) — a confirmation shouldn't occlude the screen (LF-008).
      const t = setTimeout(() => setJustConnected(false), 3000);
      wasConnected.current = true;
      return () => clearTimeout(t);
    }
    if (!connected) wasConnected.current = false;
  }, [connected]);

  if (justConnected) {
    return (
      // bottom-right snackbar — clear of the page H1 it used to overlap.
      <div
        style={{
          position: "fixed", bottom: 20, right: 20, zIndex: 80,
          maxWidth: 380, width: "calc(100% - 40px)",
          display: "flex", alignItems: "center", gap: 14, padding: "12px 16px",
          borderRadius: 12, background: "var(--surface, #0d1117)",
          border: "1px solid var(--green, #3fb950)", boxShadow: "0 10px 34px rgba(0,0,0,0.4)",
        }}
      >
        <div style={{ flex: 1 }}>
          <div style={{ fontWeight: 600, fontSize: 13.5, color: "var(--green, #3fb950)" }}>
            LinkedIn connected
          </div>
          <div className="muted" style={{ fontSize: 12.5 }}>
            You're signed in. the parent program will use this session on this machine only — you can close the browser window.
          </div>
        </div>
      </div>
    );
  }

  // No floating prompt: when not yet connected, the inline onboarding checklist on Home
  // owns the "connect" call-to-action. This component stays silent until success.
  return null;
}