"""Test: the sequence walker stages, and never sends.

The three engine files under engine/ that walk a person through a sequence
(flow_steps.py, flow_run.py, flow_actions.py) are shared with the parent program,
byte for byte. There, a setting lets the engine carry a due message itself. Here
there is no such setting - LinkChat's config has no field for it - so the engine
must put every due message in front of you and carry nothing. This test walks two
people through the starter sequence on a scratch database and proves:

  1. a due message is STAGED (written down, waiting for you) and the sender the
     parent program uses is never reached
  2. the engine walks the person on ONLY when the road out reports the message went
  3. the starter's ladder is two messages and then silence, as its own words say
  4. a reply is matched against the branch the person is standing on, and the
     answer is staged - not sent
  5. an inbox conversation is tied to a person by profile address, or by a name only
     ONE person has; two people with the same name stay untied
  6. LinkChat's settings carry no way to turn the engine's own sending on
  7. the three engine files are the parent program's, unchanged (when it is here)

Run:  python tests/test_flow_stages_never_sends.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
FAILS = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + (("   [" + str(detail)[:300] + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


print("\n=== 6. the settings have no switch for it ===")
cfg_src = (ROOT / "engine" / "config.py").read_text(encoding="utf-8")
check("config.py has no flow_auto_send field (absent = off, and nothing can turn it on)",
      "flow_auto_send" not in cfg_src)
from engine.config import Config  # noqa: E402
check("a loaded Config has no such attribute either", not hasattr(Config(), "flow_auto_send"))

print("\n=== 7. the engine files are the parent program's, unchanged ===")
PARENT = Path.home() / "Documents" / "LinkForge" / "linkforge"
for f in ("flow_steps.py", "flow_run.py", "flow_actions.py"):
    if PARENT.exists():
        # a fresh clone on Windows checks out CRLF; the rule is about the words, not the endings
        norm = lambda b: b.replace(b"\r\n", b"\n")  # noqa: E731
        same = norm((PARENT / f).read_bytes()) == norm((ROOT / "engine" / f).read_bytes())
        check("%s is byte-identical to the parent program's" % f, same)
    else:
        check("%s: parent program not on this machine, nothing to compare" % f, True)

print("\n=== 1-4. the walk, on a scratch database ===")
tmp = Path(tempfile.mkdtemp(prefix="linkchat-flow-test-"))
try:
    import engine.db as db
    from engine.inbox import db as cvdb
    from engine import flows_engine as fe
    from engine import flow_run as FR

    db.DB_PATH = tmp / "scratch.db"
    cvdb.DB_PATH = tmp / "inbox.db"
    cvdb.DATA_DIR = tmp
    with db.connect():
        pass
    cvdb.init()

    vid = fe.import_flows_json(str(ROOT / "sequences" / "starter-sequence.json"),
                               name="Starter sequence", activate=True)

    def boom(*a, **k):
        raise AssertionError("the parent program's sender was reached")
    FR._default_sender = boom

    STAGED = []

    def stager(lead, bubbles, key, node, ref):
        STAGED.append((lead["id"], bubbles, key, node))
        return "in the test's queue"
    FR.STAGER = stager

    OK = lambda action: (True, "test")  # noqa: E731
    T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def add_lead(name, url):
        with db.connect() as conn:
            cur = conn.execute("INSERT INTO leads (profile_url, full_name, status, accepted_at, created_at, updated_at) "
                               "VALUES (?,?,?,?,?,?)", (url, name, "accepted", T0.isoformat(), T0.isoformat(), T0.isoformat()))
            return cur.lastrowid

    def journey(jid):
        with db.connect() as conn:
            return dict(conn.execute("SELECT * FROM journeys WHERE id=?", (jid,)).fetchone())

    lead = add_lead("Ada Example", "https://www.linkedin.com/in/ada-example")
    jid = FR.enrol(lead, now=T0)
    s = FR.pass_(now=T0, live=True, sender=boom, can_act=OK)
    j = journey(jid)
    check("the opener is staged, not sent (staged 1, sent 0)", s["staged"] == 1 and s["sent"] == 0, s)
    check("the person waits for your approval at the opener", j["waiting_for"] == "approval" and j["node_key"] == "R0.opener", j)
    check("what was staged is the starter's gap, not words", STAGED and "{" in " ".join(STAGED[0][1]), STAGED)
    key = STAGED[0][2]
    s = FR.pass_(now=T0 + timedelta(hours=1), live=True, sender=boom, can_act=OK)
    check("a second pass stages nothing more", s["staged"] == 0 and len(STAGED) == 1, s)

    r = FR.release(key, "not sent: refused by the send gate", by="you")
    check("a refused carry leaves them waiting where they were", r["ok"] is False and journey(jid)["waiting_for"] == "approval", r)
    r = FR.release(key, "sent", by="you", at=T0 + timedelta(hours=2))
    j = journey(jid)
    check("a carry that went walks them on to the one follow-up, due four days later",
          r["ok"] and j["node_key"] == "R0.f1" and j["waiting_for"] == "clock"
          and FR._parse(j["next_wake_at"]) == T0 + timedelta(hours=2, days=4), j)

    D4 = T0 + timedelta(days=4, hours=3)
    s = FR.pass_(now=D4, live=True, sender=boom, can_act=OK)
    check("day 4: the follow-up is staged, not sent", s["staged"] == 1 and s["sent"] == 0 and STAGED[-1][3] == "R0.f1", s)
    FR.release(STAGED[-1][2], "sent", by="you", at=D4)
    j = journey(jid)
    check("after it, thirty quiet days are set before they are left alone",
          j["node_key"] == "R0.leave" and FR._parse(j["next_wake_at"]) == D4 + timedelta(days=30), j)
    s = FR.pass_(now=D4 + timedelta(days=31), live=True, sender=boom, can_act=OK)
    j = journey(jid)
    check("day 35: left alone - parked, two messages in total, nothing else staged",
          j["status"] == "parked" and j["sends_done"] == 2 and s["staged"] == 0 and len(STAGED) == 2, (j, s))

    # 4. a reply, matched against the branch they stand on, answer staged
    lead2 = add_lead("Bob Example", "https://www.linkedin.com/in/bob-example")
    j2 = FR.enrol(lead2, now=T0)
    FR.pass_(now=T0, live=True, sender=boom, can_act=OK)
    FR.release(STAGED[-1][2], "sent", by="you", at=T0)
    cx = cvdb.connect()
    urn = "urn:li:msg_conversation:(test,bob)"
    cvdb.upsert_conversation(cx, thread_urn=urn, name="Bob Example", preview="Thanks for connecting - how does it work?",
                             last_dir="in", profile_url="https://www.linkedin.com/in/bob-example",
                             last_at=str(int((T0 + timedelta(hours=1)).timestamp() * 1000)))
    cx.execute("UPDATE conversations SET lead_id=? WHERE thread_urn=?", (lead2, urn)); cx.commit(); cx.close()
    s = FR.pass_(now=T0 + timedelta(hours=1, minutes=5), live=True, sender=boom, can_act=OK)
    j = journey(j2)
    check("their reply is seen and matched by the word lists alone (no reader installed)",
          s["replies"] == 1 and s.get("matched") == 1 and j["node_key"] == "R1.move", (s, j["node_key"]))
    s = FR.pass_(now=T0 + timedelta(hours=1, minutes=30), live=True, sender=boom, can_act=OK)
    check("the answer is staged for you a few minutes after their reply - not sent",
          s["staged"] == 1 and s["sent"] == 0 and STAGED[-1][3] == "R1.move", s)

    print("\n=== 5. tying inbox conversations to people ===")
    from engine.__main__ import _link_inbox_to_people
    add_lead("Cara Example", "https://www.linkedin.com/in/cara-example")
    add_lead("Dan Twice", "https://www.linkedin.com/in/dan-twice-1")
    add_lead("Dan Twice", "https://www.linkedin.com/in/dan-twice-2")
    cx = cvdb.connect()
    cvdb.upsert_conversation(cx, thread_urn="urn:li:msg_conversation:(test,cara)", name="Cara Example", preview="hi", last_dir="in", last_at="1")
    cvdb.upsert_conversation(cx, thread_urn="urn:li:msg_conversation:(test,dan)", name="Dan Twice", preview="hi", last_dir="in", last_at="1")
    cx.commit(); cx.close()
    _link_inbox_to_people()
    cx = cvdb.connect()
    cara = cx.execute("SELECT lead_id FROM conversations WHERE participant_name='Cara Example'").fetchone()[0]
    dan = cx.execute("SELECT lead_id FROM conversations WHERE participant_name='Dan Twice'").fetchone()[0]
    cx.close()
    check("a name only one person has is tied to them", cara is not None, cara)
    check("a name two people share is left untied, on purpose", dan is None, dan)
except Exception as exc:
    import traceback
    traceback.print_exc()
    check("the walk ran", False, "%s: %s" % (type(exc).__name__, exc))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
