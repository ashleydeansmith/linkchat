"""salesnav.py — Sales Navigator source layer (the LH2 'collect from a list' step).

The connect pipeline is fed from Sales Navigator saved lead lists / searches (NOT the
vault — the vault is mostly people you're already connected to). This module:
  - --probe  : read-only recon of your saved lead lists (names, links, structure).
  - (later)  : collect a list's members into a CAMPAIGN, viewing each profile the
               human way and recording info into the CRM at pipeline stage 'collected'.

Read-only and human-paced. No Voyager/internal API. SELECTORS in --probe are
best-guess pending the live recon.
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
from . import db
from . import safe_close as _safe_close   # shared dead-browser-safe teardown
from .withdraw import _arg_int

AGENT = "engine-salesnav"
SHOTS = DATA_DIR / "screenshots"
LISTS_URL = "https://www.linkedin.com/sales/lists/people"

LISTS_JS = r"""() => {
  const trunc = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const links = [...document.querySelectorAll('a[href*="/sales/lists/people/"]')]
    .filter(a => a.offsetParent)
    .map(a => ({name: trunc(a.textContent, 60), href: a.getAttribute('href')}))
    .filter(x => x.name);
  const seen = new Set(); const lists = [];
  for (const l of links) { if (!seen.has(l.href)) { seen.add(l.href); lists.push(l); } }
  return {
    url: location.href,
    upsell: /try sales navigator|free trial|reactivate|sign in/i.test((document.body.innerText || '').slice(0, 500)),
    listCount: lists.length,
    lists: lists.slice(0, 40),
    bodyHint: trunc(document.body.innerText, 220),
  };
}"""


def probe() -> None:
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(LISTS_URL, wait_until="domcontentloaded", timeout=45_000)
                time.sleep(5)
                page.mouse.wheel(0, 900)
                time.sleep(2)
                print(json.dumps(page.evaluate(LISTS_JS), indent=2, ensure_ascii=False))
                SHOTS.mkdir(parents=True, exist_ok=True)
                shot = str(SHOTS / "salesnav_lists.png")
                page.screenshot(path=shot, full_page=True)
                print(f"[screenshot] {shot}", file=sys.stderr)
                ops.log_action(AGENT, "scrape", target="salesnav-lists", result="ok")
            finally:
                _safe_close(ctx)


ROWS_JS = r"""() => {
  const trunc = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const links = [...document.querySelectorAll('a[href*="/sales/lead/"]')].filter(a => a.offsetParent);
  const rows = []; const seen = new Set();
  for (const a of links) {
    const name = trunc(a.textContent, 40);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    let row = a;
    for (let i = 0; i < 5 && row; i++) { if ((row.textContent || '').length > 80) break; row = row.parentElement; }
    rows.push({name, href: a.getAttribute('href'), rowText: trunc(row ? row.textContent : '', 150)});
  }
  return {
    url: location.href,
    listName: trunc((document.querySelector('h1') || {}).textContent, 60),
    rowCount: rows.length,
    rows: rows.slice(0, 6),
    publicInLinks: document.querySelectorAll('a[href*="/in/"]').length,
    pagination: !!document.querySelector('.artdeco-pagination, [aria-label*="agination"], button[aria-label*="Next"]'),
    bodyHint: trunc(document.body.innerText, 200),
  };
}"""


def probe_list(href: str) -> None:
    url = href if href.startswith("http") else "https://www.linkedin.com" + href
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                try:   # works for lead lists AND saved-search result pages
                    page.wait_for_selector('a[href*="/sales/lead/"]', timeout=25_000)
                except Exception:
                    pass
                time.sleep(3)
                page.mouse.wheel(0, 1200)
                time.sleep(2)
                print(json.dumps(page.evaluate(ROWS_JS), indent=2, ensure_ascii=False))
                SHOTS.mkdir(parents=True, exist_ok=True)
                shot = str(SHOTS / "salesnav_list_rows.png")
                page.screenshot(path=shot, full_page=True)
                print(f"[screenshot] {shot}", file=sys.stderr)
                ops.log_action(AGENT, "scrape", target=url, result="ok")
            finally:
                _safe_close(ctx)


LEAD_JS = r"""() => {
  const trunc = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const inLinks = [...document.querySelectorAll('a[href*="/in/"]')]
    .map(a => a.getAttribute('href')).filter(Boolean).slice(0, 6);
  const an = (k) => trunc((document.querySelector('[data-anonymize="' + k + '"]') || {}).textContent, 80);
  return {
    url: location.href,
    name: an('person-name') || trunc((document.querySelector('h1') || {}).textContent, 60),
    title: an('job-title') || an('headline'),
    company: an('company-name'),
    location: an('location'),
    inLinks: inLinks,
    overflowButtons: [...document.querySelectorAll('button')]
      .map(b => (b.getAttribute('aria-label') || '').trim())
      .filter(a => /more|action|overflow|profile/i.test(a)).slice(0, 8),
  };
}"""


def probe_lead(href: str) -> None:
    url = href if href.startswith("http") else "https://www.linkedin.com" + href
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                time.sleep(6)
                print(json.dumps(page.evaluate(LEAD_JS), indent=2, ensure_ascii=False))
                try:   # expand the actions overflow (...) menu to find the public LinkedIn URL
                    ovf = page.get_by_role("button", name=re.compile(r"actions overflow|open actions", re.I))
                    if ovf.count():
                        ovf.first.click()
                        time.sleep(2.0)
                        SHOTS.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(SHOTS / "salesnav_lead_menu.png"))
                        menu = page.evaluate(r"""() => {
                          const vis = e => e.offsetParent && (e.textContent || '').trim();
                          return [...document.querySelectorAll('a, button, [role="menuitem"], li')]
                            .filter(vis)
                            .map(e => ({t: (e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 44),
                                        href: e.getAttribute('href') || null}))
                            .filter(x => /linkedin|profile|copy|view|url/i.test(x.t) || (x.href && x.href.includes('/in/')))
                            .slice(0, 24);
                        }""")
                        print("menu-items ->", json.dumps(menu, ensure_ascii=False))
                        page.keyboard.press("Escape")
                except Exception as e:  # noqa: BLE001
                    print("[ovf err]", str(e)[:60])
                SHOTS.mkdir(parents=True, exist_ok=True)
                shot = str(SHOTS / "salesnav_lead.png")
                page.screenshot(path=shot, full_page=True)
                print(f"[screenshot] {shot}", file=sys.stderr)
                ops.log_action(AGENT, "profile_view", target=url, result="ok")
            finally:
                _safe_close(ctx)


# ---------------------------------------------------------------------------
# Collect step — source (lead list / saved search / all-saved-leads) -> campaign
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_closed_target(e: Exception) -> bool:
    """True when an exception is Playwright telling us the page/context/browser went
    away mid-operation (tab crash, session contention) rather than a logic bug."""
    msg = str(e).lower()
    return "closed" in msg or "target page" in msg or "crash" in msg


def _arg_str(flag: str, default: str | None = None) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


# ---------------------------------------------------------------------------
# Shared lead-PANEL access (used by BOTH inmail and connect — single source of
# truth, not duplicated per lane). A Sales Nav lead is reached by navigating to its
# source list and CLICKING its row to open the right-hand detail panel — never by a
# cold `page.goto('/sales/lead/{id}')`, which HANGS on the SPA app-shell loader
# (the lead route cold-boots the whole Sales Nav app). Validated read-only 2026-06-13.
# ---------------------------------------------------------------------------

def first_visible(loc, limit: int = 8):
    """First VISIBLE element of a locator, or None (avoids .first grabbing a hidden dup)."""
    for i in range(min(loc.count(), limit)):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return None


def lead_urn(profile_url: str) -> str:
    """Stable Sales Nav lead identity from a /sales/lead/{id} URL — the URN before any
    ',NAME_SEARCH,...' suffix. Used to match the lead's ROW on its source list."""
    tail = profile_url.split("/sales/lead/")[-1]
    return tail.split("?")[0].split(",")[0]


