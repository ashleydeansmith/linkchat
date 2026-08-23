"""flows_sensors.py — ConversationForge F1 outcome sensors + the lead_id backfill.

Nothing can LEARN until outcomes are measured (plan: sensors are the hard prerequisite
for F3's numbers-on-canvas). This lane is READ-heavy and browser-free:

  1. BACKFILL conversations.lead_id (the long-owed name-join debt, roadmap C3):
     canonical /in/ URL first, name+headline heuristic second, unmatched left NULL and
     REPORTED. Never overwrites a non-NULL id; name collisions are excluded-and-counted,
     never guessed (§6b-11 — two "John Smith"s must not merge stats).
  2. RECONCILE history: outbound rows in `messages` matched against the active version's
     arm content-lineage become 'sent' stamps with cohort='backfill' (finding 8 — the
     dominant path, Ashley sending by hand, must not read as "sent ≈ 0"). Idempotent:
     same natural key a live send would use.
  3. MATCHED: classify each joined conversation's latest inbound text against the active
     version and stamp the branch (full thread body when the mirror holds it, else the
     preview — the source is recorded; threads needing full bodies land in a deep-sync
     request queue the inbox sync lane consumes, §6c-3. This sensor never opens a browser).
  4. SECOND EXCHANGE: their inbound AFTER our stamped send (full-thread seq proof when
     available, else the preview/direction heuristic — flagged as such).
  5. BOOKED: reads the meetings-feed JSONL (FILE CONTRACT — meeting-triage writes it;
     LinkForge never touches Fathom, §5.4). ref=lead_id joins exactly; name-join is the
     fallback-with-flag; ambiguous names are counted, never guessed.

Run:  python -m linkforge flows-sensors
Output: data/metrics/flows-sensors.json + RESULT line (house lane convention).
"""
from __future__ import annotations

import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import DATA_DIR, db
from . import flows_engine as fe
from .canon import canon_in
from .config import Config
from .inbox import db as cvdb


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_name(s: str | None) -> str:
    import re, unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s.lower())).strip()


def _ms_to_iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 1. conversations.lead_id backfill
# ---------------------------------------------------------------------------

def backfill_conversation_leads() -> dict:
    """Fill conversations.lead_id where NULL. URL join first, name+headline second.
    Returns honest counts; never overwrites, never guesses a collision."""
    with db.connect() as conn:
        leads = [dict(r) for r in conn.execute(
            "SELECT id, profile_url, full_name, headline FROM leads")]
    by_canon: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for l in leads:
        cu = canon_in(l["profile_url"])
        if cu:
            by_canon.setdefault(cu, []).append(l)
        n = _norm_name(l["full_name"])
        if n:
            by_name.setdefault(n, []).append(l)

    out = {"url_joined": 0, "name_joined": 0, "name_collisions": 0,
           "headline_mismatch": 0, "unmatched": 0, "already_linked": 0}
    cx = cvdb.connect()
    try:
        rows = cx.execute("SELECT id, lead_id, participant_name, participant_headline, "
                          "participant_profile_url FROM conversations").fetchall()
        for r in rows:
            if r["lead_id"] is not None:
                out["already_linked"] += 1
                continue
            hit = None
            cu = canon_in(r["participant_profile_url"])
            if cu and cu in by_canon and len(by_canon[cu]) == 1:
                hit, how = by_canon[cu][0], "url_joined"
            else:
                n = _norm_name(r["participant_name"])
                cands = by_name.get(n, [])
                if len(cands) > 1:
                    out["name_collisions"] += 1
                    continue
                if len(cands) == 1:
                    lh, ch = _norm_name(cands[0]["headline"]), _norm_name(r["participant_headline"])
                    # headline check only when BOTH sides carry one; prefix-compatible = same person
                    if lh and ch and not (lh[:24] == ch[:24] or lh in ch or ch in lh):
                        out["headline_mismatch"] += 1
                        continue
                    hit, how = cands[0], "name_joined"
            if hit is None:
                out["unmatched"] += 1
                continue
            cx.execute("UPDATE conversations SET lead_id=? WHERE id=? AND lead_id IS NULL",
                       (hit["id"], r["id"]))
            out[how] += 1
        cx.commit()
    finally:
        cx.close()
    return out


