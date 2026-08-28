"""flow_actions.py — the step kinds that are not a message: one person, one page, one act.

WHY THIS EXISTS (Build Plan V3 §8, Phase 3). A step in the flow is any action, and Ashley's
list was: withdraw an invitation, invite to the webinar, send the booking link, put on the
never-message list, write the stage into the CRM. Every one of those already has a lane
that does it in bulk — `withdraw.py` walks the Sent page, `events.py` walks the invite
picker — and each lane opens its own browser context and takes the shared lock, which a
pass already owns. So each act is lifted out here as a function that takes THE PASS'S
PAGE and acts for ONE person, reusing the lane's own selectors, verify-after checks and
ledger writes, and changing none of them.

Rules every function keeps:
  * it navigates to its own page first and never assumes where the page was left;
  * `live=False` reports what it WOULD do and touches nothing (the shadow run);
  * the lane's own cap is checked by the caller (`safety.can_act` per action type);
  * a red-listed person is refused before any page is opened;
  * the outcome is a plain word the pass records: done | would | skipped | failed.

crm_write is Phase 4 and stays `not_built` here.
"""
from __future__ import annotations

import time

from . import db


# ---------------------------------------------------------------------------
# red list — no browser, no lock
# ---------------------------------------------------------------------------

def red_list_one(lead: dict, reason: str | None, live: bool = True) -> tuple[str, str]:
    """Put one person on the never-message list. `stamp_note=False`: the engine owns three
    display keys in a Nexus note and nothing else; a fourth key through a side door is the
    fault the first critique named."""
    from . import redlist
    token = lead.get("profile_url") or lead.get("member_urn") or lead.get("full_name")
    if not token:
        return "skipped", "no identifier to red-list by"
    if not live:
        return "would", f"would red-list {token}"
    try:
        res = redlist.cmd_add(token, reason=reason or "flow step", category="flow", stamp_note=False)
        return "done", str(res.get("verb") or "added")
    except Exception as e:  # noqa: BLE001
        return "failed", f"{type(e).__name__}: {e}"[:160]


# ---------------------------------------------------------------------------
# the booking link — a send whose words carry the link, only after an affirmative
# ---------------------------------------------------------------------------

def booking_link(conn, version_id: int) -> tuple[str | None, str | None]:
    """(url, rule) from the active version's meta — read for the first time by the engine
    (audit §5.5: nothing ever read it back). None when the flow carries none."""
    import json
    row = conn.execute("SELECT meta FROM flow_versions WHERE id=?", (version_id,)).fetchone()
    if not row or not row["meta"]:
        return None, None
    try:
        meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
    except Exception:  # noqa: BLE001
        return None, None
    bl = (meta or {}).get("booking_link") or {}
    if isinstance(bl, str):
        return bl, None
    return bl.get("url"), bl.get("rule")


def fill_booking_link(bubbles: list[str], url: str | None) -> list[str]:
    """`{booking_link}` in a bubble becomes the link; a bubble that is only the token and
    no link exists is dropped rather than sent as a bare token."""
    out = []
    for b in bubbles:
        if "{booking_link}" in b:
            if not url:
                continue
            b = b.replace("{booking_link}", url)
        out.append(b)
    return out


# ---------------------------------------------------------------------------
# withdraw one pending invitation — on the pass's page
# ---------------------------------------------------------------------------

