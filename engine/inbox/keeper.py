"""keeper.py — the inbox half's read-only bridge to the ONE shared LinkedIn keeper.

the inbox half does NOT own a browser. It connects to the SAME keeper Chromium the parent program
runs (the only process allowed to open the linkedin-session profile), holds the SAME
shared READ_LOCK every the parent program lane holds while driving it, checks the SAME 'scrape'
budget, logs to the SAME ledger, and reads — never writes — the already-rendered DOM.

The two DOM selector strings are vendored from the parent program's inbox (LIST_JS / READ_JS) so
the inbox half can run before the later merge; selftest() fails LOUD if the live DOM no
longer matches, so selector drift surfaces as an error rather than silent bad data.
After the merge these collapse back to `from the parent program import inbox`.

Heavy deps (playwright, engine.browser, linkedin_browser) are imported lazily inside
the functions that drive the browser, so importing this module for the pure helpers
(read_list / selftest parsing) or the DB CLI stays cheap and can't fail at import time.
"""
from __future__ import annotations

import contextlib
import re

from . import AGENT

INBOX_URL = "https://www.linkedin.com/messaging/"
_THREAD_RE = re.compile(r"/messaging/thread/([^/?#]+)")
_MONTH = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b")

# LinkedIn stamps a conversation from TODAY with a clock time and an older one
# with a month. Only the month was looked for, so every conversation from today
# fell past it to a blunt 40-character cut and kept its timestamp and the start
# of the message inside the person's name: "Marcus Oyelaran 10:42 AM 10:42 AM
# You:". Found 2026-08-25 on a real inbox - eleven of twelve rows were from
# today, so eleven of twelve names were wrong. It had gone unseen because a
# reply older than today parses correctly.
_TIME = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\b")
# Some rows carry neither, and lead with a relative age instead.
_AGO = re.compile(r"\b\d+\s*(?:m|h|d|w|mo|y)\b|\byesterday\b", re.I)

# --- vendored DOM selectors (source of truth: the parent program's inbox) -------------------

# List rows. Per row capture: the thread URN (from the row's thread anchor href when
# present), the collapsed innerText (name + latest-message preview, author-prefixed),
# and the link href if any. The URN MAY be absent in the list DOM (LinkedIn sometimes
# only exposes it on open) — selftest + the deep-read fallback (page.url after opening a
# thread) handle that; an empty URN is never trusted as a key.
LIST_JS = r"""() => {
  return [...document.querySelectorAll('.msg-conversation-listitem')].map(it => {
    const a = it.querySelector('a[href*="/messaging/thread/"]');
    const href = a ? a.getAttribute('href') : '';
    const m = (href || '').match(/\/messaging\/thread\/([^/?#]+)/);
    return {
      urn:  m ? m[1] : '',
      href: href || '',
      text: (it.innerText || '').replace(/\s+/g, ' ').trim(),
    };
  }).filter(r => r.text);
}"""

# Classify every bubble in the currently-open thread (incoming = '--other'). The text
# node is normally `__body`; fall back to `__message-bubble` for the variants that omit
# it (confirmed live 2026-06-22 — both yield identical text on a normal thread).
READ_JS = r"""() => {
  return [...document.querySelectorAll('.msg-s-event-listitem')].map(it => {
    const c = it.getAttribute('class') || '';
    const b = it.querySelector('.msg-s-event-listitem__body')
           || it.querySelector('.msg-s-event-listitem__message-bubble');
    return { dir: c.includes('--other') ? 'in' : 'out',
             text: b ? b.innerText.trim() : '' };
  }).filter(m => m.text);
}"""


# --- pure parsing helpers (no browser) ----------------------------------------------

def _row_pending(text: str) -> bool:
    """Row is reply-pending iff the latest-message preview author is NOT us."""
    return ("You: " not in text) and ("You sent" not in text)


