"""The name on a conversation must be a name, and nothing else.

    python tests/test_inbox_row_names.py

THE FAULT THIS EXISTS TO CATCH
------------------------------
A row in the LinkedIn inbox reads:

    <name> <when> <who said it>: <preview of the message>

LinkChat cuts the name off the front by finding whatever stamps the row with a
time. It used to look for a MONTH only - and LinkedIn writes a month on an old
conversation but a clock time on one from today. So every conversation from
today fell past the cut to a blunt 40-character slice and kept the timestamp,
and the start of the message, inside the person's name:

    "Malcom Ovwighose 10:42 AM 10:42 AM  You: You must be crazy snowed under?"

Found on 2026-08-25 by syncing a real inbox: eleven of twelve rows were from
that day, so eleven of twelve names were wrong. It had gone unseen for months
because any reply older than today parses correctly, and a test written from
imagination would have used "Jane Bloggs Aug 14" and passed.

WHY IT IS WORTH A TEST OF ITS OWN
The name is what a member reads down the list, and it is one of the two things
the send gate checks - their own name field must not be typed back at them. A
name carrying a timestamp is a dirty record on both counts. The greeting itself
survives, because first_name_of takes the first word, and that is checked here
too so nobody assumes it.

Every row below is verbatim from a real inbox. Add to them rather than
inventing new shapes - invented rows are what hid this for months.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.inbox.keeper import _row_name        # noqa: E402
from engine import names                          # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, why: str = "") -> None:
    print("  %-6s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        for line in str(why).splitlines():
            print("         %s" % line)
        failures.append(label)


# (row as LinkedIn rendered it, the name it must yield)
REAL_ROWS = [
    # --- from today: a clock time. These were all broken before 2026-08-25.
    ("Malcom Ovwighose 10:42 AM 10:42 AM  You: You must be crazy snowed under?",
     "Malcom Ovwighose"),
    ("Marcin Mleczko 11:02 AM  You: https://example.com", "Marcin Mleczko"),
    ("Rayhan Mahmood 12:17 PM  Rayhan: thanks for connecting", "Rayhan Mahmood"),
    ("Louis Dunne 12:58 PM  You: And bring the deck", "Louis Dunne"),
    ("Akshat Sharma 1:02 PM  You: You around?", "Akshat Sharma"),
    ("Mariia Potupchik 1:04 PM  You: ok", "Mariia Potupchik"),
    ("Vamshi Krishna Y 1:09 PM  Vamshi: hi", "Vamshi Krishna Y"),
    ("Jeremy Shorter 3:09 PM 3:09 PM  You: yes", "Jeremy Shorter"),
    # --- older: a month. These already worked and must not regress.
    ("Jane Bloggs Aug 14  You: thanks", "Jane Bloggs"),
    ("Status is online Peter Smith Jul 2  Peter: hello", "Peter Smith"),
    ("Status is reachable Ana De Silva Dec 11  You: sure", "Ana De Silva"),
    # --- a relative age, which neither of the two above catches.
    ("Tom Baker 2h  Tom: quick one", "Tom Baker"),
]


def main() -> int:
    print("=" * 72)
    print("  the name on a conversation is a name")
    print("=" * 72)

    wrong = []
    for row, want in REAL_ROWS:
        got = _row_name(row)
        if got != want:
            wrong.append("%r -> %r, wanted %r" % (row[:46], got, want))
    check("every real inbox row gives the right name (%d rows)" % len(REAL_ROWS),
          not wrong, "\n".join(wrong))

    # The properties that matter even for a row shape nobody has seen yet.
    dirty = []
    for row, _ in REAL_ROWS:
        got = _row_name(row)
        if any(ch.isdigit() for ch in got):
            dirty.append("%r keeps a digit: %r" % (row[:40], got))
        elif ":" in got:
            dirty.append("%r keeps a colon: %r" % (row[:40], got))
        elif got.endswith((" AM", " PM", " am", " pm")):
            dirty.append("%r keeps a clock: %r" % (row[:40], got))
        elif not got.strip():
            dirty.append("%r gives an empty name" % row[:40])
    check("no name carries a time, a colon or a digit", not dirty,
          "\n".join(dirty))

    # The greeting is taken from the name, so it is checked rather than assumed.
    bad_greet = []
    for row, want in REAL_ROWS:
        first = names.first_name_of(_row_name(row))
        if not first or first != want.split()[0]:
            bad_greet.append("%r would be greeted %r, wanted %r"
                             % (row[:40], first, want.split()[0]))
    check("the greeting takes the right first name", not bad_greet,
          "\n".join(bad_greet))

    print()
    if failures:
        print("NOT CLEAN: %d" % len(failures))
        return 1
    print("Names are clean on today's rows and on older ones. Clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
