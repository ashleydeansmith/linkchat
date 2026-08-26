"""A flow that goes out and comes back must still have its messages in it.

    python tests/test_flow_round_trip.py

THE FAULT THIS EXISTS TO CATCH
------------------------------
export_flows_json wrote eight keys per branch and `templates` was not one of
them. Templates are the AGREED MESSAGES - the actual words that get sent. So
exporting a flow and importing it again produced a flow with every branch, every
pattern, every link, and not one message.

It was silent in the worst way. On 2026-08-25 the live v6.2 flow was carried out
of the parent program and into LinkChat: the import reported nine branches and twenty-nine
links and looked complete. All ten locked messages were gone. The only trace was
the guidance prose that survived, still instructing the reader to "send
templates[0]" - pointing at something no longer in the file.

Downstream it went quiet rather than loud, which is why nobody caught it:

    no 't' arms  ->  give_version() returns None
                 ->  suggest_for_text() returns an empty payload
                 ->  every conversation in the review queue reads "off the map"
                 ->  nothing can ever be proposed, so nothing can be approved

A backup taken with that exporter also had no messages in it.

WHAT IT CHECKS
Build a flow with messages, send it out, bring it back, and compare. The
comparison is on the WORDS, because that is what was lost - a check on the
branch count would have passed happily the whole time it was broken.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
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


# A flow shaped like the real one: guidance prose on the branch, and the agreed
# messages underneath it as bubble lists.
SOURCE = {
    "name": "round trip",
    "openers": [{"id": "B", "label": "Opener B", "text": "Hey {name} · Have you built one?"}],
    "branches": [
        {
            "id": "R1", "label": "They said not yet", "color": "#111",
            "patterns": ["not yet", "i have not", "nope"],
            "read": "They have not built one.",
            "next_move": "Send the locked message - templates[0] after opener B.",
            "never": ["Never draft a new one."],
            "templates": [
                ["What?! no way!", "What's kept you from hiring one?"],
                ["What?! no way!", "Mine only took 15 mins to set up"],
            ],
            "forward": [{"on": "they answer", "then": "route to R2"}],
        },
        {
            "id": "R2", "label": "They build them", "color": "#222",
            "patterns": ["i build", "we build"],
            "read": "They already build them.",
            "next_move": "Send the locked message.",
            "never": [],
            "templates": [["Glad to find someone else doing it", "Solo or a team?"]],
            "forward": [],
        },
    ],
}


def messages_of(doc: dict) -> dict:
    """branch id -> the messages on it, joined the way they are sent."""
    out = {}
    for b in doc.get("branches", []):
        got = []
        for t in b.get("templates") or []:
            got.append(" · ".join(t) if isinstance(t, list) else str(t))
        out[b["id"]] = got
    return out


def main() -> int:
    print("=" * 72)
    print("  a flow keeps its messages through an export and an import")
    print("=" * 72)

    tmp = Path(tempfile.mkdtemp(prefix="linkchat-roundtrip-"))
    try:
        import engine.db as db
        db.DB_PATH = tmp / "scratch.db"
        with db.connect():
            pass
        from engine import flows_engine as fe

        src_file = tmp / "source.json"
        src_file.write_text(json.dumps(SOURCE, ensure_ascii=False), encoding="utf-8")

        vid = fe.import_flows_json(str(src_file), name="round trip")
        graph = fe.version_graph(vid)
        t_arms = [a for a in graph["arms"]
                  if (a.get("arm_key") or "").startswith("t")]
        check("importing a flow keeps its messages (%d found)" % len(t_arms),
              len(t_arms) == 3,
              "expected 3 agreed messages on the way in, found %d" % len(t_arms))

        # The downstream consequence, checked rather than reasoned about.
        # give_version only answers for an ACTIVE version carrying a 't' arm, and
        # when it answers None the cockpit proposes nothing at all - which is how
        # this bug actually showed up.
        fe.activate_version(vid)
        check("once it is switched on, the cockpit can propose from it",
              fe.give_version() is not None,
              "give_version() came back None, so suggest_for_text would return "
              "an empty payload and every conversation would read 'off the map'")

        back = fe.export_flows_json(vid)
        want, got = messages_of(SOURCE), messages_of(back)

        missing = []
        for bid, msgs in want.items():
            if got.get(bid) != msgs:
                missing.append("  %s: went out as %r, came back as %r"
                               % (bid, msgs, got.get(bid)))
        check("every message survives the round trip", not missing,
              "\n".join(missing))

        # And the shape, so a fix to one does not quietly cost the other.
        check("the branches survive too",
              [b["id"] for b in back["branches"]] and
              set(b["id"] for b in back["branches"]) == {"R1", "R2"},
              "branches came back as %s" % [b.get("id") for b in back["branches"]])
        check("the patterns survive too",
              all(b.get("patterns") for b in back["branches"]),
              "a branch came back with no patterns, so it could never match")

        # Import what we exported: the real journey is out and back IN again.
        again = tmp / "again.json"
        again.write_text(json.dumps(back, ensure_ascii=False), encoding="utf-8")
        vid2 = fe.import_flows_json(str(again), name="round trip 2")
        arms2 = [a for a in fe.version_graph(vid2)["arms"]
                 if (a.get("arm_key") or "").startswith("t")]
        check("re-importing the export keeps the messages (%d)" % len(arms2),
              len(arms2) == 3,
              "the second import kept %d of 3 - this is the shape a backup "
              "takes, so a backup would be missing them too" % len(arms2))
    except Exception as exc:  # noqa: BLE001
        check("the round trip runs at all", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print("NOT CLEAN: %d" % len(failures))
        return 1
    print("A flow carries its messages out and back. Clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
