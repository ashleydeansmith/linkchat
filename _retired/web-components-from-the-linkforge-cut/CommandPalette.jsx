import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { ENGINE_LABEL } from "../useStatus.js";

// ⌘K / Ctrl-K command palette. Navigation + a few SAFE quick-actions. Deliberately
// does NOT offer "go Live" as a one-keystroke action — arming Live stays a friction
// decision on Home. Closes on Esc / scrim / after running a command.
export default function CommandPalette({ onNavigate, onRefresh, onClose }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef(null);

  const commands = useMemo(() => [
    { id: "home", label: "Go to Home", hint: "dashboard", run: () => onNavigate("home") },
    { id: "inbox", label: "Go to Inbox", hint: "replies & conversations", run: () => onNavigate("inbox") },
    { id: "people", label: "Go to People", hint: "leads", run: () => onNavigate("people") },
    { id: "campaigns", label: "Go to Campaigns", hint: "recipes", run: () => onNavigate("campaigns") },
    { id: "messages", label: "Go to Queues", hint: "manual outbound lanes", run: () => onNavigate("messages") },
    { id: "automation", label: "Go to Automation", hint: "schedule", run: () => onNavigate("automation") },
    { id: "settings", label: "Go to Settings", hint: "preferences", run: () => onNavigate("settings") },
    { id: "eng-off", label: `Engine: ${ENGINE_LABEL.off}`, hint: "nothing runs", run: async () => { await api.setEngine("off"); onRefresh?.(); } },
    { id: "eng-prac", label: `Engine: ${ENGINE_LABEL.rehearsal}`, hint: "dry-run, sends nothing", run: async () => { await api.setEngine("rehearsal"); onRefresh?.(); } },
    { id: "pause", label: "Pause everything", hint: "stop all lanes now", danger: true, run: async () => { await api.pause(); onRefresh?.(); } },
  ], [onNavigate, onRefresh]);

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase();
    if (!s) return commands;
    return commands.filter((c) => (c.label + " " + c.hint).toLowerCase().includes(s));
  }, [q, commands]);

  useEffect(() => { inputRef.current?.focus(); }, []);
  useEffect(() => { setSel(0); }, [q]);

  function onKey(e) {
    if (e.key === "Escape") { onClose(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(filtered.length - 1, s + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(0, s - 1)); }
    else if (e.key === "Enter") {
      e.preventDefault();
      const c = filtered[sel];
      if (c) { c.run(); onClose(); }
    }
  }

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="cmdk" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="cmdk-input"
          placeholder="Type a command…  (Esc to close)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
        />
        <div className="cmdk-list">
          {filtered.length === 0 ? (
            <div className="cmdk-empty">No matching commands</div>
          ) : (
            filtered.map((c, i) => (
              <div
                key={c.id}
                className={"cmdk-item" + (i === sel ? " active" : "") + (c.danger ? " danger" : "")}
                onMouseEnter={() => setSel(i)}
                onClick={() => { c.run(); onClose(); }}
              >
                <span>{c.label}</span>
                <span className="cmdk-hint">{c.hint}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
