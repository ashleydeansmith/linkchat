"""gather.py — running the jobs you already have, from inside LinkChat.

You built these in Session 2 and they live in your CRM, in `_engine/gather.py`.
LinkChat does NOT have its own copy of them and never will. It runs yours.

That matters more than it sounds. A second copy would keep its own records, count
against its own ceiling and ask its own permission - so you would have two
programs each politely staying inside a limit, on one LinkedIn account, going
over it together. Running yours means one set of records, one ceiling, one
answer about what happened today.

The four jobs:

    find      goes out and brings people back, four ways
    ask       asks somebody to connect
    undo      takes back requests nobody answered
    accepted  works out who said yes, and records it

Every one of them runs in one of two modes. `probe` does everything except the
outward action and tells you what it would have done. `commit` does it. Nothing
here picks commit for you.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# A job started from a window must not make a black console box appear, so every
# spawn carries this. Without it Windows gives the child its own console and it
# flashes up on screen - and redirecting the output does not stop that, because
# the console is created before anything is written to it.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

JOBS = {
    "find":     "Go out and bring people back",
    "ask":      "Ask people to connect",
    "undo":     "Take back requests nobody answered",
    "accepted": "Work out who said yes",
}

SOURCES = {
    "export":      "A spreadsheet already on your computer",
    "connections": "The people you are already connected to",
    "search":      "One search, read deep",
    "reactions":   "Everyone who reacted to one post",
}


def script(root):
    """Your own gather, inside your CRM. None of this works without it."""
    return Path(root) / "_engine" / "gather.py"


def installed(root):
    return script(root).is_file()


def state(root):
    """What LinkChat can tell you about Gather without running anything."""
    path = script(root)
    if not path.is_file():
        return {
            "installed": False,
            "why": "Session 2 of your CRM installs these four jobs. Until that is "
                   "done there is nothing here for LinkChat to run.",
            "jobs": {}, "sources": {},
        }
    return {"installed": True, "why": "", "jobs": JOBS, "sources": SOURCES,
            "script": str(path)}


def run(root, job, mode="probe", source=None, term=None, limit=None, timeout=900):
    """Run one of your jobs and hand back what it said.

    mode is 'probe' or 'commit', and nothing here defaults to commit.
    """
    root = Path(root)
    path = script(root)
    if not path.is_file():
        return {"ok": False, "out": "", "err":
                "Gather is not installed in this CRM yet.", "cmd": ""}
    if job not in JOBS:
        return {"ok": False, "out": "", "err": "no such job: %s" % job, "cmd": ""}
    if mode not in ("probe", "commit"):
        return {"ok": False, "out": "", "err": "mode must be probe or commit", "cmd": ""}

    argv = [sys.executable, str(path), job]
    if job == "find":
        argv.append(source or "connections")
        if term:
            argv.append(str(term))
    if limit:
        argv += ["--max", str(int(limit))]
    argv.append("--" + mode)

    try:
        p = subprocess.run(argv, cwd=str(root / "_engine"), capture_output=True,
                           text=True, timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "", "cmd": " ".join(argv[1:]),
                "err": "that job was still going after %d minutes, so LinkChat "
                       "stopped waiting. It may still have done some of its work - "
                       "look at your records before running it again." % (timeout // 60)}
    except Exception as exc:
        return {"ok": False, "out": "", "err": str(exc), "cmd": " ".join(argv[1:])}

    return {"ok": p.returncode == 0, "code": p.returncode,
            "out": (p.stdout or "")[-8000:], "err": (p.stderr or "")[-4000:],
            "cmd": " ".join(argv[1:]), "mode": mode}
