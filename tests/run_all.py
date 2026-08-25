"""Everything, in one command.

    python tests/run_all.py

Nine checks run, in the order a fault would matter:

  installs      does this work on a computer that is not the one it was built on
  names         does anybody's LinkedIn name field get typed back at them
  the gate      can anything reach a person without clearing every check
  the starter   does the sequence members start from carry any words of its own
  agreement     does every control on a screen reach a door the engine has
  names again   does a conversation carry a person's name, or a timestamp
  round trip    does a flow still have its messages after a trip through a file
  the CRM       does it learn who a message went to, and whose hand let it go
  the walk      does every door answer with a sentence rather than a crash

The first three read the code. The last one starts the engine and presses every
door, because the fault where a screen asks for something the engine no longer
has is invisible to reading: the engine falls over, the screen draws nothing, and
what you see is an empty list.

Nothing here sends anything. The two doors that can put words in front of a
person are pressed with words that must be refused, so what is proven is the
refusal.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

RUNS = [
    ("installs on a clean machine", "test_installs_on_a_clean_machine.py"),
    ("nobody's name typed back", "test_name_decoration.py"),
    ("nothing reaches a person unapproved", "test_nothing_sends_unapproved.py"),
    ("the starter is a shape, not a script", "test_starter_sequence.py"),
    ("the screens and the engine agree", "test_screens_and_engine_agree.py"),
    ("a conversation name is a name", "test_inbox_row_names.py"),
    ("a flow keeps its messages", "test_flow_round_trip.py"),
    ("the CRM gets updated", "test_crm_gets_updated.py"),
    ("the walk", "walk.py"),
]


def main() -> int:
    results = []
    for label, script in RUNS:
        print("\n" + "=" * 72)
        print("  " + label)
        print("=" * 72)
        # utf-8 on purpose. A child writing a tick or a cross into a pipe on
        # Windows gets the old codepage and dies on the character, so a checker
        # crashes only when it has bad news and reads green forever.
        proc = subprocess.run([sys.executable, str(HERE / script)], cwd=str(ROOT),
                              env={**__import__("os").environ,
                                   "PYTHONIOENCODING": "utf-8"})
        results.append((label, proc.returncode == 0))

    print("\n" + "=" * 72)
    for label, ok in results:
        print("  %-8s %s" % ("PASS" if ok else "FAIL", label))
    bad = [l for l, ok in results if not ok]
    print("")
    if bad:
        print("NOT CLEAN: " + ", ".join(bad))
        return 1
    print("Clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