def campaign_source(campaign_id) -> str | None:
    """The Sales Nav list/search URL a campaign's leads were collected from (targeting.ref)."""
    if not campaign_id:
        return None
    c = db.campaign_get(campaign_id)
    return (c or {}).get("source") or None


def wait_lead_ready(page, timeout: float = 30.0) -> bool:
    """Poll until the lead's Message control (link OR button) appears — the signal the
    detail panel has rendered. Sales Nav is a heavy SPA; a fixed sleep races the loader."""
    deadline = time.time() + timeout
    rx = re.compile(r"^message\b", re.I)
    while time.time() < deadline:
        try:
            page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:
            pass
        for role in ("button", "link"):
            if page.get_by_role(role, name=rx).count():
                return True
        time.sleep(1.0)
    return False


def open_lead_panel(page, urn: str, max_pages: int = 8) -> bool:
    """On the (already-open) source list, find the lead's row by URN and click it to open
    the right-hand DETAIL PANEL. Pages with Next until found/exhausted. Returns True if
    the panel opened (its Message control is present)."""
    for _pg in range(max_pages):
        try:
            page.wait_for_selector('a[href*="/sales/lead/"]', timeout=12_000)
        except Exception:
            pass
        for _ in range(5):   # inner-scroll the page's rows in (virtualised list)
            links = page.locator('a[href*="/sales/lead/"]')
            try:
                links.nth(max(links.count() - 1, 0)).scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                page.mouse.wheel(0, 1600)
            time.sleep(0.6)
        row = page.locator(f'a[href*="{urn}"]')
        if row.count():
            r0 = first_visible(row, 6) or row.first
            try:
                r0.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass
            time.sleep(0.6)
            r0.click(timeout=8_000)
            return wait_lead_ready(page, timeout=20)
        try:    # not on this page → Next
            nxt = page.get_by_role("button", name=re.compile(r"^next$", re.I))
            if not nxt.count() or not nxt.first.is_enabled():
                return False
            nxt.first.click()
            time.sleep(random.uniform(2.5, 4.0))
        except Exception:
            return False
    return False


