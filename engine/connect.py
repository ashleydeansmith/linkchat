"""connect.py — the Connect lane: send connection requests to not-yet-connected leads.

Works the DB's status='new' leads (imported, no connection-date). For each: open the
profile, CONFIRM live that you're not already connected (vault flags are unreliable),
click Connect, optionally attach a templated note, and record the invite. Sending is
gated by safety.can_act('connect'), which enforces the weekly connect cap — so this
REFUSES to send while you're over the weekly limit (by design, safety-first).

Modes (python -m engine connect [--probe [url] | --dry-run | --commit] [--max N] [--fast]):
  --probe [url]  READ-ONLY: open a profile (given url, else the first 'new' lead), dump
                 the action-button structure + screenshot to lock the Connect selectors.
  (default)      --dry-run: list the next N 'new' leads from the DB (no browsing).
  --commit       Send requests, gated by enabled + dry_run + the weekly connect cap.

SELECTORS in _send_connect are best-guess pending a live --probe (LinkedIn's profile
action area changes often and hides Connect under 'More' for some profiles).
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from . import ops
import linkedin_browser as lb

from . import DATA_DIR
from .config import Config
from . import db, safety, safe_close
from . import browser as kb   # keeper: reattach + verify (keeper-stability fix)
from .salesnav import first_visible as _first_visible   # shared (one source of truth)
from .withdraw import _arg_int   # reuse the CLI int-flag helper

AGENT = "engine-connect"

# How many pages of a Sales Nav source list the resolve walk may page through before giving
# up. The walk is FORWARD-ONLY and stops as soon as it has `need` leads. Fresh-collected
# leads live on page 1-2 (collect reads front-to-back); a queued lead NOT in the first few
# pages has almost certainly dropped out of the dynamic search (filters like 'posted in the
# last 30 days' churn daily) and no amount of paging will find it — deep walks are what
# the operator saw as 'stuck in Sales Nav' for minutes (3x on 2026-07-11). Keep this SHORT.
MAX_RESOLVE_PAGES = 3

# The sn-invite SWEEP gets a deeper budget than the URN-hunting resolve walk: it invites
# ANY invitable row it meets, so page 7 is as productive as page 1 — deeper is more leads,
# not ghost-chasing. Fallback only — the live value comes from cfg.connect_max_sweep_pages
# (rebuild 2026-07-13: 10 was too shallow once the front pages saturated with consumed rows).
MAX_SWEEP_PAGES = 10
SHOTS = DATA_DIR / "screenshots"


# --- sweep resume-cursor (fix/connect-sweep-cursor, 2026-07-19) --------------------
# The sweep used to restart at page 1 every run (and every self-heal re-open), spending
# its whole walk on front pages saturated with already-consumed rows, then accepting a
# wedged Next at page >=6 as a true end — 5/40 + 16/35 "end_of_search" on ~100-page
# searches. the operator's ruling: collection exists so the lane REMEMBERS where the campaign
# got to and starts there. The cursor = the first page still worth working, advanced
# only past pages observed 100% consumed/connection this run, monotonic forward.

def _sweep_cursor_path():
    return db.DATA_DIR / "sweep_cursors.json"


def _sweep_cursors() -> dict:
    try:
        return json.loads(_sweep_cursor_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  (missing/corrupt file = no cursor, page 1)
        return {}


def _search_fingerprint(src) -> str:
    """Session-invariant identity of a campaign's search. A cursor is a PAGE NUMBER, and a
    page number only means something against a FIXED search — so the fingerprint strips the
    volatile &sessionId (which changes every browser session) and keeps the saved query.
    Same search across sessions => same fingerprint; re-cut filters => different one."""
    from . import nav
    try:
        return nav._strip_session_id(src or "") or (src or "")
    except Exception:  # noqa: BLE001
        return src or ""


def _sweep_cursor_get(campaign_id) -> int:
    try:
        return max(1, int(_sweep_cursors().get(str(campaign_id), {}).get("page", 1)))
    except Exception:  # noqa: BLE001
        return 1


def _sweep_cursor_criteria_changed(campaign_id, src) -> bool:
    """True only when a cursor is stored for this campaign AND its fingerprint differs from
    the current search — i.e. the operator re-cut the filters, so the stored page number now
    points at the wrong people. A fresh campaign (no stored cursor) is never a 'change'."""
    stored = _sweep_cursors().get(str(campaign_id), {}).get("fp")
    return bool(stored) and stored != _search_fingerprint(src)


def _sweep_cursor_reset(campaign_id, src) -> None:
    """Forget the frontier and re-stamp the new fingerprint: the next walk starts at page 1
    under the new criteria. This is the automatic reset for a search-criteria change."""
    cur = _sweep_cursors()
    cur[str(campaign_id)] = {"page": 1, "fp": _search_fingerprint(src), "updated": _now()}
    try:
        _sweep_cursor_path().write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  [sn-invite] cursor reset write failed ({str(e)[:60]}) — non-fatal")


def _sweep_cursor_set(campaign_id, page: int, src=None) -> None:
    """Monotonic forward WITHIN a fixed search — a proven-consumed front never un-proves
    itself. A search-criteria change (via _sweep_cursor_reset) or deleting
    sweep_cursors.json is the deliberate reset path. When `src` is given the search
    fingerprint is stamped so a later criteria change is detectable."""
    cur = _sweep_cursors()
    rec = cur.get(str(campaign_id), {})
    prev = 1
    try:
        prev = int(rec.get("page", 1))
    except Exception:  # noqa: BLE001
        pass
    entry = {"page": max(1, int(page), prev), "updated": _now()}
    fp = _search_fingerprint(src) if src is not None else rec.get("fp")
    if fp:
        entry["fp"] = fp
    cur[str(campaign_id)] = entry
    try:
        _sweep_cursor_path().write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"  [sn-invite] cursor write failed ({str(e)[:60]}) — non-fatal")


def _consumed_prefix_step(prefix_end: int, intact: bool, abs_pg: int,
                          invitable: int, rows_read: int,
                          finished: int = 0) -> tuple[int, bool]:
    """One page's verdict for the cursor: the consumed prefix extends only while it is
    contiguous from the walk's cursor base AND the page has no work LEFT on it. Work left
    over (or a gap in observation) freezes it — conservative by design.

    `rows_read` is the EVIDENCE gate (2026-07-30). A page whose rows never rendered reads
    as 0 rows / 0 invitable — identical, to this function, to a page of 25 already-actioned
    rows. Scoring it 'consumed' advanced the cursor past pages nobody ever worked, and
    _sweep_cursor_set is monotonic forward, so the loss was permanent: 2026-07-22 burnt
    pages 3-5 and 07-23 pages 2-3 on 0-row reads, leaving the cursor at 11 and the lane
    fast-forwarding into a frontier it had never touched (0/40, 0/40, 6/40). No rows read
    is no evidence: freeze the prefix and re-walk the page next run.

    `finished` is the WORK-LEFT gate (2026-08-18). It counts the invitable rows this walk
    got to and finished with — invited, recorded as already pending, statused, or refused
    by the PAGE itself (menu unreadable, no Send button, Connect item not clickable). A row
    the page refuses is not work the walk is leaving behind; before this parameter existed
    it read exactly like a row we never touched, and one permanently-unreadable row froze
    the bookmark for good. Campaign 13's cursor sat on page 4 from 2026-08-01 to 08-17
    because Vlad Pent (page 4) and Kira Khoroshilova (page 5) logged "menu unreadable —
    left collected" on every run; each of the four daily slots then burnt 14 of its 15
    walk pages on a front that yielded nothing and reached the real frontier with its
    page-load budget gone (23/40 on 08-17). Only rows the walk never reached — quota
    filled mid-page, safety gate, browser death, a mid-attempt exception — are work left,
    and only those freeze the bookmark."""
    if not intact:
        return prefix_end, False
    if rows_read <= 0:
        return prefix_end, False
    if max(0, invitable - finished) == 0 and abs_pg == prefix_end + 1:
        return abs_pg, True
    return prefix_end, False


def _end_is_suspicious(reason: str, sent: int, need: int) -> bool:
    """Any end_of_search short of quota earns a fresh-open retry: 2026-07-19 proved
    page-6/7 'ends' on ~100-page searches are wedged Next buttons in disguise; the old
    pages_walked<6 test accepted them. Re-opens stay capped at 2 + the page budget."""
    return reason == "end_of_search" and sent < need


# Shortfall reasons worth ONE same-day top-up retry. SINGLE SOURCE OF TRUTH — scheduler
# imports this rather than keeping its own copy, because the two drifting apart is how a
# spent search kept earning retries (2026-08-07).
TRANSIENT_SHORTFALL_REASONS = frozenset({"pagination_failure", "render_failure"})


def _is_transient_shortfall(reason: str) -> bool:
    """Is re-running this lane later today capable of producing a different answer?

    end_of_search is deliberately NOT transient. A search that has reached its last page
    holds nothing more however many times we walk it, and a retry spends a scheduler slot
    plus a page-load budget to prove that again.
    """
    return reason in TRANSIENT_SHORTFALL_REASONS


def _classify_blank_open(is_reopen: bool, resume_page: int) -> str:
    """A search that renders no rows: which organ actually failed?

    A FIRST open that will not paint is a genuine render failure — the search should
    have rows and we could not read them. That class (2026-08-05, three runs, zero pages
    walked) is still unnamed; it stays classified as render_failure rather than guessed at,
    and _blank_open_evidence captures what was on the page so the next one names itself.

    A RE-OPEN is different evidence entirely. The self-heal only ever re-queues a search
    whose walk just ended at page N; opening at N and finding nothing is the search
    confirming its own end. Calling that render_failure (2026-08-07) blamed the renderer
    for an exhausted source, told the scheduler the shortfall was transient so it queued
    another retry into the same finished search, and left the connect-quota invariant
    firing at the lane — 40 times — while the source sat empty.
    """
    return "end_of_search" if (is_reopen and resume_page > 1) else "render_failure"


def _may_reopen(reopens: int, proven_end: bool) -> bool:
    """May this search be re-queued for another fresh open?

    `proven_end` is set once a re-open has itself come back empty. At that point the end
    is no longer in doubt and re-queueing only repeats the empty open — which is how
    2026-08-07 spent re-open 2/2 discovering the same thing twice.
    """
    if proven_end:
        return False
    return reopens < 2


def _exhaustion_note(campaign_id, reason: str) -> str | None:
    """The honest line for a spent search: how many leads are stranded behind it.

    No code fix invents leads. When a campaign's search reaches its end with the quota
    unfilled, the number that decides what to do next is not a selector or a retry count —
    it is how many collected leads can no longer be reached, because the invite path
    requires meeting the lead on a live search row and this search no longer returns them.
    Campaign 13 held 2,009 such leads on 2026-08-10 while the lane reported 'fresh: 1'.
    """
    if reason != "end_of_search":
        return None
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM leads WHERE campaign_id=? AND status='collected'",
                (campaign_id,)).fetchone()
        stranded = int(row["n"] if row is not None else 0)
    except Exception:  # noqa: BLE001
        return None
    return (f"campaign {campaign_id}: search EXHAUSTED — reached its last page. "
            f"{stranded} collected lead(s) remain unreachable through it "
            f"(the invite path needs a live search row). Cut a new search to refill.")


def _fast_forward(page, n_pages: int, stats: dict, budget: int) -> int:
    """Click Next n_pages times with NO row/menu work — consumed pages skip in seconds
    and the menu-work wedge never arms. Returns pages actually advanced; a stall just
    means the walk starts wherever we got to."""
    from .salesnav import _SCROLL_RESULTS_JS, _wait_rows_settled
    done = 0
    for _ in range(n_pages):
        if stats["page_loads"] >= budget:
            break
        try:
            if not _wait_rows_settled(page):
                break
            for _s in range(12):   # lazy list: render enough for the pager to exist
                try:
                    moved = bool(page.evaluate(_SCROLL_RESULTS_JS))
                except Exception:  # noqa: BLE001
                    page.mouse.wheel(0, 1600)
                    moved = True
                time.sleep(0.35)
                if not moved:
                    break
            nxt = page.get_by_role("button", name=re.compile(r"^next$", re.I))
            if not nxt.count():
                break
            if not nxt.first.is_enabled():
                settled = False
                for _w in range(4):
                    time.sleep(3.0)
                    if nxt.first.is_enabled():
                        settled = True
                        break
                if not settled:
                    break
            nxt.first.click()
            stats["page_loads"] += 1
            time.sleep(random.uniform(1.5, 2.5))
            done += 1
        except Exception:  # noqa: BLE001
            break
    # Render the LANDING page before handing back: a bare Next click lands on a page whose
    # lazy rows haven't loaded yet, so the caller's first walk iteration reads 0 rows and
    # false-ends "no Next button — end of results" with 90+ pages still to go (2026-07-23,
    # cursor page 9 → sent 0). Wait for rows to settle, then scroll them in.
    if done:
        _wait_rows_settled(page)
        prev = -1
        for _s in range(12):
            n = page.locator('a[href*="/sales/lead/"]').count()
            try:
                moved = bool(page.evaluate(_SCROLL_RESULTS_JS))
            except Exception:  # noqa: BLE001
                page.mouse.wheel(0, 1600)
                moved = True
            time.sleep(0.5)
            if not moved and n == prev:
                break
            prev = n
    return done

# Weekly-invite-limit suppression (rebuild workstream C). When LinkedIn's weekly dialog
# fires, the lane writes `until` here and every later connect run refuses instantly until
# it passes — instead of the daily schedule burning page loads into a hard refusal.
# CONNECT-SCOPED on purpose: never routed through safety.can_act, so it can't mute
# messaging/withdraw (test_connect_rebuild guards this).
SUPPRESS_PATH = DATA_DIR / "connect_suppressed.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _suppressed_until() -> str | None:
    """ISO timestamp the connect lane is suppressed until, or None."""
    try:
        until = json.loads(SUPPRESS_PATH.read_text(encoding="utf-8")).get("until") or ""
        return until if until > _now() else None
    except Exception:  # noqa: BLE001
        return None


def _suppress_connect(reason: str) -> str:
    """Suppress connect runs until next Monday 00:00 UTC (LinkedIn's weekly window)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    monday = (now + timedelta(days=(7 - now.weekday()) or 7)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    SUPPRESS_PATH.write_text(json.dumps(
        {"until": monday.isoformat(), "reason": reason, "set_at": _now()}), encoding="utf-8")
    return monday.isoformat()


