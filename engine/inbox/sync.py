"""sync.py — read-only inbox sync (human-nav DOM; Voyager rejected).

probe()  — P2 proof: read the inbox end-to-end, no DB writes.
sync()   — P3 backfill: scroll the virtualised list, open NEW/CHANGED threads, store
           conversations + messages locally. Incremental + resumable + paced.

Design (forced by the live DOM — the list exposes NO thread URN/href, learning #2 in
KONDO-STANDALONE-BUILD-PLAN.md):
  * A thread's canonical URN is only knowable by OPENING it (page.url). So the change
    signal we can read WITHOUT opening is sig = hash(participant_name | preview). On each
    run we load every conversation's stored sig from the DB; a visible row whose sig is
    unchanged is SKIPPED without opening (cheap). New/changed rows are opened, keyed by
    URN, and stored.
  * The list is virtualised (~10 rows in the DOM at once), so we drain it: process the
    first unprocessed visible row, then scroll to reveal more, until nothing new appears
    for a few scrolls or --max opens are done.
  * Resumable: the DB IS the cursor. Already-stored unchanged threads are skipped next
    run, so a crash mid-backfill just continues. --max bounds opens per run; run it
    repeatedly (or scheduled) to backfill a large inbox over many short paced passes.
  * Read-only throughout: opening a thread only SELECTS it; nothing is ever sent.
"""
from __future__ import annotations

import hashlib
import json
import random
import time

from . import AGENT, db
from . import keeper as K


def _emit(result: dict) -> dict:
    """Print the machine-readable RESULT line AND return the dict (so the API can use it)."""
    print("RESULT " + json.dumps(result))
    return result


def _sig(name: str, preview: str) -> str:
    """Change signal for a row without opening it: hash of name + latest-preview."""
    return hashlib.md5(f"{name}|{preview}".encode("utf-8")).hexdigest()


import re as _re
# LinkedIn appends screen-reader cruft to each list row's innerText; strip from the first
# marker so previews read clean ("Fine" not "Fine . Press return to go to conversation …").
_A11Y = _re.compile(
    r"\s*\.?\s*(Press (?:enter|return) to|Active conversation|Open the options list|"
    r"Open conversation options).*$", _re.IGNORECASE | _re.DOTALL)


def _clean(text: str) -> str:
    return _A11Y.sub("", text or "").strip()


def _preview(text: str) -> str:
    t = _clean(text)
    return (t.split(": ", 1)[-1][:200] if ": " in t else t[:200])


def probe() -> None:
    """P2 PROOF — connect to the keeper, prove the read works end-to-end. Read-only,
    NO DB writes."""
    from engine import ops
    with K.drive(spawn=False) as (page, msg):
        if page is None:
            return _emit({"ok": False, "msg": msg})
        ok, detail = K.selftest(page)
        rows = K.read_list(page, max_rows=10)
        print(f"selftest: {'PASS' if ok else 'FAIL'} - {detail}")
        print(f"--- first {len(rows)} inbox rows ---")
        for i, r in enumerate(rows):
            name = K._row_name(r["text"])
            pend = "<- owe reply" if K._row_pending(r["text"]) else ""
            print(f"  [{i:>2}] {name[:34]:<34} {pend}")
        ops.log_action(AGENT, "scrape", target="probe", result="ok" if ok else "fail")
        return _emit({"ok": ok, "msg": detail, "rows": len(rows)})


