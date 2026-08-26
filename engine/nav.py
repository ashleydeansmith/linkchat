"""nav.py — human-like navigation through LinkedIn (anti-bot realism).

The problem: jumping straight to a deep Sales Navigator list/search URL with
page.goto() is a bot tell — no referrer chain, no dwell, no browsing trail. A
human lands on the FEED, pauses, scrolls, then clicks through Sales Navigator ->
Lead lists -> the list.

This module reproduces that. `human_open(page, target_url)` walks the path section
by section: it clicks the real nav element when it can find it, and falls back to a
SECTION landing page (never the deep target) when it can't — so the lane always
reaches its destination, but via a believable trail. Human dwell + small scrolls +
mouse drift happen between every hop.

The click selectors (apps/bento menu, the Lists tab, My Network) are best-guess
and want one supervised tuning pass on the live DOM (like the send selectors). Until
then the soft-goto fallbacks guarantee the lane still works — just with the section
hop + dwell instead of a real click, which already removes the deep-link tell.
"""
from __future__ import annotations

import random
import time

from . import safe_close

FEED = "https://www.linkedin.com/feed/"
SALES_HOME = "https://www.linkedin.com/sales/home"
LEAD_LISTS = "https://www.linkedin.com/sales/lists/people"
NETWORK = "https://www.linkedin.com/mynetwork/"
CONNECTIONS = "https://www.linkedin.com/mynetwork/invite-connect/connections/"

# Navigation wait strategy (root-cause fix 2026-07-07). LinkedIn's SPAs — the Sales Nav
# search especially — keep loading sub-resources long past a normal page load, so
# `domcontentloaded` frequently never fires within 60s and page.goto() TIMES OUT even
# though the page has actually rendered (proven live: the search page loads fully in <1s
# with "commit", verified by screenshot). `commit` returns as soon as the navigation
# response is received; every caller then dwells + waits for a concrete selector (lead
# rows / nav shell), which is the real readiness signal. This was the single cause of the
# collect/connect "does nothing / times out" failures (Page.goto: Timeout 60000ms).
_NAV_WAIT = "commit"


def _dwell(a: float = 1.8, b: float = 4.5) -> None:
    time.sleep(random.uniform(a, b))


def _scroll(page, max_bursts: int = 2) -> None:
    try:
        for _ in range(random.randint(1, max_bursts)):
            page.mouse.wheel(0, random.randint(300, 900))
            time.sleep(random.uniform(0.5, 1.4))
        if random.random() < 0.4:                       # the occasional scroll back up
            page.mouse.wheel(0, -random.randint(150, 400))
            time.sleep(random.uniform(0.4, 0.9))
    except Exception:
        pass


def _drift(page) -> None:
    try:
        page.mouse.move(random.randint(200, 900), random.randint(150, 600),
                        steps=random.randint(5, 15))
    except Exception:
        pass


def _on(page, frag: str) -> bool:
    try:
        return frag in (page.url or "")
    except Exception:
        return False


