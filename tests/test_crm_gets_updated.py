"""When a message goes, does the CRM actually learn anything?

    python tests/test_crm_gets_updated.py

THE FAULT THIS EXISTS TO CATCH
------------------------------
On 2026-08-25 a real message was sent to a real person, and afterwards the CRM
could not say who it had gone to. The event log read:

    {"type": "message_sent", "person": null, "identifiers": ["Shabina ..."]}

`person: null`, on every event LinkChat had ever written. The cause was not a
bug in the CRM: its resolver only ever LOOKS UP a person, deliberately, because
a wrong merge is silent and permanent. Nothing in LinkChat ever created one. So
every event was filed against nobody, and none of the reading that hangs off
the log - when you last spoke to somebody, how often, what happened - could
find them.

A second fault sat beside it. A message a SEQUENCE wrote could only be sent
through the door meant for words you typed yourself, because nothing ever
called `propose`. That door skips one check by design - the one asking whether
somebody other than the author released it - so the check that the whole
"the gate is the approval" design rests on was never once exercised, and the
outbox recorded the sequence's words as the member's own.

WHAT IT CHECKS, AND HOW
Both faults are about ORDER: who is asked, and before what. So the bridge is
replaced with a stand-in that records the calls made to it, and the real send
path is driven against it. No CRM, no browser, no LinkedIn - which is why this
runs from a fresh clone on a machine that has neither.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(label: str, ok: bool, why: str = "") -> None:
    print("  %-6s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        for line in str(why).splitlines():
            print("         %s" % line)
        failures.append(label)


class FakeBridge:
    """A stand-in CRM that writes nothing and remembers everything asked of it."""

    AUTHOR = "linkchat-sequence"

    def __init__(self):
        self.calls: list[tuple] = []
        self.staged_as = None

    # --- the things _carry asks of a CRM ---------------------------------
    def can(self, job):
        self.calls.append(("can", job))
        return True, []

    def is_held(self, *identifiers):
        self.calls.append(("is_held", identifiers))
        return False

    def you(self):
        return "A Member"

    def propose(self, item_id, body, summary="", to="", identifier="", thread_urn=""):
        self.calls.append(("propose", item_id, to))
        return {"id": item_id, "author": self.AUTHOR}

    def approve(self, item_id, reviewer=None):
        self.calls.append(("approve", item_id, reviewer or self.you()))
        return {"id": item_id, "approved": True}

    def ensure_person(self, name, identifier=None):
        self.calls.append(("ensure_person", name, identifier))
        return "person-id", True

    def stage(self, message):
        self.calls.append(("stage", message.get("author")))
        self.staged_as = "sequence"
        return Path("nowhere.md")

    def stage_your_own(self, message):
        self.calls.append(("stage_your_own", message.get("author")))
        self.staged_as = "your own"
        return Path("nowhere.md")

    def log(self, type_, *identifiers, **kw):
        self.calls.append(("log", type_, identifiers))
        return {}

    def did_act(self, what, who):
        self.calls.append(("did_act", what, who))


def order_of(calls, name):
    for i, c in enumerate(calls):
        if c[0] == name:
            return i
    return -1


def main() -> int:
    print("=" * 72)
    print("  the CRM learns who a message went to, and who let it go")
    print("=" * 72)

    from engine import server

    # --- 1. a person is placed BEFORE the event is written ----------------
    b = FakeBridge()
    server._carry(b, to="Someone Real", identifier="https://www.linkedin.com/in/abc",
                  body="hello there", thread_urn="", kind="reply",
                  written_by="you")
    names = [c[0] for c in b.calls]
    check("the CRM is asked to place the person at all",
          "ensure_person" in names,
          "ensure_person was never called, so the event log has nobody to record "
          "against and every event reads person: null")

    i_person, i_log = order_of(b.calls, "ensure_person"), order_of(b.calls, "log")
    check("the person is placed BEFORE the event is written",
          i_person != -1 and i_log != -1 and i_person < i_log,
          "ensure_person at %d, log at %d - placing them afterwards is too late, "
          "the event has already been filed against nobody" % (i_person, i_log))

    logged = [c for c in b.calls if c[0] == "log"]
    ids = logged[0][2] if logged else ()
    check("the event carries the link AND the name",
          len(ids) >= 2 and any("/in/" in str(x) for x in ids),
          "the event went out with %r - the resolver takes the strongest key it "
          "is given, and one of them is often the only one the CRM knows" % (ids,))

    # --- 2. words you typed take the your-own door ------------------------
    check("a message you typed is staged as your own",
          b.staged_as == "your own",
          "staged as %r" % b.staged_as)

    # --- 3. a sequence's message takes the sequence door ------------------
    b2 = FakeBridge()
    server._carry(b2, to="Someone Real", identifier="https://www.linkedin.com/in/abc",
                  body="the agreed words", thread_urn="", kind="reply",
                  written_by=FakeBridge.AUTHOR, item_id="linkchat-1-abc")
    names2 = [c[0] for c in b2.calls]
    check("a message the sequence wrote is released by somebody first",
          "approve" in names2,
          "approve was never called, so nothing was ever independently released - "
          "this is the check the whole design rests on")
    check("and it is staged as the sequence's work, not as yours",
          b2.staged_as == "sequence",
          "staged as %r, so the outbox would record the sequence's words as the "
          "member's own" % b2.staged_as)

    i_appr, i_stage = order_of(b2.calls, "approve"), order_of(b2.calls, "stage")
    check("released BEFORE it is staged",
          i_appr != -1 and i_stage != -1 and i_appr < i_stage,
          "approve at %d, stage at %d" % (i_appr, i_stage))

    # --- 3b. the person's name is filled, and nothing else is guessed -----
    # The agreed messages carry {name}. Nothing filled it, so check three
    # refused them - on a real inbox three of seven suggestions could not be
    # sent at all. The name is filled from the conversation; a gap only a
    # person can fill is left in, so the check still stops the message.
    from engine import names as _names
    from engine.flows_sensors import unresolved as _unresolved

    def fill(body, who):
        first = _names.first_name_of(who) or ""
        for t in ("{name}", "{first_name}", "{firstname}"):
            body = body.replace(t, first)
        return body

    filled = fill("Good connecting with you {name}", "Mariia Potupchik")
    check("the person's name is filled in",
          "Mariia" in filled and not _unresolved(filled),
          "came out as %r" % filled)

    left = fill("{their build} - sounds cool!", "Mariia Potupchik")
    check("a gap only a person can fill is still refused",
          bool(_unresolved(left)),
          "%r passed, so a message reading '{their build}' could reach somebody" % left)

    # --- 4. the door that makes the sequence path reachable ---------------
    paths = {r.path for r in server.app.routes if getattr(r, "path", "").startswith("/api")}
    check("there is a door for releasing a message a sequence wrote",
          "/api/crm/approve-suggested" in paths,
          "without it the only way to send is the door for your own words, and "
          "every sequence message goes out recorded as yours")

    print()
    if failures:
        print("NOT CLEAN: %d" % len(failures))
        return 1
    print("The CRM is told who, and by whose hand. Clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
