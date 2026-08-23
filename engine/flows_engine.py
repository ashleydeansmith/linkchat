"""flows_engine.py — the ConversationForge engine seam (F1, 2026-07-15).

ONE module owns flow truth: the classifier, the active-version read, the stamp
ledger, arm assignment, and flows.json import/export. flows_chart, the send path,
the sensors, the API and the watchdog all consume THIS — nobody re-implements the
flow logic by eye (plan §5.3).

Design rules baked in (plan V3 + §6c adversarial review):
  * active_version() is a FRESH DB read per invocation — lanes are subprocesses,
    nothing caches across sends, so a draft edit is invisible until Activate and an
    activation is visible to the very next send without any restart (§6-8 / §6a-8).
  * classify() is priority-ordered first-match-wins over the ACTIVE version's branch
    patterns (finding 5). Draft versions can never touch live classification.
  * stamp() is idempotent (natural event_key + INSERT OR IGNORE — finding 6), keyed
    by canonical profile URL (finding 10), append-only. It takes an OPEN connection
    so the send path can stamp inside its own transaction.
  * Version clones share a lineage_uuid; arm stats bind to content-hash lineage and
    per-lead arm assignment is sticky at first exposure (finding 7).
  * Zero LLM anywhere in this module — deterministic, beta-distributable core.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .canon import canon_in

# v0 classifier precedence (flows_chart, 2026-07-14): R5 (pitch-back) outranks the
# yes/no reads because sellers say 'yes' too; R2 before R1 because 'no' substrings
# hide inside longer yes-replies less often than vice versa. Imports map this order
# onto the explicit priority column; unknown branch keys land after, in file order.
_V0_PRIORITY = {"R5": 0, "R2": 1, "R1": 2, "R4": 3, "R3": 4}

MIN_PATTERN_LEN = 3   # a bare 1-2 char substring swallows the inbox (finding 5)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def content_hash(body: str | None) -> str:
    """Stable identity of a piece of copy — arm stats bind to this, not to the row id,
    so re-keying or cloning a version never reshuffles an arm's history."""
    return hashlib.sha1(_norm_text(body).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Pattern validation (finding 5): the match-preview ADVISES, the validator BLOCKS.
# ---------------------------------------------------------------------------

def validate_pattern(p) -> str | None:
    """Return an error string if this pattern must be rejected, else None.
    The length floor targets GENERIC short substrings ('a', 'ok', 'no' — they swallow
    the inbox); a punctuation-only pattern like ':)' is a deliberate emoticon match
    and passes at 2 chars (the live R3 branch uses it)."""
    if not isinstance(p, str):
        return "pattern must be a string"
    t = _norm_text(p)
    if not t:
        return "empty pattern matches everything"
    floor = MIN_PATTERN_LEN if re.search(r"\w", t) else 2
    if len(t) < floor:
        return f"pattern '{p}' is under {floor} chars — it would swallow the inbox"
    return None


def validate_patterns(patterns) -> list[str]:
    if not isinstance(patterns, (list, tuple)):
        return ["patterns must be a list"]
    return [e for e in (validate_pattern(p) for p in patterns) if e]


# ---------------------------------------------------------------------------
# Pure classifier — shared by the live engine AND flows_chart's flows.json path.
# ---------------------------------------------------------------------------

def classify_ordered(text: str | None, ordered: list[tuple[str, list[str]]]) -> str | None:
    """First-match-wins over (node_key, patterns) pairs already in priority order.
    Substring match on whitespace-normalised lowercase text; unmatched -> None."""
    t = _norm_text(text)
    if not t:
        return None
    for node_key, patterns in ordered:
        if any(p and _norm_text(p) in t for p in (patterns or [])):
            return node_key
    return None


def branches_to_ordered(branches: list[dict]) -> list[tuple[str, list[str]]]:
    """flows.json branch list -> priority-ordered pairs, reproducing the v0 order."""
    keyed = [(b.get("id") or "", b.get("patterns") or []) for b in branches]
    return sorted(keyed, key=lambda kp: (_V0_PRIORITY.get(kp[0], 100), kp[0]))


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def active_version(conn=None, campaign_id: int | None = None) -> dict | None:
    """The single active flow version for a scope — campaign-specific first, the
    NULL-scope default second (finding 11). ALWAYS a fresh read; never cached."""
    def _q(cx):
        if campaign_id is not None:
            r = cx.execute("SELECT * FROM flow_versions WHERE status='active' AND "
                           "scope_campaign_id=?", (campaign_id,)).fetchone()
            if r:
                return dict(r)
        r = cx.execute("SELECT * FROM flow_versions WHERE status='active' AND "
                       "scope_campaign_id IS NULL").fetchone()
        return dict(r) if r else None
    if conn is not None:
        return _q(conn)
    with db.connect() as cx:
        return _q(cx)


def version_graph(version_id: int, conn=None) -> dict:
    """The whole graph in one payload: version row + nodes + edges + arms."""
    def _q(cx):
        v = cx.execute("SELECT * FROM flow_versions WHERE id=?", (version_id,)).fetchone()
        if not v:
            raise KeyError(f"flow version {version_id} not found")
        nodes = [dict(r) for r in cx.execute(
            "SELECT * FROM flow_nodes WHERE version_id=? ORDER BY priority, node_key",
            (version_id,))]
        for n in nodes:
            n["patterns"] = json.loads(n["patterns"]) if n["patterns"] else []
            n["meta"] = json.loads(n["meta"]) if n["meta"] else {}
        edges = [dict(r) for r in cx.execute(
            "SELECT * FROM flow_edges WHERE version_id=? ORDER BY id", (version_id,))]
        arms = [dict(r) for r in cx.execute(
            "SELECT * FROM flow_arms WHERE version_id=? ORDER BY node_key, arm_key",
            (version_id,))]
        out = dict(v)
        out["meta"] = json.loads(out["meta"]) if out["meta"] else {}
        out.update({"nodes": nodes, "edges": edges, "arms": arms})
        return out
    if conn is not None:
        return _q(conn)
    with db.connect() as cx:
        return _q(cx)


def _ordered_branches(version_id: int, conn) -> list[tuple[str, list[str]]]:
    rows = conn.execute(
        "SELECT node_key, patterns FROM flow_nodes WHERE version_id=? AND kind='branch' "
        "ORDER BY priority, node_key", (version_id,)).fetchall()
    return [(r["node_key"], json.loads(r["patterns"]) if r["patterns"] else []) for r in rows]


def classify(text: str | None, campaign_id: int | None = None) -> tuple[str | None, dict | None]:
    """Classify one reply against the ACTIVE version. Returns (node_key|None, version|None).
    No active version -> (None, None): the caller falls back to flows.json (interim)."""
    with db.connect() as conn:
        v = active_version(conn, campaign_id)
        if not v:
            return None, None
        return classify_ordered(text, _ordered_branches(v["id"], conn)), v


def create_draft(name: str | None = None, clone_from: int | None = None,
                 scope_campaign_id: int | None = None) -> int:
    """Create a draft version — empty, or a full clone of an existing version.
    A clone KEEPS the lineage_uuid (finding 7): same flow, new revision."""
    now = _now()
    with db.connect() as conn:
        if clone_from is not None:
            src = version_graph(clone_from, conn)
            lineage, scope = src["lineage_uuid"], src["scope_campaign_id"]
            vname = name or src["name"]
        else:
            lineage, scope = str(uuid.uuid4()), scope_campaign_id
            vname = name or "untitled flow"
        cur = conn.execute(
            "INSERT INTO flow_versions (lineage_uuid, name, scope_campaign_id, status, "
            "meta, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (lineage, vname, scope, "draft",
             json.dumps(src["meta"]) if clone_from is not None else None,
             "editor", now, now))
        vid = cur.lastrowid
        if clone_from is not None:
            for n in src["nodes"]:
                conn.execute(
                    "INSERT INTO flow_nodes (version_id, node_key, kind, label, read, color,"
                    " patterns, priority, body, meta, canvas_x, canvas_y)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (vid, n["node_key"], n["kind"], n["label"], n["read"], n["color"],
                     json.dumps(n["patterns"]), n["priority"], n["body"],
                     json.dumps(n["meta"]) if n["meta"] else None,
                     n["canvas_x"], n["canvas_y"]))
            for e in src["edges"]:
                conn.execute(
                    "INSERT INTO flow_edges (version_id, from_node, to_node, cond_type,"
                    " cond_value) VALUES (?,?,?,?,?)",
                    (vid, e["from_node"], e["to_node"], e["cond_type"], e["cond_value"]))
            for a in src["arms"]:
                conn.execute(
                    "INSERT INTO flow_arms (version_id, node_key, arm_key, body,"
                    " content_hash, enabled, retired_at) VALUES (?,?,?,?,?,?,?)",
                    (vid, a["node_key"], a["arm_key"], a["body"], a["content_hash"],
                     a["enabled"], a["retired_at"]))
        return vid


