"""One person on the do-not-message list, however their link is spelled.

    python tests/test_hold_list_cannot_be_bypassed.py

THE FAULT THIS EXISTS TO CATCH
------------------------------
A hold recorded as `linkedin.com/in/someone` did not match when the same person
arrived as `https://www.linkedin.com/in/someone`. Nine of the ten spellings
LinkedIn actually produces got straight through the gate.

It was not theoretical. On 2026-08-26 a message was sent, in a sandbox, to a
person sitting on the hold list, and the send path returned 200 and wrote it to
the outbox. The spelling that got through is the one LinkChat itself captures
off a thread - scheme, www, full URL - while a person adding somebody to their
list by hand naturally pastes the short form. So the two halves of the same
program disagreed about who a person was, and the disagreement opened the one
gate that must never open.

WHERE THE WEAKNESS IS, AND WHERE THE FIX IS
The key function lives in the CRM, which LinkChat does not own and must not
edit. What LinkChat owns is WHAT IT ASKS ABOUT - and the question accepts as
many identifiers as it is given. So it is given every form.

That direction is the whole point. Asking about more spellings can only ever
find MORE holds, never fewer. It makes the gate stricter. The dangerous move
would be the opposite: loosening a check so something gets past.

WHAT IT CHECKS
Every spelling finds the hold, and somebody who is NOT on the list is still
allowed - because a check that refuses everybody is not a working check, it is
a broken program that happens to be safe.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.crm_bridge import _every_form_of        # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, why: str = "") -> None:
    print("  %-6s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        for line in str(why).splitlines():
            print("         %s" % line)
        failures.append(label)


# Every one of these is a real spelling of ONE person's profile link.
SPELLINGS = [
    "linkedin.com/in/held-person",
    "https://www.linkedin.com/in/held-person",
    "https://www.linkedin.com/in/held-person/",
    "https://linkedin.com/in/held-person",
    "http://www.linkedin.com/in/held-person",
    "https://www.LinkedIn.com/in/Held-Person",
    "https://www.linkedin.com/in/held-person?utm_source=share",
    "https://www.linkedin.com/in/held-person/en",
    "www.linkedin.com/in/held-person",
    "/in/held-person",
]

# The forms a hold might have been WRITTEN in, by hand or by a tool.
RECORDED_AS = [
    "linkedin.com/in/held-person",
    "https://www.linkedin.com/in/held-person",
    "held-person",
]


def main() -> int:
    print("=" * 72)
    print("  the do-not-message list cannot be walked around")
    print("=" * 72)

    # --- 1. whatever it was recorded as, every spelling must reach it ------
    misses = []
    for recorded in RECORDED_AS:
        for spelling in SPELLINGS:
            asked = [a.lower() for a in _every_form_of(spelling, "")]
            if recorded.lower() not in asked:
                misses.append("held as %r, arriving as %r -> not asked about"
                              % (recorded, spelling))
    check("every spelling of a link reaches every way a hold was written "
          "(%d combinations)" % (len(RECORDED_AS) * len(SPELLINGS)),
          not misses, "\n".join(misses[:6]))

    # --- 2. two different people must never collide -----------------------
    mine = {a.lower() for a in _every_form_of(
        "https://www.linkedin.com/in/held-person", "")}
    theirs = {a.lower() for a in _every_form_of(
        "https://www.linkedin.com/in/a-different-person", "")}
    overlap = mine & theirs
    check("two different people share no identifier",
          not overlap,
          "these would make one person's hold silence the other: %s"
          % sorted(overlap)[:4])

    # --- 3. a plain name is not turned into a link ------------------------
    from_name = _every_form_of("Dana Whitfield")
    check("a name is left as a name",
          from_name == ["Dana Whitfield"],
          "a name was expanded into %r, which could match somebody else"
          % (from_name,))

    # --- 4. nothing in, nothing out ---------------------------------------
    check("empty input asks about nothing",
          _every_form_of("", None, "   ") == [],
          "empty input produced %r" % (_every_form_of("", None, "   "),))

    # --- 5. it holds against a REAL hold list, end to end ------------------
    # Built from scratch so this runs on a machine with no CRM on it.
    tmp = Path(tempfile.mkdtemp(prefix="linkchat-holds-"))
    try:
        src = ROOT / "tests" / "_fake_crm"          # not shipped; skip if absent
        if src.exists():
            shutil.copytree(src, tmp / "crm")
            from engine import crm_bridge
            b = crm_bridge.Bridge(tmp / "crm")
            b.parts["holds"].hold("linkedin.com/in/held-person", reason="test")
            got = [s for s in SPELLINGS if not b.is_held(s, "")]
            check("against a real hold list, no spelling gets through",
                  not got, "these got through: %s" % got[:4])
            check("somebody not on the list is still allowed",
                  not b.is_held("https://www.linkedin.com/in/a-stranger", "Stranger"),
                  "a stranger was refused, which means the check refuses everybody")
        else:
            print("  ---    no CRM fixture here; the checks above stand on their own")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print("NOT CLEAN: %d" % len(failures))
        return 1
    print("One person, however their link is written. Clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