def _row_name(text: str) -> str:
    """Participant name = the text before whatever stamps the row with a time.

    A row reads "<name> <when> <who said it>: <preview>". Cut at the earliest of
    the three ways LinkedIn writes <when> - a month for anything older, a clock
    time for today, a relative age on some rows - rather than at a month alone,
    which left every conversation from today carrying its timestamp inside the
    person's name.

    The 40-character fallback stays for a row that carries none of them, but it
    is now the rare case rather than the everyday one.
    """
    t = re.sub(r"^Status is (online|reachable)\s*", "", text)
    cuts = [m.start() for m in (_MONTH.search(t), _TIME.search(t), _AGO.search(t))
            if m]
    if cuts:
        return t[:min(cuts)].strip()
    # No stamp at all: fall back, but never mid-word and never past a preview.
    head = t.split(":")[0] if ":" in t[:60] else t
    return head[:40].strip()


# --- browser-driving helpers (read-only) --------------------------------------------

_PROFILE_LINKS_JS = r"""() => {
  const out = [];
  document.querySelectorAll('a[href*="/in/"]').forEach(a => {
    out.push({href: a.getAttribute('href') || '',
              text: (a.innerText || '').replace(/\s+/g, ' ').trim()});
  });
  return out;
}"""


def _canon_profile(href: str) -> str:
    """A profile link with the tracking and the trailing slash taken off."""
    h = (href or "").split("?")[0].rstrip("/")
    if h.startswith("/"):
        h = "https://www.linkedin.com" + h
    return h


def thread_profile_url(page, name: str | None) -> str | None:
    """The OTHER person's profile link, read off the thread that is open.

    Why this is worth doing: without it the only thing LinkChat knows about a
    person is the name on the row, and a name is a weak key - two people share
    one, and a CRM keyed on a name cannot tell them apart. The profile link is
    the durable one, and it is what makes the CRM able to say "this event was
    about THAT person" instead of recording it against nobody.

    Why it is read off the page rather than asked for: an open thread shows
    both people, so the link is there for a person to see, and reading what is
    on the page is how the rest of this file works. The other way - calling
    LinkedIn's own internal interface - was deliberately taken out of LinkChat
    and is not coming back in through a side door.

    BOTH people are linked on an open thread, so the match is on the name we
    already hold. No name, or no link carrying it, returns None: a wrong
    profile link is worse than none, because it would file this conversation
    under somebody else.
    """
    if not name:
        return None
    try:
        links = page.evaluate(_PROFILE_LINKS_JS) or []
    except Exception:
        return None

    want = re.sub(r"[^a-z ]", "", str(name).lower()).strip()
    if not want:
        return None
    first = want.split()[0]

    best = None
    for l in links:
        href, text = l.get("href") or "", l.get("text") or ""
        if "/in/" not in href:
            continue
        got = re.sub(r"[^a-z ]", "", text.lower()).strip()
        if not got:
            continue
        if got.startswith(want) or want in got:
            return _canon_profile(href)        # full name: the strongest match
        # "View Sabina's profile" - the first name, which is still theirs and
        # never the signed-in person's unless they share a first name.
        if best is None and first and re.search(r"\b%s\b" % re.escape(first), got):
            best = _canon_profile(href)
    return best


def keeper_running() -> bool:
    """True iff the shared keeper's CDP endpoint is answering. No spawn, no drive."""
    try:
        from engine import browser as B
        return bool(B.keeper_running())
    except Exception:
        return False


def read_list(page, max_rows: int = 50) -> list[dict]:
    """Open the inbox and return up to max_rows raw list rows. Read-only."""
    page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3500)
    rows = page.evaluate(LIST_JS) or []
    return rows[:max_rows]


# The clickable conversation element (LinkedIn exposes the thread URN only on the URL
# AFTER you open it — the list rows carry neither href nor URN, confirmed live 2026-06-22).
CONVO_LINK_SEL = ".msg-conversations-container__convo-item-link"


LISTITEM_SEL = ".msg-conversation-listitem"


def read_current_thread(page) -> tuple[str, list[dict]]:
    """Read whatever thread is currently open in the right pane: wait for bubbles, then
    return (canonical_thread_urn from page.url, classified_messages). Read-only."""
    page.wait_for_timeout(2500)
    with contextlib.suppress(Exception):
        page.wait_for_selector(".msg-s-event-listitem", timeout=8000)
    m = _THREAD_RE.search(page.url or "")
    return (m.group(1) if m else ""), (page.evaluate(READ_JS) or [])