def activate_version(version_id: int) -> dict:
    """Transactional swap: retire the current active in the SAME scope, promote this
    draft, audit the event. Exactly one active per scope survives (plan §5.1)."""
    now = _now()
    with db.connect() as conn:
        v = conn.execute("SELECT * FROM flow_versions WHERE id=?", (version_id,)).fetchone()
        if not v:
            raise KeyError(f"flow version {version_id} not found")
        if v["status"] == "active":
            return dict(v)
        scope = v["scope_campaign_id"]
        if scope is None:
            conn.execute("UPDATE flow_versions SET status='retired', retired_at=?, "
                         "updated_at=? WHERE status='active' AND scope_campaign_id IS NULL",
                         (now, now))
        else:
            conn.execute("UPDATE flow_versions SET status='retired', retired_at=?, "
                         "updated_at=? WHERE status='active' AND scope_campaign_id=?",
                         (now, now, scope))
        conn.execute("UPDATE flow_versions SET status='active', activated_at=?, "
                     "updated_at=? WHERE id=?", (now, now, version_id))
        conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                     (now, None, "flow_activated",
                      json.dumps({"version_id": version_id, "name": v["name"],
                                  "scope_campaign_id": scope})))
        row = conn.execute("SELECT * FROM flow_versions WHERE id=?", (version_id,)).fetchone()
        return dict(row)


# ---------------------------------------------------------------------------
# Stamps — the append-only analytic ledger.
# ---------------------------------------------------------------------------

def event_key(event: str, *, canonical_url: str | None = None, lineage: str | None = None,
              node_key: str | None = None, message_id: int | None = None,
              extra: str | None = None) -> str:
    """Natural idempotency key per event class (finding 6):
      sent            -> one per message row (the intent row id is the natural unit)
      matched         -> one per (lineage, person, node): re-classification is a no-op
      second_exchange -> one per (lineage, person, node): first counts, repeats don't
      booked          -> one per (person, meeting time)
      edge_traversed  -> one per (lineage, person, edge)"""
    if event == "sent" and message_id is not None:
        return f"sent|msg|{message_id}"
    parts = [event, lineage or "-", canonical_url or "-", node_key or "-"]
    if extra:
        parts.append(extra)
    return "|".join(parts)


