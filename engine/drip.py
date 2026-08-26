"""drip.py — the message / drip lane.

Generates VARIED openers (mechanically, NO LLM) from openers.txt and — once the composer
is wired — types them at human speed into LinkedIn conversations, one bubble at a time,
with stop-on-reply. Variation is mandatory (identical messages at volume = spam/ban):
  - spintax {a|b|c} resolved at random per send
  - {first_name} personalisation
  - || splits into separate bubbles
  - a NO-EXACT-REPEAT guard (hash the phrasing with the name stripped; re-roll a dup)

v1 (this build): opener generation + `--preview` (offline, proves the variation).
Next: message-composer probe -> human-speed typing send (gated by safety).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

from . import ops
import linkedin_browser as lb

from . import PKG_DIR, DATA_DIR
from . import db, safe_close, focus_field
from . import flows_engine
from . import browser as kb   # keeper: per-lead reattach + retry (keeper-stability fix)

AGENT = "engine-drip"
SHOTS = DATA_DIR / "screenshots"
OPENERS_PATH = PKG_DIR / "openers.txt"
TEMPLATES_DIR = DATA_DIR / "templates"   # named template sets, drafted natively in the app
DEFAULT_TEMPLATE = "Openers"
_SPIN = re.compile(r"\{([^{}]*\|[^{}]*)\}")   # innermost {a|b|c} (has a pipe; {first_name} won't match)


def seed_templates() -> None:
    """Make data/templates/ exist; migrate the legacy openers.txt in as the
    default set the first time."""
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    default = TEMPLATES_DIR / f"{DEFAULT_TEMPLATE}.txt"
    if not default.exists() and OPENERS_PATH.exists():
        default.write_text(OPENERS_PATH.read_text(encoding="utf-8"), encoding="utf-8")


def template_names() -> list[str]:
    seed_templates()
    return sorted(p.stem for p in TEMPLATES_DIR.glob("*.txt"))


def load_openers(template: str | None = None) -> list[str]:
    """Load the variant lines of a named template set (data/templates/{name}.txt);
    falls back to the legacy package openers.txt."""
    seed_templates()
    path = TEMPLATES_DIR / f"{template or DEFAULT_TEMPLATE}.txt"
    if not path.exists():
        if template:
            print(f"[warn] template set '{template}' not found — using {DEFAULT_TEMPLATE}")
        path = TEMPLATES_DIR / f"{DEFAULT_TEMPLATE}.txt"
    if not path.exists():
        path = OPENERS_PATH
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def resolve_spintax(s: str) -> str:
    """Replace every {a|b|c} with a random choice, innermost first."""
    while True:
        m = _SPIN.search(s)
        if not m:
            return s
        s = s[:m.start()] + random.choice(m.group(1).split("|")) + s[m.end():]


# personalisation variables the engine fills from a lead's record
KNOWN_VARS = ("first_name", "company", "title", "location")
_MISSING = ""   # private-use sentinel marking a var the lead did not have


def _fill(s: str, fields: dict) -> str:
    """Replace {var} tokens BEFORE spintax (so a var nested inside a {a|b} branch
    resolves). A var the lead lacks becomes a sentinel — that lets spintax still pick
    a branch, and lets render() detect that the CHOSEN branch needed a missing var."""
    for k in KNOWN_VARS:
        tok = "{" + k + "}"
        if tok in s:
            v = (fields.get(k) or "").strip()
            s = s.replace(tok, v if v else _MISSING)
    return s


# A placeholder token that survived fill + spintax = personalisation that did NOT resolve
# (a var the message engine doesn't know, or a raw token that never went through _fill).
# Sending it would leak "Hi {first_name}" to a prospect, so every send path guards on this
# and REFUSES to send when it's non-empty. Spintax {a|b} is resolved before this runs.
_LEFTOVER = re.compile(r"\{[^{}]*\}|\[[^\[\]]*\]")


def unresolved(text: str) -> list[str]:
    """Every unresolved placeholder in a FINAL (filled + spintax-resolved) message.
    Empty list = safe to send. The send-time guard blocks on any non-empty result."""
    return _LEFTOVER.findall(text or "")


def personalise(text: str, fields: dict) -> str:
    """Fill a single blob (e.g. a connect NOTE) the same way messages are filled:
    {var} → the lead's value, then resolve spintax. Not split into bubbles."""
    return resolve_spintax(_fill(text or "", fields)).replace(_MISSING, "")


