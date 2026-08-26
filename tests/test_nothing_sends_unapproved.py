"""Test: nothing reaches a person unless you decided it should.

This replaces an earlier test that said nothing could send at all. That was the
wrong rule, and it was wrong in a way worth writing down, because the same
mistake is easy to make again.

The rule was never "a machine must not carry your words". It is "a machine must
not DECIDE somebody should hear from you". Those come apart at the approval. Once
you have read a message and said yes, something carrying it into the conversation
is you using a tool — the same as a connection request going out because you
asked for one. Making you paste it in by hand adds nothing to the decision, which
was already made; it only breaks the loop, so no reply comes back and the
sequence never learns what happened.

So this test guards the gate rather than the absence of a send. Five things must
be true before a character reaches anybody:

  1. the parts that do the checking are present
  2. the person is not on the hold list
  3. it is not near enough to a recent message to be a copy
  4. a sequence wrote it and could not approve its own work
  5. a human approved it

Run:  python tests/test_nothing_sends_unapproved.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
FAILS = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label
          + (("   [" + detail + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


server = (ENGINE / "server.py").read_text(encoding="utf-8")
bridge = (ENGINE / "crm_bridge.py").read_text(encoding="utf-8")
keeper = (ENGINE / "inbox" / "keeper.py").read_text(encoding="utf-8")

print("\n=== 1. there is exactly ONE place that can carry a message ===")

senders = []
for f in sorted(ENGINE.rglob("*.py")):
    body = f.read_text(encoding="utf-8", errors="replace")
    if re.search(r"^def send_message\s*\(", body, re.M):
        senders.append(str(f.relative_to(ROOT)))
check("only one function can send", len(senders) == 1, ", ".join(senders) or "none")
check("and it is the inbox keeper",
      senders == ["engine\\inbox\\keeper.py"] or senders == ["engine/inbox/keeper.py"],
      ", ".join(senders))

print("\n=== 2. there is ONE road out, and every route takes it ===")

callers = []
for f in sorted(ENGINE.rglob("*.py")):
    body = f.read_text(encoding="utf-8", errors="replace")
    if f.name == "keeper.py":
        continue
    if re.search(r"\bsend_message\s*\(", body):
        callers.append(str(f.relative_to(ROOT)))
check("exactly one file carries a message", len(callers) == 1, ", ".join(callers) or "none")
check("and it is the engine's own server", any(c.endswith("server.py") for c in callers),
      ", ".join(callers))

# The road is one function. Two roads is how a check gets added to one of them and
# not the other, and it is always the forgotten one that reaches somebody.
carry_start = server.find("def _carry(")
carry_end = server.find("\n@app.", carry_start + 1)
carry = server[carry_start:carry_end if carry_end > 0 else len(server)]
check("the road out is one function", carry_start >= 0, "no _carry() found")
check("the send happens inside it", "send_message(" in carry)
check("nothing else in the file sends",
      server.count("send_message(") == carry.count("send_message("),
      "%d in the file, %d on the road" % (server.count("send_message("),
                                          carry.count("send_message(")))

# Every way a message can start must end up on that road.
WAYS = ["def crm_approve(", "def crm_reply("]
for way in WAYS:
    at = server.find(way)
    if at < 0:
        check("%s exists" % way.strip("def ("), False, "not found")
        continue
    stop = server.find("\n@app.", at + 1)
    fn = server[at:stop if stop > 0 else len(server)]
    check("%s goes through the one road" % way[4:-1], "_carry(" in fn)
    check("%s does not send by itself" % way[4:-1], "send_message(" not in fn)

print("\n=== 3. every check stands in front of it ===")

order = [
    ("the checking parts are installed", 'bridge.can("draft")'),
    ("the person is not on the hold list", "bridge.is_held("),
    ("nothing is left unfilled in the words", "_unfilled("),
    ("their own name field is not typed back", "_their_decoration("),
    ("it is not a copy of a recent message", "_too_similar_to_recent("),
    ("it was written down first", "bridge.stage("),
]
positions = {}
for name, needle in order:
    at = carry.find(needle)
    check("before sending: %s" % name, at >= 0, "not found")
    positions[name] = at

send_at = carry.find("send_message(")
for name, _ in order:
    if positions[name] >= 0:
        check("%s is checked BEFORE the send" % name, positions[name] < send_at,
              "runs after")

# The sixth check is only for a message a SEQUENCE wrote. A reply you typed
# yourself was already decided by a person, so there is nothing left to review -
# but a sequence's message must still clear your own review step, and that call
# has to sit in front of the send like all the others.
approve_at = carry.find("bridge.approve(")
check("a sequence's message still clears your review step", approve_at >= 0)
check("and that happens BEFORE the send", 0 <= approve_at < send_at)
check("it is asked for only when a sequence wrote it",
      "if item_id:" in carry and carry.find("if item_id:") < approve_at)

print("\n=== 4. the sequence still cannot approve its own work ===")

check("approving names a reviewer who is not the author",
      "reviewer or self.you()" in bridge and 'AUTHOR = "linkchat-sequence"' in bridge)
check("a refusal from the review step is raised, not swallowed",
      "raise NotAllowed(str(exc))" in bridge)

print("\n=== 5. a missing guard still means no ===")

check("no hold list means everyone is held",
      re.search(r"holds.*is None:\s*\n\s*return True", bridge, re.S) is not None)
check("no send gate means refuse", "the send gate is not installed" in bridge)
check("no browser lock means refuse", "the browser lock is not installed" in bridge)

print("\n=== 6. the words still go to your outbox either way ===")

check("it is written down before it is carried",
      positions["it was written down first"] >= 0
      and positions["it was written down first"] < send_at)
check("the outbox copy is your own CRM's, not one LinkChat invented",
      "stage_for_you" in bridge)

print("\n=== 7. nobody's details are baked in ===")

PERSONAL = [r"urn:li:fsd_profile:[A-Za-z0-9_-]{10,}",
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"]
for f in sorted(ENGINE.rglob("*.py")):
    body = "\n".join(l for l in f.read_text(encoding="utf-8", errors="replace").splitlines()
                     if not l.strip().startswith("#"))
    hits = [m for pat in PERSONAL for m in re.findall(pat, body)
            if "example" not in m.lower()]
    check("%s carries nobody's details" % f.relative_to(ROOT), not hits, ", ".join(hits[:2]))

print("\n=== 8. no file names one particular computer ===")

# WHAT THIS CATCHES, AND WHY IT WAS ADDED. Section 7 above looks for a person's
# profile address and their email address. It was written after two of those
# nearly shipped - and it found none of the three lines that named one particular
# computer: a folder of activity records, a signed-in browser, and a note written
# into a private vault. On anybody else's machine those folders do not exist, so
# every job that reached them failed. On the machine they were copied from, they
# wrote into somebody else's records.
#
# A path with a person's name in it is the same fault as an email address with
# their name in it, so it is checked the same way.

# The unix ones require a trailing slash, so what is matched is PATH-shaped.
# Without it, `/home/i.test(head)` - a piece of JavaScript inside a Python
# string, testing for the word "home" - was read as somebody's home folder and
# failed a file that names no computer at all. A check that cries wolf gets
# worked around, and then it is not a check.
# One backslash or two, because a path is written both ways in real source.
# A raw string leaves one in the file; the ordinary escaped spelling leaves two.
# Only the single form was matched, so the more common spelling walked past the
# check written to catch exactly it.
HOMES = [r"C:\\{1,2}Users\\{1,2}(?!Public|Default|All Users)[A-Za-z0-9._-]+",
         r"/Users/(?!Shared)[A-Za-z0-9._-]+/",
         r"/home/[A-Za-z0-9._-]+/"]
for f in sorted(ENGINE.rglob("*.py")):
    body = f.read_text(encoding="utf-8", errors="replace")
    lines = [l for l in body.splitlines() if not l.strip().startswith("#")]
    hits = [m for pat in HOMES for line in lines for m in re.findall(pat, line)]
    check("%s names no particular computer" % f.relative_to(ROOT), not hits,
          ", ".join(sorted(set(hits))[:2]))

print("\n=== 9. one ceiling decides who gets contacted, and it is yours ===")

ops_src = (ENGINE / "ops.py").read_text(encoding="utf-8")
check("LinkChat's own book-keeping rules on reading and nothing else",
      "is not something this part decides" in ops_src)
check("the shared daily limit is asked of your CRM",
      "limits.allow(" in bridge and "cap_from_config" in bridge)
check("nothing keeps a second list of work waiting to go out",
      "return []" in ops_src.split("def list_queue")[1].split("\ndef ")[0])

print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