def stamp(conn, *, event: str, node_key: str, ev_key: str,
          canonical_url: str | None = None, lead_id: int | None = None,
          thread_urn: str | None = None, version_id: int | None = None,
          lineage_uuid: str | None = None, arm_key: str | None = None,
          arm_hash: str | None = None, cohort: str = "fresh",
          account_id: str = "default", detail: str | None = None,
          stamped_at: str | None = None) -> bool:
    """Append one stamp on an OPEN connection (caller owns the transaction — the send
    path stamps inside the same transaction as the message confirm). INSERT OR IGNORE
    on the natural key: re-runs and re-classifications can never double-count.
    Returns True if a row was actually inserted."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO flow_stamps (account_id, canonical_url, lead_id, thread_urn,"
        " version_id, lineage_uuid, node_key, arm_key, arm_hash, cohort, event, event_key,"
        " stamped_at, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, canonical_url, lead_id, thread_urn, version_id, lineage_uuid,
         node_key, arm_key, arm_hash, cohort, event, ev_key,
         stamped_at or _now(), detail))
    return cur.rowcount > 0


def assign_arm(conn, canonical_url: str, version: dict, node_key: str) -> tuple[str, str] | None:
    """Sticky per-lead arm assignment (finding 7): first exposure picks the enabled arm
    with the fewest assignments (balances the split deterministically); every later
    call returns the SAME arm even if arms were edited since. Returns (arm_key, hash)."""
    lineage = version["lineage_uuid"]
    row = conn.execute(
        "SELECT arm_key, arm_hash FROM flow_arm_assignments WHERE canonical_url=? AND "
        "lineage_uuid=? AND node_key=?", (canonical_url, lineage, node_key)).fetchone()
    if row:
        return row["arm_key"], row["arm_hash"]
    arms = conn.execute(
        "SELECT arm_key, content_hash FROM flow_arms WHERE version_id=? AND node_key=? "
        "AND enabled=1 ORDER BY arm_key", (version["id"], node_key)).fetchall()
    if not arms:
        return None
    counts = {a["arm_key"]: 0 for a in arms}
    for r in conn.execute(
            "SELECT arm_key, COUNT(*) n FROM flow_arm_assignments WHERE lineage_uuid=? "
            "AND node_key=? GROUP BY arm_key", (lineage, node_key)):
        if r["arm_key"] in counts:
            counts[r["arm_key"]] = r["n"]
    pick = min(arms, key=lambda a: (counts[a["arm_key"]], a["arm_key"]))
    conn.execute(
        "INSERT OR IGNORE INTO flow_arm_assignments (canonical_url, lineage_uuid, node_key,"
        " arm_key, arm_hash, assigned_at) VALUES (?,?,?,?,?,?)",
        (canonical_url, lineage, node_key, pick["arm_key"], pick["content_hash"], _now()))
    return pick["arm_key"], pick["content_hash"]


def _tokens(s: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", (s or "").lower()))


def match_arm_by_body(body: str | None, version: dict, conn) -> dict | None:
    """Reconcile a SEND's full text (all bubbles joined) against the active version's
    arm copy (finding 8: the dominant path — a human sending by hand — must not be
    invisible to the stamps). Personalisation slots, punctuation drift and spintax
    make exact hashing brittle, so after the exact-hash fast path this scores token
    Jaccard similarity per arm (slots stripped) and requires BOTH a floor (>=0.75)
    and a margin over the runner-up (>=0.05) — ambiguous copy attributes to nothing
    rather than the wrong arm (the support-engine gate pattern)."""
    if not body:
        return None
    h = content_hash(body)
    row = conn.execute(
        "SELECT node_key, arm_key, content_hash FROM flow_arms WHERE version_id=? AND "
        "content_hash=?", (version["id"], h)).fetchone()
    if row:
        return dict(row)
    bt = _tokens(body)
    if len(bt) < 4:
        return None   # too little copy to attribute honestly
    scored = []
    for a in conn.execute("SELECT node_key, arm_key, body, content_hash FROM flow_arms "
                          "WHERE version_id=? AND enabled=1", (version["id"],)):
        at = _tokens(re.sub(r"\{[a-z_]+\}", " ", a["body"] or ""))
        if len(at) < 4:
            continue
        sim = len(bt & at) / len(bt | at)
        scored.append((sim, {"node_key": a["node_key"], "arm_key": a["arm_key"],
                             "content_hash": a["content_hash"]}))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.75:
        return None
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.05:
        return None   # two arms this close = ambiguous, never guess
    return scored[0][1]


# ---------------------------------------------------------------------------
# flows.json import / export (flows.json = interchange format, DB = truth)
# ---------------------------------------------------------------------------

def import_flows_json(src, name: str = "imported flow", activate: bool = False,
                      lineage: str | None = None) -> int:
    """Seed a version from a flows.json document (path or dict). Openers and branch
    next-moves become nodes+arms; forward branches become typed-where-possible edges
    ('silence' -> timeout, everything else stays an honest display label until Ashley
    types it in the F2 editor — prose is a label, never a condition, finding 4)."""
    doc = src if isinstance(src, dict) else json.loads(Path(src).read_text(encoding="utf-8"))
    errs: list[str] = []
    for b in doc.get("branches", []):
        errs += [f"{b.get('id')}: {e}" for e in validate_patterns(b.get("patterns", []))]
    if errs:
        raise ValueError("flows.json failed pattern validation: " + "; ".join(errs))
    now = _now()
    meta = {k: doc[k] for k in ("escalation", "give_bank", "drafting_rules", "_doc")
            if k in doc}
    # LINEAGE (finding 7): a re-import of the SAME flow must continue its lineage or
    # every stamp ever taken is orphaned. Priority: explicit param > a "lineage" field
    # in the document (export emits one) > the current default-scope active version's
    # lineage (re-seeding the flow you're running IS the same flow) > a fresh UUID.
    if not lineage:
        lineage = doc.get("lineage")
    if not lineage:
        cur_active = active_version(campaign_id=None)
        lineage = cur_active["lineage_uuid"] if cur_active else None
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO flow_versions (lineage_uuid, name, scope_campaign_id, status,"
            " meta, source, created_at, updated_at) VALUES (?,?,NULL,'draft',?,?,?,?)",
            (lineage or str(uuid.uuid4()), name, json.dumps(meta, ensure_ascii=False),
             "import", now, now))
        vid = cur.lastrowid

        def node(key, kind, *, label=None, read=None, color=None, patterns=None,
                 priority=100, body=None, nmeta=None):
            conn.execute(
                "INSERT INTO flow_nodes (version_id, node_key, kind, label, read, color,"
                " patterns, priority, body, meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (vid, key, kind, label, read, color,
                 json.dumps(patterns, ensure_ascii=False) if patterns is not None else None,
                 priority, body,
                 json.dumps(nmeta, ensure_ascii=False) if nmeta else None))

        def arm(node_key, arm_key, body):
            conn.execute(
                "INSERT INTO flow_arms (version_id, node_key, arm_key, body, content_hash)"
                " VALUES (?,?,?,?,?)", (vid, node_key, arm_key, body, content_hash(body)))

        def edge(frm, to, ctype="label", cval=None):
            conn.execute(
                "INSERT OR IGNORE INTO flow_edges (version_id, from_node, to_node,"
                " cond_type, cond_value) VALUES (?,?,?,?,?)", (vid, frm, to, ctype, cval))

        for o in doc.get("openers", []):
            key = f"opener-{o['id']}"
            node(key, "opener", label=o.get("label"), body=o.get("text"))
            arm(key, "a", o.get("text") or "")
        for i, b in enumerate(doc.get("branches", [])):
            bid = b["id"]
            nmeta = {"never": b.get("never", [])}
            if b.get("entry_timeout_days"):
                nmeta["entry_timeout_days"] = int(b["entry_timeout_days"])
            node(bid, "branch", label=b.get("label"), read=b.get("read"),
                 color=b.get("color"), patterns=b.get("patterns", []),
                 priority=_V0_PRIORITY.get(bid, 100 + i), nmeta=nmeta)
            # a TIMEOUT-entry branch (e.g. R0 no-reply) is entered by silence after an
            # opener, not by classifying text — wire opener -> branch timeout edges
            # (finding 4: typed conditions, never prose)
            if b.get("entry_timeout_days"):
                for o in doc.get("openers", []):
                    edge(f"opener-{o['id']}", bid, "timeout_days",
                         str(int(b["entry_timeout_days"])))
            mkey = f"{bid}-move"
            node(mkey, "move", label=f"{bid} → next move", body=b.get("next_move"))
            if b.get("next_move"):
                arm(mkey, "a", b["next_move"])
            # templates = the branch's real reply copy (the give library, v4.5+): each
            # template (a bubble array, or a plain string) becomes a selectable arm on the
            # move node, so the finished copy lands on the canvas — not just the next_move
            # guidance prose. Additive: branches with no templates are unchanged (arm 'a'
            # only). Bubbles join with ' · ' (the send-time bubble separator).
            for ti, tmpl in enumerate(b.get("templates", []) or []):
                body = (" · ".join(x for x in tmpl if x)
                        if isinstance(tmpl, list) else str(tmpl or "")).strip()
                if body:
                    arm(mkey, f"t{ti + 1}", body)
            edge(bid, mkey, "outcome", "matched")
            for f in b.get("forward", []):
                on, then = _norm_text(f.get("on")), f.get("then") or ""
                tkey = f"{bid}-fwd-{content_hash(f.get('on') or '')[:6]}"
                # route-to-branch prose ("route to R1") -> a real edge to that branch
                m = re.search(r"\broute(?:s|d)? to (R\d[a-z]?)\b", then, re.I)
                target = m.group(1).upper() if m else tkey
                if target == tkey:
                    node(tkey, "terminal", label=f.get("then"))
                if on == "silence":
                    edge(mkey, target, "timeout_days", "4")
                else:
                    edge(mkey, target, "label", f.get("on"))
    # activate OUTSIDE the import transaction: activate_version opens its own
    # connection and must see the committed draft
    if activate:
        activate_version(vid)
    return vid


def export_flows_json(version_id: int) -> dict:
    """Round-trip a version back to the flows.json interchange shape."""
    g = version_graph(version_id)
    arms_by_node: dict[str, list] = {}
    for a in g["arms"]:
        arms_by_node.setdefault(a["node_key"], []).append(a)
    openers, branches = [], []
    nodes = {n["node_key"]: n for n in g["nodes"]}
    edges_from: dict[str, list] = {}
    for e in g["edges"]:
        edges_from.setdefault(e["from_node"], []).append(e)
    for n in sorted((x for x in g["nodes"] if x["kind"] == "opener"),
                    key=lambda x: x["node_key"]):
        openers.append({"id": n["node_key"].removeprefix("opener-"),
                        "label": n["label"], "text": n["body"]})
    for n in sorted((x for x in g["nodes"] if x["kind"] == "branch"),
                    key=lambda x: x["priority"]):
        mkey = f"{n['node_key']}-move"
        move = nodes.get(mkey)
        fwd = []
        for e in edges_from.get(mkey, []):
            on = (f"silence ({e['cond_value']}d)" if e["cond_type"] == "timeout_days"
                  else e["cond_value"] or "")
            tgt = nodes.get(e["to_node"])
            then = (tgt["label"] if tgt and tgt["kind"] == "terminal"
                    else f"route to {e['to_node']}")
            fwd.append({"on": on, "then": then})
        b = {
            "id": n["node_key"], "label": n["label"], "color": n["color"],
            "patterns": n["patterns"], "read": n["read"],
            "next_move": move["body"] if move else None,
            "never": (n["meta"] or {}).get("never", []),
            "forward": fwd,
        }
        if (n["meta"] or {}).get("entry_timeout_days"):
            b["entry_timeout_days"] = n["meta"]["entry_timeout_days"]
        branches.append(b)
    out = {"version": g["id"], "lineage": g["lineage_uuid"], "name": g["name"],
           "openers": openers, "branches": branches}
    out.update(g["meta"] or {})
    return out


# ---------------------------------------------------------------------------
# Stats — computed from the ledger, never stored on the flow (plan decision 4).
# ---------------------------------------------------------------------------

# F3 paint minimum sample (finding 9 + the variant-A mirage): a rate over fewer
# than this many denominators is NOISE — the paint function must refuse to colour
# it and must always show n, so a lucky 1/1 never reads as a winning branch.
MIN_PAINT_N = 20


def _rate(num: int, den: int) -> dict | None:
    """A conversion rate with its denominator, or None if there's no denominator.
    `paintable` gates COLOUR on the min-sample floor — the number always shows, the
    colour only appears once there's enough data to trust it (finding 9)."""
    if not den:
        return None
    return {"num": num, "den": den, "pct": round(100 * num / den, 1),
            "n": den, "paintable": den >= MIN_PAINT_N}