def public_url_from_panel(page) -> str | None:
    """With a lead-detail panel OPEN, read the public /in/ URL from its actions-overflow
    menu's 'View LinkedIn profile' link. Replaces the cold-goto to /sales/lead/ (which
    hangs) for connect's URL resolution. Read-only. Returns the clean /in/ URL or None."""
    for lbl in ("actions", "overflow", "more"):
        bb = first_visible(page.locator(f'button[aria-label*="{lbl}" i]'), 6)
        if bb is None:
            continue
        try:
            bb.click()
            time.sleep(1.2)
            link = page.locator('a[href*="linkedin.com/in/"], a[href^="/in/"]')
            for i in range(min(link.count(), 8)):
                if link.nth(i).is_visible():
                    h = link.nth(i).get_attribute("href")
                    if h and "/in/" in h:
                        try:
                            page.keyboard.press("Escape")
                        except Exception:
                            pass
                        return h.split("?")[0]
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
        except Exception:
            continue
    return None


def _canon_in(href: str | None) -> str | None:
    """Deprecated alias — the ONE implementation now lives in `engine.canon`.

    Kept because `connections.py` and others import this name. The old body missed the
    `/en` locale suffix the connections scraper emits and scheme-less hosts; both would
    have inserted duplicate `leads` rows once the scraper ran at scale.
    """
    from .canon import canon_in
    return canon_in(href)