def classify_search_row(dbrow: dict | None, invite_unknown: bool) -> str:
    """The invitable rule (rebuild workstream A) — pure, falsifiably tested.

    'invite'  : row we may invite (queued 'collected', never a connection)
    'fresh'   : not in the DB — a prospect the dynamic search serves TODAY; insert + invite
    'consumed': already actioned (invited/accepted/replied/skipped/stale/...) — never re-invite
    'connection': 1st-degree — the structural never-invite guard (schema v3)
    'unknown-skip': not in DB and invite_unknown_rows is off (old queue-gated behaviour)
    """
    if dbrow is None:
        return "fresh" if invite_unknown else "unknown-skip"
    if dbrow.get("is_connection"):
        return "connection"
    if dbrow.get("status") == "collected":
        return "invite"
    return "consumed"


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _arg_str(flag: str, default: str | None = None) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _queue_leads(limit: int, campaign: str | None = None) -> list[dict]:
    """Connect targets = 'collected' leads that are NOT already 1st-degree connections,
    optionally filtered to one campaign by name (--campaign).

    The `is_connection = 0` guard is load-bearing, not defensive tidiness. `csv_import`
    lands every row as 'collected' (csv_import.py), so importing the connections export
    on 2026-07-09 put 6,396 people the operator is ALREADY connected to into this queue. The
    old docstring asserted these were "genuine Sales Nav non-connections" — an assumption
    nothing enforced. Status is pipeline state and any lane may rewrite it; being a
    connection is a permanent fact about the person, so it gets its own column.
    """
    db.sync_red_list_from_json()   # Layer A: red-listed people are invisible to the queue
    q = ("SELECT l.id, l.profile_url, l.full_name, l.company, l.title, l.campaign_id "
         "FROM leads l LEFT JOIN campaigns c ON l.campaign_id = c.id "
         "WHERE l.status = 'collected' AND l.profile_url IS NOT NULL "
         "AND COALESCE(l.is_connection, 0) = 0 "
         "AND NOT EXISTS (SELECT 1 FROM red_list r WHERE r.lead_id = l.id "
         "OR r.canon_url = l.profile_url OR r.member_urn = l.profile_url)")
    params: list = []
    if campaign:
        q += " AND c.name = ?"
        params.append(campaign)
    # NEWEST first. The row-menu invite lane can only act on leads still VISIBLE in the
    # live search; a dynamic search ('posted in last 30 days' etc.) churns daily, so
    # freshly-collected leads are near-guaranteed on page 1-2 while June's cohort is
    # mostly gone. Oldest-first filled the whole over-fetch with ghosts (2026-07-12).
    q += " ORDER BY l.id DESC LIMIT ?"
    params.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def _record_invite(lead_id: int) -> None:
    now = _now()
    with db.connect() as conn:
        # IDEMPOTENT (2026-07-12): the sn-invite lane records a lead it re-encounters as
        # 'Connect — Pending' on a later pass; without this guard every re-encounter
        # inserted a fresh invites row (14 real -> 44 recorded in one night).
        dup = conn.execute("SELECT 1 FROM invites WHERE lead_id=? AND status='pending'",
                           (lead_id,)).fetchone()
        if not dup:
            conn.execute("INSERT INTO invites (lead_id, sent_at, status) VALUES (?,?, 'pending')",
                         (lead_id, now))
        conn.execute("UPDATE leads SET status='invited', "
                     "invited_at=COALESCE(invited_at, ?), last_action_at=?, "
                     "updated_at=? WHERE id=?", (now, now, now, lead_id))


def _update_status(lead_id: int, observed: str) -> None:
    # already_connected → 'connection' + is_connection=1, NEVER 'accepted': 'accepted' means
    # "our invite came back yes" and feeds the accept-rate/safety posture. A profile we found
    # already connected is a fact about the roster, not a funnel outcome (2026-07-11 doctrine;
    # the old 'accepted' mapping was quietly recreating the 6,400-unearned-accepts poison).
    mapping = {"already_connected": "connection", "pending": "invited", "no_connect": "skipped"}
    with db.connect() as conn:
        if observed == "already_connected":
            conn.execute("UPDATE leads SET status='connection', is_connection=1, "
                         "last_action_at=?, updated_at=? WHERE id=?", (_now(), _now(), lead_id))
        else:
            conn.execute("UPDATE leads SET status=?, last_action_at=?, updated_at=? WHERE id=?",
                         (mapping.get(observed, "skipped"), _now(), _now(), lead_id))


# ---------------------------------------------------------------------------
# Probe (read-only) — surface the profile action buttons
# ---------------------------------------------------------------------------

STATUS_JS = r"""() => {
  const h1 = document.querySelector('main h1') || document.querySelector('h1');
  const isAction = (b) => {
    const a = b.getAttribute('aria-label') || '';
    return /invite .* to connect|^message |^follow |more actions/i.test(a) || a === 'More';
  };
  // Climb from the name to the nearest ancestor holding the profile action buttons,
  // so sidebar/feed buttons (Follow on suggested people, Like on posts) don't pollute it.
  let scope = h1;
  for (let i = 0; i < 7 && scope; i++) {
    if ([...scope.querySelectorAll('button, a[role="button"]')].some(isAction)) break;
    scope = scope.parentElement;
  }
  scope = scope || document;
  const A = [...scope.querySelectorAll('button, a[role="button"]')];
  const aria = (re) => A.some(b => re.test(b.getAttribute('aria-label') || ''));
  return {
    name: ((h1 || {}).textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
    connect: aria(/invite .* to connect/i),
    pending: aria(/pending/i),
    message: aria(/^message /i),
    follow:  aria(/^follow /i),
    more:    aria(/more actions/i) || A.some(b => (b.getAttribute('aria-label') || '') === 'More'),
  };
}"""

# When a connectable profile is found, dump the Connect control's real markup.
DETAIL_JS = r"""() => {
  const out = [];
  for (const b of document.querySelectorAll('button, a[role="button"], a')) {
    const a = b.getAttribute('aria-label') || '';
    if (/invite .* to connect/i.test(a))
      out.push({tag: b.tagName, role: b.getAttribute('role'),
                aria: a.slice(0, 70), text: (b.textContent || '').trim().slice(0, 24)});
  }
  return out;
}"""

# Dropdown items revealed after clicking the profile's 'More' button.
MENU_JS = r"""() => {
  const items = [...document.querySelectorAll('[role="menuitem"], .artdeco-dropdown__content button, .artdeco-dropdown__content a, [role="menu"] button, [role="menu"] a')];
  return items.filter(e => e.offsetParent)
    .map(e => ({text: (e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 30),
                aria: (e.getAttribute('aria-label') || '').slice(0, 70)}))
    .slice(0, 16);
}"""


# READ-ONLY probe helper for Sales Nav LEAD DETAIL pages (/sales/lead/...). The public
# /in/ STATUS_JS/MENU_JS don't match the SN lead DOM, so this dumps EVERY visible button/
# link in the top action region with its aria-label + text — the ground-truth capture of
# how "Connect" is offered on a lead page (the operator's 2-click flow). Nothing is clicked here.
SN_ACTIONS_JS = r"""() => {
  const out = [];
  for (const b of document.querySelectorAll('button, a[role="button"], a, [role="menuitem"]')) {
    if (!b.offsetParent) continue;                       // visible only
    const aria = (b.getAttribute('aria-label') || '').trim();
    const text = (b.textContent || '').replace(/\s+/g, ' ').trim();
    if (!aria && !text) continue;
    const r = b.getBoundingClientRect();
    if (r.top < 0 || r.top > 760) continue;              // page-header action region only
    out.push({tag: b.tagName, aria: aria.slice(0, 64), text: text.slice(0, 28),
              x: Math.round(r.x), y: Math.round(r.y)});
  }
  return out.slice(0, 44);
}"""


def _classify(s: dict) -> str:
    if s.get("connect"):
        return "NOT_CONNECTED"
    if s.get("pending"):
        return "pending"
    if s.get("message"):
        return "connected"
    if s.get("follow"):
        return "follow_only"
    return "unknown"


def probe_panel() -> None:
    """GATE 0 (read-only): open a campaign's Sales Nav SEARCH, click the first few queued
    lead ROWS to render their detail PANEL (which loads fine, unlike a /sales/lead/ deep-link
    which returns an empty body), and try to read each lead's public /in/ URL from the panel's
    'View LinkedIn profile' overflow link. Proves whether 'capture /in/ at collect time' is
    feasible on the CURRENT Sales Nav DOM. Writes NOTHING (no _store_public_url, no invite)."""
    from . import nav
    from .salesnav import (campaign_source, lead_urn, open_lead_panel, _public_url,
                           _wait_rows_settled, _collect_lead_rows)
    camp_name = _arg_str("--campaign")
    n = _arg_int("--max") or 3
    # Resolve the campaign source from ANY collected lead of this campaign (the leads
    # themselves may have churned out — that's fine, we test the panel on whatever rows the
    # search shows TODAY, not on a specific queued URN).
    seed = _queue_leads(1, camp_name)
    if not seed:
        print(f"[gate0] no collected leads{' for ' + camp_name if camp_name else ''}")
        return
    src = campaign_source(seed[0].get("campaign_id"))
    if not src:
        print("[gate0] campaign has no Sales Nav source URL")
        return
    results: list[tuple] = []
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        try:
            kb.stop_keeper()
            kb.ensure_keeper(wait_sec=120)
        except Exception as e:  # noqa: BLE001
            print(f"  [keeper] restart failed ({str(e)[:60]}) — continuing")
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:   # open the search the PROVEN way: step to feed, then cross-doc load
                    if "/sales/" in (page.url or ""):
                        page.goto(nav.FEED, wait_until=nav._NAV_WAIT, timeout=30_000)
                        time.sleep(random.uniform(1.5, 3.0))
                    page.goto(src, wait_until="domcontentloaded", timeout=45_000)
                except Exception as e:  # noqa: BLE001
                    print(f"  [gate0] search open failed: {str(e)[:70]}")
                    return
                if not _wait_rows_settled(page):
                    print("  [gate0] search rendered NO rows — cannot test the panel")
                    return
                # Test the panel on rows ACTUALLY VISIBLE in the search today (churn-proof
                # test of the mechanism itself, independent of which queued lead we want).
                rows = _collect_lead_rows(page) or []
                print(f"  [gate0] {len(rows)} rows visible; testing the first {min(n, len(rows))}")
                SHOTS.mkdir(parents=True, exist_ok=True)
                for i, row in enumerate(rows[:n]):
                    urn = lead_urn(row.get("href") or row.get("id") or "")
                    name = row.get("name") or urn[:16]
                    if not urn:
                        continue
                    try:
                        opened = open_lead_panel(page, urn)
                        inurl = _public_url(page) if opened else None
                    except Exception as e:  # noqa: BLE001
                        opened, inurl = False, None
                        print(f"  [gate0] {name}: error {str(e)[:60]}")
                    print(f"  [gate0] {name}: panel_opened={opened} /in/={inurl}")
                    results.append((name, opened, inurl))
                    if i == 0 and opened:
                        page.screenshot(path=str(SHOTS / "gate0_panel.png"))
                    try:
                        if "/sales/lead/" in (page.url or ""):
                            page.go_back(wait_until="commit", timeout=15_000)
                            time.sleep(2)
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                safe_close(ctx)
    ok = sum(1 for _, _o, u in results if u)
    print(f"\n[GATE 0] panel-opened {sum(1 for _,o,_u in results if o)}/{len(results)}, "
          f"resolved /in/ for {ok}/{len(results)}. "
          + ("CAPTURE-/in/ IS FEASIBLE." if ok else "capture-/in/ FAILED on current DOM — see gate0_panel.png."))