def _node_funnel(kind: str, ev: dict) -> dict:
    """Per-node conversion funnel from raw event counts. DESCRIPTIVE by construction
    (the population that reached this node is self-selected) — so these rates are
    reported with n but the canvas renders them UNCOLOURED. Move/opener nodes carry
    the send→reply funnel; branch nodes carry match volume only."""
    sent = ev.get("sent", 0)
    out = {"type": "descriptive"}
    if kind in ("move", "opener"):
        out["reply_rate"] = _rate(ev.get("second_exchange", 0), sent)       # send → they replied again
        out["booked_rate"] = _rate(ev.get("booked", 0), sent)               # send → booked call
    if kind == "branch":
        out["matched"] = ev.get("matched", 0)
        out["no_reply"] = ev.get("no_reply", 0)
    return out


def _arm_contrast(arms: dict) -> dict | None:
    """Within-node A/B contrast — the ONE causal comparison (arm assignment is
    randomised + sticky). Returns each arm's reply/booked rate + a `leader` only when
    BOTH contenders clear MIN_PAINT_N (else the canvas shows numbers, no winner).
    This is the only place the paint is allowed to colour a comparison green/red."""
    if len(arms) < 2:
        return None
    rows = {}
    for ak, ev in arms.items():
        sent = ev.get("sent", 0)
        rows[ak] = {"sent": sent,
                    "reply_rate": _rate(ev.get("second_exchange", 0), sent),
                    "booked_rate": _rate(ev.get("booked", 0), sent)}
    # a winner needs every arm above the floor AND a real gap on the reply rate
    rated = {k: v for k, v in rows.items() if v["reply_rate"]}
    all_paintable = rated and all(v["reply_rate"]["paintable"] for v in rated.values())
    leader = None
    if all_paintable and len(rated) >= 2:
        ranked = sorted(rated.items(), key=lambda kv: -kv[1]["reply_rate"]["pct"])
        top, second = ranked[0], ranked[1]
        if top[1]["reply_rate"]["pct"] - second[1]["reply_rate"]["pct"] >= 5.0:
            leader = top[0]
    return {"type": "causal", "arms": rows, "leader": leader,
            "min_n": MIN_PAINT_N, "enough_data": bool(all_paintable)}