def _public_url(page) -> str | None:
    """Get the lead's public /in/ URL. Fast path: any /in/ link already on the page
    (content-rich leads). Fallback: open the actions overflow (...) menu and read the
    'View LinkedIn profile' href — reliable for EVERY lead, including content-less ones."""
    href = page.evaluate("""() => {
      const a = document.querySelector('a[href*="/in/"]');
      return a ? a.getAttribute('href') : null;
    }""")
    if href:
        return _canon_in(href)
    try:
        ovf = page.get_by_role("button", name=re.compile(r"actions overflow|open actions", re.I))
        if ovf.count():
            ovf.first.click()
            time.sleep(1.3)
            href = page.evaluate("""() => {
              const links = [...document.querySelectorAll('a[href*="/in/"]')];
              const v = links.find(a => /view linkedin profile/i.test(a.textContent || '')) || links[0];
              return v ? v.getAttribute('href') : null;
            }""")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return _canon_in(href)
    except Exception:
        pass
    return None


def _ensure_campaign(name: str, source_type: str, source_ref: str) -> int:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO campaigns (name, type, targeting, status, created_at) VALUES (?,?,?,?,?)",
            (name, "connect", json.dumps({"source_type": source_type, "ref": source_ref}),
             "active", _now()),
        )
        return cur.lastrowid


def _collect_lead_rows(page) -> list[dict]:
    """Read the list rows: name + canonical lead id (/sales/lead/{ID}) PLUS the
    title/company/location the row already shows. PURE list-read — never opens a
    profile (the v1.2 rule); we only read what's already on the row. Fields degrade
    to '' when the row doesn't carry them, so personalisation re-rolls gracefully."""
    return page.evaluate(r"""() => {
      const out = {};
      const clean = s => (s || '').replace(/\s+/g, ' ').replace(/ is reachable$| was last active.*$/i, '').trim();
      const anon = (row, k) => {
        const e = row && row.querySelector('[data-anonymize="' + k + '"]');
        return e ? clean(e.textContent) : '';
      };
      for (const a of document.querySelectorAll('a[href*="/sales/lead/"]')) {
        const h = a.getAttribute('href'); if (!h) continue;
        const id = h.split('?')[0].split(',')[0];   // /sales/lead/{ID}
        const name = clean(a.textContent);
        if (!name) continue;
        const row = a.closest('li') || a.closest('tr') || a.parentElement;
        // NOTE: on the SEARCH-results DOM data-anonymize="title" is the real job title
        // ("Owner"/"CEO"/"Director"); data-anonymize="job-title" is TENURE ("3 yrs in
        // role") — LinkedIn's misnomer. Prefer 'title', never fall back to 'job-title'.
        const rec = {id, name, href: h,
                     title: anon(row, 'title') || anon(row, 'headline'),
                     company: anon(row, 'company-name'),
                     location: anon(row, 'location')};
        // keep the row with the shortest (cleanest) name token, but preserve any
        // title/company we managed to read across duplicate anchors
        if (!out[id]) { out[id] = rec; }
        else {
          if (name.length < out[id].name.length) out[id].name = name;
          out[id].title = out[id].title || rec.title;
          out[id].company = out[id].company || rec.company;
          out[id].location = out[id].location || rec.location;
        }
      }
      return Object.values(out);
    }""")