def _probe_salesnav_lead(page, url: str) -> None:
    """READ-ONLY capture of a Sales Nav lead-detail page's Connect flow. Waits for the
    lead header to actually render (the fixed-dwell public probe caught a pre-render frame),
    screenshots it, dumps the visible action controls, then best-effort opens an overflow
    menu and dumps + screenshots that. No invite is ever sent."""
    from . import nav as _nav
    # WARM the SALES NAV app first: a cold /sales/lead/ deep-link stalls on the app-shell
    # loader (observed 2026-07-22). The regular /feed/ boots the WRONG app — warm on the SN
    # app itself (proven to render in audit), then navigate to the lead in-app.
    try:
        page.goto("https://www.linkedin.com/sales/home", wait_until="domcontentloaded", timeout=45_000)
        time.sleep(6.0)
        print(f"  [sn-lead] warmed on SN app: {(page.url or '')[:56]}")
    except Exception as e:  # noqa: BLE001
        print(f"  [sn-lead] SN warm failed: {str(e)[:60]}")
    try:
        page.goto(url, wait_until=_nav._NAV_WAIT, timeout=45_000)
    except Exception as e:  # noqa: BLE001
        print(f"  [sn-lead] goto failed: {str(e)[:70]}")
        return
    # SN lead pages are slow and the SPA keeps loading past 'commit'. the operator's hypothesis:
    # bake in LinkedIn's slowness — wait a LONG time (up to ~90s), report readiness plainly.
    rendered = False
    for i in range(90):
        try:
            if page.locator("h1, [data-anonymize='person-name']").first.is_visible():
                rendered = True
                break
        except Exception:  # noqa: BLE001
            pass
        if i and i % 15 == 0:
            try:
                probe = page.evaluate("() => ({title: document.title, "
                    "len: (document.body ? document.body.innerText.length : 0), "
                    "leadEls: document.querySelectorAll('[data-anonymize], .profile-topcard, "
                    "artdeco-entity-lockup').length})")
                print(f"  [sn-lead] +{i}s readiness -> {json.dumps(probe, ensure_ascii=False)}")
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.0)
    time.sleep(3.0)
    try:
        final = page.evaluate("() => ({title: document.title, url: location.href, "
            "bodyLen: (document.body ? document.body.innerText.length : 0)})")
        print(f"  [sn-lead] final readiness -> {json.dumps(final, ensure_ascii=False)}")
    except Exception:  # noqa: BLE001
        pass
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / "connect_probe_lead.png"))
    print(f"  [sn-lead] rendered={rendered} url={(page.url or '')[:72]}")
    print("  [sn-lead] header actions ->",
          json.dumps(page.evaluate(SN_ACTIONS_JS), ensure_ascii=False))
    # Best-effort overflow open — try common labels; screenshot + dump whatever opens.
    for name in (r"^more$", r"more actions", r"overflow", r"open menu"):
        btn = _first_visible(page.get_by_role("button", name=re.compile(name, re.I)), 2)
        if btn is None:
            continue
        try:
            btn.click(timeout=5_000)
            time.sleep(1.4)
            page.screenshot(path=str(SHOTS / "connect_probe_lead_menu.png"))
            print(f"  [sn-lead] menu after '{name}' ->",
                  json.dumps(page.evaluate(MENU_JS), ensure_ascii=False))
            page.keyboard.press("Escape")
            return
        except Exception as e:  # noqa: BLE001
            print(f"  [sn-lead] '{name}' click err: {str(e)[:50]}")
    print("  [sn-lead] no overflow matched — read connect_probe_lead.png for the control")


def probe() -> None:
    """Scan up to --max 'new' leads (or one explicit url), report each one's LIVE
    connection status, and dump the Connect control's markup for the first genuinely
    not-connected profile. Read-only — also reveals how reliable the 'new' status is."""
    n = _arg_int("--max") or 8
    url = next((a for a in sys.argv if a.startswith("http")), None)
    leads = [{"profile_url": url, "full_name": None}] if url else _queue_leads(n, _arg_str("--campaign"))
    if not leads:
        print("no 'new' leads in DB — run import-vault first")
        return
    tally: dict[str, int] = {}
    dumped = False
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        # Sales Nav renders reliably only on a FRESH keeper (same rule commit()/audit()
        # rely on — a long-lived keeper serves the 'Browser locked' splash instead of the
        # page). Restart before opening the read context so the probe actually sees LinkedIn.
        try:
            kb.stop_keeper()
            kb.ensure_keeper(wait_sec=120)
        except Exception as e:  # noqa: BLE001
            print(f"  [keeper] pre-probe restart failed ({str(e)[:60]}) — continuing")
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if url and "/sales/lead/" in url:
                    _probe_salesnav_lead(page, url)
                    return
                for idx, L in enumerate(leads):
                    try:
                        # wait_until MUST be "commit" (nav._NAV_WAIT), never "domcontentloaded":
                        # LinkedIn's SPA keeps loading sub-resources long past load, so
                        # domcontentloaded often never fires and goto() burns its whole timeout on
                        # a page that has actually rendered. That was root-caused on 2026-07-07 and
                        # fixed in nav.py — but these two raw goto()s in connect.py bypassed nav and
                        # kept the bug. The dwell + evaluate below is the real readiness signal.
                        from . import nav as _nav
                        page.goto(L["profile_url"], wait_until=_nav._NAV_WAIT, timeout=45_000)
                        time.sleep(3.5)
                        s = page.evaluate(STATUS_JS)
                        cls = _classify(s)
                        tally[cls] = tally.get(cls, 0) + 1
                        print(f"  {cls:14s} {(s.get('name') or L.get('full_name') or '?'):<26} {L['profile_url']}")
                        ops.log_action(AGENT, "profile_view", target=L["profile_url"], result="ok")
                        if idx == 0:
                            SHOTS.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(SHOTS / "connect_probe.png"))
                            try:   # expand 'More' once to capture the Connect menu item
                                more = page.get_by_role("button", name=re.compile(r"more actions", re.I))
                                if not more.count():
                                    more = page.locator("main").get_by_role("button", name=re.compile(r"^more$", re.I))
                                if more.count():
                                    more.first.click()
                                    time.sleep(1.2)
                                    print("   more-menu ->", json.dumps(page.evaluate(MENU_JS), ensure_ascii=False))
                                    page.keyboard.press("Escape")
                            except Exception as e:  # noqa: BLE001
                                print("   [more-menu err]", str(e)[:60])
                        if cls == "NOT_CONNECTED" and not dumped:
                            dumped = True
                            print("   connect control ->", json.dumps(page.evaluate(DETAIL_JS), ensure_ascii=False))
                        time.sleep(random.uniform(1.5, 3.0))
                    except Exception as e:  # noqa: BLE001
                        print(f"  [err] {L['profile_url']}: {str(e)[:70]}")
            finally:
                safe_close(ctx)
    print("\nstatus tally:", tally)


# ---------------------------------------------------------------------------
# The connect action (selectors to confirm via --probe)
# ---------------------------------------------------------------------------

def _vanity(url: str | None) -> str | None:
    m = re.search(r"/in/([^/?#]+)", url or "")
    return m.group(1) if m else None


def _send_connect(page, cfg: Config, lead: dict | None = None) -> str:
    """Send a connection request on the open profile.
    Returns 'sent' | 'already_connected' | 'pending' | 'no_connect'.

    SAFE-BY-DESIGN (2026-06-13): the Connect control is an <a> LINK whose invite href
    carries the LEAD's OWN vanityName (observed: get_by_role('button') never matched it,
    and the old `invite .* to connect` text matcher also matched the page's SIDEBAR
    'people you may know' connects -> wrong-person risk). We match ONLY the link whose
    href carries THIS lead's vanity (from the open /in/ URL), click a VISIBLE copy, and
    NEVER fall back to a name/role match that could hit a suggestion. No vanity match =
    no connectable control = skip."""
    # Layer B (the hard stop): refuse to invite a red-listed person, before any page read.
    if lead is not None and db.red_list_match(
            url=lead.get("profile_url"), lead_id=lead.get("id"), name=lead.get("full_name")):
        db.log_event("redlist-blocked", lead.get("id"), "connect._send_connect")
        return "no_connect"
    vanity = _vanity(page.url)
    connect = None
    if vanity:
        # Scope to <main>: there's an identical connect link in the STICKY top bar
        # (outside main) that reports visible but is COVERED by the Sales Nav icon, so
        # clicking it hangs 30s (observed 2026-06-13 via elementFromPoint). The in-card
        # action-bar copy lives inside <main> and is the real hit target.
        connect = _first_visible(page.locator(f'main a[href*="custom-invite/?vanityName={vanity}"]'))
    if connect is None and vanity:
        # Connect can be tucked under the profile 'More' actions menu on some layouts.
        # Scope to <main> (the sticky-top-bar 'More' copy is covered → its click hangs
        # 30s) and BOUND the click so a covered/missing control SKIPS fast instead of
        # stalling the lane 30s per lead (observed live 2026-06-13 on 2nd/3rd-degree leads).
        more = _first_visible(page.locator("main").get_by_role("button", name=re.compile(r"^more$", re.I)), 3)
        if more is not None:
            try:
                more.click(timeout=6_000)
                time.sleep(1.0)
                # The open menu STATES the relationship — read it before hunting a control.
                # Creator-mode probes (2026-07-11): a pending invite shows a 'Pending' item;
                # an existing connection shows 'Remove connection' (and no Connect at all).
                # Classifying here stops the lane clicking phantoms on profiles it should skip.
                mtxt = _menu_text(page)
                if re.search(r"\bpending\b", mtxt, re.I):
                    return "pending"
                if re.search(r"remove connection", mtxt, re.I):
                    return "already_connected"
                connect = _first_visible(page.locator(f'a[href*="vanityName={vanity}"]'))
                if connect is None:
                    # Creator-mode / follow-primary profiles (live screenshot 2026-07-11,
                    # data/screenshots/connect_send_missing.png): the dropdown's Connect is a
                    # MENU ITEM, not a custom-invite anchor, so the vanity match above finds
                    # nothing and the lane used to die 'send_dialog_missing'. Clicking by name
                    # is safe HERE because we scope to the OPEN dropdown, which contains only
                    # this profile's own actions — a sidebar 'people you may know' Connect can
                    # never match inside it.
                    menu = _first_visible(page.locator(
                        '.artdeco-dropdown__content--is-open, div[role="menu"]'), 2)
                    if menu is not None:
                        connect = (_first_visible(menu.get_by_role(
                                       "button", name=re.compile(r"invite .* to connect|^connect$", re.I)), 4)
                                   or _first_visible(menu.locator(
                                       '[role="button"]:has-text("Connect"), li:has-text("Connect")'), 4))
                        if connect is not None:
                            print("   [more-menu] using the dropdown Connect item (follow-primary profile)")
            except Exception:
                connect = None   # More didn't open a connect → not connectable here
    if connect is None:
        if page.get_by_role("button", name=re.compile(r"pending", re.I)).count():
            return "pending"
        if (page.get_by_role("link", name="Message", exact=True).count()
                or page.get_by_role("button", name=re.compile(r"^message", re.I)).count()):
            return "already_connected"   # 1st-degree: messageable, nothing to connect
        return "no_connect"              # 3rd-degree / no available connect

    # Click, then VERIFY the invite dialog rendered — never assume the click took.
    # Live 2026-07-11 (two identical screenshots): on creator-mode profiles the More-menu
    # Connect item takes FOCUS from our click but never activates — the menu just sits
    # open and no dialog appears. So escalate: normal click -> Enter on the focused item
    # -> JS click, re-checking for the dialog after each. Only give up when all three
    # activations produced nothing.
    def _dialog_open() -> bool:
        try:
            return page.locator('[role="dialog"], .artdeco-modal').count() > 0
        except Exception:  # noqa: BLE001
            return False

    try:
        connect.click(timeout=8_000)
    except Exception:
        return "no_connect"   # control vanished/covered — skip rather than hang
    time.sleep(1.3)
    if not _dialog_open():
        try:
            connect.press("Enter", timeout=3_000)
            time.sleep(1.3)
        except Exception:  # noqa: BLE001
            pass
    if not _dialog_open():
        try:
            connect.evaluate("el => el.click()")
            time.sleep(1.3)
        except Exception:  # noqa: BLE001
            pass
    # Personalise the note the SAME way messages are (2026-07-08): {first_name}/{company}/
    # {title}/{location} → the lead's values. Previously the raw template was typed, so a
    # note of "Hi {first_name}" reached the prospect literally. If a token can't resolve
    # (unknown var / missing data), send the invite WITHOUT a note rather than a broken one.
    note = (cfg.connect_note_template or "").strip()
    if note and lead is not None:
        from . import drip
        note = drip.personalise(note, drip._lead_fields(lead)).strip()
        if drip.unresolved(note):
            print(f"  [note dropped] {lead.get('full_name')}: unresolved placeholder "
                  f"{drip.unresolved(note)} — sending invite without a note")
            note = ""
        leak = drip.names.leaked_decoration(note, lead.get("full_name"))
        if leak:
            # A connection request is the FIRST thing this person sees. Typing their own
            # emoji back is the tell that gets the request deleted (2026-08-20).
            print(f"  [note dropped] {lead.get('full_name')}: decorated name {leak!r} "
                  f"leaked into the note — sending invite without a note")
            note = ""
    # Every control below is scoped to the INVITE DIALOG. A page-wide `^send` match is a
    # wrong-click hazard: with the More menu open (creator-mode flow) it matched the menu's
    # 'Send profile in a message' item and reported a phantom 'sent' (live 2026-07-11).
    # No dialog = nothing to send = classify honestly.
    dlg = _first_visible(page.locator('[role="dialog"], .artdeco-modal'), 3)
    if dlg is None:
        return _send_not_offered(page)
    if note:
        add = _first_visible(dlg.get_by_role("button", name=re.compile(r"add a note", re.I)), 3)
        if add is not None:
            try:
                add.click(timeout=6_000)
                time.sleep(0.8)
            except Exception:
                pass
        box = _first_visible(dlg.get_by_role("textbox"), 4)
        if box is not None:
            box.fill(note)
        snd = _first_visible(dlg.get_by_role("button", name=re.compile(r"^send", re.I)), 4)
    else:
        snd = (_first_visible(dlg.get_by_role("button", name=re.compile(r"send without a note", re.I)), 3)
               or _first_visible(dlg.get_by_role("button", name=re.compile(r"^send", re.I)), 4))
    if snd is None:
        # Bug fix 2026-07-06: this path used to fall through and return 'sent' with NO
        # click — the 4/4 "no Pending" failures. Read what LinkedIn actually showed.
        return _send_not_offered(page)
    try:
        snd.click(timeout=6_000)
    except Exception:
        return _send_not_offered(page)
    time.sleep(1.0)
    # LinkedIn can swallow the send with a refusal dialog (weekly cap / email gate) —
    # classify it now so the failure is named, not just "no Pending".
    txt = _dialog_text(page)
    if _LIMIT_RE.search(txt):
        return "weekly_limit"
    if _EMAIL_RE.search(txt):
        return "email_required"
    return "sent"