def render(template: str, fields: dict) -> tuple[list[str], bool]:
    """Fill {vars} -> resolve spintax -> split into clean bubbles. Returns
    (bubbles, complete); complete=False means the chosen wording needed a var the
    lead didn't have (generate re-rolls onto a variant that doesn't)."""
    s = resolve_spintax(_fill(template, fields))
    complete = _MISSING not in s
    s = s.replace(_MISSING, "")
    bubbles = []
    for b in s.split("||"):
        b = re.sub(r"\s+", " ", b).replace(" ,", ",").replace(" .", ".").replace(" !", "!")
        b = re.sub(r"\s+-\s*$", "", b)          # drop a dangling trailing " -"
        b = re.sub(r"\b(a|the|at|for)\s*$", "", b, flags=re.I)  # drop an orphaned leading-word tail
        b = b.strip()
        if b:
            bubbles.append(b)
    return bubbles, complete


def thread_verdict(items: int, theirs: int, prior: int) -> str:
    """The stop-on-reply / resume decision — pure and falsifiably tested.

    'replied' : a message FROM THEM exists (--other), or the thread has messages we
                never recorded (an existing conversation we must not cold-template).
    'resume'  : the thread holds only OUR recorded bubble(s) — a half-send; continue
                from the first unrecorded bubble (the 2026-07-14 fix: 8 people were
                left at "Hey {name}" because our own bubble was misread as a reply).
    'fresh'   : empty thread — send from the top.
    """
    if theirs > 0:
        return "replied"
    if items > 0 and prior == 0:
        return "replied"
    if items > 0 or prior > 0:
        return "resume"
    return "fresh"


# The greeting name is cleaned in names.py, not here — the parent program and the the automation folder
# senders share one definition of "the name a human would type". Re-exported so
# every existing caller (connect.py notes, sequence.py, flows_sensors.py, the
# tests) keeps importing drip.first_name_of and gets the cleaning for free.
from . import names  # noqa: E402
from .names import first_name_of, clean_name, strip_symbols, was_decorated  # noqa: E402,F401


def _lead_fields(lead: dict) -> dict:
    """Pull the personalisation vars off a lead record (first name from full_name).

    Every one of these four gets typed into the message, so every one of them is
    stripped of emoji and joiner characters first. A company field of "Acme 🚀"
    is the same self-report as a decorated name: it says we copied a profile field
    without reading it. The name gets the full clean (honourifics, credential tail,
    shouting); company/title/location only lose the symbols, because "Acme, Inc"
    and "Head of Sales, EMEA" are real values a comma-cut would wreck."""
    # Prefer the greeting computed at intake (db.upsert_lead, schema v10). Recomputing
    # here is the fallback for a lead dict assembled outside the database.
    fn = (lead.get("greet_name") or "").strip() or first_name_of(lead.get("full_name"))
    return {"first_name": fn,
            "company": strip_symbols(lead.get("company")),
            "title": strip_symbols(lead.get("title")),
            "location": strip_symbols(lead.get("location"))}


def _phrasing_hash(bubbles: list[str], first_name: str | None) -> str:
    """Hash the phrasing with the name stripped, so the same wording sent to two
    different people counts as a repeat."""
    txt = "||".join(bubbles).lower()
    if first_name:
        txt = txt.replace(first_name.lower(), "{n}")
    return hashlib.md5(txt.encode("utf-8")).hexdigest()


def generate(first_name: str | None, templates: list[str], recent: set[str],
             tries: int = 14, fields: dict | None = None) -> tuple[list[str], str]:
    """Pick a template and render it, re-rolling for (a) a fully-personalised variant
    (no missing {vars}) and (b) a phrasing that isn't a recent repeat. first_name stays
    positional for back-compat; pass fields={'company':..,'title':..} to use those vars."""
    f = dict(fields or {})
    if first_name and not f.get("first_name"):
        f["first_name"] = first_name
    if not f.get("first_name"):
        f["first_name"] = "there"
    best_complete = None   # a var-complete render (even if a repeat)
    best_any = None        # any render at all
    for _ in range(tries):
        bubbles, complete = render(random.choice(templates), f)
        h = _phrasing_hash(bubbles, f.get("first_name"))
        if best_any is None:
            best_any = (bubbles, h)
        if complete:
            if h not in recent:
                recent.add(h)
                return bubbles, h
            if best_complete is None:
                best_complete = (bubbles, h)
    result = best_complete or best_any
    recent.add(result[1])
    return result