# Scroll the RESULTS CONTAINER (the scrollable inner div that holds the lead rows),
# ~one viewport per call. Confirmed live (2026-06-15): Sales Nav search results live in
# a scrollable <div overflow-y-auto>, NOT the window — and Playwright's
# scroll_into_view_if_needed is a NO-OP once the last row is already visible, so the
# list never advanced. This climbs from a lead row to its scrollable ancestor (else the
# largest scrollable element containing lead links, else the window) and forces a scroll.
# Returns true if it actually moved (false => at the bottom).
_SCROLL_RESULTS_JS = r"""() => {
  const scrollable = el => { const cs = getComputedStyle(el);
    return (cs.overflowY === 'auto' || cs.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 40; };
  const hasLeads = el => el && el.querySelector && el.querySelector('a[href*="/sales/lead/"]');
  let box = null;
  const lead = document.querySelector('a[href*="/sales/lead/"]');
  let el = lead && lead.parentElement;
  while (el) { if (scrollable(el)) { box = el; break; } el = el.parentElement; }
  if (!box) { let bs = 0;
    for (const d of document.querySelectorAll('div,main,section,ul')) {
      if (scrollable(d) && hasLeads(d) && d.scrollHeight > bs) { bs = d.scrollHeight; box = d; } } }
  if (box) { const b = box.scrollTop;
    box.scrollTop = Math.min(box.scrollHeight, box.scrollTop + Math.round(box.clientHeight * 0.9));
    return box.scrollTop > b; }
  const se = document.scrollingElement; const b = se.scrollTop;
  window.scrollBy(0, Math.round(se.clientHeight * 0.9)); return se.scrollTop > b;
}"""


def _rows_count(page) -> int:
    """How many lead-row anchors are currently in the DOM (skeleton placeholders carry
    no /sales/lead/ href, so this counts only REAL rows)."""
    try:
        return int(page.evaluate(
            "() => document.querySelectorAll('a[href*=\"/sales/lead/\"]').length"))
    except Exception:  # noqa: BLE001
        return 0


def _wait_rows_settled(page, tries: int = 14) -> int:
    """Wait until the real lead rows have rendered and their count holds steady for two
    reads — so we never read a page mid-paint (skeletons) and mistake a still-loading page
    for an empty/last one. Returns the settled row count (0 only if none ever appeared)."""
    last, stable = -1, 0
    for _ in range(tries):
        c = _rows_count(page)
        if c > 0 and c == last:
            stable += 1
            if stable >= 2:
                return c
        else:
            stable = 0
        last = c
        time.sleep(1.0)
    return _rows_count(page)