_LIMIT_RE = re.compile(r"weekly invitation limit|reached the (weekly )?limit|too many invitations", re.I)
_EMAIL_RE = re.compile(r"email (address )?to (connect|invite)|know\b.{0,30}\bemail", re.I)


def _menu_text(page) -> str:
    """Visible text of the OPEN profile dropdown/menu ('' if none). One read, no clicks."""
    try:
        return page.evaluate(
            "() => { const m = document.querySelector("
            "'.artdeco-dropdown__content--is-open, div[role=\"menu\"]');"
            " return m ? (m.innerText || '').replace(/\\s+/g, ' ') : ''; }") or ""
    except Exception:  # noqa: BLE001
        return ""


def _dialog_text(page) -> str:
    """Visible modal/dialog text (first 400 chars) — what LinkedIn actually put in front
    of the user when the send flow stopped."""
    try:
        return page.evaluate(
            "() => { const d = document.querySelector('[role=\"dialog\"], .artdeco-modal');"
            " return d ? (d.innerText || '').replace(/\\s+/g, ' ').slice(0, 400) : ''; }") or ""
    except Exception:  # noqa: BLE001
        return ""


def _node_text(loc, limit: int = 400) -> str:
    """Visible text of THE element we are judging — never a page-wide re-query.
    `_dialog_text` runs document.querySelector, which returns the FIRST dialog node in DOM
    order whether or not it is visible; `first_visible` returns the first VISIBLE one. When
    a hidden modal shell sits ahead of the real dialog those are different nodes, and the
    page-wide read reports '' no matter what LinkedIn said."""
    try:
        return " ".join(((loc.inner_text() or "")).split())[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _await_invite_dialog(page, first: float = 1.5, settle: float = 6.0, step: float = 0.5):
    """Wait for the invite dialog to actually PAINT, then read it. Returns (dlg, send, text).

    fix/invite-dialog-empty-read (2026-08-06). The old read slept a fixed 1.5s, sampled the
    modal ONCE and treated a missing Send as a fact about the LEAD — a terminal skip, no
    retry. That sample cannot distinguish "the modal shell has mounted but its content has
    not painted yet" from "LinkedIn genuinely offers no Send here", and 26 skips in
    scheduler.log between 2026-07-12 and 2026-08-03 report the dialog text as EMPTY every
    single time, which is the unpainted-shell signature: every real refusal renders words.
    Its siblings already hold the opposite discipline — the profile flow re-checks
    ("never assume the click took") and salesnav.wait_lead_ready polls because "a fixed
    sleep races the loader".

    `first` keeps today's proven opening beat unchanged, so an already-painted dialog is
    still taken on the first read and nothing about the common path speeds up or slows down.
    Everything after it is pure ADDITION: poll until the dialog offers a Send button (send
    it) or shows copy (a named refusal the caller can classify and suppress on), and give up
    only when `settle` expires — bounded, because the lane has a wall clock and 40 leads.
    The text is read from the dialog we judged; the page-wide read survives only as a
    last-resort fallback when the scoped read came back blank, so the weekly-limit guard can
    never see LESS than it saw before."""
    time.sleep(first)
    t0 = time.monotonic()
    dlg = snd = None
    txt = ""
    while True:
        dlg = _first_visible(page.locator('[role="dialog"], .artdeco-modal'), 3)
        if dlg is not None:
            snd = _first_visible(dlg.get_by_role("button", name=re.compile(r"^send", re.I)), 4)
            txt = _node_text(dlg)
            if snd is not None or txt:
                return dlg, snd, txt
        if time.monotonic() - t0 >= settle:
            break
        time.sleep(step)
    return dlg, snd, (txt or _dialog_text(page))


def _send_not_offered(page) -> str:
    """The invite dialog never offered a clickable Send — classify what LinkedIn showed
    instead, and screenshot it (data/screenshots/connect_send_missing.png) so the next
    diagnosis reads evidence, not guesses."""
    txt = _dialog_text(page)
    try:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / "connect_send_missing.png"))
    except Exception:  # noqa: BLE001
        pass
    if _LIMIT_RE.search(txt):
        return "weekly_limit"
    if _EMAIL_RE.search(txt):
        return "email_required"
    print(f"   [send-missing] dialog said: {txt[:200] or '(no dialog rendered)'}")
    return "send_dialog_missing"


def _verify_pending(page, tries: int = 3) -> bool:
    """verify-after-action (keeper-stability fix): after clicking Connect/Send, the profile's
    action bar flips to 'Pending'. Re-read it a few times (it updates a beat late) and only
    treat the invite as REAL when Pending is seen — closes the old fire-and-assume gap where
    a click was recorded as an invite without confirmation. A read failure counts as
    unconfirmed (False). No navigation, so this spends no profile_view budget."""
    for _ in range(max(1, tries)):
        try:
            s = page.evaluate(STATUS_JS)
            if s.get("pending"):
                return True
        except Exception:
            pass
        time.sleep(1.2)
    # Follow-primary (creator-mode) profiles keep Connect/Pending inside the 'More'
    # menu — the action bar never shows Pending there, so the read above is blind to a
    # REAL sent invite (2026-07-06). Check the menu.
    #
    # ⚠ The menu may ALREADY BE OPEN — the creator-mode send flow clicks Connect inside it
    # and LinkedIn doesn't always close it. The old code clicked More unconditionally,
    # which TOGGLED an open menu closed and then found nothing: two live invites on
    # 2026-07-11 (Pavan/Elliot) were sent, showed Pending in the menu, and were still
    # reported unconfirmed. Read first; only click More if no menu is open. Retry once —
    # the Pending item can render a beat after the send.
    try:
        for attempt in range(2):
            txt = _menu_text(page)
            if not txt:
                more = _first_visible(page.locator("main").get_by_role(
                    "button", name=re.compile(r"^more$", re.I)), 2)
                if more is None:
                    return False
                more.click(timeout=4_000)
                time.sleep(1.2)
                txt = _menu_text(page)
            if re.search(r"\bpending\b", txt, re.I):
                page.keyboard.press("Escape")
                return True
            page.keyboard.press("Escape")
            time.sleep(1.5)
    except Exception:  # noqa: BLE001
        pass
    return False


def _store_public_url(lead_id: int, public_url: str) -> bool:
    """Persist a resolved public /in/ URL onto the lead so NO future run has to re-open
    the Sales Nav search to find it again. Keeps sales_nav_url as the /sales/lead/ origin.

    Returns True if this lead is still connectable, False if it must be skipped.

    Two hazards this guards, both live:
      * The raw URL was written straight through, bypassing canonicalisation.
      * `leads.profile_url` is UNIQUE. A Sales-Nav prospect very often resolves to
        someone already in the DB — most likely one of the 6,400 existing 1st-degree
        connections. The old code would raise IntegrityError and kill the whole batch.
        Instead we treat it as what it is: the same person, already known. We merge
        (skip this duplicate row, inherit `is_connection`) and refuse to invite.
    """
    from . import canon
    url = canon.canon_in(public_url)
    if not url:
        return False
    with db.connect() as conn:
        other = conn.execute(
            "SELECT id, is_connection FROM leads WHERE profile_url=? AND id!=?",
            (url, lead_id)).fetchone()
        if other:
            # Same person under two rows. Retire THIS row; the surviving row keeps the
            # canonical URL. Never invite: either the other row is an existing connection,
            # or it is itself queued and will be handled once.
            conn.execute("UPDATE leads SET status='skipped', is_connection=?, updated_at=? "
                         "WHERE id=?",
                         (other["is_connection"] or 0, _now(), lead_id))
            # MERGE GOTCHA fix (2026-07-13): any message journey pointing at the retired
            # row must follow the person to the survivor, or it silently never sends
            # (bit 6 leads on 2026-07-12, repointed by hand that night). Only repoint when
            # the survivor has no journey of its own — never double-enrol one person.
            if not conn.execute("SELECT 1 FROM sequence_state WHERE lead_id=?",
                                (other["id"],)).fetchone():
                conn.execute("UPDATE sequence_state SET lead_id=? WHERE lead_id=?",
                             (other["id"], lead_id))
            merged = other["id"]
        else:
            conn.execute("UPDATE leads SET profile_url=?, updated_at=? WHERE id=?",
                         (url, _now(), lead_id))
            merged = None
    if merged is not None:
        db.log_event("duplicate-merged", lead_id,
                     f"resolves to {url}, already held by lead {merged}")
        return False
    return True


