"""withdraw.py — the Withdraw lane (the safe first live action).

Cancels your OWN stale pending connection invites on LinkedIn's Sent-invitations
page, to stay under LinkedIn's pending-invite + weekly-invite ceilings. This is the
lowest-risk action on the platform (you're undoing your own requests), which is why
it's the first lane we validate live.

Three modes (CLI: python -m engine withdraw [--probe|--dry-run|--commit]):

  --probe     READ-ONLY DOM recon: load the page, dump structure + screenshot so we
              can confirm the live selectors. No clicks. Safe to run live.
  (default)   --dry-run: read the pending invites, parse their ages, report which
              exceed config.withdraw_after_days. No clicks. Safe to run live.
  --commit    Actually withdraw the aged invites — ONLY if config.enabled is true AND
              config.dry_run is false. Each withdrawal passes safety.can_act('withdraw')
              + the shared ops budget, is throttled (human pause), and is logged.

Reuses the shared session (linkedin_browser) + governance (linkedin_ops) in place.
SELECTORS BELOW ARE BEST-GUESS pending a live --probe; the probe output finalises them.
"""
from __future__ import annotations

import json
import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

from . import ops
import linkedin_browser as lb

from . import DATA_DIR
from .config import Config
from . import safety, safe_close
from . import browser as kb   # keeper: per-invite reattach + retry (keeper-stability fix)
from . import db

AGENT = "engine-withdraw"


def _invite_red_listed(v: dict) -> bool:
    """Layer B decision shared by the withdraw loop and its tests. A total block INCLUDES
    not touching a red-listed person's pending invite — so their invite is left alone."""
    return bool(db.red_list_match(url=v.get("profile_url"), name=v.get("name")))
SENT_URL = "https://www.linkedin.com/mynetwork/invitation-manager/sent/"
SHOTS = DATA_DIR / "screenshots"

# Read-only recon: surface the structure we need to lock selectors against.
PROBE_JS = r"""() => {
  const trunc = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const cards = [...document.querySelectorAll('[role="listitem"]')]
      .filter(e => e.querySelector('a[href*="/in/"]'));
  const wd = [...document.querySelectorAll('[role="button"], button, a')]
      .filter(e => /^\s*withdraw/i.test(e.getAttribute('aria-label') || '')
                || /^\s*withdraw\s*$/i.test((e.textContent || '').trim()));
  const f = wd[0];
  const showMore = [...document.querySelectorAll('button')]
      .filter(b => /show more|more results|see more/i.test(b.textContent || ''))
      .map(b => trunc(b.textContent, 40));
  return {
    url: location.href,
    listitems: cards.length,
    inLinks: document.querySelectorAll('a[href*="/in/"]').length,
    withdrawControls: wd.length,
    firstWithdraw: f ? {tag: f.tagName, role: f.getAttribute('role'),
                        aria: trunc(f.getAttribute('aria-label'), 90),
                        text: trunc(f.textContent, 40)} : null,
    showMoreButtons: showMore,
    hasPaginationWidget: !!document.querySelector(
        '.artdeco-pagination, [aria-label*="agination"], nav[aria-label*="agination"]'),
    scrollHeight: document.scrollingElement ? document.scrollingElement.scrollHeight : null,
    sampleCardText: cards[0] ? trunc(cards[0].textContent, 220) : null,
  };
}"""


# ---------------------------------------------------------------------------
# Age parsing — LinkedIn shows relative text like "Sent 3 weeks ago"
# ---------------------------------------------------------------------------

_AGE = re.compile(r"(today|yesterday|(\d+)\s*(minute|hour|day|week|month|year))", re.I)