def open_thread_by_index(page, index: int) -> tuple[str, list[dict]]:
    """Open conversation #index by CLICKING its list element (LinkedIn messaging is a
    single page: clicking loads the thread in the right pane and updates the URL to
    /messaging/thread/<urn>/). Returns (canonical_thread_urn, classified_messages).
    Read-only — a click that only SELECTS a conversation sends nothing."""
    links = page.query_selector_all(CONVO_LINK_SEL)
    if index >= len(links):
        return "", []
    with contextlib.suppress(Exception):
        links[index].click(timeout=8000)
    return read_current_thread(page)


def open_listitem(page, item_handle) -> tuple[str, list[dict]]:
    """Open a thread from a given list-item element handle (click its convo link, or the
    item itself as fallback) and read it. Read-only."""
    link = None
    with contextlib.suppress(Exception):
        link = item_handle.query_selector(CONVO_LINK_SEL)
    with contextlib.suppress(Exception):
        (link or item_handle).click(timeout=8000)
    return read_current_thread(page)


def visible_listitems(page) -> list:
    """Current visible conversation list-item element handles (virtualised — only the
    rows near the viewport exist in the DOM at any moment)."""
    return page.query_selector_all(LISTITEM_SEL)


def scroll_list(page, settle_ms: int = 1300) -> int:
    """Scroll the last visible list item into view to lazy-load more, then return the new
    visible row count. The list is virtualised, so this is how we reach a 600-inbox."""
    items = page.query_selector_all(LISTITEM_SEL)
    if items:
        with contextlib.suppress(Exception):
            items[-1].scroll_into_view_if_needed(timeout=3000)
    page.wait_for_timeout(settle_ms)
    return len(page.query_selector_all(LISTITEM_SEL))


def item_text(item_handle) -> str:
    """Collapsed innerText of a list-item handle (name + latest-message preview)."""
    try:
        return (item_handle.inner_text() or "").replace("\n", " ").strip()
    except Exception:
        return ""


# The real profile photo carries 'profile-displayphoto' in its licdn src (the ghost/default
# avatar does not), so this selector captures a photo only when the contact has one.
AVATAR_SEL = 'img[src*="profile-displayphoto"]'


def row_avatar(item_handle) -> str:
    """Profile-photo URL for a list-item handle, or '' if the contact has no photo."""
    try:
        img = item_handle.query_selector(AVATAR_SEL)
        return img.get_attribute("src") if img else ""
    except Exception:
        return ""


_AVATARS_JS = """() => [...document.querySelectorAll('.msg-conversation-listitem')]
    .map(it => { const img = it.querySelector('img[src*="profile-displayphoto"]');
                 return { name:(img && img.alt || '').trim(), src: img ? img.src : '' }; })
    .filter(r => r.name && r.src);"""


def avatars_on_page(page) -> list[dict]:
    """Avatars currently rendered (no navigation) — call repeatedly while scrolling."""
    return page.evaluate(_AVATARS_JS) or []


def read_list_avatars(page) -> list[dict]:
    """One inbox load + read (no scroll). For the scrolled full backfill use avatars_on_page
    inside a scroll loop (LinkedIn lazy-loads photos as you scroll)."""
    page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3500)
    return avatars_on_page(page)


# How LinkChat knows a conversation is open on screen: the reply box exists on the
# page. It is used to LOOK, never to write - nothing in LinkChat puts text in it.
THREAD_IS_OPEN_SEL = ".msg-form__contenteditable"
# The Send control, pressed only for a message that has cleared the gate.
SEND_BTN_SEL = "button.msg-form__send-button, button.msg-form__send-btn, .msg-form__send-toggle"


MSG_SEARCH_SELS = ('#search-conversations',
                   'input[placeholder*="Search messages" i]',
                   'input[aria-label*="Search messages" i]',
                   '.msg-search-form__input', 'input[role="combobox"][type="text"]')


# The conversation title (name) inside a row. Probed live 2026-07-24: inbox rows carry NO
# anchor and the thread_urn appears NOWHERE in the DOM — LinkedIn keeps that mapping internal.
# The ONLY DOM handle on a conversation is the participant NAME in its title, so nav matches by name.
NAME_TITLE_SEL = (".msg-conversation-listitem__participant-names, "
                  ".msg-conversation-card__participant-names")