def avatars(max_stagnant: int = 4) -> dict:
    """Backfill profile photos via a SCROLLED list pass (no thread opens). LinkedIn lazy-
    loads avatars as you scroll, so we scroll the whole list, accumulating each row's photo
    (matched to stored rows by participant name). Cheap (one scrape) and read-only."""
    from engine import ops
    db.init()
    cx = db.connect()
    seen: dict[str, str] = {}
    try:
        with K.drive(spawn=False) as (page, msg):
            if page is None:
                return _emit({"ok": False, "msg": msg})
            # only chase photos for STORED conversations still missing one (they sit near the
            # top), so we stop early instead of scrolling the entire 600+ live inbox.
            want = {r["participant_name"] for r in cx.execute(
                "SELECT participant_name FROM conversations WHERE participant_name IS NOT NULL "
                "AND (participant_avatar IS NULL OR participant_avatar='')")}
            page.goto(K.INBOX_URL, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(3500)
            scrolls, stagnant = 0, 0
            while scrolls < 8 and stagnant < max_stagnant:
                for r in K.avatars_on_page(page):
                    if r["src"]:
                        seen[r["name"]] = r["src"]
                if want and want <= set(seen):     # every stored row that needed a photo has one
                    break
                before = len(seen)
                page.wait_for_timeout(500)         # let lazy imgs paint
                K.scroll_list(page, settle_ms=1000)
                scrolls += 1
                stagnant = stagnant + 1 if len(seen) <= before else 0
            # store only the photos for names we actually have rows for
            updated = sum(1 for name, src in seen.items() if db.set_avatar_by_name(cx, name, src))
            ops.log_action(AGENT, "scrape", target="avatars", result="ok")
        return _emit({"ok": True, "avatars_found": len(seen), "avatars_updated": updated})
    finally:
        cx.close()


# --- the two send functions that used to be here ---------------------------
#
# Both drove LinkedIn's own composer: one typed a reply and clicked Send, the
# other attached a file. They are gone, and their absence is the design rather
# than an oversight.
#
# LinkChat never carries your words the last few inches. A sequence writes a
# message, you approve it, and it lands in the outbox in your CRM marked unsent
# for you to send. Reading the inbox is unaffected: reading is not sending.


def _sync_state():
    import json
    from . import DATA_DIR
    p = DATA_DIR / "inbox_sync_state.json"
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return p, {}


# --- the two send functions that used to be here ---------------------------
#
# Both drove LinkedIn's own composer: one typed a reply and clicked Send, the
# other attached a file. They are gone, and their absence is the design rather
# than an oversight.
#
# LinkChat never carries your words the last few inches. A sequence writes a
# message, you approve it, and it lands in the outbox in your CRM marked unsent
# for you to send. Reading the inbox is unaffected: reading is not sending.


# --- voice notes and the internal-interface reader used to be here -----------
#
# They called LinkedIn's own internal interface rather than reading the pages the
# way a person does, and two of them sent things. The keeper functions underneath
# them were removed earlier today, so these could only ever have raised.
#
# What is left is `sync()`, which opens threads and reads what is on them.


def sync(max_deep: int = 20, max_stagnant: int = 4) -> None:
    """Backfill: scroll the inbox, open up to max_deep NEW/CHANGED threads, store them.
    Skips unchanged threads (by name+preview sig) without opening. Read-only + paced."""
    from engine import ops
    db.init()
    cx = db.connect()
    # Stored change-signals, keyed by participant name (the only list-visible identity).
    known = {row["participant_name"]: row["list_hash"]
             for row in cx.execute(
                 "SELECT participant_name, list_hash FROM conversations "
                 "WHERE participant_name IS NOT NULL")}
    processed: set[str] = set()           # names handled this run (skip or open)
    opened = skipped = no_urn = 0
    stop_reason = None                     # set by the budget break; else derived post-loop
    try:
        # spawn=True: reading the inbox is the job that OPENS the browser.
        #
        # This is the only place in LinkChat that opens one, on purpose. It is
        # the first thing a member does, it is a read rather than a send, and
        # it is where the sign-in has to happen. Everything else - opening a
        # conversation, and the send path - still attaches to the browser this
        # opened and refuses when there is not one, so no message can quietly
        # start a browser on its own.
        with K.drive(spawn=True) as (page, msg):
            if page is None:
                return _emit({"ok": False, "msg": msg})
            ok, detail = K.selftest(page)
            if not ok:
                return _emit({"ok": False, "msg": f"selftest failed: {detail}"})
            page.goto(K.INBOX_URL, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(3500)

            stagnant = 0
            while opened < max_deep and stagnant < max_stagnant:
                items = K.visible_listitems(page)
                # find the first VISIBLE row we haven't handled this run
                target = tname = tprev = ttext = None
                for it in items:
                    text = K.item_text(it)
                    name = K._row_name(text)
                    if name and name not in processed:
                        target, tname, tprev, ttext = it, name, _preview(text), text
                        break
                if target is None:
                    # nothing new visible -> scroll to reveal more
                    before = len(items)
                    after = K.scroll_list(page)
                    stagnant = stagnant + 1 if after <= before else 0
                    continue
                stagnant = 0
                processed.add(tname)
                sig = _sig(tname, tprev)
                if known.get(tname) == sig:
                    skipped += 1                       # unchanged -> don't open
                    continue
                # opening a thread is a 'scrape' against the shared daily cap (60/day) —
                # stop cleanly when exhausted so a big backfill paces over many runs and
                # never trips LinkedIn's radar (the account-health acceptance gate).
                ok_b, used_b, cap_b, _ = ops.check_budget("scrape")
                if not ok_b:
                    stop_reason = f"scrape budget reached ({used_b}/{cap_b}) - resume next run"
                    break
                last_dir = "in" if K._row_pending(ttext) else "out"
                avatar = K.row_avatar(target) or None
                urn, msgs = K.open_listitem(page, target)
                if urn:
                    # The thread is open right here, so this is the one moment
                    # the other person's profile link is on the screen. Without
                    # it the CRM only ever learns a name, and a name is a weak
                    # key - the event log then records "sent to a name nobody
                    # can identify" rather than to a person.
                    purl = K.thread_profile_url(page, tname)
                    cid = db.upsert_conversation(
                        cx, thread_urn=urn, name=tname, preview=tprev,
                        last_dir=last_dir, list_hash=sig, avatar=avatar,
                        profile_url=purl)
                    db.replace_messages(cx, cid, msgs)
                    known[tname] = sig
                    opened += 1
                    ops.log_action(AGENT, "scrape", target=urn, result="ok")
                else:
                    no_urn += 1
                time.sleep(random.uniform(2.5, 6.0))   # human pacing between opens
            if stop_reason is None:
                stop_reason = ("max_deep reached - more may remain, run again"
                               if opened >= max_deep else "inbox fully scanned (no more rows)")
            c = db.counts(cx)
        return _emit({"ok": True, "opened": opened, "skipped": skipped,
                      "no_urn": no_urn, "stop": stop_reason, **c})
    finally:
        cx.close()


def fetch_one(conv_id: int) -> dict:
    """Read one conversation in full, now, because you opened it.

    The backfill above reads a fixed number of threads per run, so a conversation
    further down the list has a name and a preview stored but no messages. Opening
    it used to ask for a command that did not exist, and the screen painted the
    answer as a conversation with nothing in it — the same picture as a person who
    has never written to you.

    This is the same read the backfill does, aimed at one thread.
    """
    db.init()
    cx = db.connect()
    try:
        row = cx.execute("SELECT id, thread_urn, participant_name FROM conversations "
                         "WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            return _emit({"ok": False, "msg": "no conversation with that number"})
        urn = row["thread_urn"]
        if not urn:
            return _emit({"ok": False,
                          "msg": "this conversation has no address on LinkedIn yet — "
                                 "sync the inbox and it will get one"})
        from engine import ops
        allowed, used, cap, why = ops.check_budget("scrape")
        if not allowed:
            return _emit({"ok": False, "msg": why})
        with K.drive(spawn=False) as (page, msg):
            if page is None:
                return _emit({"ok": False, "msg": msg})
            if not K._open_thread_via_inbox(page, urn, row["participant_name"]):
                return _emit({"ok": False,
                              "msg": "could not open that conversation in LinkedIn"})
            found_urn, msgs = K.read_current_thread(page)
            if not msgs:
                return _emit({"ok": True, "messages": 0,
                              "msg": "opened it, and there is nothing written in it yet"})
            db.replace_messages(cx, conv_id, msgs)
            ops.log_action(AGENT, "scrape", target=found_urn or urn, result="ok")
            return _emit({"ok": True, "messages": len(msgs)})
    finally:
        cx.close()
