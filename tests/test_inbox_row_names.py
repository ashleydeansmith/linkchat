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

    "Marcus Oyelaran 10:42 AM 10:42 AM  You: You must be crazy snowed under?"

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

Every row below keeps the SHAPE of a real inbox row exactly - the doubled clock
time, the "You:" prefix, the preview running on past it, the "Status is online"
lead-in. That shape is the whole point: rows invented from imagination would
have read "Jane Bloggs Aug 14" and passed while the fault sat there for months.

The NAMES are stand-ins. The real ones were somebody's actual LinkedIn
connections, with fragments of their messages beside them, and this repository
is public. Add rows by copying a real shape and changing the name.
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
ROWS = [
    # --- from today: a clock time. These were all broken before 2026-08-25.
    ("Marcus Oyelaran 10:42 AM 10:42 AM  You: You must be crazy snowed under?",
     "Marcus Oyelaran"),
    ("Mateusz Wiercinski 11:02 AM  You: https://example.com", "Mateusz Wiercinski"),
    ("Rayhan Mahfouz 12:17 PM  Rayhan: thanks for connecting", "Rayhan Mahfouz"),
    ("Lewis Dunmore 12:58 PM  You: And bring the deck", "Lewis Dunmore"),
    ("Aarav Shastri 1:02 PM  You: You around?", "Aarav Shastri"),
    ("Mariya Petrenko 1:04 PM  You: ok", "Mariya Petrenko"),
    ("Vikram Chandra R 1:09 PM  Vikram: hi", "Vikram Chandra R"),
    ("Jeremy Shawcross 3:09 PM 3:09 PM  You: yes", "Jeremy Shawcross"),
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
    for row, want in ROWS:
        got = _row_name(row)
        if got != want:
            wrong.append("%r -> %r, wanted %r" % (row[:46], got, want))
    check("every real inbox row gives the right name (%d rows)" % len(ROWS),
          not wrong, "\n".join(wrong))

    # The properties that matter even for a row shape nobody has seen yet.
    dirty = []
    for row, _ in ROWS:
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
    for row, want in ROWS:
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