def graduation_readiness(contrast: dict | None, ev: dict) -> dict:
    """F4 readiness INDICATOR — pure information, enables NOTHING. Tells Ashley whether a
    branch has earned the right to be *considered* for mechanical-send graduation. It never
    sends and never flips a switch: actual graduation is Ashley-only AND blocked on the
    conversation-state machine (§6b-14), so this is a read-only 'is it ready to discuss'.
      no_data      — nothing sent yet
      gathering    — sends happening, not yet at the sample floor
      no_winner    — enough data, no arm clearly better (keep testing or accept parity)
      has_winner   — a clear winner above the floor: a graduation CANDIDATE for Ashley"""
    sent = ev.get("sent", 0)
    if not sent:
        return {"status": "no_data", "sendable": False}
    if not contrast or len(contrast.get("arms", {})) < 2:
        # single-arm node: readiness is about volume only, never an auto-send verdict
        floor = MIN_PAINT_N
        return {"status": "gathering" if sent < floor else "single_arm_stable",
                "sendable": False, "n": sent}
    if not contrast.get("enough_data"):
        return {"status": "gathering", "sendable": False,
                "n": sent, "need": MIN_PAINT_N}
    if contrast.get("leader"):
        return {"status": "has_winner", "sendable": False,   # sendable stays False by design
                "winner": contrast["leader"], "n": sent,
                "note": "graduation candidate — Ashley-only; auto-send blocked on the "
                        "conversation-state machine (plan §6b-14)"}
    return {"status": "no_winner", "sendable": False, "n": sent}