def preview(n: int, template: str | None = None) -> None:
    templates = load_openers(template)
    if not templates:
        print(f"no openers found (template set: {template or DEFAULT_TEMPLATE})")
        return
    names = ["Daniel", "Sarah", "Mohammed", "Lily", "Andrej", "Enda", "Robbie",
             "Maneesha", "Tarquin", "Sohail"]
    recent: set[str] = set()
    for i in range(n):
        name = names[i % len(names)]
        bubbles, _ = generate(name, templates, recent)
        print(f"\n[{name}]")
        for b in bubbles:
            print(f"   - {b}")
    print(f"\n{len(recent)} unique phrasings / {n} generated  "
          f"({len(templates)} variants in set '{template or DEFAULT_TEMPLATE}')")


def human_type(page, text: str) -> None:
    """Type text one character at a time at human speed (variable cadence). Never Enter."""
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.045, 0.14))


def type_test() -> None:
    """SAFE live test: open a conversation, find the composer, generate an opener and TYPE
    it at human speed — then CLEAR it. NEVER sends. Proves the composer + human typing."""
    templates = load_openers()
    if not templates:
        print("no openers found in openers.txt")
        return
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=180, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                # Exercise the REAL send path: a 1st-degree PROFILE -> compose URL, where
                # the composer is the stable `.msg-form__contenteditable` (the /messaging/
                # inbox renders its composer inside an iframe, so document-scoped locators
                # miss it — that path is NOT what send_to_lead uses). Auto-source a target:
                # a /in/ URL on the CLI, else the first queued lead, else any lead.
                target = next((a for a in sys.argv if "/in/" in a), None)
                if not target:
                    q = _queue(1) or _any_messageable(1)
                    target = q[0]["profile_url"] if q else None
                if not target:
                    print("no target profile (pass a /in/ URL or collect a queued lead first)")
                    return
                if not _open_composer_for_profile(page, target):
                    print("no message composer found (not 1st-degree, or page didn't render)")
                    return
                box = page.locator('.msg-form__contenteditable')
                info = page.evaluate(r"""() => {
                  const tb = document.querySelector('div[contenteditable="true"]');
                  const send = [...document.querySelectorAll('button')].find(
                    b => /^send$/i.test((b.textContent || '').trim()) || /send/i.test(b.getAttribute('aria-label') || ''));
                  return {
                    box: tb ? {tag: tb.tagName, aria: (tb.getAttribute('aria-label') || '').slice(0, 60)} : null,
                    sendButton: send ? {text: (send.textContent || '').trim().slice(0, 20),
                                        aria: (send.getAttribute('aria-label') || '').slice(0, 40),
                                        disabled: send.disabled} : null,
                  };
                }""")
                print("composer ->", json.dumps(info, ensure_ascii=False))
                bubbles, _ = generate("there", templates, set())
                msg = bubbles[0]
                print(f"typing at human speed (NO send): {msg}")
                box.first.click()
                time.sleep(0.6)
                human_type(page, msg)
                time.sleep(0.8)
                SHOTS.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(SHOTS / "drip_typetest.png"))
                print(f"[screenshot] {SHOTS / 'drip_typetest.png'}", file=sys.stderr)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                print("[cleared the box — nothing was sent]")
                ops.log_action(AGENT, "scrape", target="composer-typetest", result="ok")
            finally:
                safe_close(ctx)


def _queue(limit: int) -> list[dict]:
    db.sync_red_list_from_json()   # Layer A: red-listed people are invisible to the queue
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, profile_url, full_name, company, title, location FROM leads "
            "WHERE status = 'queued_message' AND profile_url LIKE '%/in/%' "
            "AND NOT EXISTS (SELECT 1 FROM red_list r WHERE r.lead_id = leads.id "
            "OR r.canon_url = leads.profile_url OR r.member_urn = leads.profile_url) "
            "ORDER BY id LIMIT ?", (limit,))]


