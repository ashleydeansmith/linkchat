"""accept_sync.py — connection-sync / accept-detection lane.

Detects which of OUR outstanding invites have been accepted, so the pipeline can
advance 'invited' -> 'accepted' (Connected) and the message-on-accept sequence
(WP5) can fire. This is the lane that makes the flow view's Connected box real.

Method — two evidence classes, cheapest first:

  PRIMARY (bulk, free): diff our leads at status='invited' against LinkedIn's
    current Sent-invitations list (reusing the withdraw lane's loader). A lead we
    invited that is NO LONGER pending has either been accepted, expired, or
    withdrawn by us. We never record withdrawals in the DB, so we distinguish by
    OUR OWN invited_at age:
      • left pending AND invited < withdraw_after_days old  -> ACCEPTED (confident)
      • left pending AND older                              -> AMBIGUOUS (could be
        our own later withdrawal or a LinkedIn expiry) -> only accepted with --confirm.

  CONFIRM (per-profile, budgeted, opt-in via --confirm): visit the ambiguous
    profiles and read the profile action buttons (connect.py STATUS_JS); a
    'Message' button present = connected.

--probe locks the pending-list DOM read-only first. --commit is triple-gated
(enabled + dry_run=false + --commit) and paced; the bulk diff itself costs one
scrape, confirmations cost profile_view budget.

Matching an invited lead to a pending-list row is by canonical /in/ URL first,
falling back to normalised name (sales-nav-collected leads carry a /sales/lead/
URL until connect resolves the /in/, so name is the bridge).

IDENTITY, AND WHY THE NAME BRIDGE IS NOT ENOUGH (2026-07-31)
    `connect._invite_from_search` — the 2026-07-11 Sales-Nav row-menu redesign —
    invites straight off the search row and never resolves a public profile, so
    every lead invited since 2026-07-14 holds ONLY a /sales/lead/ URN: 0 of 316,
    measured. `_norm_url` of a /sales/lead/ URL can never be an element of a set
    of /in/ slugs, so the URL arm was dead for the whole cohort and the entire
    population was judged by exact display-string equality across two LinkedIn
    surfaces that render names differently. A name that renders differently read
    as "left the pending list" -> probable accept -> the connections truth-test
    (keyed the same way) correctly refused to confirm -> HELD, forever, re-entering
    the same false computation every run. Accepts marked between 2026-07-14 and
    2026-07-31: zero. Held pile: 2 -> 15 -> 27 -> 61 -> 90 -> 142.

    So the pending list is now read for what it actually is: a free identity
    source. Every card carries the person's public /in/ URL, and a lead we match
    to a card gets that URL stamped on it (`_harvest_identity`), which means the
    day they DO accept we confirm them by identity rather than by display string —
    and the message lane can reach them at all (a /sales/lead/-only accept can
    never be messaged).

    The two comparisons are DELIBERATELY ASYMMETRIC:
      * pending membership (`_match_pending`) tolerates decoration, because a
        false "still pending" only delays an accept by one run and can never put
        a message in front of anybody;
      * the connections truth-test (`_match_connection`) stays STRICT — URL
        identity, else exact name — because a false match there messages a
        stranger. That is the 2026-07-12 Milica false-accept and it is pinned by
        tests/test_accept_sync_name_only_match.py.
"""
from __future__ import annotations

import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

from . import ops
import linkedin_browser as lb

from . import db, emit_result, safe_close

AGENT = "engine-accept-sync"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_url(u: str | None) -> str:
    if not u:
        return ""
    u = u.split("?")[0].rstrip("/").lower()
    m = re.search(r"/in/([^/]+)", u)
    return m.group(1) if m else u


def _norm_name(n: str | None) -> str:
    return re.sub(r"\s+", " ", (n or "").strip().lower())


# Decoration classes observed on the live cohort of 481 invited leads: 11 emoji /
# pictograph names ("☎️Peter Chen🦙", "Hannah Wright 🔜 Develop Brighton"), 7 with a
# credential or job title appended after a comma ("Teddy James, CAIA", "Harsh Divecha,
# Digital Workplace Specialist"), and parenthesised nicknames ("Sachintha (Sachy)
# Abeyrathne"). Sales Navigator and the sent-invitations card do not always render the
# same set, so the same human arrives under two strings.
_BRACKETED = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_CRED_TAIL = re.compile(r",.*$", re.S)