def _resolve_public_urls(page, leads: list[dict], need: int | None = None) -> int:
    """Map queued /sales/lead/ ids to public /in/ URLs, so the connect loop has something it
    can actually invite (the 'Invite to connect' button exists ONLY on the public profile).

    ONE FORWARD WALK, NOT A PER-LEAD HUNT (root-cause fix 2026-07-11). The previous version
    called salesnav.open_lead_panel(urn) once PER LEAD, and open_lead_panel re-scans the list
    from the current page — up to 8 pages, scrolling 5x each — hunting for that one URN. On a
    ~2,483-lead saved search (~100 pages) the wanted lead is almost never in the next 8 pages,
    so each lead burned 1-3 minutes of scrolling, resolved nothing, and was skipped. Net
    effect: connect ran 11+ minutes, sent 0 invites, and printed NOTHING. That is the whole
    "connect does nothing" bug (live-confirmed by py-spy: stuck in open_lead_panel).

    The hunt was never necessary. EVERY row in the source list is already a lead of this
    campaign in our DB, so we don't need one SPECIFIC lead — we need `need` invitable ones.
    So: walk the list forward page by page, and resolve whichever pending leads we meet, in
    the order the list gives them. O(pages) panel opens instead of O(leads x pages) scrolling,
    and it stops the moment it has enough.

    Mutates each resolved lead's 'profile_url' in place. Returns how many were resolved."""
    from . import nav
    from .salesnav import (campaign_source, lead_urn, first_visible,
                           public_url_from_panel, wait_lead_ready,
                           _wait_rows_settled)   # collect's proven row reader — reused, not re-invented
    pending = [L for L in leads if "/sales/lead/" in (L.get("profile_url") or "")]
    if not pending:
        return 0

    by_src: dict[str, list[dict]] = {}
    for L in pending:
        src = campaign_source(L.get("campaign_id"))
        if src:
            by_src.setdefault(src, []).append(L)

    resolved = 0
    for src, group in by_src.items():
        want: dict[str, dict] = {}
        for L in group:
            u = lead_urn(L.get("profile_url") or "")
            if u:
                want[u] = L
        if not want:
            continue
        # Open the source list the way COLLECT does — the only navigation that has ever been
        # proven to render rows on this saved search (live-verified 2026-07-11 via
        # salesnav.gather_preview: 10 real rows).
        #
        # Why NOT nav.human_open() here (the 2026-07-11 "0 row(s)" bug): human_open walks
        # feed -> /sales/home -> search. But once we are INSIDE the Sales Nav SPA, a goto to a
        # "#query=(...)" saved-search URL is an in-app HASH navigation — the document never
        # reloads, so the results never render and we sit on /sales/home with zero lead rows.
        # A cross-document load into the search URL renders fine. So: if we're inside the SPA,
        # step OUT to the feed first, which guarantees the next goto is a real document load.
        try:
            if "/sales/" in (page.url or ""):
                page.goto(nav.FEED, wait_until=nav._NAV_WAIT, timeout=30_000)
                time.sleep(random.uniform(1.5, 3.0))   # human beat, not a bot bounce
            page.goto(src, wait_until="domcontentloaded", timeout=45_000)
        except Exception as e:  # noqa: BLE001
            print(f"  [resolve] couldn't open the source list ({str(e)[:70]})")
            continue
        n_rows = _wait_rows_settled(page)
        if not n_rows:
            print(f"  [resolve] source list rendered NO lead rows — {len(group)} lead(s) "
                  f"left as-is. Check the campaign's Sales Nav search still returns results.")
            continue
        print(f"  [resolve] source list open: {n_rows} row(s) on screen")

        print(f"  [resolve] walking the source list for {len(want)} queued lead(s)"
              + (f", need {need}" if need else ""))

        for pg in range(1, MAX_RESOLVE_PAGES + 1):
            if need and resolved >= need:
                break
            # wait for the virtualised list to settle (never read a page mid-paint)
            if not _wait_rows_settled(page):
                print(f"  [resolve] page {page_offset + pg}: no rows rendered — stopping the walk")
                break
            # A Sales Nav page holds ~25 leads but renders them LAZILY as you scroll.
            # Scrolling the last ANCHOR into view does NOT trigger the loader — the page
            # plateaued at 8-10 rows and queued leads below the fold were 'not found'
            # (three 8-page ghost walks on 2026-07-11 = the 'stuck in Sales Nav' the operator
            # watched). collect reads full pages because it scrolls the INNER results
            # container (_SCROLL_RESULTS_JS) — use exactly that.
            from .salesnav import _SCROLL_RESULTS_JS
            prev = -1
            for _ in range(12):
                n = page.locator('a[href*="/sales/lead/"]').count()
                moved = False
                try:
                    moved = bool(page.evaluate(_SCROLL_RESULTS_JS))
                except Exception:  # noqa: BLE001
                    page.mouse.wheel(0, 1600)
                time.sleep(0.7)
                if not moved and n == prev:
                    break   # bottom reached and nothing new rendered
                prev = n

            # every lead row visible on THIS page, in list order
            try:
                hrefs = page.locator('a[href*="/sales/lead/"]').evaluate_all(
                    "els => els.map(e => e.getAttribute('href'))")
            except Exception:  # noqa: BLE001
                hrefs = []
            urns_here = []
            for h in hrefs or []:
                u = lead_urn(h or "")
                if u and u in want and u not in urns_here:
                    urns_here.append(u)

            print(f"  [resolve] page {pg}: {len(hrefs or [])} row(s), "
                  f"{len(urns_here)} of them queued")

            for u in urns_here:
                if need and resolved >= need:
                    break
                L = want[u]
                try:
                    row = page.locator(f'a[href*="{u}"]')
                    if not row.count():
                        continue
                    r0 = first_visible(row, 6) or row.first
                    try:
                        r0.scroll_into_view_if_needed(timeout=3_000)
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.5)
                    r0.click(timeout=8_000)
                    if not wait_lead_ready(page, timeout=20):
                        print(f"  [resolve] {L.get('full_name')}: lead panel never became ready")
                        continue
                    pub = public_url_from_panel(page)
                    if not pub:
                        print(f"  [resolve] {L.get('full_name')}: panel open but no public "
                              f"/in/ link found in its overflow menu")
                        continue
                    if _store_public_url(L["id"], pub):
                        L["profile_url"] = pub
                        resolved += 1
                        print(f"  [resolve] {L.get('full_name') or '?':<26} -> {pub}")
                    else:
                        # duplicate of a row we already hold (very often an existing
                        # 1st-degree connection) — drop it from this batch, never invite
                        L["_skip"] = True
                        print(f"  [merge] {L.get('full_name')}: already known as {pub} — skipped")
                    # On a SEARCH page (vs a lead list) the row click can NAVIGATE to the
                    # lead's full page instead of opening a side panel — go back to the
                    # list or the rest of the walk reads a page with no rows.
                    if "/sales/lead/" in (page.url or ""):
                        try:
                            page.go_back(wait_until="commit", timeout=15_000)
                            time.sleep(2)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as e:  # noqa: BLE001
                    if kb.is_browser_closed_error(e):
                        raise   # let the caller's keeper-stability wrapper handle it
                    continue    # one bad row must never end the walk

            if need and resolved >= need:
                break
            try:    # forward to the next page — never restart the walk
                nxt = page.get_by_role("button", name=re.compile(r"^next$", re.I))
                if not nxt.count() or not nxt.first.is_enabled():
                    print("  [resolve] end of list")
                    break
                nxt.first.click()
                time.sleep(random.uniform(2.5, 4.0))
            except Exception:  # noqa: BLE001
                break

    if resolved:
        print(f"  [resolve] mapped {resolved} lead(s) to public profiles (stored — "
              "future runs skip the search entirely)")
    else:
        print("  [resolve] resolved NOTHING — no queued lead was found in the source list")
    return resolved