def _row_matches_name(item, name: str) -> bool:
    """Does this conversation row belong to `name`? Prefer the title (participant-names) but fall
    back to the row's full text. Requires BOTH first and last name when we have them, so a common
    first name ('Paul') can't grab the wrong row."""
    nm = (name or "").lower().strip()
    if not nm:
        return False
    title = ""
    with contextlib.suppress(Exception):
        el = item.query_selector(NAME_TITLE_SEL)
        title = (el.inner_text() if el else "") or ""
    hay = (title or (item.inner_text() or "")).lower()
    if nm in hay:
        return True
    parts = [p for p in nm.split() if p]
    if len(parts) >= 2:
        return parts[0] in hay and parts[-1] in hay
    return bool(parts) and parts[0] in hay


def _click_row(page, item) -> bool:
    """Click a conversation row (the clickable DIV — these rows have no <a>). True once the
    composer shows."""
    link = item.query_selector(CONVO_LINK_SEL) or item
    with contextlib.suppress(Exception):
        link.scroll_into_view_if_needed(timeout=3000)
    try:
        link.click(timeout=8000)
    except Exception:
        return False
    for _ in range(6):                          # give the thread pane time to mount
        if page.query_selector(THREAD_IS_OPEN_SEL):
            return True
        page.wait_for_timeout(400)
    return False


def _find_and_click_by_name(page, name: str) -> bool:
    """Scan the loaded rows and click the one whose title matches `name`."""
    for item in page.query_selector_all(LISTITEM_SEL):
        with contextlib.suppress(Exception):
            if _row_matches_name(item, name) and _click_row(page, item):
                return True
    return False


def _search_and_click(page, name: str) -> bool:
    """The human way to reach anyone not on screen: type their NAME into the messaging search box,
    press Enter (probed 2026-07-24 — WITHOUT Enter the list never filters), then click the matching
    row. Returns True once the thread opens with a composer."""
    if not name:
        return False
    box = next((page.query_selector(s) for s in MSG_SEARCH_SELS if page.query_selector(s)), None)
    if not box:
        return False
    try:
        box.click(timeout=5000)
        with contextlib.suppress(Exception):
            box.fill("")
        page.keyboard.type(name, delay=45)      # type like a person
        page.wait_for_timeout(700)
        page.keyboard.press("Enter")            # <-- the step that actually runs the search
    except Exception:
        return False
    page.wait_for_timeout(3000)                 # let the results filter down
    return _find_and_click_by_name(page, name)


def _open_thread_via_inbox(page, thread_urn: str, name: str = None) -> bool:
    """HUMAN navigation to a conversation: open the messaging INBOX and SEARCH the person's name,
    then click their row. NEVER a direct goto to /messaging/thread/<urn>/ (deep-linking reloads the
    thread page = a loud AI tell, ruled 2026-07-24). Rows carry no urn (probed) so name is the only
    handle. Returns True once the thread is open with a composer."""
    # Already sitting on the right thread with a live composer? Reuse it — the most human thing.
    if thread_urn and thread_urn in (page.url or "") and page.query_selector(THREAD_IS_OPEN_SEL):
        return True
    if not name:                                # no name = nothing to search or match on
        return False
    page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=45_000)
    page.wait_for_timeout(3000)
    # 1) already visible near the top (a fresh reply usually is)? click it by name.
    if _find_and_click_by_name(page, name):
        return True
    # 2) the human move for anyone else — search their name and click the result.
    if _search_and_click(page, name):
        return True
    # 3) short scroll fallback (a few rows), never an endless scroll.
    for _ in range(6):
        before = len(page.query_selector_all(LISTITEM_SEL))
        if _find_and_click_by_name(page, name):
            return True
        if scroll_list(page) <= before:
            break
    return False