def _name_key(n: str | None) -> str:
    """A display name reduced to the part both LinkedIn surfaces render the same way.

    USED ON THE PENDING SIDE ONLY. Loosening here is fail-closed: it can only make a
    lead look STILL PENDING, which delays an accept by one run and messages nobody.
    `_match_connection` deliberately does NOT use this — see the module docstring.
    """
    s = _norm_name(n)
    s = _BRACKETED.sub(" ", s)
    s = _CRED_TAIL.sub("", s)
    # Drop pictographs/emoji (So), modifier symbols (Sk) and the joiners/variation
    # selectors that glue them together (Cf); keep letters, marks and spacing.
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("So", "Sk", "Cf"))
    s = re.sub(r"[^\w\s'\-.]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip(" .-'")


def _age_days(invited_at: str | None) -> float | None:
    if not invited_at:
        return None
    try:
        dt = datetime.fromisoformat(invited_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return None


def _invited_leads() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, profile_url, full_name, invited_at FROM leads WHERE status='invited'")]


def _mark_accepted(lead_id: int, evidence: str) -> None:
    now = _now()
    with db.connect() as conn:
        conn.execute("UPDATE leads SET status='accepted', accepted_at=?, last_action_at=?, "
                     "updated_at=? WHERE id=?", (now, now, now, lead_id))
        conn.execute("UPDATE invites SET status='accepted' WHERE lead_id=? AND status='pending'",
                     (lead_id,))
        conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                     (now, lead_id, "accepted", evidence))
    # comms mesh (fire-and-forget, never blocks): connection accepted -> CRM
    try:
        from . import mesh
        mesh.emit("connect-engine", "crm-writer", summary=f"accepted: {evidence}")
    except Exception:
        pass
    # WP5 hook: enrol the freshly-accepted lead into its campaign's message sequence.
    try:
        from . import sequence
        sequence.enrol_on_accept(lead_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pending-list read (reuses the withdraw loader)
# ---------------------------------------------------------------------------

def _load_pending(page) -> list[dict]:
    from .withdraw import _load_all, _read_invites
    _load_all(page)
    return _read_invites(page)


def _match_pending(lead: dict, pending: list[dict]) -> dict | None:
    """The sent-invitation row this lead is still sitting on, or None if it has left.

    Identity first: when the lead already holds a public /in/ URL, an exact URL match is
    proof and wins outright. Only then do we fall back to the name bridge, and that
    bridge compares decoration-stripped keys (`_name_key`) rather than raw strings —
    the seam that judged 316 identity-less leads by display string alone.

    Returns the ROW, not a bool, because the row carries the public /in/ URL we harvest.
    """
    lead_url = _norm_url(lead.get("profile_url"))
    has_in = "/in/" in (lead.get("profile_url") or "")
    if has_in and lead_url:
        for r in pending:
            if r.get("profile_url") and _norm_url(r["profile_url"]) == lead_url:
                return r
    key = _name_key(lead.get("full_name"))
    if not key:
        return None
    for r in pending:
        if _name_key(r.get("name")) == key:
            return r
    return None


def _harvest_identity(lead: dict, row: dict | None) -> str | None:
    """Stamp the public /in/ URL the pending card just gave us onto an identity-less lead.

    Free — the card was already read this run, no extra page load, no LinkedIn action.
    Returns the URL stored, or None when there was nothing to do (no /in/ on the card,
    the lead already has one) or when the person turned out to be a row we already hold
    (connect._store_public_url merges; that surviving row owns the human).

    Does NOT touch lead status: a still-pending lead stays 'invited'.
    """
    if not row:
        return None
    if "/in/" not in (row.get("profile_url") or ""):
        return None
    if "/in/" in (lead.get("profile_url") or ""):
        return None
    from . import canon
    url = canon.canon_in(row["profile_url"])
    if not url:
        return None
    from .connect import _store_public_url
    return url if _store_public_url(lead["id"], url) else None


def _truth_window(n_candidates: int) -> int:
    """How many recently-added connection cards the truth-test must read.

    A window smaller than the candidate set cannot resolve the set: the check asks "is
    any of my N candidates among the most recent K connections", and a genuine accept
    that happened before those K stays invisible however correct the matching is. The
    lane ran a fixed K=150 while the candidate set grew 12 -> 15 -> 27 -> 61 -> 90 -> 142
    (and two runs read only 70 and 80 cards). The list also contains connections we never
    invited, so the window is a multiple of the batch, and it is capped because walking a
    6,000-card list is a long scroll for a daily lane.
    """
    return max(150, min(600, int(n_candidates) * 3))


def _recent_connections(page, max_cards: int = 150) -> list[dict]:
    """Read the first ~max_cards cards of the Connections page (sorted recently-added).
    THE TRUTH-TEST for accepts (plan V3 workstream G): absence-from-pending alone is NOT
    acceptance — invites also leave the pending list on expiry or withdrawal (Milica M.
    was false-marked accepted TWICE, 2026-07-12 and -13, and auto-enrolled for messaging
    both times). A fresh accept must appear near the top of the connections list; the
    card also carries the public /in/ URL we harvest for the message lane."""
    from . import nav
    from .connections import (CONNECTIONS_URL, READ_JS, _canon_in,
                              _click_show_more, _scroll_step)
    out, seen = [], set()
    try:
        page.goto(CONNECTIONS_URL, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_selector('a[href*="/in/"]', timeout=25_000)
    except Exception as e:  # noqa: BLE001
        print(f"  [truth-test] connections page would not load ({str(e)[:60]}) — "
              "NO accepts will be marked this run")
        return out
    time.sleep(2.5)
    # Scroll with the connections lane's OWN machinery — the list only extends via its
    # 'Show more results' button + container scroll; a bare mouse-wheel plateaus at the
    # first ~20 cards (live 2026-07-14: 20/300 read before this reused _click_show_more).
    stale = 0
    for i in range(120):
        try:
            chunk = page.evaluate(READ_JS) or []
        except Exception:  # noqa: BLE001
            chunk = []
        grew = 0
        for c in chunk:
            url = _canon_in(c.get("href"))
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "name": _norm_name(c.get("name")),
                        "raw_name": (c.get("name") or "").strip()})
            grew += 1
        if len(out) >= max_cards:
            break
        if not grew:
            stale += 1
            if _click_show_more(page):
                stale = 0
            elif stale >= 4:
                break   # genuine end of list
        else:
            stale = 0
        _scroll_step(page, i)
        time.sleep(0.9)
    return out


def _match_connection(lead: dict, recent: list[dict]) -> str | None:
    """The lead's public /in/ URL if it appears in the recent-connections window —
    by canonical URL when the lead already holds one, else by normalised name."""
    lead_url = _norm_url(lead.get("profile_url"))
    lead_name = _norm_name(lead.get("full_name"))
    for c in recent:
        if lead_url and "/in/" in (lead.get("profile_url") or "") and \
                _norm_url(c["url"]) == lead_url:
            return c["url"]
        if lead_name and c["name"] == lead_name:
            return c["url"]
    return None


def probe() -> None:
    from .withdraw import _open
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx, page = _open(pw)
            try:
                pending = _load_pending(page)
                print(f"pending invites currently on LinkedIn: {len(pending)}")
                for r in pending[:10]:
                    print(f"  {r.get('name') or '?':<28} {r.get('profile_url') or ''}")
                ops.log_action(AGENT, "scrape", target="sent-invites", result="ok")
            finally:
                safe_close(ctx)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(max_n: int, dry_run: bool, confirm_each: bool) -> None:
    from .config import Config
    from . import safety
    from .withdraw import _open
    cfg = Config.load()
    if not dry_run and (not cfg.enabled or cfg.dry_run):
        print(f"[refused] enabled={cfg.enabled} dry_run={cfg.dry_run} — showing dry-run:\n")
        dry_run = True

    invited = _invited_leads()
    if not invited:
        print("no outstanding invited leads to check.")
        emit_result("accept-sync", True, "No outstanding invites to check")
        return

    threshold = cfg.withdraw_after_days
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=300, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx, page = _open(pw)
            try:
                pending = _load_pending(page)
                ops.log_action(AGENT, "scrape", target="sent-invites", result="ok")

                accepted_conf, ambiguous = [], []
                resolvable = resolved = merged_out = 0
                for L in invited:
                    row = _match_pending(L, pending)
                    if row is not None:
                        # STILL PENDING — and the card just handed us this person's
                        # public /in/ URL for free. Stamp it, so the day they DO accept
                        # we confirm them by identity instead of by display string.
                        if "/in/" not in (L["profile_url"] or "") and \
                                "/in/" in (row.get("profile_url") or ""):
                            resolvable += 1
                            if not dry_run:
                                if _harvest_identity(L, row):
                                    resolved += 1
                                else:
                                    merged_out += 1
                        continue
                    age = _age_days(L["invited_at"])
                    if age is not None and age < threshold:
                        accepted_conf.append((L, f"left-pending @ {age:.0f}d (< {threshold}d)"))
                    else:
                        ambiguous.append((L, age))

                print(f"\ninvited leads checked: {len(invited)}  ·  pending now: {len(pending)}")
                if resolvable:
                    print(f"identity: {resolvable} still-pending lead(s) hold no public /in/ URL"
                          + (f" — resolved {resolved}"
                             + (f", {merged_out} not stamped (already held under another row)"
                                if merged_out else "")
                             if not dry_run else " (would resolve on --commit)"))
                print(f"confident accepts (recent, left pending): {len(accepted_conf)}")
                print(f"ambiguous (older, left pending — need --confirm): {len(ambiguous)}")
                for L, ev in accepted_conf[:30]:
                    print(f"  [accept] {L['full_name'] or '?':<26} {ev}")
                for L, age in ambiguous[:30]:
                    print(f"  [ambig ] {L['full_name'] or '?':<26} "
                          f"{('%.0fd' % age) if age is not None else 'age?'}")

                if dry_run:
                    print("\n[dry-run] nothing written. Arm + --commit to mark accepts.")
                    emit_result("accept-sync", True,
                                f"Rehearsal — {len(accepted_conf)} confident accept(s), "
                                f"{len(ambiguous)} ambiguous")
                    return

                marked = 0
                held = 0
                # 1) "confident" accepts — TRUTH-TESTED against the connections page
                #    (one page load covers the whole batch; recently-added sort puts
                #    fresh accepts at the top). Not present -> HELD as 'invited' and
                #    re-checked next run; never marked, never enrolled for messaging.
                if accepted_conf:
                    recent = _recent_connections(page,
                                                 max_cards=_truth_window(len(accepted_conf)))
                    print(f"  [truth-test] read {len(recent)} recent connection card(s)")
                    for L, ev in accepted_conf:
                        if marked >= max_n:
                            break
                        pub = _match_connection(L, recent) if recent else None
                        if not pub:
                            held += 1
                            print(f"  [held] {L['full_name'] or '?'}: left pending but NOT "
                                  "on the connections page — re-check next run")
                            continue
                        # Harvest the /in/ URL the card just gave us (the message lane
                        # needs it; a /sales/lead/-only accept can never be messaged).
                        if "/in/" not in (L.get("profile_url") or ""):
                            from .connect import _store_public_url
                            if not _store_public_url(L["id"], pub):
                                # merged into a row we already hold — that row owns the
                                # person; don't double-mark this retired duplicate
                                print(f"  [merge] {L['full_name']}: already known as {pub}")
                                continue
                        _mark_accepted(L["id"], ev + "; on connections page")
                        marked += 1
                        print(f"  accepted {L['full_name']} (verified)")

                # 2) ambiguous — confirm each by a budgeted profile visit, if asked
                if confirm_each:
                    from .connect import STATUS_JS, _classify
                    for L, age in ambiguous:
                        if marked >= max_n:
                            break
                        ok, why = safety.can_act("profile_view", cfg)
                        if not ok:
                            print(f"[stop] {why}")
                            break
                        url = L["profile_url"]
                        if "/in/" not in url:
                            print(f"  [skip] {L['full_name']}: no /in/ URL to confirm")
                            continue
                        try:
                            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                            time.sleep(3)
                            cls = _classify(page.evaluate(STATUS_JS))
                            ops.log_action(AGENT, "profile_view", target=url, result="ok")
                            if cls == "connected":
                                _mark_accepted(L["id"], "profile-confirmed connected")
                                marked += 1
                                print(f"  accepted {L['full_name']} (confirmed)")
                            else:
                                print(f"  [not connected] {L['full_name']} ({cls})")
                            time.sleep(safety.next_delay(cfg, marked))
                        except Exception as e:  # noqa: BLE001
                            print(f"  [err] {L['full_name']}: {str(e)[:70]}")

                print(f"\n[done] marked {marked} lead(s) accepted"
                      + (f", {held} held (not yet on the connections page)" if held else "")
                      + ".")
                emit_result("accept-sync", True,
                            f"Marked {marked} lead(s) as Connected"
                            + (f" ({held} held for re-check)" if held else ""),
                            count=marked, held=held, resolved=resolved)
            finally:
                safe_close(ctx)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from .withdraw import _arg_int
    if "--probe" in sys.argv:
        probe()
    else:
        sync(_arg_int("--max") or 50, dry_run="--commit" not in sys.argv,
             confirm_each="--confirm" in sys.argv)


if __name__ == "__main__":
    main()