def _invite_from_search(page, leads: list[dict], need: int, cfg: Config) -> dict:
    """Send invites DIRECTLY from the Sales Nav search results — the 2026-07-11 redesign,
    rebuilt 2026-07-13 (plan V3): the LIVE SEARCH is the source of targets, the DB is the
    ledger of who we've touched — never a gatekeeper. A row not in the DB is a FRESH
    prospect the dynamic search serves today: it is inserted (collect's parser, canonical
    upsert) and invited in the same pass, so the lane can never again starve while the
    search is full (the 2026-07-13 "sent 2/40, reported ok" failure).

    Every SN search row carries a 'See more actions for {Name}' dropdown with a native
    **Connect** item, and the same menu shows 'Connect — Pending' (greyed) once an invite
    is out — screenshot-proven on the live account. So for /sales/lead/ leads there is NO
    reason to resolve a public /in/ URL, visit the profile, and hunt the profile's Connect
    button (the path that wedged the browser three times on 2026-07-11 and whose panel
    extraction no longer matches the SN DOM at all). ONE search-page load per batch:
    scroll the list (collect's proven inner-container scroller), and for each queued row
    open its menu — 'Pending' = already invited (record it, free verify); active
    'Connect' = click → SN's invite modal → Send → re-open the menu and require 'Pending'
    before recording. Fewer requests per invite by an order of magnitude, which is also
    the fix for the 'too many requests in too short a time' throttle the old design earned.

    Marks each handled lead `_skip` so the profile-path loop never re-touches it.
    Returns a stats dict — the honest run contract (plan V3 workstream C):
      {sent, reason, pages_walked, page_loads, consumed, fresh, queued, unknown_skipped}
    reason ∈ quota_filled | end_of_search | page_budget | max_pages | pagination_failure |
             render_failure | weekly_limit | safety_gate | nothing_to_do.
    Newly-discovered pending rows are recorded but not counted against `need`."""
    from . import nav
    from .salesnav import (campaign_source, lead_urn, _SCROLL_RESULTS_JS,
                           _wait_rows_settled, _collect_lead_rows)

    stats = {"sent": 0, "reason": "nothing_to_do", "pages_walked": 0, "page_loads": 0,
             "consumed": 0, "fresh": 0, "queued": 0, "unknown_skipped": 0}
    pending = [L for L in leads if "/sales/lead/" in (L.get("profile_url") or "")
               and not L.get("_skip")]
    if not pending or need <= 0:
        return stats
    by_src: dict[str, list[dict]] = {}
    for L in pending:
        src = campaign_source(L.get("campaign_id"))
        if src:
            by_src.setdefault(src, []).append(L)

    max_pages = int(getattr(cfg, "connect_max_sweep_pages", 0) or MAX_SWEEP_PAGES)
    budget = int(getattr(cfg, "connect_page_load_budget", 0) or 30)
    invite_unknown = bool(getattr(cfg, "invite_unknown_rows", False))
    sent = 0
    # WORK QUEUE, not a plain dict walk: a source whose pagination wedges mid-walk gets
    # RE-QUEUED (max 2) for a fresh open — consumed pages skip in seconds, and a stuck
    # Next advances fine on fresh page state (live pattern 07-13/14: heavy menu work on
    # a page wedges its Next; the same boundary paginates cleanly after a re-open).
    work = list(by_src.items())
    reopens: dict[str, int] = {}
    resume_pg: dict[str, int] = {}   # a re-open resumes at the page the walk died on
    proven_end: set[str] = set()     # a re-open that came back empty PROVED the end
    for src, group in work:
        if sent >= need:
            break
        camp_id = group[0].get("campaign_id")
        want = {}
        for L in group:
            u = lead_urn(L.get("profile_url") or "")
            if u:
                want[u] = L
        if not want:
            continue
        # Open the search EXACTLY like collect does — raw URL, domcontentloaded — because
        # collect is the only opener with a clean record (2-for-2 tonight, 50 rows 90s
        # before a stripped-URL open rendered zero). Escalate on a blank page instead of
        # giving up: reload (clears SN's 'Trouble loading' white panel), then the
        # sessionId-stripped URL as the last resort (nav.py's stale-sessionId theory).
        opened = False
        for attempt, url in enumerate((src, None, nav._strip_session_id(src)), 1):
            stats["page_loads"] += 1
            try:
                if url is None:
                    page.reload(wait_until="domcontentloaded", timeout=45_000)
                else:
                    if "/sales/" in (page.url or "") and "#" in url:
                        # in-app hash nav never reloads the document — step out first
                        page.goto(nav.FEED, wait_until=nav._NAV_WAIT, timeout=30_000)
                        time.sleep(random.uniform(1.5, 3.0))
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:  # noqa: BLE001
                print(f"  [sn-invite] open attempt {attempt} failed ({str(e)[:50]})")
                continue
            if _wait_rows_settled(page):
                opened = True
                break
            print(f"  [sn-invite] open attempt {attempt}: page rendered no rows"
                  + ("" if attempt == 3 else " — retrying"))
            time.sleep(8)
        if not opened:
            # WHICH open was this? A first open that will not paint is a render failure.
            # A re-open at the page the walk just died on is the search confirming its own
            # end — blaming the renderer for that (2026-08-07) sent the scheduler off to
            # retry a finished search and left connect-quota firing at the wrong organ.
            is_reopen = reopens.get(src, 0) > 0
            resumed_at = resume_pg.get(src, 1)
            stats["reason"] = _classify_blank_open(is_reopen, resumed_at)
            if stats["reason"] == "end_of_search":
                proven_end.add(src)
                print(f"  [sn-invite] re-open at page {resumed_at} rendered no rows — that "
                      f"page is past the last one. END OF SEARCH confirmed, not a render "
                      f"failure; {len(group)} lead(s) stay collected.")
                note = _exhaustion_note(camp_id, "end_of_search")
                if note:
                    print(f"  [sn-invite] {note}")
            else:
                print(f"  [sn-invite] search would not render rows after 3 attempts — "
                      f"{len(group)} lead(s) left for next run [render_failure]")
            continue
        # RESUME CURSOR: start where the campaign got to, not page 1 (the operator 2026-07-19).
        # First honour a search-criteria change — a re-cut filter set makes the stored page
        # number point at the wrong people, so forget the frontier and walk from page 1.
        if _sweep_cursor_criteria_changed(camp_id, src):
            print(f"  [sn-invite] campaign {camp_id}: search criteria changed since the "
                  "last run — resetting resume cursor to page 1")
            _sweep_cursor_reset(camp_id, src)
        cursor = _sweep_cursor_get(camp_id)
        target = max(cursor, resume_pg.get(src, 1))
        page_offset = 0
        if target > 1:
            print(f"  [sn-invite] resume: fast-forwarding past {target - 1} page(s) "
                  f"(cursor {cursor}" +
                  (f", re-open resume {resume_pg[src]}" if src in resume_pg else "") + ")")
            page_offset = _fast_forward(page, target - 1, stats, budget)
            if page_offset < target - 1:
                print(f"  [sn-invite] fast-forward reached page {page_offset + 1} of "
                      f"{target} — walking from here")
        # Cursor bookkeeping: the consumed prefix is only provable from the cursor base;
        # pages skipped for a re-open resume beyond it stay unobserved this pass and the
        # contiguity rule in _consumed_prefix_step freezes the prefix automatically.
        prefix_end, prefix_intact = cursor - 1, True
        last_abs = page_offset
        for pg in range(1, max_pages + 1):
            if sent >= need:
                stats["reason"] = "quota_filled"
                break
            if stats["page_loads"] >= budget:
                stats["reason"] = "page_budget"
                print(f"  [sn-invite] page-load budget spent ({budget}) — ending walk")
                break
            # Settle on the FIRST walk page too WHEN we fast-forwarded into it (page_offset>0):
            # that landing page was never settle-checked by the open logic, so reading it raw
            # gave 0 rows and a false end (2026-07-23). A fresh page-1 open is already settled.
            if (pg > 1 or page_offset > 0) and not _wait_rows_settled(page):
                stats["reason"] = "render_failure"
                print(f"  [sn-invite] page {page_offset + pg}: no rows rendered — stopping [render_failure]")
                break
            stats["pages_walked"] += 1
            abs_pg = page_offset + pg
            last_abs = abs_pg
            prev = -1
            for _ in range(12):   # render the full page (lazy list)
                n = page.locator('a[href*="/sales/lead/"]').count()
                moved = False
                try:
                    moved = bool(page.evaluate(_SCROLL_RESULTS_JS))
                except Exception:  # noqa: BLE001
                    page.mouse.wheel(0, 1600)
                time.sleep(0.7)
                if not moved and n == prev:
                    break
                prev = n
            # Read FULL row data (name/title/company), not just hrefs — an unknown row
            # is inserted as a fresh lead of this campaign and invited in the same pass.
            # Every row of this search is campaign-targeted by definition; the DB is the
            # ledger of who we've touched, never the gatekeeper (plan V3 §2 — the old
            # queue-gated rule starved the lane once the June cohort drifted out of the
            # dynamic search: 2026-07-13, pages 3-4 held ~90 fresh prospects, 4 matches).
            try:
                page_rows = _collect_lead_rows(page) or []
            except Exception:  # noqa: BLE001
                page_rows = []
            here, comp = [], {"consumed": 0, "fresh": 0, "queued": 0,
                              "connection": 0, "unknown_skipped": 0}
            with db.connect() as _conn:
                for row in page_rows:
                    u = lead_urn(row.get("href") or row.get("id") or "")
                    if not u:
                        continue
                    dbrow = _conn.execute(
                        "SELECT id, profile_url, full_name, campaign_id, status, "
                        "COALESCE(is_connection,0) AS is_connection FROM leads "
                        "WHERE profile_url LIKE ?", (f"%{u}%",)).fetchone()
                    dbrow = dict(dbrow) if dbrow else None
                    verdict = classify_search_row(dbrow, invite_unknown)
                    if dbrow:
                        _conn.execute("UPDATE leads SET last_seen_on_search_at=? WHERE id=?",
                                      (_now(), dbrow["id"]))
                    if verdict == "invite":
                        comp["queued"] += 1
                        here.append((u, want.get(u) or dbrow))
                    elif verdict == "fresh":
                        snurl = "https://www.linkedin.com" + (row.get("id") or "")
                        if not row.get("name") or "/sales/lead/" not in snurl:
                            comp["unknown_skipped"] += 1
                            continue
                        db.upsert_lead(_conn, profile_url=snurl, full_name=row["name"],
                                       title=row.get("title") or None,
                                       company=row.get("company") or None,
                                       location=row.get("location") or None,
                                       source="salesnav", status="collected")
                        _conn.execute(
                            "UPDATE leads SET sales_nav_url=?, campaign_id=?, "
                            "last_seen_on_search_at=? WHERE profile_url=?",
                            (snurl, camp_id, _now(), snurl))
                        fresh = _conn.execute(
                            "SELECT id, profile_url, full_name, campaign_id FROM leads "
                            "WHERE profile_url=?", (snurl,)).fetchone()
                        comp["fresh"] += 1
                        here.append((u, dict(fresh)))
                    elif verdict == "connection":
                        comp["connection"] += 1
                    elif verdict == "unknown-skip":
                        comp["unknown_skipped"] += 1
                    else:
                        comp["consumed"] += 1
            for k in ("consumed", "fresh", "queued", "unknown_skipped"):
                stats[k] += comp[k]
            print(f"  [sn-invite] page {abs_pg}: {len(page_rows)} row(s) — "
                  f"{len(here)} invitable ({comp['queued']} queued + {comp['fresh']} fresh), "
                  f"{comp['consumed']} consumed, {comp['connection']} connections, "
                  f"{comp['unknown_skipped']} skipped")
            # The page's cursor verdict is taken AFTER the attempts below, not before
            # them: whether a page still holds work is a fact about what the walk could
            # DO with its rows, not about how they looked in the DB (2026-08-18).
            # `finished` counts the rows this walk got to and is done with, however the
            # page answered; a row we never reach is never counted, so quota-stop,
            # safety-gate and browser-death all freeze the bookmark exactly as before.
            finished = 0
            for u, L in here:
                if sent >= need:
                    break
                ok, why = safety.can_act("connect", cfg)
                if not ok:
                    print(f"  [stop] safety gate: {why}")
                    stats["sent"], stats["reason"] = sent, "safety_gate"
                    return stats
                # Counted here, before the name check and the attempt: from this point on
                # the walk has committed to this row and will leave the page having dealt
                # with it — including the outcomes where the PAGE refuses us (menu
                # unreadable, no Send, Connect not clickable). Those rows stay queued in
                # the DB, but re-walking their page next run has never once produced a
                # send (campaign 13, pages 4-17, every run 2026-08-01 → 08-17).
                finished += 1
                name = (L.get("full_name") or "").strip()
                if not name:
                    continue
                try:
                    # PROVEN name-scoped lookup FIRST (live-verified 07-12/13); the
                    # row-scoped locator is the FALLBACK for names the accessible-name
                    # match can't hit (emoji/flags: Vlad Pent 🇬🇧🇺🇦 failed twice on
                    # 2026-07-13). Priority order matters: the row-scoped path is newer
                    # and less proven, so it only runs when the proven path finds nothing.
                    anchor = page.locator(f'a[href*="{u}"]').first
                    try:
                        anchor.scroll_into_view_if_needed(timeout=4_000)
                    except Exception:  # noqa: BLE001
                        page.mouse.wheel(0, 400)
                        time.sleep(0.8)
                        anchor.scroll_into_view_if_needed(timeout=4_000)
                    btn = page.get_by_role("button",
                                           name=f"See more actions for {name}").first
                    if not btn.count():
                        row_box = anchor.locator(
                            'xpath=ancestor-or-self::*[self::li or self::tr]'
                            '[.//button[contains(@aria-label, "See more actions")]][1]')
                        if row_box.count():
                            btn = row_box.get_by_role(
                                "button", name=re.compile(r"see more actions", re.I)).first
                    btn.scroll_into_view_if_needed(timeout=4_000)
                    btn.click(timeout=6_000)
                    time.sleep(1.2)
                    # The menu lives in the element the button's aria-controls points at
                    # (id like 'hue-menu-ember3793', classes OBFUSCATED — '_container_x5gf48…',
                    # NOT artdeco; DOM-dumped live 2026-07-11). Never hunt it by class.
                    menu = None
                    try:
                        ctl = btn.get_attribute("aria-controls")
                        if ctl:
                            menu = page.locator(f"#{ctl}")
                            if not menu.count():
                                menu = None
                    except Exception:  # noqa: BLE001
                        menu = None
                    try:
                        mtxt = (menu.inner_text() if menu is not None else _menu_text(page)) or ""
                        mtxt = " ".join(mtxt.split())
                    except Exception:  # noqa: BLE001
                        mtxt = ""
                    if re.search(r"connect\s*[—-]\s*pending|\bpending\b", mtxt, re.I):
                        page.keyboard.press("Escape")
                        _record_invite(L["id"])
                        L["_skip"] = True
                        print(f"  [sn-invite] {name}: already PENDING — recorded, not re-sent")
                        continue
                    if not mtxt:
                        # An UNREADABLE menu is a UI failure, not a fact about the lead —
                        # never re-status on it. On 2026-07-13 an orphaned duplicate lane
                        # fighting for the keeper made every menu read empty and 66 queued
                        # leads were silently re-statused out of the queue.
                        page.keyboard.press("Escape")
                        print(f"  [sn-invite] {name}: menu unreadable — left collected")
                        continue
                    if not re.search(r"\bconnect\b", mtxt, re.I):
                        page.keyboard.press("Escape")
                        L["_skip"] = True
                        _update_status(L["id"], "no_connect")
                        print(f"  [sn-invite] {name}: menu offers no Connect — skipped "
                              f"(menu: {mtxt[:60]})")
                        continue
                    item = None
                    if menu is not None:
                        cand = menu.locator('li, [role="menuitem"], button, div[role="button"], a')
                        for i in range(min(cand.count(), 12)):
                            el = cand.nth(i)
                            try:
                                t = (el.inner_text() or "").strip()
                            except Exception:  # noqa: BLE001
                                continue
                            if re.fullmatch(r"\s*connect\s*", t, re.I):
                                item = el
                                break
                    if item is None:
                        page.keyboard.press("Escape")
                        print(f"  [sn-invite] {name}: Connect item not clickable — skipped")
                        continue
                    item.click(timeout=6_000)
                    # SETTLE, don't sample: one fixed-sleep read could not tell an unpainted
                    # modal shell from a genuine refusal, and skipped the lead either way
                    # (26 'no Send (empty)' skips, 2026-07-12 → 2026-08-03).
                    dlg, snd, txt = _await_invite_dialog(page)
                    if dlg is not None:
                        if snd is None:
                            page.keyboard.press("Escape")
                            if _LIMIT_RE.search(txt):
                                until = _suppress_connect("LinkedIn weekly invitation limit")
                                print("  [stop] LinkedIn weekly invitation limit — ending run; "
                                      f"connect suppressed until {until[:10]}")
                                stats["sent"], stats["reason"] = sent, "weekly_limit"
                                return stats
                            # Name WHICH failure this was, so the next occurrence is
                            # diagnosable from the log alone: words = LinkedIn refused;
                            # still blank after the settle-poll = the modal never painted.
                            print(f"  [sn-invite] {name}: invite dialog had no Send "
                                  f"({txt[:120] or 'still blank after settle-poll'})"
                                  " — skipped, left queued")
                            continue
                        snd.click(timeout=6_000)
                        time.sleep(1.5)
                    # VERIFY from the row menu: only 'Pending' makes it real.
                    btn.click(timeout=6_000)
                    time.sleep(1.2)
                    try:
                        mtxt = " ".join(((menu.inner_text() if menu is not None
                                          else _menu_text(page)) or "").split())
                    except Exception:  # noqa: BLE001
                        mtxt = ""
                    page.keyboard.press("Escape")
                    if re.search(r"pending", mtxt, re.I):
                        _record_invite(L["id"])
                        ops.log_action(AGENT, "connect", target=L["profile_url"], result="ok",
                                       detail="sn-row invite; menu shows Pending")
                        L["_skip"] = True
                        sent += 1
                        print(f"  invited {name} (verified: menu shows Pending)")
                        time.sleep(random.uniform(25, 60) if "--fast" in sys.argv
                                   else safety.next_delay(cfg, sent))
                    else:
                        ops.log_action(AGENT, "connect", target=L["profile_url"],
                                       result="failed", detail="sn-row invite unconfirmed")
                        print(f"  [unconfirmed] {name}: menu does not show Pending — "
                              f"not recorded")
                except Exception as e:  # noqa: BLE001
                    if kb.is_browser_closed_error(e):
                        raise
                    # A crash mid-attempt is not a finished row — the page never got to
                    # answer, so this row is still work and must keep its page workable.
                    finished -= 1
                    print(f"  [sn-invite] {name}: {str(e)[:70]} — skipped")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:  # noqa: BLE001
                        pass
            prefix_end, prefix_intact = _consumed_prefix_step(
                prefix_end, prefix_intact, abs_pg, len(here), len(page_rows), finished)
            # Save the cursor the moment it advances — the walk's common exits
            # (keeper exception, cap-stop mid-page) never reach the end-of-walk
            # save, and a bookmark that only survives a clean exit is no bookmark
            # (proven 2026-07-19: both live passes bypassed it).
            if prefix_end + 1 > cursor:
                cursor = prefix_end + 1
                _sweep_cursor_set(camp_id, cursor, src=src)
                print(f"  [sn-invite] cursor: campaign {camp_id} resumes at "
                      f"page {cursor} from now on")
            if sent >= need:
                stats["reason"] = "quota_filled"
                break
            # PAGINATION HONESTY (rebuild workstream B). Three defects killed the
            # 2026-07-13 walk at page 4/10 with no log line: (1) a transiently-DISABLED
            # Next (LinkedIn disables it while a page loads) read as end-of-list;
            # (2) a Next click that didn't actually advance went undetected (page "4"
            # was page 3 re-read — same lead invitable on both); (3) every failure was
            # a bare silent `break`. Now: settle-recheck the disabled state, verify the
            # first row URN actually CHANGED after the click (retry once), and name the
            # reason for every exit.
            try:   # clear any lingering menu/dialog overlay before touching Next
                page.keyboard.press("Escape")
                time.sleep(0.4)
            except Exception:  # noqa: BLE001
                pass
            first_before = lead_urn(page_rows[0].get("href") or page_rows[0].get("id") or "") \
                if page_rows else None
            # Surface the pager BEFORE reading Next: Sales Nav's virtualised list drops the
            # Next button from the DOM until the bottom scrolls into view. _fast_forward
            # scrolls first; the main-loop pagination did NOT — so a resumed/deep page falsely
            # read "no Next button — end of results" with 90+ pages still to go (2026-07-23:
            # cursor fast-forwarded to page 9, then instantly false-ended and sent 0).
            for _s in range(12):
                try:
                    if not page.evaluate(_SCROLL_RESULTS_JS):
                        break
                except Exception:  # noqa: BLE001
                    page.mouse.wheel(0, 1600)
                time.sleep(0.35)
            advanced = False
            for attempt in (1, 2):
                try:
                    nxt = page.get_by_role("button", name=re.compile(r"^next$", re.I))
                    if not nxt.count():
                        stats["reason"] = "end_of_search"
                        print(f"  [sn-invite] page {page_offset + pg}: no Next button — end of results")
                        break
                    if not nxt.first.is_enabled():
                        # transient-disabled while the page loads — recheck patiently
                        # (2026-07-14: one 2.5s recheck called page 1 of a deep search
                        # "end of results" and the 40-quota run sent 1)
                        for _ in range(4):
                            time.sleep(4.0)
                            if nxt.first.is_enabled():
                                break
                        if not nxt.first.is_enabled():
                            stats["reason"] = "end_of_search"
                            print(f"  [sn-invite] page {page_offset + pg}: Next disabled after settle — "
                                  "end of results")
                            break
                    nxt.first.click()
                    stats["page_loads"] += 1
                    time.sleep(random.uniform(2.5, 4.0))
                    _wait_rows_settled(page)
                    try:
                        first_now = page.locator('a[href*="/sales/lead/"]').first \
                            .get_attribute("href")
                        first_now = lead_urn(first_now or "")
                    except Exception:  # noqa: BLE001
                        first_now = None
                    if first_before and first_now == first_before:
                        print(f"  [sn-invite] page {page_offset + pg}: Next click did not advance "
                              f"(attempt {attempt})")
                        continue   # retry the click once
                    advanced = True
                    break
                except Exception as e:  # noqa: BLE001
                    if kb.is_browser_closed_error(e):
                        raise
                    print(f"  [sn-invite] page {page_offset + pg}: Next failed ({str(e)[:60]})")
            if not advanced:
                if stats["reason"] not in ("end_of_search",):
                    stats["reason"] = "pagination_failure"
                    print(f"  [sn-invite] walk ended at page {page_offset + pg}: PAGINATION FAILURE — "
                          "the search would not advance")
                break
        else:
            stats["reason"] = "max_pages"
            print(f"  [sn-invite] walk ended: max sweep pages reached ({max_pages})")
        # Persist the cursor: the next run (and any re-open below) starts past the
        # pages this walk PROVED fully consumed — the whole point of collection
        # (the operator 2026-07-19: "remember where the campaign got up to").
        if prefix_end + 1 > cursor:
            _sweep_cursor_set(camp_id, prefix_end + 1, src=src)
            print(f"  [sn-invite] cursor advanced: campaign {camp_id} resumes at "
                  f"page {prefix_end + 1} next run")
        # ANY end_of_search short of quota is a wedged-Next suspect (2026-07-19:
        # pg-6/7 'ends' on ~100-page searches); one fresh open tells a flake from a
        # real end, and the re-open now RESUMES at the page the walk died on.
        suspicious_end = _end_is_suspicious(stats["reason"], sent, need)
        if ((stats["reason"] == "pagination_failure" or suspicious_end) and sent < need
                and stats["page_loads"] < budget
                and _may_reopen(reopens.get(src, 0), src in proven_end)):
            reopens[src] = reopens.get(src, 0) + 1
            resume_pg[src] = max(last_abs, 1)
            work.append((src, group))
            print(f"  [sn-invite] stuck-page self-heal ({stats['reason']}): re-queueing "
                  f"this search for a fresh open at page {resume_pg[src]} "
                  f"(re-open {reopens[src]}/2)")
        elif stats["reason"] == "end_of_search" and sent < need:
            # The end stands — either already proven by an empty re-open, or the re-open
            # budget is spent. Say the thing that actually decides what happens next: a
            # spent search is not a lane fault and no retry refills it.
            note = _exhaustion_note(camp_id, "end_of_search")
            if note:
                print(f"  [sn-invite] {note}")
    stats["sent"] = sent
    return stats


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def audit() -> None:
    """READ-ONLY composition walk of a campaign's live search (plan V3 workstream E).

    Per page: how many rows are consumed / queued-invitable / fresh (not in DB) /
    connections. No invites, no lead inserts — the only write is the
    last_seen_on_search_at stamp on rows we meet. This is the diagnostic that would have
    named the 2026-07-13 failure in one run, and the live-DOM harness for the walk code
    (Gate 1 of the rollout)."""
    from . import nav
    from .salesnav import (campaign_source, lead_urn, _SCROLL_RESULTS_JS,
                           _wait_rows_settled, _collect_lead_rows)
    cfg = Config.load()
    camp_name = _arg_str("--campaign")
    max_pages = _arg_int("--pages") or int(getattr(cfg, "connect_max_sweep_pages", 25))
    with db.connect() as conn:
        q = ("SELECT c.id, c.name FROM campaigns c WHERE EXISTS "
             "(SELECT 1 FROM leads l WHERE l.campaign_id=c.id)")
        camps = [dict(r) for r in conn.execute(q)]
    if camp_name:
        camps = [c for c in camps if c["name"] == camp_name]
    targets = [(c, campaign_source(c["id"])) for c in camps]
    targets = [(c, s) for c, s in targets if s and "/sales/" in s]
    if not targets:
        print(f"[audit] no Sales Nav campaign found{' named ' + camp_name if camp_name else ''}.")
        return
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=300, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        try:
            kb.stop_keeper()
            kb.ensure_keeper(wait_sec=120)
        except Exception as e:  # noqa: BLE001
            print(f"  [keeper] pre-run restart failed ({str(e)[:60]}) — continuing")
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                for camp, src in targets:
                    print(f"\n=== AUDIT: campaign '{camp['name']}' (id {camp['id']}) ===")
                    opened = False
                    for attempt, url in enumerate((src, None, nav._strip_session_id(src)), 1):
                        try:
                            if url is None:
                                page.reload(wait_until="domcontentloaded", timeout=45_000)
                            else:
                                if "/sales/" in (page.url or "") and "#" in url:
                                    page.goto(nav.FEED, wait_until=nav._NAV_WAIT, timeout=30_000)
                                    time.sleep(random.uniform(1.5, 3.0))
                                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        except Exception as e:  # noqa: BLE001
                            print(f"  [audit] open attempt {attempt} failed ({str(e)[:50]})")
                            continue
                        if _wait_rows_settled(page):
                            opened = True
                            break
                        time.sleep(8)
                    if not opened:
                        print("  [audit] search would not render rows — RENDER FAILURE")
                        continue
                    tot = {"rows": 0, "queued": 0, "fresh": 0, "consumed": 0,
                           "connection": 0}
                    end_reason = "max_pages"
                    for pg in range(1, max_pages + 1):
                        if pg > 1 and not _wait_rows_settled(page):
                            end_reason = "render_failure"
                            break
                        prev = -1
                        for _ in range(12):
                            n = page.locator('a[href*="/sales/lead/"]').count()
                            moved = False
                            try:
                                moved = bool(page.evaluate(_SCROLL_RESULTS_JS))
                            except Exception:  # noqa: BLE001
                                page.mouse.wheel(0, 1600)
                            time.sleep(0.7)
                            if not moved and n == prev:
                                break
                            prev = n
                        try:
                            page_rows = _collect_lead_rows(page) or []
                        except Exception:  # noqa: BLE001
                            page_rows = []
                        comp = {"queued": 0, "fresh": 0, "consumed": 0, "connection": 0}
                        with db.connect() as conn:
                            for row in page_rows:
                                u = lead_urn(row.get("href") or row.get("id") or "")
                                if not u:
                                    continue
                                dbrow = conn.execute(
                                    "SELECT id, status, COALESCE(is_connection,0) AS "
                                    "is_connection FROM leads WHERE profile_url LIKE ?",
                                    (f"%{u}%",)).fetchone()
                                dbrow = dict(dbrow) if dbrow else None
                                if dbrow:
                                    conn.execute("UPDATE leads SET last_seen_on_search_at=? "
                                                 "WHERE id=?", (_now(), dbrow["id"]))
                                v = classify_search_row(dbrow, True)
                                comp["fresh" if v == "fresh" else
                                     "queued" if v == "invite" else
                                     "connection" if v == "connection" else "consumed"] += 1
                        tot["rows"] += len(page_rows)
                        for k in comp:
                            tot[k] += comp[k]
                        print(f"  page {pg:>3}: {len(page_rows):>3} rows — "
                              f"{comp['queued']:>3} queued  {comp['fresh']:>3} fresh  "
                              f"{comp['consumed']:>3} consumed  {comp['connection']:>2} conn")
                        first_before = lead_urn(page_rows[0].get("href") or
                                                page_rows[0].get("id") or "") if page_rows else None
                        advanced = False
                        for attempt in (1, 2):
                            try:
                                nxt = page.get_by_role("button", name=re.compile(r"^next$", re.I))
                                if not nxt.count():
                                    end_reason = "end_of_search"
                                    break
                                if not nxt.first.is_enabled():
                                    time.sleep(2.5)
                                    if not nxt.first.is_enabled():
                                        end_reason = "end_of_search"
                                        break
                                nxt.first.click()
                                time.sleep(random.uniform(2.5, 4.0))
                                _wait_rows_settled(page)
                                try:
                                    fn = page.locator('a[href*="/sales/lead/"]').first \
                                        .get_attribute("href")
                                    fn = lead_urn(fn or "")
                                except Exception:  # noqa: BLE001
                                    fn = None
                                if first_before and fn == first_before:
                                    continue
                                advanced = True
                                break
                            except Exception as e:  # noqa: BLE001
                                if kb.is_browser_closed_error(e):
                                    raise
                        if not advanced:
                            if end_reason == "max_pages":
                                end_reason = "pagination_failure"
                            break
                    with db.connect() as conn:
                        unseen = conn.execute(
                            "SELECT COUNT(*) FROM leads WHERE campaign_id=? AND "
                            "status='collected' AND last_seen_on_search_at IS NULL",
                            (camp["id"],)).fetchone()[0]
                    print(f"\n  [audit] '{camp['name']}': {tot['rows']} rows over the walk — "
                          f"{tot['queued']} queued-invitable, {tot['fresh']} FRESH (not in DB), "
                          f"{tot['consumed']} consumed, {tot['connection']} connections. "
                          f"Walk ended: {end_reason}.")
                    print(f"  [audit] queue reality: {unseen} 'collected' lead(s) of this "
                          f"campaign were NOT seen on the search (drift candidates — "
                          f"see --mark-stale).")
            finally:
                try:
                    pg0 = ctx.pages[0] if ctx.pages else None
                    if pg0 is not None and "/sales/" in (pg0.url or ""):
                        pg0.goto(nav.FEED, wait_until=nav._NAV_WAIT, timeout=20_000)
                except Exception:  # noqa: BLE001
                    pass
                safe_close(ctx)


