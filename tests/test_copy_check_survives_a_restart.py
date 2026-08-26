"""Test: closing LinkChat does not empty the check that stops repeated messages.

WHAT THIS EXISTS TO CATCH
-------------------------
The check that refuses a message nearly identical to a recent one kept its list
of recent messages in memory and nowhere else. Close LinkChat and open it again
- which a member does every day, and which a crash does for them - and it had
forgotten every message ever approved. The identical sentence, refused a second
earlier, went straight through.

That is the shape of fault worth a test of its own: the check is still there,
still runs, still says nothing is wrong. It has simply stopped being able to
find anything. A member reading the screen has no way to tell.

The words already live in your outbox, one file per message, written before
anything is carried anywhere. So the check reads them back rather than keeping
a second copy of its own, and this test proves it does.

Run:  python tests/test_copy_check_survives_a_restart.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS = []


def check(label, ok, detail=""):
    print("  %-6s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        if detail:
            print("         %s" % str(detail)[:300])
        FAILS.append(label)


SENTENCE = ("This is the sentence under test, long enough to be looked at, "
            "and it must only ever be accepted once.")


class FakePaths:
    def __init__(self, folder):
        self.folder = folder

    def outbox_dir(self):
        return self.folder


class FakeBridge:
    """Enough of a CRM for the check: a root, and somewhere its outbox lives."""

    def __init__(self, root):
        self.root = root
        self.parts = {"crm_paths": FakePaths(root / "_state" / "outbox")}


def write_outbox_message(root, body):
    """One outbox file, in the shape the CRM writes them."""
    folder = root / "_state" / "outbox"
    folder.mkdir(parents=True, exist_ok=True)
    n = len(list(folder.glob("*.md")))
    (folder / ("2026-08-26-%02d--Somebody.md" % n)).write_text(
        "---\nto: Somebody\nsent: false\n---\n\n"
        "# You wrote this, in Conversations\n\nSome explanation.\n\n"
        "---\n\n" + body + "\n\n---\n\n- has-something-to-say: yes\n",
        encoding="utf-8")


def main():
    print("=" * 72)
    print("  does the copy check survive LinkChat being closed?")
    print("=" * 72)

    from engine import server

    with tempfile.TemporaryDirectory(prefix="linkchat-copycheck-") as tmp:
        root = Path(tmp)
        bridge = FakeBridge(root)

        # --- 1. First time it is seen, it is allowed through -----------------
        server._RECENT_BODIES[:] = []
        server._RECENT_LOADED_FROM = None
        first = server._too_similar_to_recent(bridge, SENTENCE)
        check("a sentence nobody has sent is allowed", first is None, first)

        # --- 2. Straight away, the same sentence is refused ------------------
        again = server._too_similar_to_recent(bridge, SENTENCE)
        check("the same sentence, straight after, is refused", again is not None)

        # --- 3. LinkChat closes and opens. That is what these two lines are. -
        # The outbox has the message in it, because staging wrote it there.
        write_outbox_message(root, SENTENCE)
        server._RECENT_BODIES[:] = []
        server._RECENT_LOADED_FROM = None

        after = server._too_similar_to_recent(bridge, SENTENCE)
        check("and STILL refused after LinkChat is closed and opened",
              after is not None,
              "it was accepted - the check emptied itself on restart, which is "
              "the fault this test exists for")

        # --- 4. It has not become a check that refuses everything ------------
        server._RECENT_BODIES[:] = []
        server._RECENT_LOADED_FROM = None
        other = server._too_similar_to_recent(
            bridge,
            "A completely different reply about something else entirely, "
            "written for one person and nobody else.")
        check("a different sentence is still allowed", other is None, other)

        # --- 5. No CRM to read from means it does not crash ------------------
        class Bare:
            root = Path(tmp)
            parts = {}
        server._RECENT_BODIES[:] = []
        server._RECENT_LOADED_FROM = None
        try:
            server._too_similar_to_recent(Bare(), SENTENCE)
            check("a CRM that cannot say where its outbox is does not crash it", True)
        except Exception as exc:
            check("a CRM that cannot say where its outbox is does not crash it",
                  False, "%s: %s" % (exc.__class__.__name__, exc))

    print()
    print("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
