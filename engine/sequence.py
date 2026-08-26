"""sequence.py — the per-lead message SEQUENCE engine (WP5).

This is the lane-to-journey upgrade. Instead of firing one message lane at a
population, each accepted lead walks its OWN timeline: message 1 right after they
connect, message 2 N days later, and so on, halting the instant they reply.

Source of truth for the steps = the campaign's `message` component (so the
builder stays the one place you design a campaign). A message component may carry
a `steps` list — [{template, wait_days}, ...]; a component without `steps` is
treated as a single step from its {template, wait_days}.

Per-lead cursor = the dormant `sequence_state` table:
    lead_id · sequence_id(=campaign_id, pragmatic reuse) · current_step ·
    next_due_at · status(active|stopped_reply|completed) · last_sent_at

Lifecycle:
  • accept_sync marks a lead 'accepted'  ->  enrol_on_accept() creates a
    sequence_state row at step 0, due at accepted_at + steps[0].wait_days.
  • the scheduler daemon ticks: due rows whose lead is still connected get the
    current step sent (live) or simulated (rehearsal), then advance to the next
    step's due time, or complete after the last step.
  • a reply (detected at send time by drip.send_to_lead) halts the sequence.

Rehearsal vs Live: in Rehearsal the tick SIMULATES — it advances the cursor and
records a clearly-marked rehearsed message WITHOUT touching LinkedIn, so a whole
multi-step journey can be watched end-to-end in minutes (set wait_days=0). In Live
it sends for real, gated by safety.can_act('message').
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from . import db, emit_result, safe_close


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Steps come from the campaign's message component
# ---------------------------------------------------------------------------

def campaign_steps(campaign_id: int, variant: str | None = None) -> list[dict]:
    """Steps for a campaign's message sequence — variant-aware (A/B). Default: variant 'A'."""
    return db.campaign_message_steps(campaign_id, variant)


def _lead_campaign(lead_id: int) -> int | None:
    with db.connect() as conn:
        r = conn.execute("SELECT campaign_id FROM leads WHERE id=?", (lead_id,)).fetchone()
    return r["campaign_id"] if r and r["campaign_id"] else None


def _sequence_id_for_campaign(cid: int) -> int:
    """Find or create the sequences row that backs this campaign's message
    component (the schema's sequence_state.sequence_id FK points here). The
    campaign components stay the source of truth for the STEPS; this row is just
    the FK anchor, named so we can map it back to the campaign."""
    name = f"campaign:{cid}"
    with db.connect() as conn:
        r = conn.execute("SELECT id FROM sequences WHERE name=?", (name,)).fetchone()
        if r:
            return r["id"]
        cur = conn.execute("INSERT INTO sequences (name, created_at) VALUES (?,?)",
                           (name, _now()))
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------

def enrol_on_accept(lead_id: int) -> bool:
    """Create a sequence_state row for a freshly-accepted lead, if its campaign
    has a message sequence and it isn't already enrolled. Returns True if enrolled."""
    import hashlib
    cid = _lead_campaign(lead_id)
    if cid is None:
        return False
    # A/B: assign a variant by a stable, even split over the variants that HAVE steps.
    variants = [v for v in db.campaign_variants(cid) if v.get("steps")]
    if not variants:
        return False
    vsel = variants[int(hashlib.md5(str(lead_id).encode()).hexdigest(), 16) % len(variants)]
    steps = vsel["steps"]
    sid = _sequence_id_for_campaign(cid)
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM sequence_state WHERE lead_id=? AND sequence_id=?",
            (lead_id, sid)).fetchone()
        if existing:
            return False
        acc = conn.execute("SELECT accepted_at FROM leads WHERE id=?", (lead_id,)).fetchone()
        base = _parse(acc["accepted_at"] if acc else None) or _now_dt()
        due = base + timedelta(days=steps[0]["wait_days"])
        conn.execute(
            "INSERT INTO sequence_state (lead_id, sequence_id, current_step, next_due_at, "
            "status, last_sent_at, variant) VALUES (?,?,?,?,?,?,?)",
            (lead_id, sid, 0, due.isoformat(), "active", None, vsel["name"]))
        conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                     (_now(), lead_id, "sequence-enrolled", f"variant {vsel['name']} · step 1 due {due.date()}"))
    # comms mesh (fire-and-forget, never blocks): CRM -> drip sequence
    try:
        from . import mesh
        mesh.emit("crm-writer", "drip-engine", summary=f"enrolled lead {lead_id} in drip sequence")
    except Exception:
        pass
    return True