def _gather_rows(page, max_n: int, on_page=None) -> list[dict]:
    """Page a Sales Nav source (Next button), reading list rows (name + id) up to max_n.
    Pure list-read — never opens a profile.

    CRITICAL: the Sales Nav lead list is VIRTUALISED — only ~7-8 rows live in the DOM
    at any moment, and scrolling REMOVES the rows above as it adds rows below. So we
    must read the rows REPEATEDLY as we scroll a page down, accumulating them, NOT once
    at the end (reading once only captures the final window — the 'first 7-8' bug).
    Per page: wait for it to load -> scroll-and-accumulate until no new rows -> Next.

    If `on_page(new_rows)` is given, it's called with each page's NEW (deduped) rows as
    they're found, so the caller can persist + report incrementally."""
    rows: list[dict] = []
    seen: set[str] = set()

    def _merge(dst: dict, r: dict) -> None:
        ex = dst.get(r["id"])
        if not ex:
            dst[r["id"]] = dict(r)
            return
        if len(r["name"]) < len(ex["name"]):
            ex["name"] = r["name"]
        ex["title"] = ex.get("title") or r.get("title")
        ex["company"] = ex.get("company") or r.get("company")
        ex["location"] = ex.get("location") or r.get("location")

    for _pg in range(160):   # page bound (~4000 leads) — effectively "all" for a source
        # 1. wait for this page's REAL rows to render. Sales Nav paints SKELETON
        # placeholders first (no /sales/lead/ anchors), then swaps in real rows a beat
        # later — reading too early yields 0/partial rows and (worse) makes the page look
        # "empty/last". So wait until the lead-anchor count settles at >0 (2026-07-07 fix).
        try:
            page.wait_for_selector('a[href*="/sales/lead/"]', timeout=20_000)
        except Exception:
            pass
        _wait_rows_settled(page)
        # 2. scroll DOWN the virtualised list, re-reading + accumulating each window
        page_seen: dict = {}
        stable = 0
        closed = False
        for _step in range(80):   # cover a full page through the virtualised window
            try:
                chunk = _collect_lead_rows(page)
            except Exception as e:  # noqa: BLE001
                if _is_closed_target(e):
                    closed = True
                    break
                raise
            before = len(page_seen)
            for r in chunk:
                _merge(page_seen, r)
            grew = len(page_seen) - before
            if len(rows) + len(page_seen) >= max_n:
                break
            # scroll the RESULTS CONTAINER itself one viewport (forces progress where
            # scroll_into_view was a no-op), then re-read on the next iteration
            try:
                moved = bool(page.evaluate(_SCROLL_RESULTS_JS))
            except Exception:
                moved = False
            time.sleep(random.uniform(0.7, 1.1))
            if grew == 0 and not moved:
                stable += 1
                if stable >= 2:
                    break   # at the bottom AND no new rows → page fully read
            else:
                stable = 0
        # 3. fold this page's accumulated rows into the global set + report incrementally
        new_rows = []
        for r in page_seen.values():
            if r["id"] not in seen:
                seen.add(r["id"])
                rows.append(r)
                new_rows.append(r)
                if len(rows) >= max_n:
                    break
        if new_rows and on_page:
            on_page(new_rows)
        if closed:
            print(f"  [gather stopped] browser closed after {len(rows)} leads — "
                  f"keeping what was collected")
            break
        if len(rows) >= max_n:
            break
        # 4. go to the next page. CRITICAL (2026-07-07 page-6 fix): LinkedIn DISABLES the
        # Next button while a page is loading, so a disabled Next is NOT proof of the last
        # page — reading it as end-of-list is exactly what stopped collection early (e.g.
        # "stopped at page 6"). Re-check Next a few times with short waits; only treat it as
        # the real last page if it stays absent/disabled after the page has settled.
        def _next_btn():
            return page.get_by_role("button", name=re.compile(r"^next$", re.I))
        clickable = False
        for _ in range(6):
            try:
                nb = _next_btn()
                if nb.count() and nb.first.is_enabled():
                    clickable = True
                    break
            except Exception as e:  # noqa: BLE001
                if _is_closed_target(e):
                    closed = True
                    break
            time.sleep(1.5)
        if closed:
            print(f"  [gather stopped] browser closed after {len(rows)} leads — "
                  f"keeping what was collected")
            break
        if not clickable:
            print(f"  [gather] Next disabled after settle → last page. "
                  f"Collected {len(rows)} across {_pg + 1} page(s).")
            break   # genuinely the last page
        try:
            _next_btn().first.click()
        except Exception as e:  # noqa: BLE001
            if _is_closed_target(e):
                print(f"  [gather stopped] browser closed paginating after "
                      f"{len(rows)} leads — keeping what was collected")
            break
        time.sleep(random.uniform(2.5, 4.0))
    return rows[:max_n]


