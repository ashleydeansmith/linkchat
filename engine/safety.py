"""safety.py — the one place every outward action asks permission first.

Nothing that reaches LinkedIn may skip this: connecting, withdrawing, reading a
search page, inviting somebody to an event. Each one calls can_act() and stops
if the answer is no.

WHY THIS IS NOT THE ONE THE LANES CAME WITH
The program these lanes were lifted from kept its OWN daily and weekly counts.
LinkChat must not: your CRM already has a ceiling, Gather already answers to it,
and a second set of counts is how two programs each stay inside a limit while
the ACCOUNT goes over it. LinkedIn counts the account, not the program.

So this asks your CRM, and nothing else. One ceiling, one answer.

  can_act(action)     -> (yes_or_no, why_not).  Ask BEFORE acting.
  did_act(action)     -> count it. Call AFTER it happened, never before.
  next_delay(sent)    -> how long to wait before the next one.

WHEN THE CEILING CANNOT BE READ, THE ANSWER IS NO.
A ceiling that cannot be consulted is not an absent ceiling. If Layer 6 is not
installed, or the CRM cannot be opened, every action is refused. A member sees
a sentence saying why; the account does not quietly run past a limit nobody
could check.

It makes no LinkedIn calls of its own. It reads a clock and asks your CRM.
"""
from __future__ import annotations

import random
from datetime import datetime

# What each lane counts as, in the words your CRM's ceiling already uses.
# 'scrape' is reading a page; 'connect' is asking somebody to connect; 'message'
# is putting words in front of a person. A lane whose kind is not in here is
# refused rather than guessed at.
KIND_OF = {
    "connect":  "connect",
    "withdraw": "connect",     # withdrawing is part of the same invite budget
    "invite":   "connect",
    "search":   "scrape",
    "scrape":   "scrape",
    "accept":   "scrape",
    "message":  "message",
    "dm":       "message",
}


def _bridge():
    from . import crm_bridge
    return crm_bridge.open_crm()


def can_act(action: str, cfg=None):
    """May one more of these happen right now? Returns (ok, why_not).

    cfg is accepted and ignored. The lanes were written to hand their own
    settings object to a ceiling that lived beside them; the ceiling lives in
    the CRM now, and taking the argument keeps those call sites unchanged.
    """
    kind = KIND_OF.get(str(action or "").lower())
    if not kind:
        return False, ("%r is not an action this knows how to count, so it is "
                       "refused rather than guessed at" % action)
    try:
        bridge = _bridge()
    except Exception as exc:
        return False, ("your CRM could not be opened, so the daily ceiling "
                       "cannot be read (%s)" % exc.__class__.__name__)
    try:
        return bridge.may_act(kind)
    except Exception as exc:
        return False, ("the daily ceiling could not be read (%s)"
                       % exc.__class__.__name__)


def did_act(action: str, note: str | None = None):
    """Count one, after it has actually happened.

    Counting an intention spends an allowance that was never used, and the next
    run then behaves as though it had been. So this is called after, never
    before, and a failure to count is never allowed to look like a failure to
    act.
    """
    kind = KIND_OF.get(str(action or "").lower())
    if not kind:
        return None
    try:
        return _bridge().did_act(kind, note)
    except Exception:
        return None


def next_delay(cfg=None, sent: int = 0) -> float:
    """Seconds to wait before the next one.

    Paced like a person rather than a loop: a spread rather than a fixed gap,
    and longer as the run goes on, because a steady rhythm at machine speed is
    the shape LinkedIn notices.
    """
    base = random.uniform(18.0, 46.0)
    if sent and sent % 10 == 0:
        base += random.uniform(45.0, 120.0)      # a pause, the way a person stops
    return base


def status() -> dict:
    """What the ceiling says right now, for a screen to show."""
    out = {"ok": False, "why": "", "kinds": {}}
    try:
        bridge = _bridge()
    except Exception as exc:
        out["why"] = "your CRM could not be opened (%s)" % exc.__class__.__name__
        return out
    for kind in ("connect", "scrape", "message"):
        try:
            ok, why = bridge.may_act(kind)
        except Exception as exc:
            ok, why = False, exc.__class__.__name__
        out["kinds"][kind] = {"allowed": bool(ok), "why": why or ""}
    out["ok"] = any(v["allowed"] for v in out["kinds"].values())
    out["checked"] = datetime.now().isoformat(timespec="seconds")
    return out