def enrol_all_accepted() -> int:
    """Backfill: enrol every 'accepted' lead not already in a sequence.

    HARD GUARD (2026-07-11): an accept we did not earn is not an accept. A lead only
    enrols if it came through OUR connect funnel — is_connection=0 (never a pre-existing
    connection) AND accepted_at IS NOT NULL (accept_sync actually observed the accept).
    Without this, a roster import that lands people as 'accepted' silently enrols the
    whole address book into a cold drip. Mirrors connect._queue_leads, which already
    excludes is_connection so the connect lane cannot invite someone we know.
    """
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM leads WHERE status='accepted' "
            "AND COALESCE(is_connection, 0) = 0 "
            "AND accepted_at IS NOT NULL")]
    return sum(1 for i in ids if enrol_on_accept(i))


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

def due_states(now: datetime | None = None) -> list[dict]:
    now = now or _now_dt()
    if now.tzinfo is None:
        # the scheduler daemon passes a naive local-clock datetime; next_due_at is
        # always aware — comparing them raises TypeError, killing every tick
        now = now.astimezone()
    out = []
    db.sync_red_list_from_json()   # Layer A: a red-listed lead never comes due
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.lead_id, s.sequence_id, s.current_step, s.next_due_at, "
            "COALESCE(s.variant, 'A') AS variant, "
            "l.campaign_id, l.full_name, l.profile_url, l.company, l.title, l.location, "
            "l.status AS lead_status "
            "FROM sequence_state s JOIN leads l ON s.lead_id=l.id "
            "WHERE s.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM red_list r WHERE r.lead_id = l.id "
            "OR r.canon_url = l.profile_url OR r.member_urn = l.profile_url) "
            "ORDER BY s.next_due_at").fetchall()
    for r in rows:
        d = _parse(r["next_due_at"])
        if d and d <= now:
            out.append(dict(r))
    return out


def _advance(state_id: int, lead_id: int, cid: int, current_step: int, n_steps: int, variant: str = "A") -> None:
    """Move the cursor to the next step (or complete after the last)."""
    steps = campaign_steps(cid, variant)
    nxt = current_step + 1
    with db.connect() as conn:
        if nxt >= n_steps:
            conn.execute("UPDATE sequence_state SET status='completed', last_sent_at=?, "
                         "current_step=? WHERE id=?", (_now(), current_step, state_id))
            conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                         (_now(), lead_id, "sequence-completed", f"after step {current_step+1}"))
        else:
            due = _now_dt() + timedelta(days=steps[nxt]["wait_days"])
            conn.execute("UPDATE sequence_state SET current_step=?, next_due_at=?, "
                         "last_sent_at=? WHERE id=?",
                         (nxt, due.isoformat(), _now(), state_id))
            conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                         (_now(), lead_id, "sequence-advanced",
                          f"step {nxt+1} due {due.date()}"))


FAIL_STRIKES = 3   # consecutive send failures before a journey is parked


def _park(state_id: int, lead_id: int, why: str) -> None:
    """Dead-letter a journey: stop retrying, surface it for a human (plan V3 workstream G)."""
    with db.connect() as conn:
        conn.execute("UPDATE sequence_state SET status='needs_attention' WHERE id=?",
                     (state_id,))
        conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                     (_now(), lead_id, "sequence-parked", why))
    print(f"  [parked] journey {state_id} (lead {lead_id}): {why}")