def parse_age_days(text: str) -> int | None:
    """Age in days from the 'Sent ... ago' clause. Anchored to that clause so a number
    in the person's headline (e.g. '20 years experience') is never mistaken for the age."""
    text = text or ""
    m = re.search(r"Sent\s+(.{1,24}?)\s+ago", text, re.I)
    if m:
        basis = m.group(1)
    else:
        m2 = re.search(r"Sent\s+(today|yesterday)", text, re.I)
        basis = m2.group(1) if m2 else ""
    am = _AGE.search(basis)
    if not am:
        return None
    if am.group(1).lower() == "today":
        return 0
    if am.group(1).lower() == "yesterday":
        return 1
    n = int(am.group(2))
    unit = am.group(3).lower()
    return n * {"minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}[unit]


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _arg_int(flag: str) -> int | None:
    """Read an integer value following a CLI flag (e.g. --max 3)."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            try:
                return int(sys.argv[i + 1])
            except ValueError:
                return None
    return None


def _arg_str(flag: str) -> str | None:
    """Read a string value following a CLI flag (e.g. --url https://...)."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def norm_url(u: str | None) -> str:
    """Normalise a LinkedIn profile URL for equality matching: drop scheme/query/
    fragment, drop a leading 'www.', lowercase, strip a trailing slash. Matches the
    shape the Sent-invites read produces (href.split('?')[0]) so a URL typed by a
    human resolves to the same key as the one scraped off the card."""
    u = (u or "").strip()
    u = re.sub(r"^https?://", "", u, flags=re.I)      # scheme
    u = u.split("?")[0].split("#")[0]                  # query / fragment
    if u.lower().startswith("www."):
        u = u[4:]
    return u.rstrip("/").lower()


def _load_all(page, max_rounds: int = 500) -> int:
    """Lazy-load the full sent-invite list. The list sits in an inner scroll container,
    so scrolling the window does nothing — instead we pull the LAST card into view,
    which forces the container to fetch the next batch.

    Patience matters: the oldest invites (the ones we most want to withdraw) are at
    the BOTTOM of the list, and one slow batch fetch must not read as 'fully loaded'.
    We only stop after ~12s of genuine no-growth, and we click any 'Show more'
    button before giving up."""
    items = page.locator('[role="listitem"]')
    # Wait for the list to actually render before scrolling, else we give up at ~10.
    try:
        page.wait_for_selector('a[aria-label^="Withdraw invitation sent to"]', timeout=20000)
    except Exception:
        pass
    last, stale = 0, 0
    for i in range(max_rounds):
        n = items.count()
        if n != last:
            stale, last = 0, n
        else:
            stale += 1
            # A 'Show more' / 'Load more' button can replace infinite scroll deep
            # in the list — click it instead of quitting.
            try:
                btn = page.get_by_role("button", name=re.compile(r"show more|load more|more results", re.I))
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    time.sleep(2.0)
                    stale = 0
            except Exception:
                pass
            if stale >= 12:        # ~12-17s of true no-growth => fully loaded
                break
        try:
            items.nth(max(n - 1, 0)).scroll_into_view_if_needed(timeout=5000)
        except Exception:
            page.mouse.wheel(0, 2600)
        time.sleep(random.uniform(0.8, 1.4))
        if i % 15 == 14:
            print(f"  ...loaded {items.count()} invites so far")
    n = items.count()
    print(f"  list fully loaded: {n} invites")
    return n


# One in-page pass collects every loaded invite — far faster than per-card round-trips.
READ_JS = r"""() => {
  const out = [];
  for (const c of document.querySelectorAll('[role="listitem"]')) {
    const wd = c.querySelector('a[aria-label^="Withdraw invitation sent to"]');
    if (!wd) continue;
    const p = c.querySelector('a[href*="/in/"]');
    const aria = wd.getAttribute('aria-label') || '';
    out.push({
      aria: aria,
      name: aria.replace(/^Withdraw invitation sent to\s*/i, '').trim() || null,
      profile_url: p ? p.href.split('?')[0] : null,
      age_text: (c.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 400),
    });
  }
  return out;
}"""


def _read_invites(page) -> list[dict]:
    """Collect all loaded sent invites in one in-page pass. The withdraw control is an
    <a aria-label='Withdraw invitation sent to NAME'>; aria is unique per person and is
    what commit() clicks. Age is parsed from the card text in Python."""
    rows = page.evaluate(READ_JS)
    for r in rows:
        r["age_days"] = parse_age_days(r["age_text"])
    return rows


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def _open(pw):
    SHOTS.mkdir(parents=True, exist_ok=True)
    ctx = lb.open_read_context(pw, headless=False)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    # Human navigation: feed -> My Network -> Manage -> Sent (soft-goto lands on the
    # Sent page if a click target isn't found, so behaviour is unchanged on fallback)
    from . import nav
    page = nav.human_open(page, SENT_URL)
    time.sleep(4)
    return ctx, page


def _reopen_sent(pw, ctx):
    """keeper-stability: after a mid-batch keeper death, reattach a fresh keeper and
    re-prepare the Sent-invitations page (navigate + reload the list) so the withdraw
    controls exist again for the retry. Returns (ctx, page) or (None, None) if the keeper
    can't be brought back."""
    newctx = kb.reattach(pw, ctx)
    if newctx is None:
        return None, None
    page = newctx.pages[0] if newctx.pages else newctx.new_page()
    from . import nav
    page = nav.human_open(page, SENT_URL)
    time.sleep(4)
    _load_all(page)
    return newctx, page


def probe() -> None:
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx, page = _open(pw)
            try:
                _load_all(page, max_rounds=3)
                data = page.evaluate(PROBE_JS)
                print(json.dumps(data, indent=2, ensure_ascii=False))
                shot = str(SHOTS / "withdraw_probe.png")
                page.screenshot(path=shot, full_page=True)
                print(f"[screenshot] {shot}", file=sys.stderr)
                ops.log_action(AGENT, "scrape", target="sent-invites", result="ok")
            finally:
                safe_close(ctx)


def inspect_modal() -> None:
    """Open the FIRST invite's withdraw dialog, dump its buttons + screenshot, then
    Escape WITHOUT confirming. Read-only diagnostic to lock the confirm selector."""
    MODAL_JS = r"""() => {
      const d = document.querySelector('[data-testid="dialog"], dialog[open]');
      if (!d) return {dialog: false};
      const btns = [...d.querySelectorAll('button')].map(b => ({
        text: (b.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
        aria: (b.getAttribute('aria-label') || '').slice(0, 60),
        testid: b.getAttribute('data-testid') || null,
      }));
      return {dialog: true,
              text: (d.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160),
              buttons: btns};
    }"""
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx, page = _open(pw)
            try:
                page.wait_for_selector('a[aria-label^="Withdraw invitation sent to"]', timeout=20000)
                page.get_by_role("link", name=re.compile(r"^Withdraw invitation sent to", re.I)).first.click()
                time.sleep(1.6)
                print(json.dumps(page.evaluate(MODAL_JS), indent=2, ensure_ascii=False))
                SHOTS.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(SHOTS / "withdraw_modal.png"))
                print(f"[screenshot] {SHOTS / 'withdraw_modal.png'}", file=sys.stderr)
                try:
                    page.keyboard.press("Escape")   # do NOT confirm
                except Exception:
                    pass
            finally:
                safe_close(ctx)


def dry_run() -> None:
    cfg = Config.load()
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx, page = _open(pw)
            try:
                _load_all(page)
                invites = _read_invites(page)
                ops.log_action(AGENT, "scrape", target="sent-invites", result="ok")
            finally:
                safe_close(ctx)

    threshold = cfg.withdraw_after_days
    aged = [v for v in invites if (v["age_days"] or 0) >= threshold]
    print(f"Pending sent invites read: {len(invites)}")
    print(f"Withdraw threshold: >= {threshold} days  ->  {len(aged)} eligible\n")
    for v in sorted(invites, key=lambda x: -(x["age_days"] or 0)):
        mark = "WITHDRAW" if (v["age_days"] or 0) >= threshold else "keep"
        print(f"  [{mark:8s}] {str(v['age_days']):>4} d  {v['name'] or '?':<28} {v['profile_url'] or ''}")
    print("\n[dry-run] no invites withdrawn. To act: set enabled=true + dry_run=false in "
          "config.json, then run with --commit.")


def commit() -> None:
    cfg = Config.load()
    if not cfg.enabled or cfg.dry_run:
        print(f"[refused] enabled={cfg.enabled} dry_run={cfg.dry_run} — not withdrawing. "
              "Set enabled=true AND dry_run=false to act. Showing dry-run instead:\n")
        dry_run()
        return

    withdrawn = 0
    from . import traffic
    lane = traffic.LaneLock(agent=AGENT, wait_sec=300)
    halted = False
    with traffic.lane_tenure(lane) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx, page = _open(pw)
            try:
                _load_all(page)
                invites = _read_invites(page)
                print(f"loaded {len(invites)} pending invites.")
                aged = sorted([v for v in invites if (v["age_days"] or 0) >= cfg.withdraw_after_days],
                              key=lambda x: -(x["age_days"] or 0))
                max_n = _arg_int("--max")
                if max_n is not None:
                    aged = aged[:max_n]
                print(f"{len(aged)} invites to withdraw (>= {cfg.withdraw_after_days} d"
                      f"{', --max ' + str(max_n) if max_n is not None else ''}).")

                for idx, v in enumerate(aged):
                    if halted:
                        break
                    # Layer B (the hard stop): a total block means we do not even withdraw a
                    # red-listed person's pending invite — leave it exactly as it is.
                    if _invite_red_listed(v):
                        db.log_event("redlist-blocked", None, f"withdraw:{v.get('name')}")
                        print(f"  [skip] {v['name']}: on the red list (do-not-contact) — invite left as-is")
                        continue
                    ok, why = safety.can_act("withdraw", cfg)
                    if not ok:
                        print(f"[stop] safety gate: {why}")
                        break
                    # keeper-stability: wrap the per-invite work so ONE keeper death
                    # reattaches + retries THIS invite instead of cascading the batch
                    # (98.8% of withdraw failures were exactly this cascade). Click/selector
                    # logic below is UNCHANGED — only wrapped with detect->reattach->retry.
                    attempts = 0
                    while True:
                        try:
                            # Target THIS specific invite by its unique aria-label.
                            link = page.get_by_role("link", name=v["aria"], exact=True)
                            if not link.count():
                                print(f"  [skip] {v['name']}: withdraw control not found")
                                break
                            link.first.click()
                            # Confirmation modal (stable data-testid) -> its 'Withdraw' button.
                            dlg = page.locator('[data-testid="dialog"]')
                            dlg.wait_for(state="visible", timeout=6000)
                            # The dialog's confirm button carries the SAME aria-label as the card
                            # link, but role=button (not link), scoped to the dialog.
                            dlg.get_by_role("button", name=v["aria"], exact=True).first.click()
                            dlg.wait_for(state="detached", timeout=10000)   # must close before next
                            ops.log_action(AGENT, "withdraw", target=v["profile_url"] or v["name"], result="ok")
                            withdrawn += 1
                            print(f"  withdrew {v['name']} ({v['age_days']}d)")
                            # --fast: tighter gaps for this low-risk lane only (global
                            # pacing stays conservative for connect/message).
                            # The gap is the YIELD boundary (traffic-control V3 item 3):
                            # the lock is released for its length; interactive sends run
                            # in their own tab, so this page's card list survives.
                            gap = (random.uniform(15, 40) if "--fast" in sys.argv
                                   else safety.next_delay(cfg, idx))
                            if not lane.pause(gap):
                                print("[stop] the browser could not be re-taken after "
                                      "yielding — stopping cleanly (resumable next run)")
                                halted = True
                            break   # invite handled — next invite
                        except Exception as e:  # noqa: BLE001
                            if kb.is_browser_closed_error(e) and attempts < 1:
                                attempts += 1
                                newctx, newpage = _reopen_sent(pw, ctx)
                                if newctx is not None:
                                    ctx, page = newctx, newpage
                                    print(f"  [reattach] keeper died mid-batch — retrying {v['name']}")
                                    continue   # retry THIS invite on the fresh keeper
                            # If a dialog stuck open, dismiss it so the next click isn't blocked.
                            try:
                                page.keyboard.press("Escape")
                                time.sleep(0.6)
                            except Exception:
                                pass
                            ops.log_action(AGENT, "withdraw", target=v["profile_url"],
                                           result="failed", detail=str(e)[:120])
                            print(f"  [skip] {v['name']}: {str(e)[:80]}")
                            break
            finally:
                safe_close(ctx)
    print(f"\n[done] withdrew {withdrawn} invite(s).")
    from . import emit_result
    emit_result("withdraw", True, f"Withdrew {withdrawn} stale invite(s)", count=withdrawn)


def withdraw_url(url: str, commit_flag: bool = False) -> None:
    """Withdraw exactly ONE pending invite by its target profile URL.

    This is the self-cleaning connect->withdraw test hook (EM-007): it deliberately
    BYPASSES the age-floor / oldest-first / bulk selection of dry_run()/commit() so a
    just-sent request (age 0) can be uniquely reversed. That bulk logic is untouched and
    still owns the scheduled job — this path targets a SINGLE named profile only.

    Without commit_flag (no --commit) OR with the master switches off it is a DRY RUN:
    it loads the Sent list, reports whether the invite is present + would be withdrawn,
    and clicks nothing. With --commit AND enabled=true + dry_run=false it clicks the ONE
    matching Withdraw affordance (the same aria-labelled control the bulk op clicks),
    then verifies the invite is GONE before recording ok. Reuses the keeper reattach/
    retry plumbing so one mid-op keeper death recovers instead of failing."""
    target = norm_url(url)
    if not target:
        print("[refused] no --url given (usage: withdraw --url <profile_url> [--commit])",
              file=sys.stderr)
        return

    # Layer B (the hard stop): a red-listed person's invite is left untouched — refuse
    # before opening any browser.
    if db.red_list_match(url=target):
        db.log_event("redlist-blocked", None, f"withdraw-url:{target}")
        print(f"[refused] {url} is on the red list (do-not-contact) — invite left as-is.")
        return

    cfg = Config.load()
    live = commit_flag and cfg.enabled and not cfg.dry_run
    if commit_flag and not live:
        print(f"[refused] enabled={cfg.enabled} dry_run={cfg.dry_run} — not withdrawing live. "
              "Set enabled=true AND dry_run=false to act. Running as dry-run:\n")

    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=300, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx, page = _open(pw)
            try:
                _load_all(page)
                invites = _read_invites(page)
                match = next((v for v in invites if norm_url(v["profile_url"]) == target), None)
                if not match:
                    print(f"[not-found] no pending invite matches {url}\n"
                          f"  (read {len(invites)} pending invites; nothing to withdraw)")
                    ops.log_action(AGENT, "withdraw", target=url, result="failed",
                                   detail="no matching pending invite")
                    return
                print(f"[match] {match['name'] or '?'} ({match['age_days']}d)  {match['profile_url']}")

                if not live:
                    print("\n[dry-run] invite found and would be withdrawn. "
                          "Re-run with --commit (enabled=true + dry_run=false) to act.")
                    ops.log_action(AGENT, "scrape", target="sent-invites", result="ok")
                    return

                ok, why = safety.can_act("withdraw", cfg)
                if not ok:
                    print(f"[stop] safety gate: {why}")
                    return

                # keeper-stability: wrap the single-invite work so ONE keeper death
                # reattaches + retries THIS invite instead of failing. Click/selector
                # logic is IDENTICAL to the bulk op — only the SELECTION differs (one
                # named profile, no age-floor). detect->reattach->retry, same as bulk.
                attempts = 0
                while True:
                    try:
                        link = page.get_by_role("link", name=match["aria"], exact=True)
                        if not link.count():
                            print(f"  [skip] {match['name']}: withdraw control not found")
                            ops.log_action(AGENT, "withdraw", target=match["profile_url"],
                                           result="failed", detail="withdraw control not found")
                            return
                        link.first.click()
                        dlg = page.locator('[data-testid="dialog"]')
                        dlg.wait_for(state="visible", timeout=6000)
                        dlg.get_by_role("button", name=match["aria"], exact=True).first.click()
                        dlg.wait_for(state="detached", timeout=10000)
                        # verify-after: the invite must be GONE (its unique aria-label
                        # cleared from the reloaded Sent list) before we record ok —
                        # mirrors connect's _verify_pending, closes the fire-and-assume gap.
                        if _verify_withdrawn(page, target):
                            ops.log_action(AGENT, "withdraw",
                                           target=match["profile_url"] or match["name"], result="ok")
                            print(f"  withdrew {match['name']} — confirmed gone from Sent list")
                        else:
                            ops.log_action(AGENT, "withdraw", target=match["profile_url"],
                                           result="failed", detail="still present after withdraw click")
                            print(f"  [unconfirmed] {match['name']}: still in Sent list after click")
                        break
                    except Exception as e:  # noqa: BLE001
                        if kb.is_browser_closed_error(e) and attempts < 1:
                            attempts += 1
                            newctx, newpage = _reopen_sent(pw, ctx)
                            if newctx is not None:
                                ctx, page = newctx, newpage
                                print(f"  [reattach] keeper died — retrying {match['name']}")
                                continue
                        try:
                            page.keyboard.press("Escape")
                            time.sleep(0.6)
                        except Exception:
                            pass
                        ops.log_action(AGENT, "withdraw", target=match["profile_url"],
                                       result="failed", detail=str(e)[:120])
                        print(f"  [skip] {match['name']}: {str(e)[:80]}")
                        break
            finally:
                safe_close(ctx)


def _verify_withdrawn(page, target: str, tries: int = 3) -> bool:
    """verify-after-action: re-read the Sent-invites list and confirm NO invite still
    matches `target` (normalised). The list updates a beat late after the modal closes,
    so re-read a few times; only True (invite genuinely absent) records an `ok`."""
    for _ in range(tries):
        time.sleep(1.2)
        try:
            still = any(norm_url(v["profile_url"]) == target for v in _read_invites(page))
        except Exception:
            continue
        if not still:
            return True
    return False


def main() -> None:
    if "--probe" in sys.argv:
        probe()
    elif "--url" in sys.argv:
        withdraw_url(_arg_str("--url") or "", commit_flag="--commit" in sys.argv)
    elif "--commit" in sys.argv:
        commit()
    else:
        dry_run()


if __name__ == "__main__":
    main()
