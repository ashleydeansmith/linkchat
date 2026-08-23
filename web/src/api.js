// The one place the screens talk to the engine (engine/server.py). Relative /api
// paths are proxied to 127.0.0.1:8790 while developing, and served from the same
// place as the screens in the built program.

async function apiError(path, r) {
  let detail = "";
  try {
    const j = await r.json();
    if (typeof j.detail === "string") detail = j.detail;
    else if (j.detail != null) detail = JSON.stringify(j.detail);
    else if (typeof j.message === "string") detail = j.message;
  } catch { /* non-JSON body */ }
  throw new Error(detail || `${path} → ${r.status}`);
}
async function get(path) {
  const r = await fetch(`/api${path}`);
  if (!r.ok) await apiError(path, r);
  return r.json();
}
async function post(path, body) {
  const r = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) await apiError(path, r);
  return r.json();
}
async function put(path, body) {
  const r = await fetch(`/api${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!r.ok) await apiError(path, r);
  return r.json();
}
async function del(path) {
  const r = await fetch(`/api${path}`, { method: "DELETE" });
  if (!r.ok) await apiError(path, r);
  return r.json();
}
async function postForm(path, formData) {
  // multipart (file uploads) — no JSON content-type; the browser sets the boundary.
  const r = await fetch(`/api${path}`, { method: "POST", body: formData });
  if (!r.ok) await apiError(path, r);
  return r.json();
}

export const api = {
  health: () => get("/health"),
  // Your CRM: what LinkChat can see, your people, what is waiting, and the two
  // ways a message reaches somebody - approving one a sequence wrote, and
  // replying in your own words. Both run the same checks.
  crmState: () => get("/crm/state"),
  crmPeople: (q) => get("/crm/people" + (q ? `?q=${encodeURIComponent(q)}` : "")),
  crmWaiting: () => get("/crm/waiting"),
  crmApprove: (body) => post("/crm/approve", body),
  crmReply: (conv_id, body) => post("/crm/reply", { conv_id, body }),
  // CommentForge fold (Phase 2) — the approval queue on /api/comment/*
  commentQueue: (status = "awaiting_review", track = "") =>
    get(`/comment/queue?status=${encodeURIComponent(status)}` +
        (track ? `&track=${encodeURIComponent(track)}` : "")),
  commentCounts: () => get("/comment/counts"),
  commentStatus: () => get("/comment/status"),
  commentModes: () => get("/comment/modes"),
  commentApprove: (id, text) => post(`/comment/queue/${id}/approve`, { text }),
  commentSkip: (id, reason) => post(`/comment/queue/${id}/skip`, { reason }),
  commentKill: (id, reason) => post(`/comment/queue/${id}/kill`, { reason }),
  commentRegenerate: (id, mode, steer) => post(`/comment/queue/${id}/regenerate`, { mode, steer }),
  commentMarkPosted: (id, campaign_tag) => post(`/comment/queue/${id}/mark-posted`, { campaign_tag }),
  commentCampaign: (id, campaign_tag) => post(`/comment/queue/${id}/campaign`, { campaign_tag }),
  commentEngagements: () => get("/comment/engagements"),
  // ConversationForge (F2)
  flowsVersions: () => get("/flows/versions"),
  flowsCreateVersion: (body) => post("/flows/versions", body),
  flowsActivate: (id) => post(`/flows/versions/${id}/activate`),
  flowsGraph: (id) => get(`/flows/versions/${id}/graph`),
  flowsSaveGraph: (id, body) => put(`/flows/versions/${id}/graph`, body),
  flowsStats: (id) => get(`/flows/stats?version_id=${id}`),
  flowsPreview: (patterns, limit = 50) => post("/flows/classify-preview", { patterns, limit }),
  flowsImport: (body) => post("/flows/import", body),
  flowsExport: (id) => get(`/flows/versions/${id}/export`),
  flowsMarkBooked: (leadId, at) => post(`/flows/leads/${leadId}/mark-booked`, { at }),
  flowsReactivateQueue: (limit = 100) => get(`/flows/reactivate-queue?limit=${limit}`),
  status: () => get("/status"),
  safety: () => get("/safety"),
  caps: () => get("/caps"),
  saveCaps: (body) => post("/caps", body),
  connectNote: () => get("/connect-note"),
  saveConnectNote: (note) => post("/connect-note", { note }),
  pipeline: () => get("/pipeline"),
  meta: () => get("/meta"),
  capability: () => get("/capability"),
  templates: () => get("/templates"),
  templateRead: (name) => get(`/templates/${encodeURIComponent(name)}`),
  templateSave: (name, content) => post(`/templates/${encodeURIComponent(name)}`, { content }),
  templateCreate: (name) => post("/templates", { name }),
  templatePreview: (name) => get(`/templates/${encodeURIComponent(name)}/preview`),
  campaigns: () => get("/campaigns"),
  campaignFlow: (id) => get(`/campaigns/${id}/flow`),
  createCampaign: (body) => post("/campaign", body),
  deleteCampaign: (id) => del(`/campaigns/${id}`),
  campaignSequence: (id) => get(`/campaigns/${id}/sequence`),
  saveCampaignSequence: (id, steps, variant = "A") => post(`/campaigns/${id}/sequence`, { steps, variant }),
  variantReport: (id) => get(`/campaigns/${id}/variant-report`),
  setCampaignSource: (id, source_type, source_ref = "") => post(`/campaigns/${id}/source`, { source_type, source_ref }),
  setCampaignComponent: (id, type, present = true) => post(`/campaigns/${id}/component`, { type, present }),
  withdrawDays: () => get("/withdraw-days"),
  saveWithdrawDays: (days) => post("/withdraw-days", { days }),
  leads: ({ campaign, status, q, limit = 300 } = {}) => {
    const p = new URLSearchParams();
    if (campaign != null) p.set("campaign", campaign);
    if (status) p.set("status", status);
    if (q) p.set("q", q);
    p.set("limit", limit);
    return get(`/leads?${p}`);
  },
  leadHistory: (id) => get(`/leads/${id}/history`),
  leadSkip: (id) => post(`/leads/${id}/skip`),
  leadRestore: (id) => post(`/leads/${id}/restore`),
  manageDeleteStatus: (status) => post("/manage/delete-status", { status }),
  manageClearMessages: () => post("/manage/clear-messages"),
  manageReset: () => post("/manage/reset"),
  schedule: () => get("/schedule"),
  scheduleToggle: (id, enabled) => post("/schedule/toggle", { id, enabled }),
  scheduleDaemon: (action) => post("/schedule/daemon", { action }),
  scheduleActions: () => get("/schedule/actions"),
  scheduleUpsert: (body) => post("/schedule/entry", body),
  scheduleDelete: (id) => del(`/schedule/${id}`),
  replies: () => get("/replies"),
  activity: (limit = 12) => get(`/activity?limit=${limit}`),
  setEngine: (state) => post("/engine", { state }),
  pause: () => post("/pause"),
  licence: () => get("/licence"),
  activateLicence: (code) => post("/licence/activate", { code }),
  eula: () => get("/eula"),
  acceptEula: () => post("/eula/accept", { accept: true }),
  update: () => get("/update"),
  applyUpdate: () => post("/update/apply"),
  browser: () => get("/browser"),
  browserAction: (action) => post("/browser", { action }),
  keeperInput: (ev) => post("/keeper/input", ev),
  run: (cmd, args = []) => post("/run", { cmd, args }),
  // Events — who would be invited (database only, no browser) and the event campaigns here
  eventsAudience: ({ event, tier = "", match = "", from_campaign = "", limit = 25 }) =>
    get(`/events/audience?event=${encodeURIComponent(event)}&tier=${encodeURIComponent(tier)}` +
        `&match=${encodeURIComponent(match)}&from_campaign=${encodeURIComponent(from_campaign)}` +
        `&limit=${limit}`),
  eventsCampaigns: () => get("/events/campaigns"),
  runGet: (id) => get(`/run/${id}`),

  // --- Inbox / messaging surface (the InboxForge merge) ---------------------
  inbox: {
    status: () => get("/inbox/status"),
    list: ({ box = "focused", tag, q, limit = 200 } = {}) => {
      const p = new URLSearchParams();
      p.set("box", box);
      if (tag != null) p.set("tag", tag);
      if (q) p.set("q", q);
      p.set("limit", limit);
      return get(`/inbox?${p}`);
    },
    open: (id) => get(`/inbox/${id}`),
    sync: (max = 20) => post(`/inbox/sync?max=${max}`),
    fetchMessages: (id) => post(`/inbox/${id}/fetch-messages`),
    note: (id, note) => post(`/inbox/${id}/note`, { note }),
    snooze: (id, until) => post(`/inbox/${id}/snooze`, { until }),
    archive: (id, archived) => post(`/inbox/${id}/archive`, { archived }),
    pin: (id, pinned) => post(`/inbox/${id}/pin`, { pinned }),
    // DM reply cockpit: read-only give-suggestion for the open thread (never sends), and the
    // best-effort learning capture fired once a suggested give's bubbles have been sent.
    suggest: (id) => get(`/inbox/${id}/suggest`),
    flowRecord: (id, body) => post(`/inbox/${id}/flow-record`, body),
    // Free-text improvement ("here's how I'd handle this") -> an improve learning stamp.
    improve: (id, body) => post(`/inbox/${id}/improve`, body),
    // DM Cockpit: ONE feed of every draft awaiting a decision — awaiting-reply drafts AND
    // re-activation drafts (each with kind + resolved bubbles), read-only. Send/Improve reuse
    // send+flowRecord; Kill stamps the item off the queue. reviewDecision kills a reply (by
    // conv_id); reactivationDecision kills a re-activation (by lead identity — no conv_id).
    reviewQueue: (limit = 50) => get(`/inbox/review-queue?limit=${limit}`),
    reviewDecision: (id, body) => post(`/inbox/${id}/review-decision`, body),
    reactivationDecision: (body) => post("/inbox/reactivation-decision", body),
    tagToggle: (id, tag_id, on) => post(`/inbox/${id}/tags`, { tag_id, on }),
    clear: (box, tag) => post("/inbox/clear", { box, tag }),
    unarchiveAll: () => post("/inbox/unarchive-all"),
    tags: () => get("/inbox/tags"),
    createTag: (name, color) => post("/inbox/tags", { name, color }),
    deleteTag: (id) => del(`/inbox/tags/${id}`),
    snippets: () => get("/inbox/snippets"),
    upsertSnippet: (name, body) => post("/inbox/snippets", { name, body }),
    deleteSnippet: (id) => del(`/inbox/snippets/${id}`),
    // Voice notes, attachments and playback are not built. The calls that used
    // to sit here are gone rather than left pointing at doors that answer "not
    // built" - a call nobody can complete is a call somebody wires a button to.
    exportUrl: () => `/api/inbox/export?format=csv`,
  },
};

// stage → dot colour, matching the mockup's semantic palette.
export function stageColor(code) {
  if (code === "accepted" || code === "event_attending") return "var(--green)";
  if (code === "replied" || code === "messaged" || code === "inmailed") return "var(--primary)";
  if (code && (code.startsWith("queued") || code.startsWith("invited") || code.startsWith("event")))
    return "var(--amber)";
  if (code === "skipped") return "var(--red)";
  return "var(--ink-3)";
}