# ---------------------------------------------------------------------------
# 2-4. stamp sensors (matched / sent-backfill / second exchange)
# ---------------------------------------------------------------------------

def _thread_index(cx) -> dict[int, dict]:
    """conversations keyed by lead_id (linked rows only) with light fields."""
    out = {}
    for r in cx.execute("SELECT id, lead_id, thread_urn, participant_profile_url, "
                        "last_preview, last_msg_dir, last_msg_at, last_synced_at "
                        "FROM conversations WHERE lead_id IS NOT NULL"):
        out[r["lead_id"]] = dict(r)
    return out


def _full_msgs(cx, conv_id: int) -> list[dict]:
    return [dict(m) for m in cx.execute(
        "SELECT direction, body, seq FROM conversation_messages "
        "WHERE conversation_id=? ORDER BY seq", (conv_id,))]


def run_stamp_sensors(account_id: str = "default") -> dict:
    """One pass over joined conversations: reconcile sent history, classify replies,
    detect second exchanges. All stamps idempotent; re-runs are free."""
    rep = {"sent_backfilled": 0, "matched": 0, "second_exchange": 0,
           "deep_sync_needed": 0, "no_active_version": False}
    with db.connect() as conn:
        v = fe.active_version(conn)
    if not v:
        rep["no_active_version"] = True
        return rep
    lineage, vid = v["lineage_uuid"], v["id"]
    deepsync: list[dict] = []

    cx = cvdb.connect()
    try:
        threads = _thread_index(cx)
        with db.connect() as conn:
            ordered = fe._ordered_branches(vid, conn)
            # ALL leads, not just thread-linked ones: the sent-history reconcile must
            # stamp sends whose conversation was never mirrored/joined too.
            leads = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, profile_url, full_name, campaign_id FROM leads")}

            # --- 2. sent-history reconcile (cohort='backfill') -------------------
            # A logical SEND is all bubbles of one (lead, step): messages stores each
            # typed bubble as its own row, so reconcile the JOINED text and stamp ONCE
            # (keyed to the first row id) — per-bubble stamps would count one send 3x.
            sends: dict[tuple, dict] = {}
            for m in conn.execute("SELECT id, lead_id, step_index, body, sent_at FROM "
                                  "messages WHERE status='sent' ORDER BY id"):
                k = (m["lead_id"], m["step_index"])
                s = sends.setdefault(k, {"first_id": m["id"], "bodies": [],
                                         "sent_at": m["sent_at"]})
                s["bodies"].append(m["body"] or "")
            for (lead_id, _step), s in sends.items():
                lead = leads.get(lead_id)
                if not lead:
                    continue
                hit = fe.match_arm_by_body(" · ".join(s["bodies"]), v, conn)
                if not hit:
                    continue
                if fe.stamp(conn, event="sent", node_key=hit["node_key"],
                            ev_key=fe.event_key("sent", message_id=s["first_id"]),
                            canonical_url=canon_in(lead["profile_url"]),
                            lead_id=lead_id, version_id=vid, lineage_uuid=lineage,
                            arm_key=hit["arm_key"], arm_hash=hit["content_hash"],
                            cohort="backfill", account_id=account_id,
                            stamped_at=s["sent_at"], detail="reconciled-history"):
                    rep["sent_backfilled"] += 1

            # --- 3+4. per-thread: matched + second exchange ----------------------
            for lead_id, th in threads.items():
                lead = leads.get(lead_id)
                if not lead:
                    continue
                cu = canon_in(lead["profile_url"]) or canon_in(th["participant_profile_url"])
                msgs = _full_msgs(cx, th["id"])
                have_full = bool(msgs)
                # latest inbound text: full thread beats the lossy preview (§6c-3)
                inbound_texts = [m["body"] for m in msgs if m["direction"] == "in" and m["body"]]
                if inbound_texts:
                    reply_text, src = inbound_texts[-1], "full-thread"
                elif th["last_msg_dir"] == "in" and (th["last_preview"] or "").strip():
                    reply_text, src = th["last_preview"], "preview"
                    deepsync.append({"conversation_id": th["id"], "thread_urn": th["thread_urn"],
                                     "why": "classified from preview — need full bodies"})
                else:
                    reply_text, src = None, None

                if reply_text:
                    node = fe.classify_ordered(reply_text, ordered)
                    # cohort honesty (§6b-17): a reply that predates this flow's
                    # activation is historical context, not a fresh-cohort observation
                    reply_at = _ms_to_iso(th["last_msg_at"])
                    cohort = ("backfill" if reply_at and v.get("activated_at")
                              and reply_at < v["activated_at"] else "fresh")
                    if node and fe.stamp(
                            conn, event="matched", node_key=node,
                            ev_key=fe.event_key("matched", canonical_url=cu,
                                                lineage=lineage, node_key=node),
                            canonical_url=cu, lead_id=lead_id, thread_urn=th["thread_urn"],
                            version_id=vid, lineage_uuid=lineage, cohort=cohort,
                            account_id=account_id, detail=f"src={src}"):
                        rep["matched"] += 1

                # second exchange: their inbound AFTER our stamped send
                sent = conn.execute(
                    "SELECT node_key, arm_key, arm_hash, cohort, MAX(stamped_at) at "
                    "FROM flow_stamps WHERE event='sent' AND lineage_uuid=? AND "
                    "(lead_id=? OR canonical_url=?)", (lineage, lead_id, cu)).fetchone()
                if not sent or not sent["at"]:
                    continue
                proved, proof = False, None
                if have_full:
                    # anchor on our LATEST outbound: "their inbound after our stamped
                    # send", not merely "they ever spoke after we first did"
                    out_seqs = [m["seq"] for m in msgs if m["direction"] == "out"]
                    in_seqs = [m["seq"] for m in msgs if m["direction"] == "in"]
                    if out_seqs and in_seqs and max(in_seqs) > max(out_seqs):
                        proved, proof = True, "full-thread"
                else:
                    last_at = _ms_to_iso(th["last_msg_at"])
                    if th["last_msg_dir"] == "in" and last_at and last_at > sent["at"]:
                        proved, proof = True, "preview-heuristic"
                        deepsync.append({"conversation_id": th["id"],
                                         "thread_urn": th["thread_urn"],
                                         "why": "second-exchange from heuristic — need full bodies"})
                if proved and fe.stamp(
                        conn, event="second_exchange", node_key=sent["node_key"],
                        ev_key=fe.event_key("second_exchange", canonical_url=cu,
                                            lineage=lineage, node_key=sent["node_key"]),
                        canonical_url=cu, lead_id=lead_id, thread_urn=th["thread_urn"],
                        version_id=vid, lineage_uuid=lineage, arm_key=sent["arm_key"],
                        arm_hash=sent["arm_hash"], cohort=sent["cohort"] or "fresh",
                        account_id=account_id, detail=f"proof={proof}"):
                    rep["second_exchange"] += 1
    finally:
        cx.close()

    q = DATA_DIR / "metrics" / "flows-deepsync-queue.json"
    q.parent.mkdir(parents=True, exist_ok=True)
    seen, dedup = set(), []
    for d in deepsync:
        if d["conversation_id"] not in seen:
            seen.add(d["conversation_id"])
            dedup.append(d)
    q.write_text(json.dumps({"generated": _now(), "entries": dedup},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    rep["deep_sync_needed"] = len(dedup)
    return rep


# ---------------------------------------------------------------------------
# 5. booked-call sensor (file contract + manual marks)
# ---------------------------------------------------------------------------

def run_booked_sensor(account_id: str = "default") -> dict:
    """meetings-feed.jsonl -> 'booked' stamps. ref=lead_id is exact (attribution at
    source, §6b-26 — scheduling links carry ?ref=<lead_id>); name-join is the
    fallback-with-flag; ambiguous = counted, never guessed."""
    cfg = Config.load()
    feed = Path(getattr(cfg, "meetings_feed_path", "") or (DATA_DIR / "meetings-feed.jsonl"))
    rep = {"feed": str(feed), "feed_present": feed.exists(), "booked": 0,
           "ref_joined": 0, "name_joined": 0, "ambiguous": 0, "unmatched": 0,
           "no_active_version": False}
    if not feed.exists():
        return rep
    with db.connect() as conn:
        v = fe.active_version(conn)
        if not v:
            rep["no_active_version"] = True
            return rep
        lineage, vid = v["lineage_uuid"], v["id"]
        by_name: dict[str, list[dict]] = {}
        for r in conn.execute("SELECT id, profile_url, full_name FROM leads"):
            n = _norm_name(r["full_name"])
            if n:
                by_name.setdefault(n, []).append(dict(r))
        for line in feed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            lead, how = None, None
            if m.get("ref"):
                row = conn.execute("SELECT id, profile_url, full_name FROM leads WHERE id=?",
                                   (m["ref"],)).fetchone()
                if row:
                    lead, how = dict(row), "ref_joined"
            if lead is None:
                cands = by_name.get(_norm_name(m.get("name")), [])
                if len(cands) > 1:
                    rep["ambiguous"] += 1
                    continue
                if len(cands) == 1:
                    lead, how = cands[0], "name_joined"
            if lead is None:
                rep["unmatched"] += 1
                continue
            cu = canon_in(lead["profile_url"])
            # credit the most recent 'sent' stamp's node/arm (descriptive attribution)
            sent = conn.execute(
                "SELECT node_key, arm_key, arm_hash, cohort FROM flow_stamps WHERE "
                "event='sent' AND lineage_uuid=? AND (lead_id=? OR canonical_url=?) "
                "ORDER BY stamped_at DESC LIMIT 1", (lineage, lead["id"], cu)).fetchone()
            node = sent["node_key"] if sent else "unattributed"
            if fe.stamp(conn, event="booked", node_key=node,
                        ev_key=fe.event_key("booked", canonical_url=cu or m.get("name"),
                                            extra=str(m.get("at") or "")),
                        canonical_url=cu, lead_id=lead["id"], version_id=vid,
                        lineage_uuid=lineage,
                        arm_key=sent["arm_key"] if sent else None,
                        arm_hash=sent["arm_hash"] if sent else None,
                        cohort=(sent["cohort"] if sent else "fresh") or "fresh",
                        account_id=account_id,
                        detail=f"join={how}; source={m.get('source', '')}"):
                rep["booked"] += 1
                rep[how] += 1
    return rep


def mark_booked(lead_id: int, at: str | None = None, account_id: str = "default") -> bool:
    """Manual fallback (plan §5.4): the UI's mark-booked button keeps the metric honest
    until the meetings feed ships. Same idempotent ledger path."""
    with db.connect() as conn:
        v = fe.active_version(conn)
        row = conn.execute("SELECT id, profile_url FROM leads WHERE id=?",
                           (lead_id,)).fetchone()
        if not row:
            raise KeyError(f"lead {lead_id} not found")
        cu = canon_in(row["profile_url"])
        lineage = v["lineage_uuid"] if v else None
        sent = conn.execute(
            "SELECT node_key, arm_key, arm_hash, cohort FROM flow_stamps WHERE "
            "event='sent' AND (lead_id=? OR canonical_url=?) "
            "ORDER BY stamped_at DESC LIMIT 1", (lead_id, cu)).fetchone()
        return fe.stamp(
            conn, event="booked", node_key=sent["node_key"] if sent else "unattributed",
            ev_key=fe.event_key("booked", canonical_url=cu or str(lead_id),
                                extra=at or "manual"),
            canonical_url=cu, lead_id=lead_id,
            version_id=v["id"] if v else None, lineage_uuid=lineage,
            arm_key=sent["arm_key"] if sent else None,
            arm_hash=sent["arm_hash"] if sent else None,
            cohort=(sent["cohort"] if sent else "fresh") or "fresh",
            account_id=account_id, stamped_at=at, detail="join=manual")


# ---------------------------------------------------------------------------
# no-reply sensor + the re-activate queue (Ashley 2026-07-15: "a tab that's a
# no reply and a re-activate"). The silent majority becomes a first-class lane:
# a TIMEOUT-entry branch (meta.entry_timeout_days, e.g. R0) is entered by N days
# of silence after an opener send — sensed here, never classified from text.
# ---------------------------------------------------------------------------

def _timeout_branches(conn, version_id: int) -> list[dict]:
    out = []
    for r in conn.execute("SELECT node_key, meta FROM flow_nodes WHERE version_id=? "
                          "AND kind='branch'", (version_id,)):
        meta = json.loads(r["meta"]) if r["meta"] else {}
        if meta.get("entry_timeout_days"):
            out.append({"node_key": r["node_key"],
                        "days": int(meta["entry_timeout_days"])})
    return out


def _last_inbound_iso(cx, th: dict) -> str | None:
    """Best available 'their last inbound' time for a thread (ms-epoch preview field)."""
    if th and th.get("last_msg_dir") == "in":
        return _ms_to_iso(th.get("last_msg_at"))
    return None


def run_no_reply_sensor(account_id: str = "default") -> dict:
    """Stamp event='no_reply' on the timeout-entry branch for every opener send that
    is still unanswered after the branch's window. Idempotent; a later reply does not
    erase the stamp (they WERE silent past the window — 'reactivated' is real signal)."""
    rep = {"no_reply": 0, "checked": 0, "no_timeout_branch": False,
           "no_active_version": False}
    with db.connect() as conn:
        v = fe.active_version(conn)
        if not v:
            rep["no_active_version"] = True
            return rep
        tbs = _timeout_branches(conn, v["id"])
        if not tbs:
            rep["no_timeout_branch"] = True
            return rep
        tb = tbs[0]                       # one silent-lane per flow for now
        lineage = v["lineage_uuid"]
        now = datetime.now(timezone.utc)
        cx = cvdb.connect()
        try:
            threads = _thread_index(cx)
            leads = {r["id"]: dict(r) for r in conn.execute(
                "SELECT id, profile_url, full_name, status FROM leads")}
            opener_sends = conn.execute(
                "SELECT lead_id, canonical_url, node_key, arm_key, arm_hash, cohort, "
                "MIN(stamped_at) at FROM flow_stamps WHERE event='sent' AND "
                "lineage_uuid=? AND node_key LIKE 'opener-%' GROUP BY lead_id",
                (lineage,)).fetchall()
            for s in opener_sends:
                rep["checked"] += 1
                lead = leads.get(s["lead_id"])
                if not lead or lead["status"] in ("replied", "done", "skipped"):
                    continue
                try:
                    sent_at = datetime.fromisoformat(s["at"])
                    if sent_at.tzinfo is None:
                        sent_at = sent_at.replace(tzinfo=timezone.utc)
                except Exception:  # noqa: BLE001
                    continue
                if (now - sent_at).days < tb["days"]:
                    continue                       # window not elapsed yet
                th = threads.get(s["lead_id"])
                inbound = _last_inbound_iso(cx, th)
                if inbound and inbound > s["at"]:
                    continue                       # they replied — not silent
                if th and any(m["direction"] == "in" for m in _full_msgs(cx, th["id"])):
                    continue                       # full thread shows an inbound
                cu = s["canonical_url"] or canon_in(lead["profile_url"])
                if fe.stamp(conn, event="no_reply", node_key=tb["node_key"],
                            ev_key=fe.event_key("no_reply", canonical_url=cu,
                                                lineage=lineage, node_key=tb["node_key"]),
                            canonical_url=cu, lead_id=s["lead_id"],
                            version_id=v["id"], lineage_uuid=lineage,
                            arm_key=s["arm_key"], arm_hash=s["arm_hash"],
                            cohort=s["cohort"] or "fresh", account_id=account_id,
                            detail=f"opener={s['node_key']}; window={tb['days']}d"):
                    rep["no_reply"] += 1
        finally:
            cx.close()
    return rep


# The two jobs the personalisation used to do, kept here so LinkChat carries no
# module it cannot see. {first_name} and its three siblings are filled from what
# is known; a choice written as {a|b|c} picks one; anything still in braces after
# that is a word nobody taught the program, and it is removed rather than typed at
# a person.
_KNOWN_VARS = ("first_name", "company", "title", "location")
_SPINTAX = re.compile(r"\{([^{}|]*\|[^{}]*)\}")


def _fill_and_choose(text: str, fields: dict) -> str:
    """Fill what is known, choose between alternatives, drop what is left."""
    out = str(text or "")
    for key in _KNOWN_VARS:
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, str(fields.get(key) or "").strip())
    while True:
        match = _SPINTAX.search(out)
        if not match:
            break
        out = out[:match.start()] + random.choice(match.group(1).split("|")) + out[match.end():]
    out = _LEFTOVER.sub("", out)
    # Tidy the hole a missing word leaves behind, so a gap reads as a shorter
    # sentence rather than as " , looks solid." with a comma hanging off nothing.
    out = re.sub(r"\s{2,}", " ", out)
    out = out.replace(" ,", ",").replace(" .", ".").replace(" !", "!").replace(" ?", "?")
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"(a|an|the|at|for|of)\s*([,.!?]|$)", r"", out, flags=re.I)
    return out.strip().strip(",").strip()