def stats(lineage_uuid: str | None = None, version_id: int | None = None,
          since: str | None = None) -> dict:
    """Per-node (and per-arm) counters from the stamp ledger. Numbers are TYPED
    (§6b-17): within-node arm splits are 'causal' (randomised assignment); everything
    cross-node is 'descriptive' (self-selected populations) — the F3 paint must render
    them differently. Always returns as_of (finding: mirror staleness honesty)."""
    with db.connect() as conn:
        if lineage_uuid is None and version_id is not None:
            v = conn.execute("SELECT lineage_uuid FROM flow_versions WHERE id=?",
                             (version_id,)).fetchone()
            if not v:
                raise KeyError(f"flow version {version_id} not found")
            lineage_uuid = v["lineage_uuid"]
        where, params = ["1=1"], []
        if lineage_uuid:
            where.append("lineage_uuid=?")
            params.append(lineage_uuid)
        if since:
            where.append("stamped_at>=?")
            params.append(since)
        nodes: dict[str, dict] = {}
        for r in conn.execute(
                f"SELECT node_key, arm_key, event, COUNT(*) n FROM flow_stamps "
                f"WHERE {' AND '.join(where)} GROUP BY node_key, arm_key, event", params):
            nd = nodes.setdefault(r["node_key"], {"events": {}, "arms": {}})
            nd["events"][r["event"]] = nd["events"].get(r["event"], 0) + r["n"]
            if r["arm_key"]:
                nd["arms"].setdefault(r["arm_key"], {})[r["event"]] = r["n"]
        # node kinds (for the funnel shape) — cheap lookup by the resolved version
        kinds = {}
        if version_id is None and lineage_uuid:
            vrow = conn.execute("SELECT id FROM flow_versions WHERE lineage_uuid=? AND "
                                "status='active'", (lineage_uuid,)).fetchone()
            version_id = vrow["id"] if vrow else None
        if version_id:
            kinds = {r["node_key"]: r["kind"] for r in conn.execute(
                "SELECT node_key, kind FROM flow_nodes WHERE version_id=?", (version_id,))}
        ambiguous = conn.execute(
            "SELECT COUNT(*) FROM flow_stamps WHERE detail LIKE 'ambiguous%'"
            + (" AND lineage_uuid=?" if lineage_uuid else ""),
            ([lineage_uuid] if lineage_uuid else [])).fetchone()[0]
    # F3 layer: funnel (descriptive, uncoloured) + arm contrast (causal, colourable).
    total_sent = total_booked = 0
    for key, nd in nodes.items():
        nd["funnel"] = _node_funnel(kinds.get(key, ""), nd["events"])
        nd["contrast"] = _arm_contrast(nd["arms"])
        if kinds.get(key) in ("move", "opener"):
            nd["readiness"] = graduation_readiness(nd["contrast"], nd["events"])
        total_sent += nd["events"].get("sent", 0)
        total_booked += nd["events"].get("booked", 0)
    overall = {"sent": total_sent, "booked": total_booked,
               "booked_rate": _rate(total_booked, total_sent)}
    return {"as_of": _now(), "lineage_uuid": lineage_uuid, "since": since,
            "nodes": nodes, "ambiguous_stamps": ambiguous, "overall": overall,
            "min_paint_n": MIN_PAINT_N,
            "stat_types": {"within_node_arms": "causal (randomised assignment)",
                           "cross_node": "descriptive only (self-selected populations)"}}


# ---------------------------------------------------------------------------
# Send-time identity capture (§6a-11): the moment WE send to a known lead we know
# both sides of the name-join — record the conversation linkage right then.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Suggestion bridge (2026-07-21): read-only give lookup for the DM reply cockpit
# + learning capture on send. The engine SUGGESTS gives for a reply and, when the
# operator sends, records what was suggested vs what was sent (as-is / edited) as a
# stamp — so the give library learns which gives land without ever touching the send
# path itself. The frontend fires the existing POST /send once per bubble.
# ---------------------------------------------------------------------------

def give_version(conn=None) -> int | None:
    """The ACTIVE flow version's id IF it carries any give ('t%') arm, else None.
    Suggestions must classify against — and pull gives from — the SAME version the live
    sensors run on (active_version), never a mid-edit draft or an un-activated re-import
    (finding, 2026-07-21 review: MAX(version_id) surfaced draft copy the operator never
    turned on and diverged from the classify() path). ALWAYS a fresh read; never cached."""
    def _q(cx):
        v = active_version(cx)
        if not v:
            return None
        r = cx.execute("SELECT 1 FROM flow_arms WHERE version_id=? AND arm_key LIKE 't%' "
                       "LIMIT 1", (v["id"],)).fetchone()
        return v["id"] if r else None
    if conn is not None:
        return _q(conn)
    with db.connect() as cx:
        return _q(cx)


def suggest_for_text(reply_text: str | None, conn=None) -> dict:
    """Classify one inbound reply against the give version's branches and return the
    branch's give arms (each split into bubbles). No give version / no branch match ->
    an honest empty payload. Read-only: nothing is stamped here."""
    def _q(cx):
        vid = give_version(cx)
        if not vid:
            return {"version": None, "branch": None, "label": None, "gives": []}
        ordered = _ordered_branches(vid, cx)
        branch = classify_ordered(reply_text, ordered)
        if not branch:
            return {"version": vid, "branch": None, "label": None, "gives": []}
        graph = version_graph(vid, cx)
        label = next((n["label"] for n in graph["nodes"] if n["node_key"] == branch), None)
        move_key = f"{branch}-move"
        gives = []
        for a in graph["arms"]:
            if a["node_key"] == move_key and (a["arm_key"] or "").startswith("t"):
                bubbles = [b.strip() for b in (a["body"] or "").split(" · ") if b.strip()]
                gives.append({"arm_key": a["arm_key"], "arm_hash": content_hash(a["body"]),
                              "bubbles": bubbles})
        return {"version": vid, "branch": branch, "label": label, "gives": gives}
    if conn is not None:
        return _q(conn)
    with db.connect() as cx:
        return _q(cx)


