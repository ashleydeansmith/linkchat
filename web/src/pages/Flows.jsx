import { useCallback, useEffect, useRef, useState } from "react";
import {
  ReactFlow, Background, Controls, Handle, Position, MarkerType,
  SelectionMode, useNodesState, useEdgesState,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./flows.css";
import { api } from "../api.js";
import Cockpit from "./Cockpit.jsx";

// ConversationForge (F2) — the interactive conversation-flow editor. A canvas of
// the steps of a sequence, edited directly, with the version discipline
// from F1: drafts are editable, the ACTIVE version is immutable (clone to edit),
// switching which sequence is in use takes effect on the very next message.
// Stats badges are neutral counts (n always shown) — the F3 conversion paint, with
// its causal/descriptive rules, layers on later.

const KIND = {
  opener:   { ic: "✉",  hue: "var(--primary)",  x: 0 },
  branch:   { ic: "⤷",  hue: "#5A76E0",         x: 360 },
  move:     { ic: "💬", hue: "#3FB27F",         x: 740 },
  terminal: { ic: "◼",  hue: "var(--ink-3)",    x: 1120 },
};
const COND = { label: "", pattern_ref: "✦", timeout_days: "⏱", outcome: "⚡" };

// A move/opener's copy is a sequence of chat BUBBLES joined by " · ". Split them back
// out so the inspector can show exactly what would land, one bubble per line.
const splitBubbles = (body) => (body || "").split(" · ").map((s) => s.trim()).filter(Boolean);

function CfNode({ data }) {
  const k = KIND[data.kind] || KIND.terminal;
  return (
    <div className={"cf-node cf-" + data.kind + (data.selected ? " sel" : "")}
         style={{ borderLeftColor: data.color || k.hue }}>
      <Handle type="target" position={Position.Left} isConnectable={data.editable} />
      <div className="cf-hd">
        <span className="cf-ic">{k.ic}</span>
        <span className="cf-key">{data.node_key}</span>
        {data.kind === "branch" && <span className="cf-prio" title="classification priority — lower matches first">#{data.priority}</span>}
      </div>
      <div className="cf-title">{data.label || data.node_key}</div>
      {data.kind === "branch" && (
        <div className="cf-sub">{(data.patterns || []).length} pattern{(data.patterns || []).length === 1 ? "" : "s"}</div>
      )}
      {(data.kind === "move" || data.kind === "opener") && data.body && (
        <div className="cf-sub cf-body-preview">{data.body}</div>
      )}
      {data.stats && (
        <div className="cf-stats">
          {Object.entries(data.stats).map(([ev, n]) => (
            <span key={ev} className={"cf-stat cf-stat-" + ev} title={ev.replace(/_/g, " ")}>
              {ev === "matched" ? "◈" : ev === "sent" ? "→" : ev === "second_exchange" ? "↩" : ev === "booked" ? "📅" : ev === "no_reply" ? "🕰" : "·"} {n}
            </span>
          ))}
        </div>
      )}
      {/* F3 funnel — DESCRIPTIVE, always uncoloured, n always shown */}
      {data.funnel?.reply_rate && (
        <div className="cf-funnel" title="descriptive rate (self-selected population) — never coloured">
          <span className="cf-fbar"><i style={{ width: `${Math.min(100, data.funnel.reply_rate.pct)}%` }} /></span>
          <span className="cf-fnum">{data.funnel.reply_rate.pct}% reply <em>n={data.funnel.reply_rate.n}</em></span>
          {data.funnel.booked_rate?.num > 0 && (
            <span className="cf-fnum">· {data.funnel.booked_rate.num} booked</span>
          )}
        </div>
      )}
      {/* F3 arm contrast — CAUSAL, the ONLY place colour is allowed */}
      {data.contrast && (
        <div className="cf-contrast">
          {Object.entries(data.contrast.arms).map(([ak, a]) => (
            <span key={ak} className={"cf-armstat" +
                (data.contrast.leader === ak ? " lead" : "") +
                (data.contrast.enough_data ? "" : " thin")}
                title={data.contrast.enough_data ? "randomised A/B — comparable"
                  : `needs n≥${data.contrast.min_n} per arm before a winner shows`}>
              {ak}: {a.reply_rate ? `${a.reply_rate.pct}% replied` : "—"} <em>of {a.sent}</em>
              {data.contrast.leader === ak && " ★"}
            </span>
          ))}
        </div>
      )}
      <Handle type="source" position={Position.Right} isConnectable={data.editable} />
    </div>
  );
}
const nodeTypes = { cf: CfNode };

const newKey = (kind, nodes) => {
  const base = { opener: "opener-new", branch: "B", move: "move", terminal: "end" }[kind] || "n";
  let i = 1;
  while (nodes.some((n) => n.node_key === `${base}${i}`)) i++;
  return `${base}${i}`;
};

export default function Flows() {
  const [versions, setVersions] = useState(null);
  const [vid, setVid] = useState(null);
  const [graph, setGraph] = useState(null);       // {status, updated_at, nodes, edges, arms, meta, ...}
  const [sel, setSel] = useState(null);           // {type:'node', key} | {type:'edge', id}
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState(null);       // {kind:'ok'|'err'|'conflict', text}
  const [stats, setStats] = useState(null);
  const [showStats, setShowStats] = useState(true);
  const [preview, setPreview] = useState(null);   // classify-preview result for the inspector
  const [importPath, setImportPath] = useState("");
  const [view, setView] = useState("canvas");     // canvas | summary | cockpit
  const posRef = useRef({});                      // node_key -> {x,y} live drag positions
  const rfRef = useRef(null);                      // React Flow instance (fitView etc.)

  // React Flow OWNS live node/edge state so drag + marquee selection are smooth; we
  // re-derive from the graph on change but preserve live positions / RF selection.
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  const say = (kind, text) => { setToast({ kind, text }); setTimeout(() => setToast(null), kind === "conflict" ? 12000 : 5000); };

  const loadVersions = useCallback(async (pickId) => {
    try {
      const r = await api.flowsVersions();
      setVersions(r.versions);
      const active = r.versions.find((v) => v.status === "active");
      const pick = pickId ?? active?.id ?? r.versions[0]?.id ?? null;
      setVid(pick);
      return r.versions;
    } catch (e) { say("err", String(e.message || e)); setVersions([]); return []; }
  }, []);

  const loadGraph = useCallback(async (id) => {
    if (id == null) { setGraph(null); return; }
    try {
      const g = await api.flowsGraph(id);
      posRef.current = {};
      setGraph(g); setDirty(false); setSel(null); setPreview(null);
      api.flowsStats(id).then(setStats).catch(() => setStats(null));
    } catch (e) { say("err", String(e.message || e)); }
  }, []);

  useEffect(() => { loadVersions(); }, [loadVersions]);
  useEffect(() => { loadGraph(vid); }, [vid, loadGraph]);

  const editable = graph?.status === "draft";

  // ---- graph -> React Flow node data --------------------------------------
  const nodeData = useCallback((n) => ({
    ...n, editable, selected: sel?.type === "node" && sel.key === n.node_key,
    stats: showStats ? stats?.nodes?.[n.node_key]?.events : null,
    funnel: showStats ? stats?.nodes?.[n.node_key]?.funnel : null,
    contrast: showStats ? stats?.nodes?.[n.node_key]?.contrast : null,
  }), [editable, sel, showStats, stats]);

  // A node's resting position: live drag pos > saved canvas coords > column layout.
  const columnPos = (graphNodes) => {
    const perCol = {};
    const out = {};
    graphNodes.forEach((n) => {
      const k = KIND[n.kind] || KIND.terminal;
      const row = (perCol[n.kind] = (perCol[n.kind] ?? -1) + 1);
      out[n.node_key] = { x: k.x, y: 40 + row * 150 };
    });
    return out;
  };

  const buildNodes = useCallback(() => {
    if (!graph) return [];
    const cols = columnPos(graph.nodes);
    return graph.nodes.map((n) => {
      const pos = posRef.current[n.node_key]
        || (n.canvas_x != null && n.canvas_y != null ? { x: n.canvas_x, y: n.canvas_y } : null)
        || cols[n.node_key];
      return { id: n.node_key, type: "cf", position: pos, draggable: editable, data: nodeData(n) };
    });
  }, [graph, editable, nodeData]);

  // Sync derived nodes into RF-managed state, but let RF keep live drag positions and
  // marquee selection across async re-derives (e.g. stats arriving) — no jump, no wipe.
  useEffect(() => {
    setNodes((prev) => {
      const by = Object.fromEntries(prev.map((p) => [p.id, p]));
      return buildNodes().map((d) => {
        const p = by[d.id];
        return p ? { ...d, position: p.position, selected: p.selected } : d;
      });
    });
  }, [buildNodes, setNodes]);

  const buildEdges = useCallback(() => {
    if (!graph) return [];
    return graph.edges.map((e) => {
      const id = String(e.id ?? `${e.from_node}→${e.to_node}:${e.cond_type}:${e.cond_value || ""}`);
      return {
        id, source: e.from_node, target: e.to_node, type: "smoothstep",
        label: `${COND[e.cond_type] || ""} ${e.cond_value || (e.cond_type === "outcome" ? "matched" : "")}`.trim(),
        selected: sel?.type === "edge" && sel.id === id,
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { strokeWidth: sel?.type === "edge" && sel.id === id ? 2.5 : 1.5 },
        data: e,
      };
    });
  }, [graph, sel]);

  useEffect(() => { setEdges(buildEdges()); }, [buildEdges, setEdges]);

  // ---- position persistence (single drag AND group/marquee drag) -----------
  const persistPositions = useCallback((list) => {
    if (!editable) return;                          // active/read-only: pan+select only, never move
    list.forEach((n) => { posRef.current[n.id] = n.position; });
    setGraph((g) => ({ ...g, nodes: g.nodes.map((x) => {
      const m = list.find((n) => n.id === x.node_key);
      return m ? { ...x, canvas_x: m.position.x, canvas_y: m.position.y } : x;
    }) }));
    setDirty(true);
  }, [editable]);
  const onNodeDragStop = useCallback((_e, n) => persistPositions([n]), [persistPositions]);
  const onSelectionDragStop = useCallback((_e, ns) => persistPositions(ns || []), [persistPositions]);

  // ---- canvas toolbar helpers ----------------------------------------------
  const fitView = () => rfRef.current?.fitView({ duration: 320, padding: 0.18 });
  const autoArrange = () => {                       // reset to the clean column layout
    if (!graph) return;
    posRef.current = {};
    const cols = columnPos(graph.nodes);
    if (editable) {
      setGraph((g) => ({ ...g, nodes: g.nodes.map((n) => ({ ...n, canvas_x: cols[n.node_key].x, canvas_y: cols[n.node_key].y })) }));
      setDirty(true);
    }
    setNodes(graph.nodes.map((n) => ({
      id: n.node_key, type: "cf", position: cols[n.node_key], draggable: editable, data: nodeData(n),
    })));
    setTimeout(fitView, 60);
  };

  // ---- mutations (all local until Save) ------------------------------------
  const patchNode = (key, patch) => {
    setGraph((g) => ({ ...g, nodes: g.nodes.map((n) => (n.node_key === key ? { ...n, ...patch } : n)) }));
    setDirty(true);
  };
  const patchArms = (fn) => { setGraph((g) => ({ ...g, arms: fn(g.arms) })); setDirty(true); };
  const addNode = (kind) => {
    const key = newKey(kind, graph.nodes);
    const node = { node_key: key, kind, label: key, read: null, color: null, patterns: [],
                   priority: kind === "branch" ? 50 : 100, body: "", meta: {}, canvas_x: null, canvas_y: null };
    setGraph((g) => ({ ...g, nodes: [...g.nodes, node] })); setDirty(true);
    setSel({ type: "node", key });
  };
  const deleteSelected = () => {
    if (!sel) return;
    if (sel.type === "node") {
      setGraph((g) => ({
        ...g,
        nodes: g.nodes.filter((n) => n.node_key !== sel.key),
        edges: g.edges.filter((e) => e.from_node !== sel.key && e.to_node !== sel.key),
        arms: g.arms.filter((a) => a.node_key !== sel.key),
      }));
    } else {
      setGraph((g) => ({ ...g, edges: g.edges.filter((e) => String(e.id ?? `${e.from_node}→${e.to_node}:${e.cond_type}:${e.cond_value || ""}`) !== sel.id) }));
    }
    setSel(null); setDirty(true);
  };
  const onConnect = useCallback((c) => {
    if (!editable) return;
    setGraph((g) => ({ ...g, edges: [...g.edges, { from_node: c.source, to_node: c.target, cond_type: "label", cond_value: "" }] }));
    setDirty(true);
  }, [editable]);

  // ---- server actions -------------------------------------------------------
  const save = async () => {
    setBusy(true);
    try {
      const r = await api.flowsSaveGraph(vid, {
        updated_at: graph.updated_at, nodes: graph.nodes, edges: graph.edges,
        arms: graph.arms, meta: graph.meta, name: graph.name,
      });
      setGraph((g) => ({ ...g, updated_at: r.updated_at })); setDirty(false);
      say("ok", "Draft saved.");
    } catch (e) {
      const msg = String(e.message || e);
      if (msg.includes("changed since")) say("conflict", "This draft changed elsewhere — reload to pick up the latest, then re-apply your edit.");
      else say("err", msg);
    } finally { setBusy(false); }
  };
  const activate = async () => {
    if (dirty) { say("err", "Save first. What gets used is exactly what is saved."); return; }
    if (!window.confirm(`Start using "${graph.name}"?

From now on this is the sequence LinkChat follows when it writes a message for you to approve. The one you are using now stops being used, and you can switch back to it whenever you like.`)) return;
    setBusy(true);
    try {
      await api.flowsActivate(vid);
      await loadVersions(vid); await loadGraph(vid);
      say("ok", "This one is now in use. It applies from the next message.");
    } catch (e) { say("err", String(e.message || e)); } finally { setBusy(false); }
  };
  const cloneToDraft = async () => {
    setBusy(true);
    try {
      const r = await api.flowsCreateVersion({ clone_from: vid });
      await loadVersions(r.id);
      say("ok", "Draft created — edits here can't touch the live flow until you Activate.");
    } catch (e) { say("err", String(e.message || e)); } finally { setBusy(false); }
  };
  const doImport = async (activateNow) => {
    if (!importPath.trim()) { say("err", "Give the flows.json path to import."); return; }
    setBusy(true);
    try {
      const r = await api.flowsImport({ path: importPath.trim(), name: "imported flow", activate: activateNow });
      await loadVersions(r.id);
      say("ok", `Imported as version ${r.id}${activateNow ? " (active)" : " (draft)"}.`);
    } catch (e) { say("err", String(e.message || e)); } finally { setBusy(false); }
  };
  const doExport = async () => {
    try {
      const j = await api.flowsExport(vid);
      const blob = new Blob([JSON.stringify(j, null, 1)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `flows-v${vid}.json`;
      a.click(); URL.revokeObjectURL(a.href);
    } catch (e) { say("err", String(e.message || e)); }
  };
  const runPreview = async (patterns) => {
    try { setPreview(await api.flowsPreview(patterns, 50)); }
    catch (e) { setPreview({ error: String(e.message || e) }); }
  };

  // ---- shared derivations for the inspector + summary -----------------------
  // The bubbles a node would actually send: prefer the arm bodies (A/B variants),
  // fall back to the node's own copy when there are no arms.
  const bubbleGroups = (nodeKey, fallbackBody) => {
    const arms = (graph?.arms || []).filter((a) => a.node_key === nodeKey && (a.body || "").trim());
    if (arms.length) return arms.map((a) => ({ label: a.arm_key, bubbles: splitBubbles(a.body) }));
    const fb = splitBubbles(fallbackBody);
    return fb.length ? [{ label: null, bubbles: fb }] : [];
  };
  const nodeLabel = (key) => graph?.nodes.find((n) => n.node_key === key)?.label || key;

  // ---- render ---------------------------------------------------------------
  if (versions === null) return <div className="center-screen"><span className="spin" /> Loading flows…</div>;

  if (!versions.length) {
    return (
      <div className="cf-empty">
        <div className="cf-empty-card">
          <h2>Your first sequence</h2>
          <p>A sequence is an opening message, and what to say back depending on how
             they reply. You write it once; it decides who is next and what they get.</p>
          <p>Nothing in a sequence reaches anybody on its own. It writes a message,
             you read it and approve it, and it goes to your outbox for you to send.</p>
          <div className="cf-empty-actions">
            <button className="btn" disabled={busy} onClick={async () => {
              const r = await api.flowsCreateVersion({ name: "My first sequence" });
              await loadVersions(r.id);
            }}>Start a sequence</button>
          </div>
        </div>
      </div>
    );
  }

  const selNode = sel?.type === "node" ? graph?.nodes.find((n) => n.node_key === sel.key) : null;
  const selEdge = sel?.type === "edge" ? graph?.edges.find((e) => String(e.id ?? `${e.from_node}→${e.to_node}:${e.cond_type}:${e.cond_value || ""}`) === sel.id) : null;
  const selArms = selNode ? graph.arms.filter((a) => a.node_key === selNode.node_key) : [];

  // A prominent, always-shown sent/replied/booked strip for one node's stats.
  const statStrip = (nodeKey) => {
    const nd = stats?.nodes?.[nodeKey];
    const ev = nd?.events || {};
    const rr = nd?.funnel?.reply_rate;
    return (
      <div className="cf-sendstrip">
        <div className="cf-ss-nums">
          <span><b>{ev.sent || 0}</b> sent</span>
          <span className="cf-ss-dot">·</span>
          <span><b>{ev.second_exchange || 0}</b> replied</span>
          <span className="cf-ss-dot">·</span>
          <span><b>{ev.booked || 0}</b> booked</span>
        </div>
        {rr && <div className="cf-ss-rate">{rr.pct}% reply rate <em>n={rr.n}</em></div>}
        {!nd && <div className="cf-ss-empty">Nothing has gone out from this step yet.</div>}
      </div>
    );
  };

  // Whole-flow summary rows: one per branch, joined to its pre-arranged move node.
  const branchRows = (graph?.nodes || [])
    .filter((n) => n.kind === "branch")
    .map((b) => {
      const moveKey = `${b.node_key}-move`;
      const moveNode = graph.nodes.find((n) => n.node_key === moveKey);
      const ev = stats?.nodes?.[moveKey]?.events || {};
      const rr = stats?.nodes?.[moveKey]?.funnel?.reply_rate;
      const firstBubble = bubbleGroups(moveKey, moveNode?.body)[0]?.bubbles?.[0] || "—";
      return {
        key: b.node_key, label: b.label || b.node_key,
        sends: firstBubble, sent: ev.sent || 0, replied: ev.second_exchange || 0,
        booked: ev.booked || 0, reply: rr ? rr.pct : null,
      };
    })
    .sort((a, b) => b.sent - a.sent);
  const overall = stats?.overall;
  const overallReplied = Object.values(stats?.nodes || {}).reduce((s, nd) => s + (nd.events?.second_exchange || 0), 0);
  const overallReplyPct = overall?.sent ? Math.round((overallReplied / overall.sent) * 1000) / 10 : null;
  const anySends = (overall?.sent || 0) > 0;

  return (
    <div className="cf-page">
      {/* version bar */}
      <div className="cf-bar">
        <div className="cf-tabs">
          <button className={view === "canvas" ? "on" : ""} onClick={() => setView("canvas")}>The map</button>
          <button className={view === "summary" ? "on" : ""} onClick={() => setView("summary")}>Results</button>
          <button className={view === "cockpit" ? "on" : ""} onClick={() => setView("cockpit")}>Cockpit</button>
        </div>
        <select value={vid ?? ""} onChange={(e) => setVid(Number(e.target.value))}>
          {versions.map((v) => (
            <option key={v.id} value={v.id}>
              v{v.id} · {v.name} · {v.status.toUpperCase()}
            </option>
          ))}
        </select>
        {graph?.status === "active" && <span className="cf-badge cf-live">In use — make a copy to change it</span>}
        {graph?.status === "draft" && <span className="cf-badge cf-draft">Draft — not being used yet</span>}
        {graph?.status === "retired" && <span className="cf-badge">RETIRED</span>}
        {stats?.overall?.booked_rate && (
          <span className="cf-kpi" title="whole-flow booked-call rate — descriptive">
            <b>{stats.overall.booked.toLocaleString()}</b> booked / {stats.overall.sent.toLocaleString()} sent
            <span className="cf-kpi-pct">{stats.overall.booked_rate.pct}%</span>
          </span>
        )}
        {showStats && (
          <span className="cf-legend" title={`Only randomised within-node A/B arms are causal and get colour, and only above n=${stats?.min_paint_n ?? 20}. Everything cross-node is descriptive: number + n, never a colour.`}>
            <span className="cf-lg-causal">★ causal</span>
            <span className="cf-lg-desc">grey descriptive</span>
          </span>
        )}
        <span className="cf-spacer" />
        {stats?.mirror_as_of && (
          <span className="cf-mirror" title="These numbers change when you sync your inbox, not the moment something happens.">
            inbox last read {stats.mirror_as_of}
          </span>
        )}
        <label className="cf-toggle">
          <input type="checkbox" checked={showStats} onChange={(e) => setShowStats(e.target.checked)} /> numbers
        </label>
        {editable && <button className="btn" disabled={busy || !dirty} onClick={save}>{dirty ? "Save draft" : "Saved"}</button>}
        {editable && <button className="btn primary" disabled={busy || dirty} title={dirty ? "Save first" : ""} onClick={activate}>Start using this one</button>}
        {!editable && <button className="btn" disabled={busy} onClick={cloneToDraft}>Make a copy I can change</button>}
        <button className="btn ghost" onClick={doExport}>Save a copy</button>
      </div>

      {toast && <div className={"cf-toast cf-toast-" + toast.kind}>
        {toast.text}
        {toast.kind === "conflict" && <button className="btn" onClick={() => loadGraph(vid)}>Reload</button>}
      </div>}

      {view === "cockpit" ? (
        <div className="cf-cockpit">
          <Cockpit />
        </div>
      ) : view === "summary" ? (
        <div className="cf-summary">
          <div className="cf-sum-head">
            <div className="cf-ins-hd">{graph?.name} — the whole picture</div>
            <div className="cf-ins-sub">
              {graph?.nodes.length} nodes · {graph?.arms.length} arms ·{" "}
              {stats?.mirror_as_of ? `inbox last read ${stats.mirror_as_of}` : "counted from what has happened"}
            </div>
          </div>

          <div className="cf-sum-totals">
            <div className="cf-sum-card"><span className="cf-sum-n">{(overall?.sent || 0).toLocaleString()}</span><span className="cf-sum-l">sent</span></div>
            <div className="cf-sum-card"><span className="cf-sum-n">{overallReplied.toLocaleString()}</span><span className="cf-sum-l">replied{overallReplyPct != null ? ` · ${overallReplyPct}%` : ""}</span></div>
            <div className="cf-sum-card"><span className="cf-sum-n">{(overall?.booked || 0).toLocaleString()}</span><span className="cf-sum-l">booked{overall?.booked_rate ? ` · ${overall.booked_rate.pct}%` : ""}</span></div>
          </div>

          {!anySends ? (
            <div className="cf-sum-empty">No sends recorded against this version yet. Once it's
              <b> active</b> and the inbox syncs, every branch fills with real numbers here.</div>
          ) : (
            <table className="cf-sum-table">
              <thead>
                <tr>
                  <th>Branch</th><th>What they get</th>
                  <th className="cf-num">Sent</th><th className="cf-num">Replied</th>
                  <th className="cf-num">Booked</th><th className="cf-num">Reply %</th>
                </tr>
              </thead>
              <tbody>
                {branchRows.map((r) => (
                  <tr key={r.key} className="cf-sum-row"
                      onClick={() => { setSel({ type: "node", key: r.key }); setView("canvas"); }}
                      title="Open on the canvas">
                    <td className="cf-sum-branch">{r.label}</td>
                    <td className="cf-sum-sends">{r.sends}</td>
                    <td className="cf-num">{r.sent}</td>
                    <td className="cf-num">{r.replied}</td>
                    <td className="cf-num">{r.booked}</td>
                    <td className="cf-num">{r.reply != null ? `${r.reply}%` : "—"}</td>
                  </tr>
                ))}
                {!branchRows.length && (
                  <tr><td colSpan={6} className="cf-ins-sub">Nothing splits the path yet.</td></tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      ) : (
      <div className="cf-shell">
        <div className="cf-canvas">
          {editable && (
            <div className="cf-tools">
              <button onClick={() => addNode("branch")} title="Split the path depending on what they reply">+ If they reply…</button>
              <button onClick={() => addNode("move")} title="Something you say to them">+ Message</button>
              <button onClick={() => addNode("terminal")} title="Nothing more happens down this path">+ Stop here</button>
              <button onClick={() => addNode("opener")} title="The first message, before they have said anything">+ Opening message</button>
              {sel && <button className="cf-danger" onClick={deleteSelected}>delete selected</button>}
            </div>
          )}
          <div className="cf-tools cf-tools-view">
            <button onClick={fitView} title="Fit it all on screen">fit view</button>
            <button onClick={autoArrange} title="Tidy the steps into a column">auto-arrange</button>
          </div>
          <ReactFlow
            nodes={nodes} edges={edges}
            nodeTypes={nodeTypes}
            onInit={(inst) => (rfRef.current = inst)}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onNodeClick={(_e, n) => setSel({ type: "node", key: n.id })}
            onEdgeClick={(_e, e) => setSel({ type: "edge", id: e.id })}
            onPaneClick={() => setSel(null)}
            onConnect={onConnect}
            onNodeDragStop={onNodeDragStop}
            onSelectionDragStop={onSelectionDragStop}
            nodesDraggable={editable}
            nodesConnectable={editable}
            selectionOnDrag
            selectionMode={SelectionMode.Partial}
            panOnDrag={[1, 2]}
            panActivationKeyCode="Space"
            multiSelectionKeyCode="Shift"
            zoomOnScroll panOnScroll={false}
            fitView minZoom={0.25} maxZoom={1.6} proOptions={{ hideAttribution: true }}>
            <Background gap={22} size={1.2} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        {/* inspector */}
        <div className="cf-inspector">
          {!sel && graph && (
            <>
              <div className="cf-ins-hd">{graph.name}</div>
              <div className="cf-ins-sub">
                {graph.nodes.length} nodes · {graph.edges.length} edges · {graph.arms.length} arms
              </div>
              {editable ? (
                <input className="cf-field" value={graph.name}
                       onChange={(e) => { setGraph((g) => ({ ...g, name: e.target.value })); setDirty(true); }} />
              ) : null}
              {graph.meta?.escalation && (
                <>
                  <div className="cf-lbl">Hand it to me instead (never automatic)</div>
                  {graph.meta.escalation.map((x, i) => <div key={i} className="cf-chip">{x}</div>)}
                </>
              )}
              {graph.meta?.give_bank && (
                <>
                  <div className="cf-lbl">Things you can send them</div>
                  {graph.meta.give_bank.map((x, i) => <div key={i} className="cf-chip">{x}</div>)}
                </>
              )}
              <div className="cf-hint">Click a node or edge to edit it{editable ? "" : " (read-only on a non-draft version)"}.
                Drag from a node's right handle to wire an edge. Drag a box over several nodes
                (or Shift-click) to select and move a whole section together.</div>
            </>
          )}

          {selNode && (
            <>
              <div className="cf-ins-hd">{KIND[selNode.kind]?.ic} {selNode.node_key}
                <span className="cf-kind">{selNode.kind}</span></div>

              {/* ---- what this node sends + its numbers + the next stage ---- */}
              {(selNode.kind === "move" || selNode.kind === "opener") && (
                <>
                  <div className="cf-lbl">What they get</div>
                  {(() => {
                    const groups = bubbleGroups(selNode.node_key, selNode.body);
                    if (!groups.length) return <div className="cf-ins-sub">No message written yet.</div>;
                    return groups.map((g, gi) => (
                      <div key={gi} className="cf-sends">
                        {g.label && groups.length > 1 && <div className="cf-sends-arm">variant {g.label}</div>}
                        {g.bubbles.map((b, bi) => <div key={bi} className="cf-bubble">{b}</div>)}
                      </div>
                    ));
                  })()}
                  {statStrip(selNode.node_key)}
                  {(() => {
                    const outs = graph.edges.filter((e) => e.from_node === selNode.node_key);
                    if (!outs.length) return <div className="cf-hint">Nothing follows this step yet.</div>;
                    return (
                      <>
                        <div className="cf-lbl">Leads to →</div>
                        {outs.map((e, i) => {
                          const tev = stats?.nodes?.[e.to_node]?.events;
                          const tk = graph.nodes.find((n) => n.node_key === e.to_node)?.kind;
                          return (
                            <button key={i} className="cf-leadsto" onClick={() => setSel({ type: "node", key: e.to_node })}>
                              <span className="cf-lt-cond">{`${COND[e.cond_type] || ""} ${e.cond_value || (e.cond_type === "outcome" ? "matched" : "")}`.trim() || "→"}</span>
                              <span className="cf-lt-label">{KIND[tk]?.ic} {nodeLabel(e.to_node)}</span>
                              {tev && <span className="cf-lt-stat">{tev.sent || 0}→ · {tev.second_exchange || 0}↩</span>}
                            </button>
                          );
                        })}
                      </>
                    );
                  })()}
                </>
              )}

              <div className="cf-lbl">Label</div>
              <input className="cf-field" disabled={!editable} value={selNode.label || ""}
                     onChange={(e) => patchNode(selNode.node_key, { label: e.target.value })} />
              {selNode.kind === "branch" && (
                <>
                  {statStrip(selNode.node_key)}
                  <div className="cf-lbl">The read (what this reply means)</div>
                  <textarea className="cf-field" rows={3} disabled={!editable} value={selNode.read || ""}
                            onChange={(e) => patchNode(selNode.node_key, { read: e.target.value })} />
                  <div className="cf-lbl">Which to check first (1 is checked first)</div>
                  <input className="cf-field cf-narrow" type="number" disabled={!editable}
                         value={selNode.priority}
                         onChange={(e) => patchNode(selNode.node_key, { priority: Number(e.target.value) })} />
                  <div className="cf-lbl">Words to look for in their reply — one per line</div>
                  <textarea className="cf-field" rows={6} disabled={!editable}
                            value={(selNode.patterns || []).join("\n")}
                            onChange={(e) => patchNode(selNode.node_key, { patterns: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
                  <button className="btn" onClick={() => runPreview(selNode.patterns || [])}>
                    Which recent replies match?
                  </button>
                  {preview && !preview.error && (
                    <div className="cf-preview">
                      <div className="cf-ins-sub">{preview.matched} of {preview.sampled} recent inbound replies match</div>
                      {preview.replies.filter((r) => r.matches).slice(0, 8).map((r, i) => (
                        <div key={i} className="cf-chip cf-hit"><b>{r.name}</b>: {r.reply}</div>
                      ))}
                    </div>
                  )}
                  {preview?.error && <div className="cf-chip cf-err">{preview.error}</div>}
                  <div className="cf-lbl">Never match if they say</div>
                  <textarea className="cf-field" rows={2} disabled={!editable}
                            value={((selNode.meta || {}).never || []).join("\n")}
                            onChange={(e) => patchNode(selNode.node_key, { meta: { ...(selNode.meta || {}), never: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) } })} />
                </>
              )}
              {(selNode.kind === "move" || selNode.kind === "opener") && (
                <>
                  {(() => {
                    const r = stats?.nodes?.[selNode.node_key]?.readiness;
                    if (!r) return null;
                    const label = {
                      no_data: "Nothing yet", gathering: `Too early to tell — ${r.n || 0} replies so far${r.need ? `, need about ${r.need}` : ""}`,
                      single_arm_stable: `Steady, only one version running (${r.n} replies)`,
                      no_winner: `Enough replies to judge, and neither version is ahead (${r.n})`,
                      has_winner: `Version ${r.winner} is doing better (${r.n} replies)`,
                    }[r.status] || r.status;
                    return (
                      <div className={"cf-grad cf-grad-" + r.status}>
                        <div className="cf-lbl">Enough replies to tell yet?</div>
                        <div className="cf-grad-row">{label}</div>
                        {r.status === "has_winner" && (
                          <div className="cf-grad-note">
                            This one is doing well enough to lean on — but it still comes to you first.
                            Auto-send stays OFF: it's blocked on the conversation-state
                            machine (safety, plan §6b-14). This flags it; it never sends.
                          </div>
                        )}
                      </div>
                    );
                  })()}
                  <div className="cf-lbl">Copy (the pre-arranged {selNode.kind === "opener" ? "opener" : "move"})</div>
                  <textarea className="cf-field" rows={4} disabled={!editable} value={selNode.body || ""}
                            onChange={(e) => patchNode(selNode.node_key, { body: e.target.value })} />
                  <div className="cf-lbl">Try two versions (each person always gets the same one)</div>
                  {selArms.map((a) => (
                    <div key={a.arm_key} className={"cf-arm" + (a.enabled ? "" : " off")}>
                      <div className="cf-arm-hd">
                        <b>Version {a.arm_key}</b>
                        {editable && (
                          <label><input type="checkbox" checked={!!a.enabled}
                            onChange={(e) => patchArms((arms) => arms.map((x) => (x.node_key === a.node_key && x.arm_key === a.arm_key ? { ...x, enabled: e.target.checked ? 1 : 0 } : x)))} /> use this one</label>
                        )}
                      </div>
                      <textarea rows={3} disabled={!editable} value={a.body}
                                onChange={(e) => patchArms((arms) => arms.map((x) => (x.node_key === a.node_key && x.arm_key === a.arm_key ? { ...x, body: e.target.value } : x)))} />
                      {showStats && stats?.nodes?.[selNode.node_key]?.arms?.[a.arm_key] && (
                        <div className="cf-ins-sub">{JSON.stringify(stats.nodes[selNode.node_key].arms[a.arm_key])}</div>
                      )}
                    </div>
                  ))}
                  {editable && (
                    <button className="btn ghost" onClick={() => {
                      const used = selArms.map((a) => a.arm_key);
                      const next = "abcdefgh".split("").find((c) => !used.includes(c)) || `x${used.length}`;
                      patchArms((arms) => [...arms, { node_key: selNode.node_key, arm_key: next, body: "", enabled: 1 }]);
                    }}>+ Add another version</button>
                  )}
                </>
              )}
            </>
          )}

          {selEdge && (
            <>
              <div className="cf-ins-hd">Edge · {selEdge.from_node} → {selEdge.to_node}</div>
              <div className="cf-lbl">When this path is taken</div>
              <select className="cf-field" disabled={!editable} value={selEdge.cond_type}
                      onChange={(e) => { const v = e.target.value; setGraph((g) => ({ ...g, edges: g.edges.map((x) => (x === selEdge ? { ...x, cond_type: v } : x)) })); setDirty(true); }}>
                <option value="label">Just a note to myself — this path never runs on its own</option>
                <option value="pattern_ref">When their reply matches the words I listed</option>
                <option value="timeout_days">When they have not replied for this many days</option>
                <option value="outcome">When something happened — they matched, or they booked</option>
              </select>
              <div className="cf-lbl">
                {selEdge.cond_type === "timeout_days" ? "How many days of silence"
                  : selEdge.cond_type === "pattern_ref" ? "Which set of words"
                  : selEdge.cond_type === "outcome" ? "Which outcome"
                  : "Your note"}
              </div>
              <input className="cf-field" disabled={!editable} value={selEdge.cond_value || ""}
                     onChange={(e) => { const v = e.target.value; setGraph((g) => ({ ...g, edges: g.edges.map((x) => (x === selEdge ? { ...x, cond_value: v } : x)) })); setDirty(true); }} />
              <div className="cf-hint">A note to yourself is only a note. Nothing happens
                down this path unless you pick one of the other three, which are the
                only ones LinkChat can act on. To follow up when somebody goes quiet,
                pick the third and put a number of days in the box.</div>
            </>
          )}
        </div>
      </div>
      )}
    </div>
  );
}
