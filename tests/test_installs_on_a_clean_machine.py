"""Test: this installs and runs on a computer that is not the one it was built on.

Two faults sat in the program until 2026-08-23, and neither of them was findable
by reading the code, because both were about the machine rather than the logic.

  1. A part of the program needed something that was never in the list of things
     to install. On the machine it was written on it was already there, so it
     worked. On a fresh computer the program starts, draws its screens, and then
     fails the first time anything touches the browser - which is the moment that
     matters and the moment somebody is watching.

  2. The door onto its own browser had the same number as another program's door.
     Both were installed on one computer. LinkChat asked for "the browser at that
     door", got the other program's browser signed into a different account, and
     drove it. That only happens on a machine with both installed, which is
     exactly the machine the live install is demonstrated from.

So this checks the things a working machine hides:

  - everything the program imports is either part of Python, part of LinkChat, or
    on the list of things to install
  - the list of things to install has no line that is only true here
  - LinkChat's browser door is its own

Run:  python tests/test_installs_on_a_clean_machine.py
"""

import ast
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


# --------------------------------------------------------------------------
# What is on the list of things to install
# --------------------------------------------------------------------------

req_text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
REQUIRED = set()
for line in req_text.splitlines():
    line = line.split("#", 1)[0].strip()
    if not line:
        continue
    REQUIRED.add(re.split(r"[<>=!\[ ]", line, maxsplit=1)[0].strip().lower())

# The name typed in `import x` is not always the name on the list. These are the
# ones where they differ, and there are few enough to write down.
INSTALLED_AS = {
    "playwright": "playwright",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "psutil": "psutil",
    "webview": "pywebview",
    "PIL": "pillow",
    "requests": "requests",
    "dotenv": "python-dotenv",
}

# Anything that comes with Python itself.
STDLIB = set(sys.stdlib_module_names)

# Anything that is part of LinkChat.
OURS = {"engine", "linkedin_browser", "tests"}
for path in ENGINE.rglob("*.py"):
    OURS.add(path.stem)
    OURS.add(path.parent.name)

print("=== 1. everything imported is Python's, LinkChat's, or on the list ===")

# An import written inside a `try:` with something to fall back on is a choice,
# not a requirement: the program keeps working without it. Those are allowed to be
# absent from the list. An import at the top of a file is not - if it is missing
# the file cannot load, and the program fails on the machine it was sent to.
def _optional_imports(tree):
    optional = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        for inner in ast.walk(node):
            if inner in node.handlers:
                continue
            if isinstance(inner, ast.Import):
                optional.update(a.name.split(".")[0] for a in inner.names)
            elif isinstance(inner, ast.ImportFrom) and inner.level == 0 and inner.module:
                optional.add(inner.module.split(".")[0])
    return optional


outside = {}
chosen = set()

# EVERY shipped Python file, not just the ones under engine/.
#
# This walked engine/ alone. linkedin_browser.py sits at the top level and was
# therefore never parsed - and on 2026-08-26 a rename left a syntax error in it,
# inside a function on the send path. The suite passed: that import is lazy, so
# nothing reads it until a message is actually being carried. It was found by
# sending one in a sandbox, which is far too late.
#
# doctor.py and linkedin_browser.py ship and run, so they are checked too.
SHIPPED = sorted(ENGINE.rglob("*.py")) + sorted(
    p for p in ROOT.glob("*.py") if p.name != "setup.py")

for path in SHIPPED:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        check("%s can be read at all" % path.relative_to(ROOT), False, str(exc))
        continue
    optional = _optional_imports(tree)
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module.split(".")[0]]
        for name in names:
            if name in STDLIB or name in OURS:
                continue
            if name in optional:
                chosen.add(name)
                continue
            outside.setdefault(name, set()).add(str(path.relative_to(ROOT)))

for name in sorted(outside):
    # An underscore and a hyphen are the same package name to pip
    # (faster_whisper / faster-whisper), so they are the same name here too.
    wanted = INSTALLED_AS.get(name, name).lower().replace("_", "-")
    on_list = wanted in {r.replace("_", "-") for r in REQUIRED}
    check("%s is on the list of things to install" % name, on_list,
          "imported by " + ", ".join(sorted(outside[name])[:2]))
for name in sorted(chosen - set(outside)):
    print("  ---   %s is optional: the program falls back without it" % name)

print("\n=== 2. the list has nothing that is only true on one computer ===")

check("no line names a folder on somebody's machine",
      not re.search(r"[A-Za-z]:\\|/Users/|/home/", req_text))
check("no line installs from a local path",
      not re.search(r"(?m)^\s*(-e|--editable|file:)", req_text))

print("\n=== 3. LinkChat's browser door is its own ===")

browser = (ENGINE / "browser.py").read_text(encoding="utf-8")
mine = re.search(r"^PORT\s*=\s*(\d+)", browser, re.M)
check("LinkChat names a browser door", mine is not None)

# The door that is already taken. A constant, on purpose.
#
# This used to look for the other program's folder on the machine and only run
# if it was there - so on a member's computer it did nothing, and after a rename
# its path pointed at a folder that never exists, so it did nothing anywhere. It
# printed "nothing to collide with" and read like a pass. A check that cannot
# fail is worse than no check.
#
# The number is the fact worth keeping. The two doors were the same until
# 2026-08-23, and LinkChat drove the other program's signed-in browser.
PORT_ALREADY_TAKEN = "9333"
check("LinkChat's browser door is not the one already taken",
      mine is not None and mine.group(1) != PORT_ALREADY_TAKEN,
      "LinkChat is on %s, which is the door the other program answers on - it "
      "would drive that browser, signed into a different account"
      % (mine.group(1) if mine else "?"))

# The screens are served on a different number again, and for the same reason.
web = (ENGINE / "__main__.py").read_text(encoding="utf-8")
web_port = re.search(r"^PORT\s*=\s*(\d+)", web, re.M)
check("the screens are served somewhere else again",
      web_port is not None and mine is not None
      and web_port.group(1) != mine.group(1))

print("\n%s" % ("ALL PASS" if not FAILS else "FAILURES: " + ", ".join(FAILS)))
sys.exit(1 if FAILS else 0)