def mark_stale() -> None:
    """Move drift casualties out of the live queue (plan V3 workstream D): 'collected'
    leads older than --older-than days (default 30) never seen on their campaign's live
    search since stamping began -> status='stale'. Reversible (status only, never a
    delete); is_connection rows untouched. Run AFTER an audit/sweep has stamped
    last_seen_on_search_at, or the verdict is meaningless."""
    days = _arg_int("--older-than") or 30
    camp_name = _arg_str("--campaign")
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = ("SELECT l.id FROM leads l LEFT JOIN campaigns c ON l.campaign_id=c.id "
         "WHERE l.status='collected' AND l.last_seen_on_search_at IS NULL "
         "AND l.created_at < ?")
    params: list = [cutoff]
    if camp_name:
        q += " AND c.name = ?"
        params.append(camp_name)
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute(q, params)]
        before = conn.execute("SELECT COUNT(*) FROM leads WHERE status='collected'").fetchone()[0]
        if "--commit" not in sys.argv:
            print(f"[dry-run] {len(ids)} lead(s) would be marked stale "
                  f"(collected >{days}d ago, never seen on the live search). "
                  f"Re-run with --commit to apply.")
            return
        now = _now()
        for i in ids:
            conn.execute("UPDATE leads SET status='stale', updated_at=? WHERE id=?", (now, i))
        after = conn.execute("SELECT COUNT(*) FROM leads WHERE status='collected'").fetchone()[0]
    db.log_event("stale-marked", None, f"{len(ids)} leads (> {days}d, unseen on search)")
    print(f"[done] marked {len(ids)} lead(s) stale. collected: {before} -> {after}.")


def dry_run() -> None:
    cfg = Config.load()
    n = _arg_int("--max") or 10
    leads = _queue_leads(n, _arg_str("--campaign"))
    ok, why = safety.can_act("connect", cfg)
    print(f"Connect queue (status='collected'): next {len(leads)}")
    print(f"Safety gate for connect right now: {'ALLOW' if ok else 'BLOCK — ' + why}\n")
    for L in leads:
        print(f"  {L['full_name'] or '?':<28} {str(L.get('title') or '')[:22]:<22} {L['profile_url']}")
    print("\n[dry-run] no requests sent. Commit is gated by enabled + dry_run + weekly cap.")


