import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { isConnected } from "../useStatus.js";
import "./inbox.css";

const BOXES = [
  ["focused", "Focused"],
  ["unread", "Unread"],
  ["needs-you", "Needs you"],
  ["snoozed", "Snoozed"],
  ["archived", "Archived"],
  ["all", "All"],
];

// db stores follow_up_at / now as "YYYY-MM-DD HH:MM:SS" local strings — match that.
function isoLocal(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function relTime(v) {
  if (!v) return "";
  const ms = /^\d+$/.test(String(v)) ? Number(v) : Date.parse(v);
  if (!ms) return "";
  const diff = (Date.now() - ms) / 1000;
  if (diff < 60) return "now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  if (diff < 604800) return `${Math.floor(diff / 86400)}d`;
  return new Date(ms).toLocaleDateString();
}
function initials(name) {
  return (name || "?").split(/\s+/).slice(0, 2).map((s) => s[0] || "").join("").toUpperCase();
}

// Turn a raw backend sync failure ("scrape budget: scrape sub-cap reached (201/200)...") into
// plain user copy (FT-2). Also collapses any doubled "(n/m) (n/m)" count to a single one, so the
// message reads cleanly even if an older backend still double-prints it. Returns "" for no message.
function humanizeSyncError(msg) {
  const m = String(msg || "").trim();
  if (!m) return "";
  const cap = m.match(/\((\d+)\s*\/\s*(\d+)\)/);          // first count only — de-dupes a doubled one
  const count = cap ? ` (${cap[1]}/${cap[2]})` : "";
  if (/budget|cap reached|sub-cap/i.test(m)) {
    return `Daily LinkedIn read-limit reached${count} — try again tomorrow.`;
  }
  if (/no keeper|start your linkedin browser/i.test(m)) {
    return "Sign in to LinkedIn first, then sync.";
  }
  if (/read_lock|another linkedin lane|lane is busy/i.test(m)) {
    return "LinkChat is busy with another LinkedIn task — try the sync again in a moment.";
  }
  if (/selftest|could not reach|not signed in|needs.?login/i.test(m)) {
    return "Couldn't reach LinkedIn — check you are signed in, then sync again.";
  }
  return m;   // unknown failure — show the raw message rather than swallow it
}

export default function Inbox({ status }) {
  // LF-C2-N2: the connection line renders through the ONE isConnected read-model, so the
  // Inbox never shows a reassuring "✓" while Home says "not signed in" for the same state.
  const connected = isConnected(status);
  const [box, setBox] = useState("focused");
  const [boxes, setBoxes] = useState({});
  const [tags, setTags] = useState([]);
  const [tagFilter, setTagFilter] = useState(null);
  const [q, setQ] = useState("");
  const [convs, setConvs] = useState([]);
  const [selId, setSelId] = useState(null);
  const [conv, setConv] = useState(null);
  const [busy, setBusy] = useState(false);
  const [sync, setSync] = useState({ running: false });
  const [keeper, setKeeper] = useState(true);
  const [banner, setBanner] = useState("");
  const [lastSync, setLastSync] = useState(null);  // FT-1: {ok} | {ok:false, msg, human} — last sync outcome this session
  const [delTag, setDelTag] = useState(null);     // label pending delete-confirm
  const [delDontAsk, setDelDontAsk] = useState(false);
  const [noAskDel, setNoAskDel] = useState(() => {
    try { return localStorage.getItem("ibx-del-noask") === "1"; } catch { return false; }
  });
  const reqRef = useRef(0);
  const convReqRef = useRef(0);          // guards async thread loads against fast click-throughs
  const [threadLoading, setThreadLoading] = useState(false);
  const prefetchedRef = useRef(new Set());   // thread ids already warmed this session
  const didPrefetchRef = useRef(false);      // prefetch runs once per session (budget-bounded)
  const userBusyRef = useRef(false);         // true while the user's OWN thread fetch is in flight

  const [loadError, setLoadError] = useState("");

  const loadMeta = useCallback(async () => {
    try {
      const [s, t] = await Promise.all([api.inbox.status(), api.inbox.tags()]);
      setBoxes(s.boxes || {});
      setSync(s.sync || { running: false });
      setKeeper(!!s.keeper);
      setTags(t.tags || []);
    } catch (e) {
      // Do NOT swallow this. It used to be ignored on the grounds that the engine
      // might still be starting, which meant a screen whose calls were all failing
      // painted itself as a calm empty inbox showing zeros. A screen that cannot
      // load has to say so - the zeros are indistinguishable from a real answer.
      setLoadError(String(e && e.message ? e.message : e));
      return;
    }
    setLoadError("");
  }, []);

  // "have we ever synced?" — nothing in any box means a first run.
  const neverSynced = (boxes.all ?? 0) === 0;
  // FT-1: a sync was attempted this session but came back blocked/failed (e.g. read-limit hit).
  const syncBlocked = !!lastSync && lastSync.ok === false;

  const loadList = useCallback(async () => {
    const token = ++reqRef.current;
    try {
      const r = await api.inbox.list({ box, tag: tagFilter, q: q || undefined });
      if (token === reqRef.current) setConvs(r.conversations || []);
    } catch (e) {
      if (token === reqRef.current) setConvs([]);
    }
  }, [box, tagFilter, q]);

  useEffect(() => { loadMeta(); }, [loadMeta]);
  useEffect(() => { loadList(); }, [loadList]);
  // auto-start the keeper the moment the Inbox opens, so opening threads / sending always has a
  // live browser without the user remembering to start it. Fire-and-forget; no-op if already up.
  // The line that used to sit here asked the engine to open the browser, at an
  // address the engine does not answer on. It failed silently on every load. The
  // browser is opened by the job that needs it, which is where the lock is taken.

  const openConv = useCallback(async (id) => {
    const token = ++convReqRef.current;   // newest click wins; stale background loads are dropped
    setSelId(id);
    setThreadLoading(true);
    // INSTANT: show the header (name/photo/headline) from the list row we already have — never blank.
    const row = convs.find((c) => c.id === id);
    if (row) setConv({ ...row, messages: [] });
    try {
      // cached thread from the local DB is 1-11ms — show it immediately, no "Loading…" gate.
      const c = await api.inbox.open(id);
      if (token !== convReqRef.current) return;        // user clicked another conversation
      setConv(c);
      if (c.messages && c.messages.length) {
        setThreadLoading(false);                        // had it cached → done, instant
      } else {
        // never opened before — pull the thread from LinkedIn in the BACKGROUND (needs the keeper),
        // then patch it in. The header stays visible the whole time instead of a blank pane.
        userBusyRef.current = true;   // tell the prefetcher to yield — a real click takes priority
        try {
          await api.inbox.fetchMessages(id);
          const fresh = await api.inbox.open(id);
          if (token === convReqRef.current) setConv(fresh);
        } catch { /* keeper down / fetch failed — header stays up, thread shows the empty hint */ }
        finally { userBusyRef.current = false; if (token === convReqRef.current) setThreadLoading(false); }
      }
      loadMeta();   // unread count may have changed
    } catch (e) {
      if (token === convReqRef.current) { setConv({ error: String(e) }); setThreadLoading(false); }
    }
  }, [loadMeta, convs]);

  // Background prefetch: warm the top few visible threads into the local cache so the first open is
  // INSTANT too. Bounded by design: runs once per session, top 8 only, skips threads already cached,
  // only when the keeper is already up (never auto-spawns just to prefetch), paces between keeper
  // hits, and yields to a real click. Each thread is fetched at most once ever (cache persists), so
  // the scrape budget cost is small and self-limiting. Best-effort — never disrupts the UI.
  const prefetchTop = useCallback(async (rows) => {
    try { const s = await api.inbox.status(); if (!s.keeper) return; } catch { return; }
    const seen = prefetchedRef.current;
    for (const c of rows.slice(0, 8)) {
      if (seen.has(c.id)) continue;
      seen.add(c.id);
      if (userBusyRef.current) await new Promise((r) => setTimeout(r, 700));   // let a real click go first
      try {
        const full = await api.inbox.open(c.id);
        if (full.messages && full.messages.length) continue;   // already cached — skip the keeper hit
        await api.inbox.fetchMessages(c.id);                    // warm the cache (keeper, scrape budget)
      } catch { /* best-effort */ }
      await new Promise((r) => setTimeout(r, 400));             // gentle pacing between keeper hits
    }
  }, []);

  useEffect(() => {
    if (didPrefetchRef.current || !convs.length) return;
    didPrefetchRef.current = true;
    prefetchTop(convs);
  }, [convs, prefetchTop]);

  async function runSync() {
    setBanner("Syncing inbox…");
    setLastSync(null);
    try {
      await api.inbox.sync();
      // poll status until the background sync finishes
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 1500));
        const s = await api.inbox.status();
        setSync(s.sync || {});
        setBoxes(s.boxes || {});
        if (!s.sync?.running) {
          const res = s.sync?.result;
          if (res && res.ok === false) {
            // FT-1/FT-2: a failed sync must NOT read as "nothing happened". Record the blocked
            // outcome (drives a distinct empty-state) and show plain, de-duped copy in the banner.
            const human = humanizeSyncError(res.msg) || "Couldn't sync — try again later.";
            setLastSync({ ok: false, msg: res.msg || "", human });
            setBanner(human);
          } else {
            setLastSync({ ok: true });
            setBanner("");
          }
          break;
        }
      }
      loadList();
    } catch (e) {
      const human = `Couldn't sync — ${e}`;
      setLastSync({ ok: false, msg: String(e), human });
      setBanner(human);
    }
  }

  // --- CRM actions (operate on the selected conversation) -------------------
  async function act(fn) {
    if (!selId) return;
    setBusy(true);
    try { await fn(); await openConv(selId); await loadList(); await loadMeta(); }
    finally { setBusy(false); }
  }
  const snooze = (d) => act(() => api.inbox.snooze(selId, d ? isoLocal(d) : null));
  const toggleArchive = () => act(() => api.inbox.archive(selId, !conv.archived_at));
  const togglePin = () => act(() => api.inbox.pin(selId, !conv.pinned));
  const toggleTag = (tagId, on) => act(() => api.inbox.tagToggle(selId, tagId, on));

  async function createTag() {
    const name = window.prompt("New label name:");
    if (!name) return;
    await api.inbox.createTag(name.trim());
    loadMeta();
  }
  async function doDeleteTag(id) {
    await api.inbox.deleteTag(id);
    if (tagFilter === id) setTagFilter(null);
    loadMeta(); loadList();
  }
  function requestDeleteTag(t) {
    if (noAskDel) { doDeleteTag(t.id); return; }   // opted out of confirms → delete straight away
    setDelDontAsk(false);
    setDelTag(t);
  }
  function confirmDeleteTag() {
    if (delDontAsk) {
      setNoAskDel(true);
      try { localStorage.setItem("ibx-del-noask", "1"); } catch { /* ignore */ }
    }
    const id = delTag.id;
    setDelTag(null);
    doDeleteTag(id);
  }

  const tomorrow9 = () => { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(9, 0, 0, 0); return d; };
  const in3h = () => new Date(Date.now() + 3 * 3600 * 1000);
  const nextWeek = () => { const d = new Date(); d.setDate(d.getDate() + 7); d.setHours(9, 0, 0, 0); return d; };

  // keyboard shortcuts (Kondo-style): J/K navigate, E archive, H snooze. Ignored while typing.
  useEffect(() => {
    const onKey = (e) => {
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "j" || k === "k") {
        if (!convs.length) return;
        e.preventDefault();
        const idx = convs.findIndex((c) => c.id === selId);
        const next = k === "j" ? Math.min(convs.length - 1, idx + 1) : Math.max(0, idx - 1);
        const target = convs[idx === -1 ? 0 : next];
        if (target) openConv(target.id);
      } else if (k === "e" && selId) { e.preventDefault(); toggleArchive(); }
      else if (k === "h" && selId) { e.preventDefault(); snooze(tomorrow9()); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [convs, selId, conv]);   // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="ibx">
      {loadError ? (
        <div className="ibx-loaderr">
          <strong>This screen could not load.</strong> Your conversations are not
          showing, and the counts here are not a real answer. Close LinkChat and
          open it again. If it keeps happening, copy this line and send it to
          whoever gave you LinkChat: {loadError}
        </div>
      ) : null}
      {/* ---- rail ---- */}
      <div className="ibx-rail">
        {BOXES.map(([key, label]) => (
          <button key={key} className={"ibx-box" + (box === key && !tagFilter ? " active" : "")}
                  onClick={() => { setBox(key); setTagFilter(null); }}>
            <span>{label}</span>
            <span className="badge">{boxes[key] ?? 0}</span>
          </button>
        ))}
        <div className="sec">Labels</div>
        {tags.length === 0 && <div className="ibx-banner">No labels yet.</div>}
        {tags.map((t) => (
          <div className="ibx-tagrow" key={t.id}>
            <button className={"ibx-box" + (tagFilter === t.id ? " active" : "")}
                    style={{ flex: 1 }}
                    onClick={() => { setTagFilter(tagFilter === t.id ? null : t.id); }}>
              <span className="ibx-tag">
                <span className="dot" style={{ background: t.color || "var(--ink-3)" }} />
                {t.name}
              </span>
              <span className="badge">{t.count ?? 0}</span>
            </button>
            <button className="x" title="Delete label" onClick={() => requestDeleteTag(t)}>✕</button>
          </div>
        ))}
        <button className="ibx-btn tiny railbtn" onClick={createTag}>+ New label</button>
        <div className="spacer" style={{ flex: 1 }} />
        <button className={"ibx-btn railbtn" + (neverSynced && keeper ? " primary" : "")}
                onClick={runSync} disabled={sync.running || !keeper} title={!keeper ? "Sign in to LinkedIn first - sync opens your inbox and reads it" : "Read your LinkedIn inbox now"}>
          {sync.running ? "Syncing…" : "↻ Sync inbox"}
        </button>
        {!keeper && (
          <div className="ibx-banner" style={{ color: "var(--amber)" }}>
            Sign in to LinkedIn first, then Sync.
          </div>
        )}
        <a className="ibx-btn tiny" href={api.inbox.exportUrl()} style={{ textAlign: "center", textDecoration: "none" }}>
          Export CSV
        </a>
      </div>

      {/* ---- list ---- */}
      <div className="ibx-list">
        <div className="ibx-search">
          <input placeholder="Search name or message…" value={q}
                 onChange={(e) => setQ(e.target.value)} />
          <div className="ibx-banner" style={{ fontSize: 10.5 }}>J/K move · E archive · H snooze · ; snippets · N note</div>
          {box === "needs-you" && !tagFilter && (
            <div className="ibx-banner" style={{ fontSize: 11.5 }}>
              Everyone whose last message is waiting on you — your one place to see who replied.
            </div>
          )}
          {banner && <div className="ibx-banner">{banner}</div>}
        </div>
        {convs.length === 0 && neverSynced && syncBlocked && (
          // FT-1: the sync ran but was blocked — DON'T keep telling the user to "sync first" as if
          // nothing happened. Reflect the failed attempt and why, in plain words.
          <div className="ibx-firstrun">
            <div className="big">⚠ Couldn’t sync your inbox</div>
            <p>{lastSync.human}</p>
            <p className="muted-s">
              Nothing was pulled in this time — so your inbox is still empty. LinkChat will pick up
              where it left off; try the sync again later.
            </p>
            <button className="ibx-btn primary" onClick={runSync} disabled={sync.running || !keeper} title={!keeper ? "Sign in to LinkedIn first - sync opens your inbox and reads it" : "Read your LinkedIn inbox now"}>
              {sync.running ? "Syncing…" : "↻ Try sync again"}
            </button>
          </div>
        )}
        {convs.length === 0 && neverSynced && !syncBlocked && (
          <div className="ibx-firstrun">
            <div className="big">👋 Your inbox is empty</div>
            <p>
              LinkChat doesn’t hold your LinkedIn messages until you pull them in.
              {" "}<b>Sync the inbox first</b> — then your conversations, labels, snooze and
              notes all work here.
            </p>
            <ol>
              <li>{connected
                ? "You're signed in to LinkedIn ✓"
                : keeper
                  ? "Browser running — you are not signed in to LinkedIn yet"
                  : "Sign in to LinkedIn in the browser LinkChat opens"}</li>
              <li>Click <b>↻ Sync inbox</b> in the left rail</li>
            </ol>
            <button className="ibx-btn primary" onClick={runSync} disabled={sync.running || !keeper} title={!keeper ? "Sign in to LinkedIn first - sync opens your inbox and reads it" : "Read your LinkedIn inbox now"}>
              {sync.running ? "Syncing…" : "↻ Sync inbox now"}
            </button>
          </div>
        )}
        {convs.length === 0 && !neverSynced && (
          <div className="ibx-empty">No conversations in this view.</div>
        )}
        {convs.map((c) => (
          <div key={c.id} className={"ibx-conv" + (selId === c.id ? " active" : "")}
               onClick={() => openConv(c.id)}>
            {c.participant_avatar
              ? <img className="ibx-av" src={c.participant_avatar} alt="" referrerPolicy="no-referrer" />
              : <span className="ibx-av">{initials(c.participant_name)}</span>}
            <div className="body">
              <div className="nm">{c.participant_name || "Unknown"}</div>
              {c.participant_headline && <div className="hl">{c.participant_headline}</div>}
              <div className="pv">{c.last_preview || ""}</div>
              {c.tags?.length > 0 && (
                <div className="ibx-chips">
                  {c.tags.map((t) => <span key={t.id} className="ibx-chip"
                    style={{ background: t.color ? t.color + "22" : undefined }}>{t.name}</span>)}
                </div>
              )}
            </div>
            <div className="meta">
              <span className="tm">{relTime(c.last_msg_at)}</span>
              {c.unread > 0 && <span className="ibx-unread">{c.unread}</span>}
              {c.last_msg_dir === "in" && <span className="ibx-owe" title="Owe a reply" />}
              {c.pinned ? <span title="Pinned">📌</span> : null}
            </div>
          </div>
        ))}
      </div>

      {/* ---- panel ---- */}
      {!conv ? (
        <div className="ibx-panel"><div className="ibx-empty" style={{ gridColumn: "1 / -1" }}>
          {selId ? "Loading…" : syncBlocked ? lastSync.human : neverSynced ? "Sync your inbox first (left rail) to load your conversations." : "Select a conversation."}
        </div></div>
      ) : conv.error ? (
        <div className="ibx-panel"><div className="ibx-empty" style={{ gridColumn: "1 / -1" }}>{conv.error}</div></div>
      ) : (
        <ConversationPanel
          conv={conv}
          threadLoading={threadLoading}
          tags={tags}
          busy={busy}
          onSent={() => act(async () => {})}
          reload={async () => { await openConv(conv.id); await loadList(); }}
          snooze={snooze}
          toggleArchive={toggleArchive}
          togglePin={togglePin}
          toggleTag={toggleTag}
          quickSnoozes={{ in3h, tomorrow9, nextWeek }}
        />
      )}

      {delTag && (
        <div className="modal-scrim" onClick={() => setDelTag(null)}>
          <div className="modal" style={{ maxWidth: 420 }} onClick={(e) => e.stopPropagation()}>
            <h3>Delete label “{delTag.name}”?</h3>
            <p>It’ll be removed from all conversations. This can’t be undone.</p>
            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, margin: "6px 0 4px", cursor: "pointer" }}>
              <input type="checkbox" checked={delDontAsk} onChange={(e) => setDelDontAsk(e.target.checked)} />
              Don’t ask me again — just delete
            </label>
            <div className="modal-actions">
              <button className="btn ghost" onClick={() => setDelTag(null)}>Cancel</button>
              <button className="btn primary" style={{ background: "var(--red)" }} onClick={confirmDeleteTag}>Delete</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// LinkedIn caps voice notes at 60s and hard-cuts anything longer; warn from 50s so the
// last 10s show a red countdown, and auto-stop at the cap so a clip is never truncated mid-word.
const VOICE_MAX_SEC = 60;
const VOICE_WARN_SEC = 50;

function fmtClock(s) {
  const m = Math.floor(s / 60);
  return m + ":" + String(s % 60).padStart(2, "0");
}

function ConversationPanel({ conv, threadLoading, tags, busy, reload, snooze, toggleArchive, togglePin, toggleTag, quickSnoozes }) {
  const [text, setText] = useState("");
  const [note, setNote] = useState(conv.note || "");
  const [sending, setSending] = useState(false);
  const [said, setSaid] = useState("");        // what the engine answered, in its words
  const [saidBad, setSaidBad] = useState(false);
  const [gifOpen, setGifOpen] = useState(false);
  const [gifQ, setGifQ] = useState("");
  const [gifs, setGifs] = useState([]);
  const [voiceState, setVoiceState] = useState("idle");  // idle | recording | review
  const [recSec, setRecSec] = useState(0);               // elapsed secs while recording
  const [recDurationMs, setRecDurationMs] = useState(0); // final length from voice-stop
  const [previewKey, setPreviewKey] = useState(0);       // cache-buster for the preview <audio>
  const [mics, setMics] = useState([]);                  // [{index,name,sr}] real input devices
  const [micDevice, setMicDevice] = useState(null);      // chosen device index (persisted)
  const [tx, setTx] = useState({});           // {audioUrl: transcript}
  const [snippets, setSnippets] = useState([]);
  const [snipOpen, setSnipOpen] = useState(false);
  const [snipEdit, setSnipEdit] = useState(null);   // {id?, name, body} being created/edited
  const [crmOpen, setCrmOpen] = useState(() => {
    try { return localStorage.getItem("ibx-crm") !== "0"; } catch { return true; }
  });
  // --- suggested-give panel (the DM reply cockpit) --------------------------
  // MANUAL-FIRST: the panel only loads a bubble into the composer; the operator taps the
  // existing Send once per bubble. Nothing here auto-sends. `sugg` is the /suggest payload.
  const [sugg, setSugg] = useState(null);        // {version,branch,label,gives:[{arm_key,arm_hash,bubbles}]} | null
  const [activeGive, setActiveGive] = useState(null);  // index of the give currently being worked
  const [curBubble, setCurBubble] = useState(null);    // bubble index just loaded into the composer
  const [sentTexts, setSentTexts] = useState([]);      // bubbles actually sent for the active give (in send order)
  const [sentIdx, setSentIdx] = useState(() => new Set());   // bubble indices sent (for the ✓ ticks)
  const [recordedGives, setRecordedGives] = useState(() => new Set()); // gives whose capture already fired (UI)
  const recordedRef = useRef(new Set());   // synchronous fire-guard so flow-record never double-fires
  const fileRef = useRef(null);
  const threadRef = useRef(null);
  const composerRef = useRef(null);
  const noteRef = useRef(null);
  const recTimerRef = useRef(null);   // setInterval handle for the recording clock
  const recStartRef = useRef(0);      // Date.now() when recording began (drift-free elapsed)
  const stoppingRef = useRef(false);  // re-entrancy guard (manual Stop + 60s auto-stop race)

  const reloadSnippets = () => api.inbox.snippets().then((r) => setSnippets(r.snippets || [])).catch(() => {});
  useEffect(() => { reloadSnippets(); }, []);

  async function saveSnippet() {
    if (!snipEdit?.name?.trim()) return;
    await api.inbox.upsertSnippet(snipEdit.name.trim(), snipEdit.body || "");
    setSnipEdit(null);
    reloadSnippets();
  }
  async function deleteSnippet(id) {
    await api.inbox.deleteSnippet(id);
    reloadSnippets();
  }

  // insert a snippet at the composer cursor, filling {first_name}/{{first_name}} from the participant
  function insertSnippet(s) {
    const fn = (conv.participant_name || "there").split(/\s+/)[0];
    const body = (s.body || "").replace(/\{\{?\s*first_name\s*\}?\}/gi, fn);
    const el = composerRef.current;
    const cur = text || "";
    let next;
    if (el && typeof el.selectionStart === "number") {
      next = cur.slice(0, el.selectionStart) + body + cur.slice(el.selectionEnd);
    } else {
      next = cur + body;
    }
    setText(next);
    setSnipOpen(false);
    setTimeout(() => { try { el && el.focus(); } catch (e) { /* ignore */ } }, 0);
  }

  // keyboard: ; opens snippets, N focuses the note (ignored while typing in a field)
  useEffect(() => {
    const onKey = (e) => {
      const t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === ";") { e.preventDefault(); setSnipOpen((o) => !o); }
      else if (e.key.toLowerCase() === "n") { e.preventDefault(); noteRef.current?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function toggleCrm() {
    setCrmOpen((o) => {
      const n = !o;
      try { localStorage.setItem("ibx-crm", n ? "1" : "0"); } catch { /* ignore */ }
      return n;
    });
  }

  useEffect(() => { setNote(conv.note || ""); setText(""); resetVoice(); }, [conv.id]);
  useEffect(() => () => clearRecTimer(), []);   // clear the recording clock on unmount

  // Fetch the give-suggestion for the opened thread and reset all give-tracking state. Read-only
  // and best-effort: a failure (or no give version / no branch match) simply shows no panel.
  useEffect(() => {
    let alive = true;
    setSugg(null); setActiveGive(null); setCurBubble(null);
    setSentTexts([]); setSentIdx(new Set()); setRecordedGives(new Set());
    recordedRef.current = new Set();
    api.inbox.suggest(conv.id)
      .then((r) => { if (alive) setSugg(r || null); })
      .catch(() => { if (alive) setSugg(null); });
    return () => { alive = false; };
  }, [conv.id]);

  // Load a suggested bubble into the composer to EDIT then send (never auto-sends). Switching to a
  // different give resets that give's send-tracking; working within the same give keeps it.
  function loadBubble(gi, bi) {
    setActiveGive((prev) => {
      if (prev !== gi) { setSentTexts([]); setSentIdx(new Set()); }
      return gi;
    });
    setCurBubble(bi);
    const give = sugg?.gives?.[gi];
    setText((give?.bubbles?.[bi]) || "");
    setTimeout(() => { try { composerRef.current?.focus(); } catch (e) { /* ignore */ } }, 0);
  }

  // Fire the suggested-vs-sent learning capture ONCE per give (best-effort; never blocks the UI).
  function recordGive(gi) {
    const give = sugg?.gives?.[gi];
    if (!give || recordedRef.current.has(gi)) return;
    recordedRef.current.add(gi);
    setRecordedGives((prev) => { const n = new Set(prev); n.add(gi); return n; });
    api.inbox.flowRecord(conv.id, {
      branch: sugg.branch,
      arm_key: give.arm_key,
      arm_hash: give.arm_hash,
      suggested_body: (give.bubbles || []).join(" · "),
      sent_body: sentTexts.join(" · "),
    }).catch(() => { /* capture is best-effort — never surface a failure */ });
  }

  // Auto-capture once every bubble of the active give has been sent (the operator can also finish
  // early via the "Done" affordance). Guarded by recordedRef so it fires at most once per give.
  useEffect(() => {
    if (activeGive == null) return;
    const give = sugg?.gives?.[activeGive];
    if (!give || !give.bubbles?.length) return;
    if (sentIdx.size >= give.bubbles.length && !recordedRef.current.has(activeGive)) {
      recordGive(activeGive);
    }
  }, [sentIdx, activeGive]);   // eslint-disable-line react-hooks/exhaustive-deps

  // The microphone list that used to load here is gone with the recorder.
  useEffect(() => {
    if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;
  }, [conv]);

  const convTagIds = new Set((conv.tags || []).map((t) => t.id));


  // Replying. GIFs, attachments and voice notes are still gone, and there is
  // still no handler for them anywhere - a handler nobody can reach today is a
  // handler somebody wires up next month, without the checks.
  //
  // Every refusal that comes back is shown in the words the engine used. A reply
  // stopped because the person is on your hold list is not an error, it is the
  // program doing its job, and saying "Send failed" would teach you to distrust
  // a working guard.
  async function sendReply() {
    const words = (text || "").trim();
    if (!words || sending) return;
    setSending(true);
    setSaid("");
    setSaidBad(false);
    try {
      const r = await api.crmReply(conv.id, words);
      setText("");
      setSaid(r.next || (r.sent ? "Sent." : "Written to your outbox."));
      setSaidBad(!r.sent);
      reload(conv.id);
    } catch (e) {
      setSaid(String(e.message || e));
      setSaidBad(true);
    } finally {
      setSending(false);
    }
  }

  async function saveNote() {
    try { await api.inbox.note(conv.id, note); } catch (e) { /* ignore */ }
  }



  function clearRecTimer() {
    if (recTimerRef.current) { clearInterval(recTimerRef.current); recTimerRef.current = null; }
  }


  // Stop the recording and go to REVIEW (play it back first) — does NOT send. Guarded so the
  // 60s auto-stop and a manual Stop click can't both fire voice-stop.


  function resetVoice() {
    clearRecTimer();
    stoppingRef.current = false;
    setVoiceState("idle"); setRecSec(0); setRecDurationMs(0);
  }


  // Writing out what a voice note says is not built, so there is no button for it.

  const profile = conv.participant_profile_url;

  return (
    <div className={"ibx-panel" + (crmOpen ? "" : " crm-collapsed")}>
      <div className="ibx-head">
        <div style={{ flex: 1 }}>
          <div className="nm">{conv.participant_name || "Unknown"}</div>
          {conv.participant_headline && <div className="hl">{conv.participant_headline}</div>}
        </div>
        {profile && <a className="ibx-btn tiny" href={profile} target="_blank" rel="noreferrer">Profile ↗</a>}
        <button className="ibx-btn tiny" onClick={toggleCrm}
          title={crmOpen ? "Hide the details panel for a wider conversation" : "Show notes, snooze & labels"}>
          {crmOpen ? "Hide details ⟩" : "⟨ Details"}
        </button>
      </div>

      <div className="ibx-thread" ref={threadRef}>
        {(conv.messages || []).map((m, i) => (
          <div key={i} className={"ibx-msg " + (m.direction === "out" ? "out" : "in")}>
            {m.body}
            {m.audio && (
              <div className="tx">A voice note. Open this conversation in LinkedIn to
                hear it — LinkChat does not play recordings back yet.</div>
            )}
            {m.attach && m.attach.url && /image|gif/i.test(m.attach.mediatype || "") && (
              <img src={m.attach.url} alt={m.attach.name || ""} />
            )}
          </div>
        ))}
        {(conv.messages || []).length === 0 && (
          <div className="ibx-empty">{threadLoading ? "Loading messages…" : "No messages loaded for this thread."}</div>
        )}
      </div>

      {/* The composer that used to be here is gone, and that is the product.
          It offered a reply box, a Send button styled as the main action, GIFs,
          attachments and a microphone - five promises this program refuses to
          keep. It sat directly under a banner saying nothing here sends, and
          pressing Send raised a box reading "Send failed: Error", which tells
          somebody the program is broken when it is doing exactly what it said. */}
      {/* The reply box. It used to say replying was not wired up, and the reason
          given was a good one: a reply typed here would have gone out without
          facing the checks every other message faces. That is fixed by sending it
          down the same road, not by opening a second one - so the hold list, the
          unfilled-words check and the copy check all stand in front of this box
          exactly as they stand in front of a sequence.

          What it does not face is the review step, and that is deliberate. That
          step exists so a sequence cannot mark its own homework. You wrote these
          words, so there is nothing left to review. */}
      <div className="ibx-composer">
        <textarea
          value={text}
          onChange={(e) => { setText(e.target.value); setSaid(""); }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendReply(); }
          }}
          placeholder={"Write your reply. It goes into the conversation when you press Send."}
          rows={3} />
        <div className="row">
          <button className="ibx-btn primary" disabled={sending || !text.trim()}
                  onClick={sendReply}>
            {sending ? "Sending…" : "Send"}
          </button>
          <span className="muted">Ctrl+Enter sends · a copy is written to your outbox first</span>
        </div>
        {said && <div className={"ibx-said" + (saidBad ? " bad" : "")}>{said}</div>}
      </div>


      <div className="ibx-crm">
        <div>
          <div className="lbl">Note</div>
          <textarea ref={noteRef} value={note} onChange={(e) => setNote(e.target.value)} onBlur={saveNote}
                    placeholder="Private note (local only)…" />
        </div>
        <div>
          <div className="lbl">Snooze {conv.follow_up_at ? `· until ${conv.follow_up_at}` : ""}</div>
          <div className="row">
            <button className="ibx-btn tiny" onClick={() => snooze(quickSnoozes.in3h())}>3 hours</button>
            <button className="ibx-btn tiny" onClick={() => snooze(quickSnoozes.tomorrow9())}>Tomorrow</button>
            <button className="ibx-btn tiny" onClick={() => snooze(quickSnoozes.nextWeek())}>Next week</button>
            {conv.follow_up_at && <button className="ibx-btn tiny" onClick={() => snooze(null)}>Clear</button>}
          </div>
        </div>
        <div>
          <div className="lbl">Labels</div>
          <div className="row">
            {tags.length === 0 && <span className="ibx-banner">Create a label in the rail.</span>}
            {tags.map((t) => {
              const on = convTagIds.has(t.id);
              return (
                <button key={t.id} className={"ibx-btn tiny" + (on ? " on" : "")}
                        disabled={busy} onClick={() => toggleTag(t.id, !on)}>
                  {on ? "✓ " : "+ "}{t.name}
                </button>
              );
            })}
          </div>
        </div>
        <div className="row">
          <button className="ibx-btn tiny" disabled={busy} onClick={togglePin}>
            {conv.pinned ? "📌 Unpin" : "📌 Pin"}
          </button>
          <button className="ibx-btn tiny" disabled={busy} onClick={toggleArchive}>
            {conv.archived_at ? "Unarchive" : "Archive"}
          </button>
        </div>
      </div>
    </div>
  );
}