def parked() -> list[dict]:
    """Every journey sitting at needs_attention, with enough about the person to judge it.

    `_park` has always written this status and NOTHING has ever read it back — so the
    "surface it for a human" half of the dead-letter design surfaced a decision the human
    had no way to execute. Seven journeys parked in one 2.5-hour window on 2026-07-19
    (four `send returned 'skipped'`, three `net::ERR_ABORTED` — one bad evening for the
    browser keeper) were still sitting there 22 days later while the dead-letters
    invariant fired 84 times about them.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.lead_id, s.current_step, s.fail_count, s.next_due_at, "
            "       l.full_name, l.profile_url, l.campaign_id, l.status AS lead_status "
            "FROM sequence_state s JOIN leads l ON l.id = s.lead_id "
            "WHERE s.status='needs_attention' ORDER BY s.id").fetchall()
    return [dict(r) for r in rows]


def unpark(commit: bool = False, drop: bool = False,
           lead_ids: list[int] | None = None) -> int:
    """Return parked journeys to the queue (or retire them). Returns how many were acted on.

    RETRY IS A SEND. A revived journey is due immediately, so the next tick puts a message
    in front of a real person — which is why `commit` defaults to False and a bare call
    only previews. `drop` is the always-safe branch: the journey is retired to
    'stopped_parked' and nobody is ever messaged.

    Reviving clears fail_count. Leaving it at the strike limit would park the journey
    again on its very next failure, which is a retry in name only.
    """
    rows = parked()
    if lead_ids is not None:
        wanted = set(lead_ids)
        rows = [r for r in rows if r["lead_id"] in wanted]
    if not rows or not commit:
        return len(rows)
    status = "stopped_parked" if drop else "active"
    now = _now()
    with db.connect() as conn:
        for r in rows:
            if drop:
                conn.execute("UPDATE sequence_state SET status=? WHERE id=?",
                             (status, r["id"]))
            else:
                conn.execute("UPDATE sequence_state SET status=?, fail_count=0, "
                             "next_due_at=? WHERE id=?", (status, now, r["id"]))
            conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                         (now, r["lead_id"],
                          "sequence-dropped" if drop else "sequence-unparked",
                          f"human decision after {r['fail_count']} failure(s)"))
    return len(rows)


def _strike(state_id: int, lead_id: int, why: str) -> None:
    """Count a consecutive send failure; park the journey at FAIL_STRIKES."""
    with db.connect() as conn:
        conn.execute("UPDATE sequence_state SET fail_count=COALESCE(fail_count,0)+1 "
                     "WHERE id=?", (state_id,))
        n = conn.execute("SELECT COALESCE(fail_count,0) FROM sequence_state WHERE id=?",
                         (state_id,)).fetchone()[0]
    if n >= FAIL_STRIKES:
        _park(state_id, lead_id, f"{n} consecutive send failures — last: {why}")
    else:
        print(f"  [strike {n}/{FAIL_STRIKES}] journey {state_id} (lead {lead_id}): {why}")


def _clear_strikes(state_id: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE sequence_state SET fail_count=0 WHERE id=?", (state_id,))


def _halt_reply(state_id: int, lead_id: int) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE sequence_state SET status='stopped_reply' WHERE id=?", (state_id,))
        conn.execute("UPDATE leads SET status='replied', updated_at=? WHERE id=?", (_now(), lead_id))
        conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                     (_now(), lead_id, "replied", "stop-on-reply during sequence"))


def tick(live: bool, max_n: int = 25) -> dict:
    """Process due sequence steps. live=False simulates (advance + record a rehearsed
    message, no LinkedIn). live=True sends for real, gated per send."""
    from .drip import load_openers, generate, _recent_hashes, send_to_lead
    from .config import Config
    from . import safety
    from . import ops
    import linkedin_browser as lb
    from playwright.sync_api import sync_playwright

    cfg = Config.load()
    due = due_states()
    summary = {"due": len(due), "sent": 0, "simulated": 0, "replied": 0,
               "completed": 0, "skipped": 0}
    if not due:
        return summary

    recent = _recent_hashes()

    if not live:
        # rehearsal: advance the cursor + record a clearly-marked rehearsed message
        for st in due[:max_n]:
            cid = st["campaign_id"]; vr = st.get("variant") or "A"
            steps = campaign_steps(cid, vr)
            step_i = st["current_step"]
            if step_i >= len(steps):
                continue
            tmpl = load_openers(steps[step_i]["template"])
            fn = (st["full_name"] or "there").split()[0]
            flds = {"first_name": fn, "company": st.get("company") or "",
                    "title": st.get("title") or "", "location": st.get("location") or ""}
            bubbles, _ = generate(fn, tmpl, recent, fields=flds) if tmpl else (["(no template)"], 0)
            with db.connect() as conn:
                conn.execute("INSERT INTO messages (lead_id, step_index, body, sent_at, status) "
                             "VALUES (?,?,?,?, 'rehearsed')",
                             (st["lead_id"], step_i, " || ".join(bubbles), _now()))
            _advance(st["id"], st["lead_id"], cid, step_i, len(steps), vr)
            summary["simulated"] += 1
            if step_i + 1 >= len(steps):   # this advance finished the sequence
                summary["completed"] += 1
        return summary

    # live send
    with ops.lock(lb.READ_LOCK, agent="engine-sequence", wait_sec=300, heartbeat=True) as got:
        if not got:
            return summary
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                for st in due:
                    if summary["sent"] >= max_n:
                        break
                    ok, why = safety.can_act("message", cfg)
                    if not ok:
                        break
                    # A lead with no public /in/ URL CANNOT be messaged via the profile
                    # composer — retrying is a pure page-load burn. Park it immediately
                    # (2026-07-13: one /sales/lead/-only journey re-drove a 45s Sales Nav
                    # goto every ~2-3 minutes for 3+ hours). accept-sync now harvests the
                    # /in/ URL at accept time, so this parks only genuine data gaps.
                    if "/in/" not in (st.get("profile_url") or ""):
                        _park(st["id"], st["lead_id"],
                              "no public /in/ URL — cannot open the composer")
                        summary["skipped"] += 1
                        continue
                    cid = st["campaign_id"]; vr = st.get("variant") or "A"
                    steps = campaign_steps(cid, vr)
                    step_i = st["current_step"]
                    if step_i >= len(steps):
                        continue
                    tmpl = load_openers(steps[step_i]["template"])
                    lead = {"id": st["lead_id"], "full_name": st["full_name"],
                            "profile_url": st["profile_url"], "company": st.get("company"),
                            "title": st.get("title"), "location": st.get("location")}
                    # DEAD-LETTER after 3 consecutive failures (plan V3 workstream G):
                    # one broken journey must never crash the tick or grind forever —
                    # the old behaviour let a single send exception abort the WHOLE tick,
                    # and the daemon re-drove it every ~2-3 minutes, all evening.
                    try:
                        res = send_to_lead(page, lead, tmpl, recent, step_index=step_i)
                    except Exception as e:  # noqa: BLE001
                        from .browser import is_browser_closed_error
                        if is_browser_closed_error(e):
                            raise   # browser death is the RUN's problem, not this lead's
                        _strike(st["id"], st["lead_id"], str(e)[:120])
                        summary["skipped"] += 1
                        continue
                    if res == "replied":
                        _halt_reply(st["id"], st["lead_id"])
                        summary["replied"] += 1
                    elif res == "sent":
                        _clear_strikes(st["id"])
                        _advance(st["id"], st["lead_id"], cid, step_i, len(steps), vr)
                        summary["sent"] += 1
                    elif res == "unconfirmed":
                        # a prior run died between the Send click and the record — the
                        # DB and reality may disagree; a human resolves it, never a retry
                        _park(st["id"], st["lead_id"],
                              "unconfirmed 'sending' row from a crashed run (F0.5b)")
                        summary["skipped"] += 1
                    else:
                        _strike(st["id"], st["lead_id"], f"send returned '{res}'")
                        summary["skipped"] += 1
            finally:
                safe_close(ctx)
    return summary


# ---------------------------------------------------------------------------
# Status + CLI
# ---------------------------------------------------------------------------

def status_report() -> None:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT s.status, COUNT(*) n FROM sequence_state s GROUP BY s.status").fetchall()
        active = conn.execute(
            "SELECT l.full_name, s.current_step, s.next_due_at FROM sequence_state s "
            "JOIN leads l ON s.lead_id=l.id WHERE s.status='active' "
            "ORDER BY s.next_due_at LIMIT 20").fetchall()
    print("== Sequence state ==")
    for r in rows:
        print(f"  {r['status']:14s} {r['n']}")
    if active:
        print("\nNext due:")
        for r in active:
            print(f"  {r['full_name'] or '?':<26} step {r['current_step']+1}  due {r['next_due_at'][:10]}")


def variant_report(campaign_id: int) -> list[dict]:
    """Per-variant A/B results: how many leads are in each variant + how many replied
    (reply = the strongest signal a message worked). reply_rate = replied / in_test."""
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(s.variant,'A') v, COUNT(*) n, "
                "SUM(CASE WHEN s.status='stopped_reply' THEN 1 ELSE 0 END) replied "
                "FROM sequence_state s JOIN leads l ON s.lead_id=l.id "
                "WHERE l.campaign_id=? GROUP BY COALESCE(s.variant,'A') ORDER BY v",
                (campaign_id,)).fetchall()
    except Exception:   # variant column not migrated yet, or no sequence_state — report empty
        return []
    return [{"variant": r["v"], "in_test": r["n"], "replied": r["replied"] or 0,
             "reply_rate": round((r["replied"] or 0) / r["n"], 3) if r["n"] else 0} for r in rows]


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--unpark" in sys.argv:
        drop = "--drop" in sys.argv
        commit = "--commit" in sys.argv
        ids = None
        if "--lead" in sys.argv:
            ids = [int(x) for x in sys.argv[sys.argv.index("--lead") + 1].split(",")]
        rows = parked()
        if ids is not None:
            rows = [r for r in rows if r["lead_id"] in set(ids)]
        if not rows:
            print("no parked journeys.")
            return
        verb = "RETIRE" if drop else "REVIVE"
        print(f"{len(rows)} parked journey(s) — would {verb}:")
        for r in rows:
            print(f"  lead {r['lead_id']:<6} {(r['full_name'] or '?')[:34]:<34} "
                  f"step {r['current_step']}  {r['fail_count']} failure(s)  "
                  f"camp {r['campaign_id']}")
        n = unpark(commit=commit, drop=drop, lead_ids=ids)
        if not commit:
            print(f"\n[preview] nothing written. Reviving sends: the next tick messages "
                  f"these {n} people. Add --commit to arm, or --drop --commit to retire "
                  f"them without messaging anybody.")
        else:
            print(f"\n{'retired' if drop else 'revived'} {n} journey(s).")
            emit_result("sequence", True,
                        f"{'Retired' if drop else 'Revived'} {n} parked journey(s)")
        return
    if "--enrol-all" in sys.argv:
        n = enrol_all_accepted()
        print(f"enrolled {n} accepted lead(s) into their sequences.")
        emit_result("sequence", True, f"Enrolled {n} accepted lead(s)")
    elif "--tick" in sys.argv:
        from .config import Config
        cfg = Config.load()
        live = "--commit" in sys.argv and cfg.enabled and not cfg.dry_run
        s = tick(live=live)
        mode = "LIVE" if live else "rehearsal"
        print(f"[{mode}] tick: {s}")
        emit_result("sequence", True,
                    f"Sequence tick ({mode}): {s['sent']} sent, {s['simulated']} simulated, "
                    f"{s['replied']} replied")
    else:
        status_report()


if __name__ == "__main__":
    main()