def _competing_connect_pid(procs, me_pid, me_create, kin=frozenset()):
    """Return the pid of a GENUINE pre-existing competing `connect --commit` lane, or None.

    `procs`: iterable of (pid, cmdline_str, create_time_or_None).

    A process is NOT a competitor when it is my own process (`me_pid`), part of my own
    process TREE (`kin` — ancestors/descendants), or born within ~2s of me. That last case
    is the fix for the 2026-07-22 `duplicate_lane` false-positive: launching via
    `python -m engine connect --commit` leaves a transient LAUNCHER TWIN carrying an
    identical command line; the old guard (skip only me.pid) counted the twin as "another
    lane" and self-refused EVERY manual run. Only an unrelated process that meaningfully
    PREDATES me is a real competing lane. If a process's create_time is unknown we fall back
    to cmdline+tree matching (conservative: the file lock serialises any true double-run)."""
    for pid, cmd, ctime in procs:
        if pid == me_pid or pid in kin:
            continue
        if not ("engine" in cmd and "connect" in cmd and "--commit" in cmd):
            continue
        if ctime is not None and me_create is not None and ctime >= me_create - 2.0:
            continue   # born with me → my own twin/child, not a pre-existing lane
        return pid
    return None


def commit() -> None:
    cfg = Config.load()
    if not cfg.enabled or cfg.dry_run:
        print(f"[refused] enabled={cfg.enabled} dry_run={cfg.dry_run} — showing dry-run:\n")
        dry_run()
        return
    # SINGLE-INSTANCE guard: two connect lanes on one keeper = every menu read fails and
    # the walk poisons the queue (2026-07-13: a kill left an orphaned lane running; the
    # relaunch then fought it for the browser). Refuse loudly instead — but never on a
    # launcher TWIN of my own invocation (2026-07-22 duplicate_lane false-positive fix).
    try:
        import psutil
        me = psutil.Process()
        try:
            me_create = me.create_time()
        except Exception:  # noqa: BLE001
            me_create = None
        kin = set()   # my own process tree — a launcher twin / worker child is not a rival
        try:
            kin |= {a.pid for a in me.parents()}
        except Exception:  # noqa: BLE001
            pass
        try:
            kin |= {c.pid for c in me.children(recursive=True)}
        except Exception:  # noqa: BLE001
            pass
        procs = []
        for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
            info = p.info
            procs.append((info.get("pid"), " ".join(info.get("cmdline") or []),
                          info.get("create_time")))
        dup = _competing_connect_pid(procs, me.pid, me_create, kin)
        if dup is not None:
            print(f"[refused] another connect lane is already running (pid {dup}) — "
                  "one keeper, one lane. Kill it or wait for it to finish.")
            from . import emit_result
            emit_result("connect", False,
                        f"Another connect run is already active (pid {dup})",
                        reason="duplicate_lane")
            return
    except ImportError:
        pass
    until = _suppressed_until()
    if until:
        msg = (f"connect is suppressed until {until[:10]} — LinkedIn's weekly invitation "
               "limit was hit; running again before it resets only burns page loads.")
        print(f"[suppressed] {msg}")
        from . import emit_result
        emit_result("connect", False, msg, count=0, requested=_arg_int("--max") or 10,
                    reason="weekly_limit_suppressed")
        return
    max_n = _arg_int("--max") or 10
    leads = _queue_leads(max_n * 4, _arg_str("--campaign"))   # over-fetch; some may be already-connected
    # CHEAPEST PATH FIRST (2026-07-11). Leads that already hold a public /in/ URL need NO
    # resolving — put them first, and skip the Sales Nav resolve pass entirely when they can
    # fill the batch. The old order did the opposite: it opened the heavy Sales Nav search
    # first (for the over-fetched /sales/lead/ stragglers), which wedged the browser so badly
    # that the READY leads' profile navigations then timed out. Live 2026-07-11: a batch whose
    # every target was already /in/ still went to Sales Nav first and sent nothing.
    leads.sort(key=lambda L: "/sales/lead/" in (L.get("profile_url") or ""))
    ready = sum(1 for L in leads if "/sales/lead/" not in (L.get("profile_url") or ""))
    skip_resolve = ready >= max_n
    if skip_resolve:
        print(f"[queue] {ready} lead(s) already have public profiles — Sales Nav not needed this run")
    sent = 0
    stopped: str | None = None   # set on a run-ending refusal (weekly invite limit)
    sweep: dict = {}             # the sn-invite sweep's stats (run contract, plan V3 §C)
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=300, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        # Start every commit run on a FRESH keeper. Empirical rule of 2026-07-11/12:
        # Sales Nav renders reliably on a freshly-started keeper and degrades on a
        # long-lived one (blank search / 'Trouble loading' panels); every failed open
        # that night was cured by a keeper restart and none recurred after one. Cookies
        # persist on disk, so login survives; we hold the browser lock, so no lane is
        # interrupted. ~10s cost buys the unattended daily run its reliability.
        try:
            kb.stop_keeper()
            kb.ensure_keeper(wait_sec=120)
        except Exception as e:  # noqa: BLE001
            print(f"  [keeper] pre-run restart failed ({str(e)[:60]}) — continuing on the existing keeper")
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # PASS A (root-cause fix 2026-07-07): resolve /sales/lead/ ids to public
                # /in/ URLs ONCE for the whole batch, so the connect loop below never
                # re-opens the 6KB search per lead. Tolerant of a keeper death via the
                # shared reattach helper; on any failure the per-lead fallback still runs.
                # /sales/lead/ leads: invite straight from the search rows (2026-07-11
                # redesign — one page load, native Connect item, menu-Pending verify).
                # The old resolve-to-/in/ pass is retired from the send path: its panel
                # extraction no longer matches the SN DOM, and its per-lead paging was
                # both the 'stuck in Sales Nav' symptom and the request-volume that
                # earned the account a 'too many requests' throttle.
                try:
                    if not skip_resolve:
                        sweep = _invite_from_search(page, leads, max_n - sent, cfg)
                except Exception as e:  # noqa: BLE001
                    if kb.is_browser_closed_error(e):
                        newctx = kb.reattach(pw, ctx)
                        if newctx is not None:
                            ctx = newctx
                            page = ctx.pages[0] if ctx.pages else ctx.new_page()
                            try:
                                sweep = _invite_from_search(page, leads, max_n - sent, cfg)
                            except Exception:  # noqa: BLE001
                                pass   # profile-path loop below covers stragglers
                    else:
                        print(f"  [resolve] batch resolve skipped: {str(e)[:80]}")
                sent += sweep.get("sent", 0)
                if sweep.get("reason") == "weekly_limit":
                    stopped = ("LinkedIn's weekly invitation limit is reached — connect "
                               f"suppressed until it resets.")
                for idx, L in enumerate(leads):
                    if sent >= max_n:
                        break
                    if L.get("_skip"):
                        continue   # merged duplicate / existing connection — never invite
                    ok, why = safety.can_act("connect", cfg)
                    if not ok:
                        print(f"[stop] safety gate: {why}")
                        break
                    # keeper-stability: wrap the per-lead work so ONE keeper death reattaches
                    # + retries THIS lead instead of cascading the whole batch. `break` exits
                    # this inner retry loop (lead handled/skipped); only a browser-closed error
                    # with a retry left `continue`s to re-drive the lead. Selector/click logic
                    # below is UNCHANGED — this only wraps the loop with detect->reattach->retry.
                    attempts = 0
                    while True:
                        try:
                            target = L["profile_url"]
                            # /sales/lead/ leads are handled by _invite_from_search above
                            # (native row-menu Connect). The old per-lead fallback — human_open
                            # the search + open_lead_panel + public_url_from_panel — is RETIRED:
                            # its panel extraction no longer matches the SN DOM (0/11 on a clean
                            # session, 2026-07-11) and its paging was the 'stuck in Sales Nav'
                            # grind. A lead the row-menu pass couldn't find stays 'collected'
                            # and is retried next run; it must never drag the batch back into
                            # the search here.
                            if "/sales/lead/" in target:
                                print(f"  [skip] {L['full_name']}: not reached via the search "
                                      f"rows this run (left collected)")
                                break
                            # "commit", not "domcontentloaded" — see the note on the probe goto
                            # above. A profile page that renders fine can still never fire
                            # domcontentloaded, burning 45s per lead and starving the batch.
                            # Imported locally: `nav` above is bound only on the /sales/lead/
                            # branch, and this line also runs for plain /in/ leads.
                            # One retry: the FIRST nav after the resolve pass leaves the Sales
                            # Nav SPA sporadically times out (live 2026-07-11: 2 of 4 leads),
                            # then the very next nav works — don't lose a lead to that.
                            from . import nav as _nav
                            try:
                                page.goto(target, wait_until=_nav._NAV_WAIT, timeout=25_000)
                            except Exception:
                                time.sleep(3)
                                page.goto(target, wait_until=_nav._NAV_WAIT, timeout=25_000)
                            time.sleep(random.uniform(2.5, 4.5))
                            ops.log_action(AGENT, "profile_view", target=target, result="ok")
                            status = _send_connect(page, cfg, L)
                            if status == "sent":
                                # verify-after-action: only record the invite if the profile
                                # confirms Pending (was fire-and-assume).
                                if _verify_pending(page):
                                    _record_invite(L["id"])
                                    ops.log_action(AGENT, "connect", target=L["profile_url"], result="ok")
                                    sent += 1
                                    print(f"  invited {L['full_name']}")
                                    time.sleep(random.uniform(25, 60) if "--fast" in sys.argv
                                               else safety.next_delay(cfg, idx))
                                else:
                                    ops.log_action(AGENT, "connect", target=L["profile_url"],
                                                   result="failed", detail="invite not confirmed (no Pending)")
                                    print(f"  [unconfirmed] {L['full_name']}: no Pending after send "
                                          "— not recorded (left collected to retry)")
                            elif status == "weekly_limit":
                                ops.log_action(AGENT, "connect", target=L["profile_url"],
                                               result="failed", detail="LinkedIn weekly invitation limit")
                                until = _suppress_connect("LinkedIn weekly invitation limit")
                                stopped = ("LinkedIn's weekly invitation limit is reached — invites "
                                           f"won't send until it resets. Connect suppressed until "
                                           f"{until[:10]}. Stopping this run.")
                                print(f"  [stop] {stopped}")
                            elif status in ("email_required", "send_dialog_missing"):
                                ops.log_action(AGENT, "connect", target=L["profile_url"],
                                               result="failed", detail=status)
                                print(f"  [skip] {L['full_name']}: {status} (left collected)")
                            else:
                                _update_status(L["id"], status)
                                print(f"  [skip] {L['full_name']}: {status}")
                            break   # lead handled — next lead
                        except Exception as e:  # noqa: BLE001
                            if kb.is_browser_closed_error(e) and attempts < 1:
                                attempts += 1
                                newctx = kb.reattach(pw, ctx)
                                if newctx is not None:
                                    ctx = newctx
                                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                                    print(f"  [reattach] keeper died mid-batch — retrying {L['full_name']}")
                                    continue   # retry THIS lead on the fresh keeper
                            ops.log_action(AGENT, "connect", target=L["profile_url"],
                                           result="failed", detail=str(e)[:120])
                            print(f"  [skip] {L['full_name']}: {str(e)[:80]}")
                            break
                    if stopped:
                        break   # run-ending refusal — no point driving more leads
            finally:
                # Park the keeper on the FEED before handing it back. The keeper window is
                # visible on the desktop; a finished run that leaves it sitting on the Sales
                # Nav search reads as "stuck in Sales Nav AGAIN" (the operator, 2026-07-12) even
                # when every invite sent. A parked feed = visibly done. Also kinder state
                # for the NEXT run: navigating out of the SN SPA is exactly what the lane
                # would do first anyway.
                try:
                    from . import nav as _nav
                    pg = ctx.pages[0] if ctx.pages else None
                    if pg is not None and "/sales/" in (pg.url or ""):
                        pg.goto(_nav.FEED, wait_until=_nav._NAV_WAIT, timeout=20_000)
                except Exception:  # noqa: BLE001
                    pass
                safe_close(ctx)
    # THE RUN CONTRACT (plan V3 workstream C): a shortfall is a first-class result. The
    # 2026-07-13 failure reported "sent 2, ok, exit 0" on a 40-quota run — never again.
    reason = ("weekly_limit" if stopped else
              "quota_filled" if sent >= max_n else
              sweep.get("reason") or "profile_path_exhausted")
    if sent < max_n:
        print(f"\n[shortfall] sent {sent}/{max_n} — reason: {reason} "
              f"(pages walked: {sweep.get('pages_walked', 0)}, "
              f"fresh inserted: {sweep.get('fresh', 0)}, "
              f"consumed rows met: {sweep.get('consumed', 0)})")
    print(f"\n[done] sent {sent} connection request(s)." + (f" STOPPED: {stopped}" if stopped else ""))
    from . import emit_result
    ok = (not stopped) and sent >= max_n
    msg = (f"Sent {sent}, then stopped: {stopped}" if stopped
           else f"Sent {sent} of {max_n} requested — {reason}" if sent < max_n
           else f"Sent {sent} connection request(s)")
    emit_result("connect", ok, msg, count=sent, requested=max_n, reason=reason,
                pages_walked=sweep.get("pages_walked", 0), fresh=sweep.get("fresh", 0),
                consumed=sweep.get("consumed", 0))


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--probe-panel" in sys.argv:
        probe_panel()
    elif "--probe" in sys.argv:
        probe()
    elif "--audit" in sys.argv:
        audit()
    elif "--mark-stale" in sys.argv:
        mark_stale()
    elif "--commit" in sys.argv:
        commit()
    else:
        dry_run()


if __name__ == "__main__":
    main()