# A placeholder that survived filling is personalisation that did not resolve.
# Showing "Hi {first_name}" to somebody is the single most obvious way to say a
# machine wrote this, so it is found rather than sent, everywhere it can appear.
_LEFTOVER = re.compile(r"\{[^{}]*\}|\[[^\[\]]*\]")


def unresolved(text: str) -> list:
    """Every placeholder still in a finished message. Empty means it is safe."""
    return _LEFTOVER.findall(text or "")


def _resolve_reactivation_bubbles(move_shape: str | None, full_name: str | None) -> list[str]:
    """Resolve the reactivation move template into the ACTUAL bubbles that would land for
    this person: fill {name}/{first_name} from the lead, resolve any spintax + the known
    {company}/{title}/{location} vars, then split on the send-time bubble separator (' · ').
    The cockpit shows REAL text, never an opener code (the V3.1 refinement)."""
    from . import names
    fn = names.first_name_of(full_name) or "there"
    body = (move_shape or "").replace("{name}", fn)
    # Fill the words we have, then blank the ones we do not, so a gap in the record
    # arrives as a shorter sentence rather than as the word "{company}" in a message.
    body = _fill_and_choose(body, {"first_name": fn})
    return [b.strip() for b in body.split(" · ") if b.strip()]


def build_reactivate_queue(limit: int = 100) -> dict:
    """The re-activate candidates: no_reply-stamped leads who are STILL silent and have
    not had their one nudge. STAGING ONLY — drafting runs the DM SOP chain (dm-conversation
    → James → Ashley taps); nothing here sends. One nudge per person, ever (the flow's
    'never' rule) — anyone with a send after their no_reply stamp is excluded for good."""
    out = {"as_of": _now(), "entries": [], "excluded_replied": 0, "excluded_nudged": 0}
    db.sync_red_list_from_json()   # Layer A: red-listed people never enter the queue
    with db.connect() as conn:
        v = fe.active_version(conn)
        if not v:
            out["no_active_version"] = True
            return out
        tbs = _timeout_branches(conn, v["id"])
        if not tbs:
            out["no_timeout_branch"] = True
            return out
        tb = tbs[0]
        # Pull the real SENDABLE template (t1, t2, ...), NEVER the imported instruction/
        # next_move prose which lands as arm 'a'. LIKE 't%' selects the actual message
        # bubbles; without this the cockpit showed the dev instruction, not a draft.
        move_arm = conn.execute(
            "SELECT body FROM flow_arms WHERE version_id=? AND node_key=? AND enabled=1 "
            "ORDER BY (arm_key LIKE 't%') DESC, arm_key ASC LIMIT 1",
            (v["id"], f"{tb['node_key']}-move")).fetchone()
        out["move_shape"] = move_arm["body"] if move_arm else None
        out["branch"] = tb["node_key"]            # the timeout branch (e.g. R0) these fold under
        cx = cvdb.connect()
        try:
            threads = _thread_index(cx)
            now = datetime.now(timezone.utc)
            rows = conn.execute(
                "SELECT ns.lead_id, ns.canonical_url, ns.stamped_at, ns.detail, "
                "l.full_name, l.profile_url, l.status, "
                "(SELECT MIN(stamped_at) FROM flow_stamps s2 WHERE s2.event='sent' AND "
                " s2.lead_id=ns.lead_id) first_sent "
                "FROM flow_stamps ns JOIN leads l ON l.id=ns.lead_id "
                "WHERE ns.event='no_reply' AND ns.lineage_uuid=? AND ns.node_key=? "
                # Layer A: a red-listed lead never enters the re-activate queue (matched by
                # lead_id, either URL namespace on the lead, or the stamp's canonical_url).
                "AND NOT EXISTS (SELECT 1 FROM red_list r WHERE r.lead_id = l.id "
                "OR r.canon_url = l.profile_url OR r.member_urn = l.profile_url "
                "OR r.canon_url = ns.canonical_url OR r.member_urn = ns.canonical_url) "
                "ORDER BY ns.stamped_at", (v["lineage_uuid"], tb["node_key"])).fetchall()
            for r in rows:
                if r["status"] in ("replied", "done", "skipped"):
                    out["excluded_replied"] += 1
                    continue
                th = threads.get(r["lead_id"])
                inbound = _last_inbound_iso(cx, th)
                if inbound and inbound > (r["first_sent"] or ""):
                    out["excluded_replied"] += 1
                    continue
                # 'nudged' = ANY send after the opener (the send is the fact; the
                # no_reply stamp is only bookkeeping) — a lead who already got a
                # second touch of any kind never re-enters the queue
                nudged = conn.execute(
                    "SELECT 1 FROM messages WHERE lead_id=? AND status='sent' AND "
                    "sent_at > ? LIMIT 1", (r["lead_id"], r["first_sent"] or "")).fetchone()
                if nudged:
                    out["excluded_nudged"] += 1
                    continue
                days = None
                try:
                    fs = datetime.fromisoformat(r["first_sent"])
                    if fs.tzinfo is None:
                        fs = fs.replace(tzinfo=timezone.utc)
                    days = (now - fs).days
                except Exception:  # noqa: BLE001
                    pass
                canonical_url = r["canonical_url"] or canon_in(r["profile_url"])
                out["entries"].append({
                    "lead_id": r["lead_id"], "name": r["full_name"],
                    "profile_url": r["profile_url"], "canonical_url": canonical_url,
                    "opener": (r["detail"] or "").split(";")[0].removeprefix("opener="),
                    "opener_sent_at": r["first_sent"], "days_silent": days,
                    # the ACTUAL resolved reactivation message for THIS person (real text,
                    # never the opener code) — what the cockpit shows and sends.
                    "bubbles": _resolve_reactivation_bubbles(out.get("move_shape"), r["full_name"]),
                })
                if len(out["entries"]) >= limit:
                    break
        finally:
            cx.close()
    return out