def withdraw_one(page, lead: dict, live: bool = True) -> tuple[str, str]:
    """Withdraw the ONE pending invitation to this person. Reuses withdraw.py's Sent-page
    reader, the aria-labelled control the bulk job clicks, and its verify-after — none of
    which is changed. Navigates to the Sent page itself; leaves the page there."""
    from . import withdraw as W
    from . import nav
    target = W.norm_url(lead.get("profile_url") or "")
    if not target:
        return "skipped", "no profile url"
    if db.red_list_match(url=lead.get("profile_url"), urn=lead.get("member_urn"), lead_id=lead.get("id")):
        return "skipped", "on the red list — invite left as-is"
    try:
        page = nav.human_open(page, W.SENT_URL)
        time.sleep(2)
        W._load_all(page)
        invites = W._read_invites(page)
    except Exception as e:  # noqa: BLE001
        return "failed", f"could not read the Sent list: {type(e).__name__}"[:160]
    match = next((v for v in invites if W.norm_url(v.get("profile_url") or "") == target), None)
    if not match:
        return "skipped", f"no pending invite to withdraw ({len(invites)} read)"
    if not live:
        return "would", f"would withdraw the invite to {match.get('name') or target} ({match.get('age_days')}d)"
    try:
        link = page.get_by_role("link", name=match["aria"], exact=True)
        if not link.count():
            return "failed", "withdraw control not found"
        link.first.click()
        dlg = page.locator('[data-testid="dialog"]')
        dlg.wait_for(state="visible", timeout=6000)
        dlg.get_by_role("button", name=match["aria"], exact=True).first.click()
        dlg.wait_for(state="detached", timeout=10000)
        if W._verify_withdrawn(page, target):
            return "done", f"withdrew the invite to {match.get('name') or target} — confirmed gone"
        return "failed", "still present after the withdraw click"
    except Exception as e:  # noqa: BLE001
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return "failed", f"{type(e).__name__}: {e}"[:160]


# ---------------------------------------------------------------------------
# invite one person to an event — on the pass's page
# ---------------------------------------------------------------------------

def invite_one(page, lead: dict, event_id: str, live: bool = True, campaign_id: int | None = None) -> tuple[str, str]:
    """Invite ONE person to a LinkedIn event through the picker events.py drives: open the
    dialog, search their name, match by LinkedIn id first, tick, press send, record. The
    picker is a shadow root — locators only, never page.evaluate (memory 2026-08-23)."""
    from . import events as E
    from . import nav
    if not event_id:
        return "skipped", "no event id on the step"
    if db.red_list_match(url=lead.get("profile_url") or ""):
        return "skipped", "on the red list"
    with db.connect() as conn:
        already = conn.execute("SELECT 1 FROM event_invites WHERE event_id=? AND (lead_id=? OR profile_url=?)",
                               (str(event_id), lead.get("id"), lead.get("profile_url"))).fetchone() \
            if _table_exists(conn, "event_invites") else None
    if already:
        return "skipped", "already invited to this event"
    if not live:
        return "would", f"would invite {lead.get('full_name')} to event {event_id}"
    try:
        page, shape = E._open_invite_dialog(page, str(event_id), nav)
        good, why = E._dialog_verdict(shape)
        if not good:
            return "failed", why
        E._search_for(page, lead.get("full_name") or "")
        hit, why = E._match_person(E._settled_rows(page), full_name=lead.get("full_name") or "",
                                   headline=lead.get("headline"), urn=lead.get("member_urn"))
        if hit is None:
            return "skipped", why
        if hit.get("checked"):
            return "skipped", "already ticked"
        if not E._tick(page, hit.get("urn") or lead.get("full_name")):
            return "failed", "the square would not tick"
        if not E._send(page):
            return "failed", "the send button did not respond"
        with db.connect() as conn:
            db.record_event_invite(conn, event_id=str(event_id), profile_url=lead.get("profile_url"),
                                   full_name=lead.get("full_name"), lead_id=lead.get("id"), campaign_id=campaign_id)
            if hit.get("urn") and not lead.get("member_urn"):
                conn.execute("UPDATE leads SET member_urn=? WHERE id=? AND (member_urn IS NULL OR member_urn='')",
                             (hit["urn"], lead.get("id")))
        from . import ops
        ops.log_action(E.AGENT, "event_invite", target=lead.get("profile_url"), result="ok")
        return "done", f"invited {lead.get('full_name')} to event {event_id}"
    except Exception as e:  # noqa: BLE001
        return "failed", f"{type(e).__name__}: {e}"[:160]


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# the cap each action draws on (safety.can_act's vocabulary)
CAP_ACTION = {"withdraw_invite": "withdraw", "invite_to_event": "event_invite",
              "send_booking_link": "message", "red_list": None, "crm_write": None}