def collect(source_url: str, name: str, max_n: int, dry_run: bool) -> None:
    """Open a Sales Nav source (list / saved search), take up to max_n leads, open each
    lead's detail page for its public /in/ URL + info, and upsert into `name`'s campaign
    at stage 'collected'. Dry-run previews (visits leads, no DB writes). Human-paced."""
    camp_id = _ensure_campaign(name, "salesnav", source_url)   # full URL so 'Collect more' can reuse it
    collected = 0
    previewed = 0
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=300) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # Human navigation: feed -> Sales Nav -> Leads -> this list (not a deep link)
                from . import nav
                page = nav.human_open(page, source_url)
                try:
                    page.wait_for_selector('a[href*="/sales/lead/"]', timeout=25_000)
                except Exception:
                    pass
                time.sleep(3)
                # Seat check (bug fix 2026-07-06): with no Sales Nav seat LinkedIn bounces
                # /sales/ URLs to its upsell page — the old behaviour read 0 rows and exited
                # "ok", a silent failure. Fail LOUD, and write the live observation back to
                # the capability cache (a lane run IS a detection).
                url = page.url or ""
                lead_links = page.locator('a[href*="/sales/lead/"]').count()
                upsell = bool(page.evaluate(
                    "() => /try sales navigator|start your free trial|buy now|choose your plan"
                    "|reactivate/i.test(((document.body && document.body.innerText) || '').slice(0, 800))"))
                if any(k in url for k in ("login", "authwall", "checkpoint")):
                    from . import emit_result
                    msg = "LinkedIn session is signed out — connect LinkedIn first, then collect."
                    print(f"[no-session] {msg}", file=sys.stderr)
                    ops.log_action(AGENT, "scrape", target=f"collect:{name}",
                                   result="failed", detail="signed out")
                    emit_result("salesnav", False, msg)
                    return
                if ("/sales/" not in url) or (upsell and not lead_links):
                    from . import capability, emit_result
                    capability.note_seat_observed(False)
                    msg = ("no Sales Navigator seat on this account — LinkedIn bounced the list "
                           "to its upsell page. This source is unavailable on your plan; use "
                           "your connections, post-engagers, a CSV or search instead.")
                    print(f"[no-seat] {msg}", file=sys.stderr)
                    ops.log_action(AGENT, "scrape", target=f"collect:{name}",
                                   result="failed", detail="no Sales Navigator seat")
                    emit_result("salesnav", False, msg)
                    return
                from . import capability
                capability.note_seat_observed(True)   # inside the SN app = seat confirmed
                print(f"campaign '{name}' (id {camp_id}); reading the list page-by-page "
                      f"({'DRY-RUN preview' if dry_run else 'COMMIT'}) — no profiles visited")

                def _persist_page(new_rows):
                    """Write + report each page's leads AS they're read, so the count
                    climbs live in the app rather than only at the very end."""
                    nonlocal collected, previewed
                    if dry_run:
                        for r in new_rows:
                            previewed += 1
                            print(f"  [dry] {r['name'][:34]:<34} https://www.linkedin.com{r['id']}")
                        return
                    with db.connect() as conn:
                        for r in new_rows:
                            snurl = "https://www.linkedin.com" + r["id"]
                            db.upsert_lead(conn, profile_url=snurl, full_name=r["name"],
                                           title=r.get("title") or None,
                                           company=r.get("company") or None,
                                           location=r.get("location") or None,
                                           source="salesnav", status="collected")
                            conn.execute("UPDATE leads SET sales_nav_url=?, campaign_id=? "
                                         "WHERE profile_url=?", (snurl, camp_id, snurl))
                            collected += 1
                            # this 'collected ' line is what the app's live-refresh watches
                            print(f"  collected {r['name']}  ({collected} so far)")

                _gather_rows(page, max_n, on_page=_persist_page)
                ops.log_action(AGENT, "scrape", target=f"collect:{name}", result="ok")
            except Exception as e:  # noqa: BLE001
                import traceback as _tb
                from . import emit_result
                _tb.print_exc()
                ops.log_action(AGENT, "scrape", target=f"collect:{name}", result="failed",
                               detail=str(e)[:160])
                emit_result("collect", False, f"Collect failed: {str(e)[:140]}")
                return
            finally:
                _safe_close(ctx)
    n = previewed if dry_run else collected
    print(f"\n[done] {'previewed ' + str(previewed) if dry_run else 'collected ' + str(collected)} "
          f"for '{name}'.")
    from . import emit_result
    if n == 0:
        emit_result("collect", False,
                    f"No leads found for '{name}' — the page may not have loaded the list, "
                    f"or the source URL isn't a lead list / saved search.")
    else:
        emit_result("collect", True,
                    (f"Rehearsal — previewed {n} lead(s) for '{name}'" if dry_run
                     else f"Collected {n} lead(s) into '{name}'"), count=n, campaign=name)