def record_suggested_send(conn, *, thread_urn: str | None = None,
                          canonical_url: str | None = None, lead_id: int | None = None,
                          branch: str,
                          arm_key: str | None = None, arm_hash: str | None = None,
                          version_id: int | None = None, lineage_uuid: str | None = None,
                          suggested_body: str = "", sent_body: str = "") -> bool:
    """Capture what a suggested give turned into on send (the learning half). Records a
    'sent' stamp cohorted suggested_asis vs suggested_edited, with the suggested/sent
    copy in detail.

    2026-07-21 review fixes:
      * attribute to the MOVE node ('{branch}-move') where the give t-arms live, so the
        capture feeds the per-arm contrast in stats() (a bare-branch stamp joined nothing);
      * carry lead_id + canonical_url so the second_exchange/booked sensors (which join on
        lead_id / canonical_url) can attach an outcome to this send;
      * this capture fires ONCE per operator send (not a re-running sensor), so the ev_key
        carries _now() at full precision — a genuine resend of the same give to the same
        person is a distinct row, never silently dropped by INSERT OR IGNORE (finding 3),
        while a re-run of the SAME send never happens here so no double-count is possible."""
    as_is = (suggested_body.strip() != "" and suggested_body.strip() == sent_body.strip())
    cohort = "suggested_asis" if as_is else "suggested_edited"
    detail = json.dumps({"branch": branch, "arm_key": arm_key,
                         "suggested": suggested_body, "sent": sent_body, "as_is": as_is},
                        ensure_ascii=False)
    move_key = f"{branch}-move"
    ev = event_key("sent", canonical_url=(canonical_url or thread_urn or branch),
                   lineage=lineage_uuid,
                   node_key=f"{move_key}:{content_hash(sent_body)[:8]}:{_now()}")
    return stamp(conn, event="sent", node_key=move_key, ev_key=ev,
                 canonical_url=canonical_url, lead_id=lead_id, thread_urn=thread_urn,
                 version_id=version_id, lineage_uuid=lineage_uuid,
                 arm_key=arm_key, arm_hash=arm_hash, cohort=cohort, detail=detail)


def record_improvement(conn, *, thread_urn=None, canonical_url=None, lead_id=None,
                       branch=None, arm_key=None, version_id=None, lineage_uuid=None,
                       text="", context=""):
    """Capture a free-text IMPROVEMENT the operator WROTE ('I don't like these — here's how I'd
    handle this one') as an 'improve' stamp. This is the written-reasoning half of learning,
    distinct from the edit-diff record_suggested_send captures — the self-learning loop reads
    these to propose better gives. Never sends; attributes to the branch's move node."""
    detail = json.dumps({"branch": branch, "arm_key": arm_key, "text": text,
                         "context": (context or "")[:600]}, ensure_ascii=False)
    move_key = f"{branch}-move" if branch else "improve"
    ev = event_key("improve", canonical_url=(canonical_url or thread_urn or branch or "improve"),
                   lineage=lineage_uuid,
                   node_key=f"{move_key}:{content_hash(text or '')[:8]}:{_now()}")
    return stamp(conn, event="improve", node_key=move_key, ev_key=ev,
                 canonical_url=canonical_url, lead_id=lead_id, thread_urn=thread_urn,
                 version_id=version_id, lineage_uuid=lineage_uuid,
                 arm_key=arm_key, cohort="operator_improvement", detail=detail)


# ---------------------------------------------------------------------------
# Review-queue helpers (DM Approvals Queue, 2026-07-22): a batch decision layer over
# the same give library. "Decided" = a sent/killed/skipped stamp NEWER than the conv's
# last inbound (so a fresh inbound re-opens the conversation). record_review_decision
# stamps a KILL/SKIP so the conv leaves the queue — it NEVER sends. Identity join and
# node attribution mirror record_suggested_send exactly (thread_urn / canonical_url /
# lead_id; attribute to the branch's move node where the give arms live).
# ---------------------------------------------------------------------------

# a decision that takes a conversation off the review queue: WE replied (sent), or the
# operator killed / skipped it. The queue re-includes the conv if a NEWER inbound lands.
DECISION_EVENTS = ("sent", "killed", "skipped")


def _to_epoch(ts) -> float | None:
    """Best-effort parse of a timestamp to epoch SECONDS, tolerant of the several clocks
    this codebase mixes: flow_stamps.stamped_at is UTC-aware ISO (_now()), the inbox's
    last_msg_at is a ms-epoch numeric string (LinkedIn's own UTC clock), and a message
    created_at is a naive '%Y-%m-%d %H:%M:%S' / ISO string. Returns None if unparseable."""
    if ts is None:
        return None
    s = str(ts).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):          # numeric epoch (ms if large, else seconds)
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    t = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)           # ISO, with or without tz; also 'YYYY-MM-DD HH:MM:SS'
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def latest_decision_epoch(mc, *, thread_urn: str | None = None,
                          canonical_url: str | None = None,
                          lead_id: int | None = None) -> float | None:
    """Epoch-seconds of the NEWEST decision stamp (sent/killed/skipped) matching this
    conversation identity, or None if never decided. The identity match is ANY of
    thread_urn / canonical_url / lead_id — the exact join record_suggested_send writes on
    (a give may be stamped by URL while a kill is stamped by URN, so we OR them)."""
    clauses, params = [], []
    if thread_urn:
        clauses.append("thread_urn=?"); params.append(thread_urn)
    if canonical_url:
        clauses.append("canonical_url=?"); params.append(canonical_url)
    if lead_id is not None:
        clauses.append("lead_id=?"); params.append(lead_id)
    if not clauses:
        return None
    q = ("SELECT stamped_at FROM flow_stamps WHERE event IN (%s) AND (%s)" %
         (",".join("?" * len(DECISION_EVENTS)), " OR ".join(clauses)))
    best = None
    for r in mc.execute(q, list(DECISION_EVENTS) + params):
        e = _to_epoch(r["stamped_at"])
        if e is not None and (best is None or e > best):
            best = e
    return best