def _click_first(page, selectors: list[str], timeout: int = 2500) -> bool:
    """Click the first visible element matching any selector. Returns success."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                _drift(page)
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Hops
# ---------------------------------------------------------------------------

def _goto_retry(page, url: str, tries: int = 2) -> None:
    """goto with a retry — the first navigation after a stale/contended browser can
    raise net::ERR_ABORTED ('frame was detached'); a short wait + retry clears it.
    Uses a short timeout: with wait_until="commit" a real navigation commits in <1-4s,
    so a long timeout only buys a long hang on an in-app/hash nav that never commits. If
    we already appear to be ON the target host, a swallow is safe — the caller verifies
    readiness by URL/selector, never by this call succeeding."""
    last = None
    for i in range(tries):
        try:
            page.goto(url, wait_until=_NAV_WAIT, timeout=20_000)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 + i)
    if last and not _on(page, "linkedin.com"):
        raise last   # only hard-fail if we're not even on LinkedIn; else proceed + verify


def warm_up(page) -> None:
    """Land on the feed like a returning user, then dwell + scroll a touch."""
    if not _on(page, "linkedin.com"):
        _goto_retry(page, FEED)
    elif not _on(page, "/feed"):
        if not _click_first(page, ['a[href="/feed/"]',
                                   'a[data-test-app-aware-link][href*="/feed"]',
                                   'header a[aria-label*="LinkedIn" i]',
                                   '.global-nav__branding-logo']):
            page.goto(FEED, wait_until=_NAV_WAIT, timeout=45_000)
    _dwell()
    _scroll(page)
    _drift(page)


def to_sales_home(page) -> None:
    """Feed -> Sales Navigator. CONFIRMED via probe (2026-06-12): the LinkedIn feed
    top-nav carries a direct 'Sales Nav' link (a[href=".../sales/"]) — a human clicks
    it; no apps/bento menu needed (that approach was probed and isn't required)."""
    if _on(page, "/sales"):
        return
    if not _click_first(page, ['a[href="https://www.linkedin.com/sales/"]',
                               'a[href$="linkedin.com/sales/"]',
                               'a[aria-label^="Sales Nav" i]',
                               'a:text-is("Sales Nav")']):
        try:
            page.goto(SALES_HOME, wait_until=_NAV_WAIT, timeout=20_000)   # section fallback
        except Exception:  # noqa: BLE001
            pass   # in-app nav may not fire a commit; open_target/selector wait confirms
    _dwell(2.5, 5.0)   # the Sales Nav app is heavy — let it settle like a human waiting
    _drift(page)


def to_lead_lists(page) -> None:
    """Sales Nav home -> Lead lists. CONFIRMED via probe: the Sales Navigator top nav
    is Home · Accounts · Leads · Messaging — a human clicks 'Leads' to reach the lead
    lists table."""
    if _on(page, "/sales/lists"):
        return
    if not _click_first(page, ['header a:text-is("Leads")', 'nav a:text-is("Leads")',
                               'a:text-is("Leads")',
                               'a[href*="/sales/lists/people"]',
                               'a[href*="/sales/index"]']):
        try:
            page.goto(LEAD_LISTS, wait_until=_NAV_WAIT, timeout=20_000)
        except Exception:  # noqa: BLE001
            pass   # in-app nav may not commit; caller's selector wait confirms readiness
    _dwell()
    _scroll(page, 1)


def to_network_sent(page) -> None:
    """Human path to the Sent-invitations page: feed -> My Network -> Manage -> Sent."""
    if not _click_first(page, ['a[href*="/mynetwork/"]', 'a:has-text("My Network")',
                               'nav a[aria-label*="My Network" i]']):
        page.goto(NETWORK, wait_until=_NAV_WAIT, timeout=45_000)
    _dwell()
    _drift(page)
    # "Manage" invitations -> the Sent tab
    _click_first(page, ['a[href*="invitation-manager"]', 'a:has-text("Manage")',
                        'button:has-text("Manage")'])
    _dwell(1.0, 2.5)
    if not _click_first(page, ['a[href*="invitation-manager/sent"]',
                               'button:has-text("Sent")', 'a:has-text("Sent")']):
        page.goto("https://www.linkedin.com/mynetwork/invitation-manager/sent/",
                  wait_until=_NAV_WAIT, timeout=45_000)
    _dwell()


def to_connections(page) -> None:
    """Human path to the first-degree Connections page: feed -> My Network ->
    Connections. Click-first with a soft-goto fallback to the connections URL, so the
    lane always lands there but via a believable trail. Distinct from to_network_sent()
    (which lands on the Sent-INVITATIONS page — the wrong page for the connections
    collector; see the human_open branch below)."""
    if _on(page, "/mynetwork/invite-connect/connections"):
        return
    if not _click_first(page, ['a[href*="/mynetwork/"]', 'a:has-text("My Network")',
                               'nav a[aria-label*="My Network" i]']):
        page.goto(NETWORK, wait_until=_NAV_WAIT, timeout=45_000)
    _dwell()
    _drift(page)
    # the "Connections" entry — a left-rail manage card / link on the My-Network page
    if not _click_first(page, ['a[href*="invite-connect/connections"]',
                               'a:has-text("Connections")',
                               'button:has-text("Connections")']):
        page.goto(CONNECTIONS, wait_until=_NAV_WAIT, timeout=45_000)
    _dwell()


def _strip_session_id(url: str) -> str:
    """Drop a stale &sessionId=... from a Sales Nav search URL. That token is bound to
    the browser session it was copied FROM; carried into another session it makes Sales
    Nav drop the whole query and open the blank filter builder. Removing it lets Sales
    Nav mint a fresh sessionId and apply the saved query (confirmed live 2026-06-17)."""
    import re as _re
    url = _re.sub(r"[?&]sessionId=[^&]*", "", url)
    return url


def open_target(page, target_url: str) -> None:
    """Final hop. For a SEARCH URL, direct-goto (its tail 'search/people' is generic and
    matching it would click the nav's blank-search link, not the saved search) with the
    stale sessionId stripped. For a LIST/LEAD URL whose tail carries a unique id, click
    the on-page row when present (real trail); else soft-goto."""
    is_search = "/sales/search/" in target_url
    if is_search:
        # When we're ALREADY inside the Sales Nav app (we arrived via /sales/home), moving
        # to a saved-search URL is an in-app HASH navigation — no fresh document load fires,
        # so even wait_until="commit" never resolves and goto hangs the full timeout, even
        # though the results DO render (proven live by screenshot). So: short timeout, and
        # SWALLOW a timeout — the caller's wait_for_selector on the actual lead rows is the
        # real readiness signal. A genuine cross-document nav still commits in <1s here.
        try:
            page.goto(_strip_session_id(target_url), wait_until=_NAV_WAIT, timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        _dwell()
        return
    try:
        tail = target_url.split("?")[0].split("/sales/")[-1]
        # only click the shortcut for a SPECIFIC resource (lists/people/<id> or lead/<id>),
        # never a generic section tail
        if tail and ("/" in tail.strip("/")) and ("lists/people/" in tail or "lead/" in tail):
            loc = page.locator(f'a[href*="{tail}"]')
            if loc.count() and loc.first.is_visible():
                _drift(page)
                loc.first.click()
                _dwell()
                return
    except Exception:
        pass
    page.goto(target_url, wait_until=_NAV_WAIT, timeout=45_000)
    _dwell()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

_NAV_MAP_JS = r"""() => {
  const trunc = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const vis = e => !!e.offsetParent;
  const grab = sel => [...document.querySelectorAll(sel)].filter(vis).map(e => ({
    tag: e.tagName, text: trunc(e.textContent, 30),
    aria: trunc(e.getAttribute('aria-label'), 50),
    href: (e.getAttribute('href') || '').split('?')[0].slice(0, 70),
    cls: trunc(e.className && e.className.toString ? e.className.toString() : '', 50),
  }));
  return {
    url: location.href,
    navLinks: grab('header a, nav a, .global-nav a').slice(0, 24),
    navButtons: grab('header button, nav button, .global-nav button').slice(0, 16),
    listRows: grab('a[href*="/sales/lists/people/"]').slice(0, 10),
    salesNavTabs: grab('a[href*="/sales/"]').slice(0, 20),
  };
}"""


def probe() -> None:
    """Map the real clickable nav targets at each hop (feed -> apps -> sales home ->
    lead lists), so the click selectors can be hardened from the live DOM instead of
    guessed. Read-only: it navigates and dumps, never changes state."""
    import json
    import sys
    from . import ops
    import linkedin_browser as lb
    from playwright.sync_api import sync_playwright
    from . import DATA_DIR

    shots = DATA_DIR / "screenshots"
    with ops.lock(lb.READ_LOCK, agent="engine-nav", wait_sec=180) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                shots.mkdir(parents=True, exist_ok=True)

                print("=== HOP 1: feed ===")
                warm_up(page)
                print(json.dumps(page.evaluate(_NAV_MAP_JS), indent=2, ensure_ascii=False))
                page.screenshot(path=str(shots / "nav_1_feed.png"))

                print("\n=== HOP 2: try apps/bento menu ===")
                opened = _click_first(page, ['button[aria-label*="App launcher" i]',
                                             'button[aria-label*="business apps" i]',
                                             '.global-nav__primary-link--app-launcher',
                                             'button[aria-label="Apps"]'])
                print(f"apps menu clicked: {opened}")
                _dwell(0.8, 1.8)
                if opened:
                    print(json.dumps(page.evaluate(
                        r"""() => [...document.querySelectorAll('a,button')].filter(e=>e.offsetParent &&
                            /sales|navigator|recruiter|learning/i.test((e.textContent||'')+(e.getAttribute('href')||'')))
                            .map(e=>({text:(e.textContent||'').replace(/\s+/g,' ').trim().slice(0,30),
                                      href:(e.getAttribute('href')||'').slice(0,60)})).slice(0,12)"""),
                        indent=2, ensure_ascii=False))
                    page.screenshot(path=str(shots / "nav_2_apps.png"))

                print("\n=== HOP 3: sales home ===")
                to_sales_home(page)
                m = page.evaluate(_NAV_MAP_JS)
                print(f"url: {m['url']}")
                print("salesNavTabs:", json.dumps(m["salesNavTabs"], indent=2, ensure_ascii=False))
                page.screenshot(path=str(shots / "nav_3_saleshome.png"))

                print("\n=== HOP 4: lead lists ===")
                to_lead_lists(page)
                m = page.evaluate(_NAV_MAP_JS)
                print(f"url: {m['url']}")
                print("listRows:", json.dumps(m["listRows"], indent=2, ensure_ascii=False))
                page.screenshot(path=str(shots / "nav_4_lists.png"))
                print(f"\n[screenshots] {shots}", file=sys.stderr)
                ops.log_action("engine-nav", "scrape", target="nav-probe", result="ok")
            finally:
                safe_close(ctx)


def _active_page(page):
    """If a hop opened Sales Navigator in a NEW tab, switch to it. Returns the page
    the caller should keep using (always the frontmost real LinkedIn tab)."""
    try:
        ctx = page.context
        # prefer the newest page that's on a sales/linkedin URL and not blank
        for p in reversed(ctx.pages):
            try:
                if p is not page and "linkedin.com" in (p.url or ""):
                    p.bring_to_front()
                    return p
            except Exception:
                continue
    except Exception:
        pass
    return page


def human_open(page, target_url: str, *, warm: bool = True):
    """Walk a human path to target_url. Always starts on the feed (unless warm=False
    because the caller already warmed up this session), then hops by section. Returns
    the active page (a hop may have opened a new tab — callers should reassign:
    `page = nav.human_open(page, url)`)."""
    if warm:
        warm_up(page)
    if "/sales/" in target_url:
        to_sales_home(page)
        page = _active_page(page)          # Sales Nav may have opened in a new tab
        if "/sales/lists/" in target_url:
            to_lead_lists(page)
        open_target(page, target_url)
    elif "invite-connect/connections" in target_url:
        to_connections(page)          # first-degree Connections page (NOT the Sent page)
    elif "invitation-manager" in target_url or "/mynetwork" in target_url:
        to_network_sent(page)
    else:
        open_target(page, target_url)
    return page