def gather_preview(source_url: str, max_n: int) -> None:
    """Page the source and report how many unique lead hrefs it yields — fast pagination
    check, no per-lead visits."""
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # Human navigation: feed -> Sales Nav -> Leads -> this list (not a deep link)
                from . import nav
                page = nav.human_open(page, source_url)
                try:
                    page.wait_for_selector('a[href*="/sales/lead/"]', timeout=25_000)
                except Exception:
                    pass
                time.sleep(3)
                rows = _gather_rows(page, max_n)
                print(f"gathered {len(rows)} unique leads (max {max_n})")
                for r in rows[:5]:
                    print("  ", r["name"], "->", r["id"])
                ops.log_action(AGENT, "scrape", target="gather", result="ok")
            finally:
                _safe_close(ctx)


def probe_rowmenu(source_url: str) -> None:
    """Inspect the FIRST result row's ... (overflow) menu on the LIST/SEARCH page — to
    confirm the public LinkedIn URL is reachable WITHOUT visiting each profile."""
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # Human navigation: feed -> Sales Nav -> Leads -> this list (not a deep link)
                from . import nav
                page = nav.human_open(page, source_url)
                try:
                    page.wait_for_selector('a[href*="/sales/lead/"]', timeout=25_000)
                except Exception:
                    pass
                time.sleep(3)
                rowbtns = page.get_by_role("button", name=re.compile(r"overflow|actions|more options", re.I))
                print("row overflow buttons found:", rowbtns.count())
                if rowbtns.count():
                    try:
                        rowbtns.first.scroll_into_view_if_needed(timeout=4000)
                        rowbtns.first.hover()
                        time.sleep(0.5)
                    except Exception:
                        pass
                    rowbtns.first.click()
                    time.sleep(1.5)
                    SHOTS.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(SHOTS / "salesnav_rowmenu.png"))
                    menu = page.evaluate(r"""() => {
                      return [...document.querySelectorAll('a, button, [role="menuitem"]')]
                        .filter(e => e.offsetParent && (e.textContent || '').trim())
                        .map(e => ({t: (e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
                                    href: e.getAttribute('href') || null}))
                        .filter(x => /linkedin|profile|copy|view|url/i.test(x.t) || (x.href && x.href.includes('/in/')))
                        .slice(0, 20);
                    }""")
                    print("rowmenu ->", json.dumps(menu, ensure_ascii=False))
            finally:
                _safe_close(ctx)


def main() -> None:
    if "--rowmenu" in sys.argv:
        src = next((a for a in sys.argv if a.startswith("http") or a.startswith("/sales/")), None)
        if not src:
            print("usage: salesnav --rowmenu <source_url>")
            return
        probe_rowmenu(src)
    elif "--gather" in sys.argv:
        src = next((a for a in sys.argv if a.startswith("http") or a.startswith("/sales/")), None)
        if not src:
            print("usage: salesnav --gather <source_url> --max N")
            return
        gather_preview(src, _arg_int("--max") or 100000)
    elif "--collect" in sys.argv:
        src = next((a for a in sys.argv if a.startswith("http") or a.startswith("/sales/")), None)
        if not src:
            print("usage: salesnav --collect <source_url> --name <name> [--max N] [--commit]")
            return
        collect(src, _arg_str("--name") or "Sales Nav collect",
                _arg_int("--max") or 100000, dry_run="--commit" not in sys.argv)   # default = whole source
    elif "--lead" in sys.argv:
        href = next((a for a in sys.argv if "/sales/lead/" in a), None)
        if not href:
            print("usage: salesnav --lead /sales/lead/<id>")
            return
        probe_lead(href)
    elif "--list" in sys.argv:
        href = next((a for a in sys.argv if a.startswith("/sales/lists/") or a.startswith("http")), None)
        if not href:
            print("usage: salesnav --list /sales/lists/people/<id>")
            return
        probe_list(href)
    elif "--probe" in sys.argv:
        probe()
    else:
        print("salesnav: --probe (lists) | --list <href> (rows) | --lead <href> (one lead's public URL)")


if __name__ == "__main__":
    main()
