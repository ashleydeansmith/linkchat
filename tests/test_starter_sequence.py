"""The starter sequence must be a shape, never a script.

    python tests/test_starter_sequence.py

`sequences/starter-sequence.json` exists so that a member opening the Sequences
screen for the first time meets a filled shape instead of a blank canvas. It
carries the FOUR ways a person comes back from an opening message, and not one
word of what to say.

That second half is the part this file guards, and it is a safety property
rather than a stylistic one. Every message body in the starter is a gap in
curly brackets. Check three of the send gate refuses any message with a gap
left in it, so the starter physically cannot send anything until the member has
replaced every gap with their own words.

If somebody ever writes real copy into this file "to make it more useful", that
copy becomes a message nine people can approve and send under their own name
without having read it. This test is what stops that being a quiet change:

  1. every body that could ever be sent still carries a gap
  2. the patterns still pass the validator, so the import is not rejected
  3. the file still imports, and comes back out the same shape
  4. no message body contains a real sentence with no gap in it
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.flows_sensors import unresolved          # noqa: E402
from engine.flows_engine import validate_patterns    # noqa: E402

STARTER = ROOT / "sequences" / "starter-sequence.json"

failures: list[str] = []


def check(label: str, ok: bool, why: str = "") -> None:
    print("  %-6s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        print("         %s" % why)
        failures.append(label)


def main() -> int:
    print("=" * 72)
    print("  the starter sequence is a shape, not a script")
    print("=" * 72)

    if not STARTER.exists():
        check("the starter sequence is where the guide says it is", False,
              "%s is not there" % STARTER.relative_to(ROOT))
        return 1
    check("the starter sequence is where the guide says it is", True)

    try:
        doc = json.loads(STARTER.read_text(encoding="utf-8"))
    except Exception as exc:
        check("it is readable", False, str(exc))
        return 1
    check("it is readable", True)

    # --- 1. Everything sendable still carries a gap ------------------------
    sendable: list[tuple[str, str]] = []
    for o in doc.get("openers", []):
        sendable.append(("opener %s" % o.get("id"), o.get("text") or ""))
    for b in doc.get("branches", []):
        sendable.append(("%s next move" % b.get("id"), b.get("next_move") or ""))
        for i, t in enumerate(b.get("templates") or []):
            body = " ".join(t) if isinstance(t, list) else str(t or "")
            sendable.append(("%s template %d" % (b.get("id"), i + 1), body))

    check("there is something to send from at all", bool(sendable),
          "no openers and no next moves - the shape is empty")

    unguarded = [(w, t) for w, t in sendable if not unresolved(t)]
    check("every message in it is refused until it is rewritten (%d checked)"
          % len(sendable),
          not unguarded,
          "these would go to a real person exactly as written: "
          + "; ".join("%s -> %r" % (w, t[:60]) for w, t in unguarded[:3]))

    # --- 2. Empty is not the same as guarded -------------------------------
    # A blank body is refused too, but it teaches nothing and it is not what
    # this file is for. The gap has to NAME what the member must write.
    silent = [w for w, t in sendable if not t.strip()]
    check("no message is simply blank", not silent,
          "blank rather than a named gap: " + ", ".join(silent[:3]))

    # --- 3. The patterns still import --------------------------------------
    errs: list[str] = []
    for b in doc.get("branches", []):
        errs += ["%s: %s" % (b.get("id"), e)
                 for e in validate_patterns(b.get("patterns", []))]
    check("the patterns pass the validator", not errs, "; ".join(errs[:3]))

    # --- 4. The shape is still the shape -----------------------------------
    ids = [b.get("id") for b in doc.get("branches", [])]
    check("the four ways back are all still there",
          set(ids) >= {"R0", "R1", "R2", "R3"},
          "found %s" % (ids or "none"))

    no_reply = next((b for b in doc.get("branches", []) if b.get("id") == "R0"), None)
    check("the no-reply branch is entered by silence, not by words",
          bool(no_reply and no_reply.get("entry_timeout_days")),
          "R0 has no entry_timeout_days, so nothing ever enters it")

    # --- 5. It actually imports, into a scratch database -------------------
    # Reading the file proves the file. This proves the journey: the same call
    # the Import box on the Sequences screen makes, against a database thrown
    # away afterwards, so nothing here touches the member's own sequences.
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="linkchat-starter-test-"))
    try:
        import engine.db as db
        db.DB_PATH = tmp / "scratch.db"
        with db.connect():
            pass
        from engine import flows_engine as fe
        vid = fe.import_flows_json(str(STARTER), name="Starter sequence")
        graph = fe.version_graph(vid)
        kinds = {}
        for n in graph["nodes"]:
            kinds[n["kind"]] = kinds.get(n["kind"], 0) + 1
        check("the Import box would accept it (%d branches, %d steps, %d links)"
              % (kinds.get("branch", 0), kinds.get("move", 0), len(graph["edges"])),
              kinds.get("branch", 0) == 4 and kinds.get("opener", 0) == 1,
              "imported as %s, which is not the shape the file describes" % kinds)

        # The same guarantee, after the trip through the database rather than
        # before it. Import rewrites bodies into arms, and an arm is what a
        # member presses approve on.
        arm_bodies = [a["body"] for a in graph["arms"] if (a.get("body") or "").strip()]
        leaky = [b for b in arm_bodies if not unresolved(b)]
        check("every step is still refused after importing (%d checked)"
              % len(arm_bodies),
              bool(arm_bodies) and not leaky,
              "these arrived on the canvas ready to send: "
              + "; ".join(repr(b[:60]) for b in leaky[:3]))
    except Exception as exc:
        check("the Import box would accept it", False, "%s: %s"
              % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print("NOT CLEAN: %d" % len(failures))
        return 1
    print("The starter carries a shape and no words. Clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