# --- carrying an approved message -------------------------------------------
#
# This types a message into LinkedIn's own box and presses Send. It is here on
# purpose, and it is the only place in LinkChat that can do it.
#
# WHAT STOPS IT BEING AUTOMATION. Nothing calls this until a message has cleared
# the gate: a sequence wrote it, the sequence was refused permission to approve
# its own work, you read it on screen and approved it, the person is not on your
# hold list, and it is not near enough to something you just sent to be a copy.
# A machine never decides on its own that somebody should hear from you.
#
# Once you HAVE decided, this carrying the words is you using a tool - the same
# as `gather.py ask` sending a connection request you authorised. Making you
# paste it into LinkedIn by hand would add no safety, because the decision was
# already made; it would only break the loop, so no reply comes back in and the
# sequence never learns what happened.
#
# Navigation is human: it opens the inbox and finds the person the way you would,
# never by jumping straight to a private address.

def send_message(page, thread_urn: str, text: str, do_send: bool = True, name: str = None) -> dict:
    """Open a thread and type `text` into LinkedIn's OWN composer, then (if do_send) click
    Send. This drives the real rendered UI in the user's session — NOT the Voyager API.
    do_send=False types + reads it back + clears (a safe dry verify, sends nothing).

    Navigation is HUMAN: open the inbox and search the person's name / click their row, never
    a deep-link goto. `name` powers the search — pass it (the conversation's participant_name)."""
    if not _open_thread_via_inbox(page, thread_urn, name=name):
        return {"ok": False, "msg": "could not open the conversation via the inbox "
                "(human nav — contact not found by search or in the message list)"}
    page.wait_for_timeout(500)
    box = page.query_selector(THREAD_IS_OPEN_SEL)
    if not box:
        return {"ok": False, "msg": "composer not found"}
    try:
        box.click(timeout=5000)
    except Exception:
        return {"ok": False, "msg": "could not focus composer"}
    page.wait_for_timeout(300)
    page.keyboard.insert_text(text)
    page.wait_for_timeout(400)
    typed = (box.inner_text() or "").strip()
    if not do_send:
        # dry verify: clear the field, send nothing
        try:
            page.keyboard.press("Control+A")
            page.keyboard.press("Delete")
        except Exception:
            pass
        return {"ok": True, "dry": True, "typed": typed[:120]}
    if not typed:
        return {"ok": False, "msg": "text did not enter the composer"}
    btn = page.query_selector(SEND_BTN_SEL)
    try:
        if btn and btn.is_enabled():
            btn.click(timeout=5000)
        else:
            page.keyboard.press("Enter")   # LinkedIn sends on Enter (Shift+Enter = newline)
    except Exception as e:   # noqa: BLE001
        return {"ok": False, "msg": f"send click failed: {e}"}
    # confirm-by-reread: our text should become the LAST outgoing bubble (don't trust the click)
    import time as _t
    needle = text.strip()[:40]
    deadline = _t.time() + 5
    while _t.time() < deadline:
        outs = [m for m in (page.evaluate(READ_JS) or []) if m["dir"] == "out"]
        if outs and needle and needle in outs[-1]["text"]:
            return {"ok": True, "sent": True, "confirmed": True}
        page.wait_for_timeout(800)
    return {"ok": True, "sent": True, "confirmed": False,
            "msg": "send clicked but not confirmed in the thread within 5s"}



def selftest(page) -> tuple[bool, str]:
    """Prove the live DOM still matches our selectors BEFORE trusting a sync. Opens the
    inbox and reads threads until one yields bubbles, asserting we can resolve a thread
    URN AND classify messages. The first row can be a Sponsored InMail / system thread
    with no normal bubbles, so we try the first few before concluding drift. Fails LOUD
    so genuine selector drift is an error, not silent bad data."""
    rows = read_list(page, max_rows=6)
    if not rows:
        return False, "LIST_JS returned no rows - inbox empty or selector drift"
    last_urn = ""
    for i in range(min(len(rows), 5)):
        urn, msgs = open_thread_by_index(page, i)
        last_urn = urn or last_urn
        if urn and msgs:
            ins = sum(1 for m in msgs if m["dir"] == "in")
            outs = sum(1 for m in msgs if m["dir"] == "out")
            return True, (f"ok - {len(rows)} rows; read thread #{i} urn={urn[:20]} "
                          f"({len(msgs)} msgs in={ins} out={outs})")
    if not last_urn:
        return False, "could not resolve any thread URN from page.url - click/nav drift"
    return False, "opened threads but none yielded messages - selector drift on bubbles"