def is_decided(mc, *, last_inbound, thread_urn: str | None = None,
               canonical_url: str | None = None, lead_id: int | None = None) -> bool:
    """True if this conversation has already been decided since its last inbound — i.e. a
    sent/killed/skipped stamp exists that is NEWER than the last inbound. A fresh inbound
    landing AFTER a past decision returns False (the conv re-enters the queue). A decision
    with no comparable inbound clock is treated as decided (fail-safe: don't re-surface)."""
    dec = latest_decision_epoch(mc, thread_urn=thread_urn, canonical_url=canonical_url,
                                lead_id=lead_id)
    if dec is None:
        return False
    li = _to_epoch(last_inbound)
    if li is None:
        return True
    return dec > li


def record_review_decision(conn, *, decision: str, thread_urn: str | None = None,
                           canonical_url: str | None = None, lead_id: int | None = None,
                           branch: str | None = None, arm_key: str | None = None,
                           version_id: int | None = None, lineage_uuid: str | None = None,
                           reason: str | None = None) -> bool:
    """Stamp a review-queue KILL or SKIP so the conversation leaves the queue. NEVER sends.
      decision 'kill' -> event 'killed'  (strong negative signal — never banked as a give);
      decision 'skip' -> event 'skipped' (neutral defer).
    Attribution mirrors record_suggested_send: the branch's MOVE node ('{branch}-move')
    when the branch is known — so a kill sits beside the give arms it rejects and feeds the
    per-branch tally — else a synthetic 'review' node for an off-map reply. The ev_key
    carries full-precision _now() so a genuine repeat decision is a distinct honest row,
    never dropped by INSERT OR IGNORE (mirrors finding 3)."""
    d = (decision or "").strip().lower()
    if d not in ("kill", "skip"):
        raise ValueError(f"decision must be 'kill' or 'skip', got {decision!r}")
    event = "killed" if d == "kill" else "skipped"
    node_key = f"{branch}-move" if branch else "review"
    detail = json.dumps({"decision": d, "branch": branch, "arm_key": arm_key,
                         "reason": reason or ""}, ensure_ascii=False)
    ev = event_key(event, canonical_url=(canonical_url or thread_urn or branch or "review"),
                   lineage=lineage_uuid, node_key=f"{node_key}:{_now()}")
    return stamp(conn, event=event, node_key=node_key, ev_key=ev,
                 canonical_url=canonical_url, lead_id=lead_id, thread_urn=thread_urn,
                 version_id=version_id, lineage_uuid=lineage_uuid,
                 arm_key=arm_key, cohort=event, detail=detail)


def reactivation_decided(mc, *, lead_id: int | None = None,
                         canonical_url: str | None = None,
                         thread_urn: str | None = None) -> bool:
    """True if a KILL or SKIP has already been recorded for this reactivation candidate.
    A reactivation candidate has no inbound to compare against (it never replied), so a
    kill/skip is PERMANENT — the decision not to nudge stands until a fresh inbound moves
    the person into the reply lane. Identity match is ANY of lead_id / canonical_url /
    thread_urn, mirroring latest_decision_epoch's OR-join."""
    clauses, params = [], []
    if thread_urn:
        clauses.append("thread_urn=?"); params.append(thread_urn)
    if canonical_url:
        clauses.append("canonical_url=?"); params.append(canonical_url)
    if lead_id is not None:
        clauses.append("lead_id=?"); params.append(lead_id)
    if not clauses:
        return False
    q = ("SELECT 1 FROM flow_stamps WHERE event IN ('killed','skipped') AND (%s) LIMIT 1"
         % " OR ".join(clauses))
    return mc.execute(q, params).fetchone() is not None


def link_conversation_to_lead(lead: dict) -> None:
    """Best-effort: set conversations.lead_id for this lead's thread by canonical URL.
    Never raises — the send path must not gain a failure mode from CRM bookkeeping."""
    try:
        cu = canon_in(lead.get("profile_url"))
        if not cu:
            return
        slug = cu.rsplit("/in/", 1)[-1]
        if not slug:
            return
        from .inbox import db as cvdb
        cx = cvdb.connect()
        try:
            # match on the /in/<slug> identity segment; verify via canon before writing
            rows = cx.execute(
                "SELECT id, participant_profile_url FROM conversations WHERE lead_id IS NULL "
                "AND participant_profile_url LIKE ?", (f"%/in/{slug}%",)).fetchall()
            for r in rows:
                if canon_in(r["participant_profile_url"]) == cu:
                    cx.execute("UPDATE conversations SET lead_id=? WHERE id=?",
                               (lead["id"], r["id"]))
            cx.commit()
        finally:
            cx.close()
    except Exception:  # noqa: BLE001 — bookkeeping only, never blocks a send
        pass