# ---------------------------------------------------------------------------
# lane entry
# ---------------------------------------------------------------------------

def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = Config.load()
    account = getattr(cfg, "flows_account_id", "default") or "default"
    report = {"generated": _now(), "account_id": account,
              "backfill": backfill_conversation_leads()}
    report["sensors"] = run_stamp_sensors(account)
    report["booked"] = run_booked_sensor(account)
    report["no_reply"] = run_no_reply_sensor(account)
    rq = build_reactivate_queue()
    report["reactivate_queue"] = {"entries": len(rq.get("entries", [])),
                                  "excluded_replied": rq.get("excluded_replied", 0),
                                  "excluded_nudged": rq.get("excluded_nudged", 0)}
    q = DATA_DIR / "metrics" / "flows-reactivate-queue.json"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(json.dumps(rq, ensure_ascii=False, indent=1), encoding="utf-8")
    out = DATA_DIR / "metrics" / "flows-sensors.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    b, s = report["backfill"], report["sensors"]
    print(f"[flows-sensors] lead_id backfill: +{b['url_joined']} url, +{b['name_joined']} name, "
          f"{b['name_collisions']} collisions excluded, {b['unmatched']} unmatched | "
          f"stamps: {s['sent_backfilled']} sent(backfill), {s['matched']} matched, "
          f"{s['second_exchange']} second-exchange | booked: {report['booked']['booked']} "
          f"| no-reply: {report['no_reply'].get('no_reply', 0)} "
          f"| re-activate queue: {report['reactivate_queue']['entries']} "
          f"| deep-sync queue: {s['deep_sync_needed']}")
    ok = not s.get("no_active_version")
    print("RESULT " + json.dumps({"lane": "flows-sensors", "ok": ok, **{
        "backfill": b, "stamps": s, "booked": report["booked"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