@contextlib.contextmanager
def drive(wait_sec: int = 300, spawn: bool = False, action: str = "scrape"):
    """Safely take ONE set of hands on the keeper.

    Holds the shared READ_LOCK (serialises against every the parent program lane), checks the
    shared budget for `action` first ('scrape' for reads, 'dm' for a send), connects over
    CDP to the keeper, yields the live page, and ALWAYS releases the lock + disconnects
    (keeper stays alive).

    Yields a (page, msg) tuple. page is None (with msg explaining why) if: budget
    exhausted, lock not won, or no keeper. spawn=False means a missing keeper does NOT
    auto-launch a browser (the safe standalone default) — the caller surfaces
    "start your LinkedIn browser".
    """
    from engine import browser as B
    from engine import ops
    import linkedin_browser as lb
    from playwright.sync_api import sync_playwright

    # WHICH CEILING IS ASKED, AND WHY ONLY ONE OF THEM.
    #
    # LinkChat counts its own reading, and that count lives in ops.py. How many
    # people you may contact in a day is a different number, it is yours, it is
    # shared with Gather, and it was already asked and answered before anything
    # got this far - by your own send gate, once, where the person is on the other
    # end. Asking a second ceiling here would be two opinions about one action,
    # which is the fault that gets an account restricted while every part reports
    # itself inside its limit.
    if action in ops.READING:
        ok, used, cap, why = ops.check_budget(action)
        if not ok:
            # `why` already carries the count, so it is not re-appended.
            yield None, why
            return
    if not B.keeper_running():
        if not spawn:
            yield None, "your LinkedIn browser is not open yet - open it and sign in first"
            return
        # spawn=True means THIS job is allowed to open the browser, and until
        # 2026-08-25 it only skipped the refusal above - it never started one,
        # so the connect below failed a moment later with something less
        # readable. Nothing in LinkChat called ensure_keeper at all, which is
        # why pressing Sync said "open it and sign in first" and no screen,
        # button or command could open it.
        if B.ensure_keeper() is None:
            yield None, ("LinkChat could not open your LinkedIn browser. Try Sync "
                         "inbox again; if it keeps happening send this line to whoever gave you this.")
            return

    # Take the SAME lock Gather takes, out of your own CRM.
    #
    # Without this LinkChat holds one lock and Gather holds a different one, and
    # each is satisfied it has the browser to itself. Two jobs then drive one
    # signed-in profile at the same time. The damage does not arrive as a crash:
    # it arrives days later as an account that stops trusting the login, and by
    # then nothing connects it back to the afternoon they overlapped.
    crm_token, crm_lock = None, None
    try:
        from engine import crm_bridge
        bridge = crm_bridge.open_crm()
        crm_lock = bridge.parts.get("browser_lock")
    except Exception:
        crm_lock = None
    if crm_lock is not None:
        try:
            crm_token = crm_lock.acquire("linkchat")
        except Exception:
            yield None, ("something else is using your LinkedIn browser right now "
                         "(Gather, most likely) - try again when it has finished")
            return

    try:
      with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=wait_sec) as got:
          if not got:
            yield None, "another LinkedIn job is running - try again shortly"
            return
          with sync_playwright() as pw:
            ctx = B.connect(pw)   # attach to the browser that is already open
            if not ctx:
                yield None, "could not reach your LinkedIn browser"
                return
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                yield page, "ok"
            finally:
                B.release(ctx)    # let go of the window; the browser stays open
    finally:
        if crm_lock is not None and crm_token is not None:
            try:
                crm_lock.release(crm_token)
            except Exception:
                pass


# --- what used to be here, and why it is not -------------------------------
#
# Everything below this line in the original was voice notes and file sending,
# done by calling LinkedIn's own internal interface from inside the page. Three
# reasons it is gone rather than switched off:
#
#   1. It carried one person's LinkedIn profile identifier as a built-in default,
#      which would have been wrong for every single person who installed this.
#   2. Calling that interface is a different and riskier way of working than
#      reading the pages the way a person does, which is what the rest of this
#      file does and what the inbox was designed around.
#   3. Sending voice notes is not what LinkChat is for.
#
# The reading above is unaffected: it opens pages and reads what is on them.