def _any_messageable(limit: int) -> list[dict]:
    """Fallback target pool for the type-test: any lead with an /in/ profile."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, profile_url, full_name, company, title, location FROM leads "
            "WHERE profile_url LIKE '%/in/%' ORDER BY id LIMIT ?", (limit,))]


def _remember_who(lead_id, learned: dict) -> None:
    """File LinkedIn's own id for a person, once, and never over the top of a better one.

    NEVER OVERWRITES. An id already on file was learned from somewhere that had a
    reason to be trusted — the event lane reads it off a checked invite row. A later
    read that disagrees is more likely to be this profile page being wrong than the
    earlier one, and quietly replacing an identifier is how one person becomes two.
    """
    urn = (learned or {}).get("member_urn")
    if not lead_id or not urn:
        return
    try:
        with db.connect() as conn:
            conn.execute("UPDATE leads SET member_urn=? WHERE id=? "
                         "AND (member_urn IS NULL OR member_urn='')", (urn, lead_id))
            conn.commit()
    except Exception:      # noqa: BLE001 — book-keeping never stops a send
        pass


def _open_composer_for_profile(page, profile_url: str, lead_id: int | None = None,
                               learned: dict | None = None) -> bool:
    """Open a 1st-degree lead's message composer via the PROVEN compose-URL path and
    return True iff the composer rendered. We do NOT click the profile 'Message' control
    (it has a COVERED sticky-duplicate + spawns the bottom-right OVERLAY whose composer
    lives in an iframe). Instead we READ the Message link's href (it carries the
    recipient id) and goto the FULL /messaging/compose page, where the composer is the
    stable `.msg-form__contenteditable`. Validated live 2026-06-22 (Send enabled on type).

    IT ALSO LEARNS WHO THIS PERSON IS, AND THAT IS WHY `lead_id` IS HERE (2026-08-23).

    Two records exist for every person. Their record in the pipeline stores the
    readable address, `/in/franco-saha-securovix`. Their conversation in the inbox
    stores LinkedIn's own id, `/in/ACoAAERbJcQ…`. Those two strings never match —
    not once in 6,880 conversations — so tying a reply back to the message that
    earned it falls through to matching on NAME, which loses everyone sharing a
    name with somebody else and cannot place 1,589 conversations at all.

    The id was already in our hands and being thrown away. The Message link read
    two lines below carries `recipient=<the id>`; it was pulled out to build the
    compose URL and then dropped. Keeping it costs no extra page, no extra action
    against the daily ceiling, and nothing the person can see, because the browser
    is on their profile anyway — it is about to message them.

    It goes into `leads.member_urn`, which already exists and which the red list,
    the connect lane, the InMail lane and the sensors ALL already check. So this
    does not only fix measurement: it makes the do-not-contact check harder to
    slip past, which matters more than any of the counting.

    NOTHING HERE MAY STOP A MESSAGE. Filing who somebody is has no business
    deciding whether they hear from you, so every write below is wrapped and a
    failure is swallowed. A send that failed because a database was busy would be
    the book-keeping causing the harm it exists to prevent.
    """
    page.goto(profile_url, wait_until="domcontentloaded", timeout=45_000)
    time.sleep(random.uniform(4.5, 6.5))   # the action bar renders a beat late
    href = None
    msglink = page.locator('main a[href*="/messaging/"]')
    for i in range(min(msglink.count(), 12)):
        h = msglink.nth(i).get_attribute("href")
        if h and "/messaging/" in h:
            href = h
            break
    if not href:
        return False   # no Message link on the profile = not 1st-degree
    learned = learned if learned is not None else {}
    m = re.search(r"recipient=([^&]+)", href)
    if m:
        compose = f"https://www.linkedin.com/messaging/compose/?recipient={m.group(1)}"
        # The compose form names the person. This is the one we want.
        learned["member_urn"] = m.group(1)
    else:   # thread-form href (existing convo) — full page, drop the overlay interop flag
        base = "https://www.linkedin.com" + href if href.startswith("/") else href
        compose = base.replace("interop=msgOverlay", "").rstrip("&?")
        # The thread form names the CONVERSATION rather than the person. Worth less,
        # still worth keeping: it is the same id the inbox files a conversation under,
        # so it ties this lead to a thread we already hold.
        t = re.search(r"/thread/([^/?#]+)", base)
        if t:
            learned["thread_urn"] = t.group(1)
    _remember_who(lead_id, learned)
    page.goto(compose, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_selector(".msg-form__contenteditable", timeout=20_000)
    except Exception:
        return False   # composer never rendered (not messageable / page issue)
    time.sleep(random.uniform(1.5, 2.5))
    return True


def _recent_hashes(n: int = 300) -> set[str]:
    s: set[str] = set()
    try:
        with db.connect() as conn:
            for r in conn.execute("SELECT body FROM messages ORDER BY id DESC LIMIT ?", (n,)):
                s.add(_phrasing_hash([r["body"]], None))
    except Exception:
        pass
    return s


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _record(lead_id: int, body: str) -> None:
    now = _now()
    with db.connect() as conn:
        conn.execute("INSERT INTO messages (lead_id, step_index, body, sent_at, status) "
                     "VALUES (?,?,?,?, 'sent')", (lead_id, 0, body, now))
        conn.execute("UPDATE leads SET status='messaged', last_action_at=?, updated_at=? WHERE id=?",
                     (now, now, lead_id))


def _set_status(lead_id: int, status: str) -> None:
    now = _now()
    with db.connect() as conn:
        conn.execute("UPDATE leads SET status=?, updated_at=? WHERE id=?", (status, now, lead_id))


def _flow_ctx_for(lead: dict, bubbles: list[str],
                  flow_ctx: dict | None) -> dict | None:
    """Resolve which flow node/arm this send belongs to, for the stamp ledger (F1).
    A flow-aware caller passes {'node_key','arm_key','arm_hash'} explicitly; otherwise
    reconcile the actual copy against the ACTIVE version's arm lineage (finding 8 —
    machine and human sends must stamp identically). No active version / no match ->
    None: nothing to attribute, so nothing is stamped. Never raises."""
    try:
        with db.connect() as conn:
            v = flows_engine.active_version(conn, lead.get("campaign_id"))
            if not v:
                return None
            if flow_ctx and flow_ctx.get("node_key"):
                out = dict(flow_ctx)
            else:
                m = flows_engine.match_arm_by_body(" · ".join(bubbles), v, conn)
                if not m:
                    return None
                out = {"node_key": m["node_key"], "arm_key": m["arm_key"],
                       "arm_hash": m["content_hash"]}
            out.update({"version_id": v["id"], "lineage_uuid": v["lineage_uuid"]})
            return out
    except Exception:  # noqa: BLE001 — attribution must never block a send
        return None


def send_to_lead(page, lead: dict, templates: list[str], recent: set,
                 step_index: int = 0, override_bubbles: list[str] | None = None,
                 source: str = "drip", flow_ctx: dict | None = None) -> str:
    """Send ONE human-typed message to ONE 1st-degree lead on an open page. The proven
    drip mechanics, factored out so the sequence engine (WP5) AND the AI-DM plugin share
    the exact same typing/stop-on-reply/record path. When `override_bubbles` is given
    (the AI-DM case) those exact bubbles are typed instead of a mechanically-generated
    opener; `source` labels the recorded message (drip | aidm). Returns
    'sent' | 'skipped' | 'replied'. Records the message but does NOT change the lead's
    pipeline status (the caller owns the journey)."""
    # Layer B (the hard stop): refuse to message a red-listed person, before the composer.
    if db.red_list_match(url=lead.get("profile_url"), lead_id=lead.get("id"),
                         name=lead.get("full_name")):
        db.log_event("redlist-blocked", lead.get("id"), "drip.send_to_lead")
        return "skipped"
    # Open the composer via the PROVEN compose-URL path (shared with the type-test):
    # profile -> read the Message link's recipient -> /messaging/compose full page, where
    # the composer is the stable `.msg-form__contenteditable`. (Clicking the profile
    # 'Message' control spawns the bottom-right OVERLAY whose composer lives in an iframe.)
    learned: dict = {}
    if not _open_composer_for_profile(page, lead["profile_url"],
                                      lead_id=lead.get("id"), learned=learned):
        return "skipped"   # not 1st-degree, or the composer never rendered
    time.sleep(random.uniform(1.5, 2.5))
    box = page.locator('.msg-form__contenteditable')
    # Stop-on-reply, authorship-aware (fixed 2026-07-14). The old check treated ANY
    # thread message as a reply — including OUR OWN half-sent bubble, so a bubble-2
    # failure + one retry falsely marked the lead 'replied' and left them with a bare
    # "Hey {name}" forever (7 people, live). Now:
    #   THEIR message present (--other)                 -> replied (stop, real signal)
    #   messages present but NONE recorded by us        -> replied (existing conversation)
    #   messages present AND we recorded k bubble(s)    -> RESUME from bubble k
    with db.connect() as conn:
        prior = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE lead_id=? AND step_index=? "
            "AND status='sent'", (lead["id"], step_index)).fetchone()[0]
    items = page.locator('.msg-s-event-listitem').count()
    theirs = page.locator('.msg-s-event-listitem--other').count()
    if thread_verdict(items, theirs, prior) == "replied":
        return "replied"
    if prior:
        print(f"  [resume] {lead.get('full_name')}: {prior} bubble(s) already sent — "
              "sending the rest")
    fn = (lead.get("greet_name") or "").strip() or first_name_of(lead["full_name"]) or "there"
    if override_bubbles is not None:
        bubbles = [b for b in (s.strip() for s in override_bubbles) if b]
        if not bubbles:
            return "skipped"
    else:
        bubbles, _ = generate(fn, templates, recent, fields=_lead_fields(lead))
    # HARD GUARD: never send a bubble that still holds a raw placeholder ("Hi {first_name}").
    # If any bubble is unsafe, abort the whole message for this lead — a half-personalised
    # send is worse than none. The lead stays queued so a fixed template retries it.
    bad = [tok for b in bubbles for tok in unresolved(b)]
    if bad:
        print(f"  [blocked] {lead.get('full_name') or lead.get('profile_url')}: "
              f"unresolved placeholder {bad} — not sent (fix the template's tokens)")
        ops.log_action(AGENT, "dm", target=lead["profile_url"], result="failed",
                       detail=f"unresolved placeholder {bad}")
        return "skipped"
    # HARD GUARD (2026-08-20): never type this person's decorated name back at them.
    # "Hey [bolt]James" is on file as SENT to "[bolt]James Marley[bolt]" - people put emoji in
    # the name field as a bot detector, and copying it back identifies us as a machine.
    # Narrow on purpose: it refuses only a decorated chunk of THIS lead's own name, so an
    # emoji the operator wrote into his own template is untouched.
    leak = next((x for b in bubbles
                 for x in [names.leaked_decoration(b, lead.get("full_name"))] if x), None)
    if leak:
        print(f"  [blocked] {lead.get('full_name') or lead.get('profile_url')}: "
              f"decorated name {leak!r} leaked into the message - not sent")
        ops.log_action(AGENT, "dm", target=lead["profile_url"], result="failed",
                       detail=f"decorated name leaked: {leak!r}")
        return "skipped"
    now = _now()
    if prior >= len(bubbles):
        return "sent"   # everything already delivered; only the advance was lost
    # F0.5b: a leftover 'sending' row means a previous run clicked Send (or was about
    # to) and DIED before confirming — reality and the DB may disagree about what this
    # human received. Never guess: hand the thread to a human (the caller parks it).
    with db.connect() as conn:
        stale = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE lead_id=? AND status='sending'",
            (lead["id"],)).fetchone()[0]
    if stale:
        print(f"  [unconfirmed] {lead.get('full_name')}: {stale} unconfirmed 'sending' "
              "row(s) from a crashed run — needs a human eye, not a retry")
        return "unconfirmed"
    _fctx = _flow_ctx_for(lead, bubbles, flow_ctx)   # F1 attribution (may be None)
    for b in bubbles[prior:]:
        # First-VISIBLE contenteditable + focus_field (NOT centre .click()): the
        # 'draft with AI' CTA span can overlay the composer centre (see focus_field).
        bx = next((box.nth(i) for i in range(min(box.count(), 8)) if box.nth(i).is_visible()), box.first)
        if not focus_field(bx):
            bx.click()      # last-ditch
        time.sleep(0.5)
        human_type(page, b)
        time.sleep(random.uniform(0.6, 1.2))
        # VERIFY the text landed (bubble 2+ loses composer focus after a send — the
        # 2026-07-14 half-send bug): empty composer = typing went nowhere; one retry.
        try:
            landed = bool((bx.inner_text() or "").strip())
        except Exception:  # noqa: BLE001
            landed = True   # unreadable box: fall through to the send-button gate below
        if not landed:
            if not focus_field(bx):
                bx.click()
            time.sleep(0.5)
            human_type(page, b)
            time.sleep(random.uniform(0.6, 1.2))
        # HONEST SEND: wait for the Send button to ENABLE (it stays disabled until the
        # composer holds text), click it, and only then record. The old code clicked
        # if-present, recorded UNCONDITIONALLY, and a disabled button burned a 30s
        # timeout or silently recorded a bubble that never went.
        sbtn = page.get_by_role("button", name=re.compile(r"^send$", re.I)).first
        ready = False
        for _ in range(8):
            try:
                if sbtn.count() and sbtn.is_enabled():
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        if not ready:
            raise RuntimeError(f"bubble {prior + bubbles.index(b) + 1}/{len(bubbles)}: "
                               "Send never enabled — typed text did not land")
        # INTENT-THEN-CONFIRM (F0.5b): record 'sending' BEFORE the irreversible click,
        # flip to 'sent' after. A crash between click and record used to LOSE the
        # record of a message the human received (and the resume then misread our own
        # unrecorded bubble as their reply). Now the divergence leaves a 'sending' row
        # the watchdog flags and this function refuses to talk past.
        with db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (lead_id, step_index, body, sent_at, status, source) "
                "VALUES (?,?,?,?, 'sending', ?)", (lead["id"], step_index, b, now, source))
            intent_id = cur.lastrowid
        try:
            sbtn.click(timeout=4_000)
        except Exception:
            # click provably did NOT fire: withdraw the intent so the retry is clean
            with db.connect() as conn:
                conn.execute("DELETE FROM messages WHERE id=? AND status='sending'",
                             (intent_id,))
            raise
        time.sleep(random.uniform(1.5, 3.0))
        with db.connect() as conn:
            conn.execute("UPDATE messages SET status='sent' WHERE id=?", (intent_id,))
            # F1: branch-stamp in the SAME transaction as the FINAL bubble's sent-confirm
            # (plan §5.3). One stamp per logical SEND, not per bubble, keyed to the
            # send's FIRST message row — the same natural key the history reconcile
            # uses, so live stamping and the sensor can never double-count each other.
            # Guarded: a stamp bug must never roll back the record of a message a human
            # really received — the confirm always wins.
            if _fctx and b == bubbles[-1]:
                try:
                    from .canon import canon_in as _ci
                    first_id = conn.execute(
                        "SELECT MIN(id) FROM messages WHERE lead_id=? AND step_index=? "
                        "AND status='sent'", (lead["id"], step_index)).fetchone()[0]
                    flows_engine.stamp(
                        conn, event="sent", node_key=_fctx["node_key"],
                        ev_key=flows_engine.event_key("sent", message_id=first_id),
                        canonical_url=_ci(lead.get("profile_url")), lead_id=lead["id"],
                        version_id=_fctx.get("version_id"),
                        lineage_uuid=_fctx.get("lineage_uuid"),
                        arm_key=_fctx.get("arm_key"), arm_hash=_fctx.get("arm_hash"),
                        detail=f"source={source}")
                except Exception:  # noqa: BLE001
                    pass
    # §6a-11: we know both sides of the identity join right now — capture it.
    flows_engine.link_conversation_to_lead(lead)
    ops.log_action(AGENT, "dm", target=lead["profile_url"], result="ok")
    return "sent"


def send(max_n: int, dry_run: bool, template: str | None = None) -> None:
    """Message the 'queued_message' leads with a varied, human-typed opener. Each lead:
    open profile -> Message -> (stop-on-reply check) -> type each bubble at human speed ->
    Send -> pause. Gated by enabled + dry_run + the message cap (safety.can_act)."""
    from .config import Config
    from . import safety
    cfg = Config.load()
    templates = load_openers(template)
    if not templates:
        print(f"no openers found (template set: {template or DEFAULT_TEMPLATE})")
        return
    if not dry_run and (not cfg.enabled or cfg.dry_run):
        print(f"[refused] enabled={cfg.enabled} dry_run={cfg.dry_run} — showing dry-run:\n")
        dry_run = True
    leads = _queue(max_n * 2)
    recent = _recent_hashes()
    with db.connect() as conn:
        total_queued = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE status='queued_message' AND profile_url LIKE '%/in/%'"
        ).fetchone()[0]

    if dry_run:
        print(f"Message queue (status='queued_message'): {total_queued} waiting; previewing up to {max_n}\n")
        for L in leads[:max_n]:
            fn = (L["full_name"] or "there").split()[0]
            bubbles, _ = generate(fn, templates, recent, fields=_lead_fields(L))
            print(f"  [dry] {L['full_name']}: " + "  ||  ".join(bubbles))
        print("\n[dry-run] nothing sent. Arm (enabled=true + dry_run=false) and --commit to send.")
        from . import emit_result
        emit_result("drip", True, f"Rehearsal — previewed {min(len(leads), max_n)} message(s), nothing sent")
        return

    sent = 0
    with ops.lock(lb.READ_LOCK, agent=AGENT, wait_sec=300, heartbeat=True) as got:
        if not got:
            print("chromium session busy — aborting", file=sys.stderr)
            return
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                for idx, L in enumerate(leads):
                    if sent >= max_n:
                        break
                    ok, why = safety.can_act("message", cfg)
                    if not ok:
                        print(f"[stop] {why}")
                        break
                    # keeper-stability: wrap the per-lead send so ONE keeper death reattaches
                    # + retries THIS lead instead of cascading the batch. send_to_lead re-opens
                    # the composer from the profile URL each call, and its stop-on-reply guard
                    # returns 'replied' if our message already landed — so a retry can't double-
                    # send. Send/click logic in send_to_lead is UNCHANGED.
                    attempts = 0
                    while True:
                        try:
                            # Single send path: delegate to send_to_lead (the same code the
                            # sequence engine uses) — it resolves the Message <a> link in <main>
                            # (NOT a button), runs the stop-on-reply check, and types each bubble
                            # via focus_field. It records messages + logs ops on success; the
                            # caller owns the lead's pipeline status.
                            result = send_to_lead(page, L, templates, recent, step_index=0)
                            if result == "sent":
                                _set_status(L["id"], "messaged")
                                sent += 1
                                print(f"  messaged {L['full_name']}")
                                time.sleep(random.uniform(40, 90) if "--fast" in sys.argv
                                           else safety.next_delay(cfg, idx))
                            elif result == "replied":
                                _set_status(L["id"], "replied")
                                print(f"  [skip] {L['full_name']}: conversation already active (stop-on-reply)")
                            else:   # 'skipped' — no Message link / composer (not 1st-degree or page degraded)
                                _set_status(L["id"], "skipped")
                                print(f"  [skip] {L['full_name']}: no Message link / composer")
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
                            ops.log_action(AGENT, "dm", target=L["profile_url"], result="failed", detail=str(e)[:120])
                            print(f"  [skip] {L['full_name']}: {str(e)[:70]}")
                            break
            finally:
                safe_close(ctx)
    print(f"\n[done] messaged {sent}.")
    from . import emit_result
    emit_result("drip", True, f"Messaged {sent} lead(s)", count=sent)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from .salesnav import _arg_str
    template = _arg_str("--template")
    if "--type-test" in sys.argv:
        type_test()
        return
    if "--send" in sys.argv:
        from .withdraw import _arg_int
        send(_arg_int("--max") or 10, dry_run="--commit" not in sys.argv, template=template)
        return
    nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
    preview(nums[0] if nums else 10, template=template)


if __name__ == "__main__":
    main()
