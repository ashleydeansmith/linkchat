"""The walk — press every door in the program and see what answers.

WHY THIS EXISTS. Reading code does not find the fault where a screen asks the
engine for something the engine no longer has. The engine answers with a page of
Python, the screen catches it and draws nothing, and what you see is an empty
list — which looks exactly like having no messages. Two of those were in here on
2026-08-23 and neither was visible by reading: a microphone list that crashed,
and a list of jobs that answered with somebody else's work.

WHAT IT JUDGES. Not whether an answer is the one you wanted — there is no data on
a fresh machine to want anything from. It judges the SHAPE of the answer:

  crashed        the engine fell over. Always a fault.
  not built      the engine said this part does not exist. Honest, not a fault.
  a Python error the answer names an exception or a file path in the program.
                 Always a fault: nobody can act on "KeyError".
  refused        the engine said no, in words. Not a fault — often the point.
  answered       it worked.

Run it with nothing running:

    python tests/walk.py

It starts the engine on a door of its own, walks it, stops it, and prints one
table. It never sends anything: the two doors that reach a person are walked with
words that cannot pass the checks in front of them, so the walk proves the
refusal rather than the send.
"""

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8799                      # not the door the program normally uses
BASE = "http://127.0.0.1:%d" % PORT

# A sentence a person can act on never contains these.
PYTHON_SMELL = re.compile(
    r"Traceback|Exception|Error'|KeyError|TypeError|ValueError|AttributeError"
    r"|ImportError|ModuleNotFound|NoneType|sqlite3\.|\.py['\"]?, line", re.I)

# Every door, and what pressing it should mean. "reaches" marks the two that can
# put words in front of a person; they are pressed with words that must be
# refused, so the walk can prove the refusal without sending.
# Doors not pressed, each with why. A GET that drives the browser is a GET that
# spends a real LinkedIn action to answer a test.
LEAVE_ALONE = {
    "/api/inbox/export": "already pressed below, with its query",
}

DOORS = [
    ("GET",  "/api/health", None),
    ("GET",  "/api/crm/state", None),
    ("GET",  "/api/crm/people", None),
    ("GET",  "/api/crm/waiting", None),
    ("GET",  "/api/gather/state", None),
    ("GET",  "/api/flows/versions", None),
    ("GET",  "/api/flows/stats", None),
    ("GET",  "/api/flows/reactivate-queue", None),
    ("POST", "/api/flows/classify-preview", {"patterns": ["interested"], "limit": 5}),
    ("GET",  "/api/inbox/status", None),
    ("GET",  "/api/inbox", None),
    ("GET",  "/api/inbox/queue", None),
    ("GET",  "/api/inbox/tags", None),
    ("GET",  "/api/inbox/snippets", None),
    ("GET",  "/api/inbox/review-queue", None),
    ("GET",  "/api/inbox/mics", None),
    ("GET",  "/api/inbox/export?format=csv", None),
    ("POST", "/api/inbox/999/note", {"note": "walked"}),
    ("POST", "/api/inbox/999/snooze", {"until": None}),
    ("POST", "/api/inbox/999/archive", {"archived": True}),
    ("POST", "/api/inbox/999/pin", {"pinned": True}),
    ("GET",  "/api/inbox/999/suggest", None),
    ("POST", "/api/inbox/999/attach", {}),
    ("POST", "/api/inbox/999/voice-send", {}),
    ("GET",  "/api/inbox/audio?u=nothing", None),
    ("POST", "/api/inbox/transcribe", {"u": "nothing"}),
    # The two that reach a person. Both are given words that must not pass.
    ("POST", "/api/crm/reply", {"conv_id": 999, "body": "Hi {first_name}, walking."}),
    ("POST", "/api/crm/approve", {"item_id": "walk-not-real", "to": "Nobody",
                                  "identifier": "https://www.linkedin.com/in/nobody-walk/",
                                  "body": "Hi {first_name}, walking."}),
    # A door that does not exist. The answer has to be a sentence too.
    ("GET",  "/api/there-is-no-such-thing", None),
]


def press(method, path, body):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000).decode("utf-8", "replace")
    except Exception as e:
        return 0, "%s: %s" % (e.__class__.__name__, e)


def judge(status, text):
    """crashed / a Python error / not built / refused / answered.

    501 is its own verdict rather than a crash. It is the engine saying "that is
    not built", which is a true and useful answer; treating it as a fall-over
    would train whoever runs this to skim past four lines every time, and the
    next real fall-over would be skimmed past with them.
    """
    if status == 501:
        return "not built"
    if status == 0 or status >= 500:
        return "crashed"
    said = text
    try:
        parsed = json.loads(text)
        said = parsed.get("detail", parsed) if isinstance(parsed, dict) else parsed
        said = said if isinstance(said, str) else json.dumps(said)
    except ValueError:
        pass
    if PYTHON_SMELL.search(said):
        return "a Python error"
    if status >= 400:
        return "refused"
    return "answered"


def main():
    print("Starting the engine on door %d." % PORT)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "engine", "serve", "--port", str(PORT)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(60):
            code, _body = press("GET", "/api/health", None)
            if code == 200:
                break
            time.sleep(0.5)
        else:
            print("The engine never answered. Nothing else can be judged.")
            return 1

        # A written-down list goes stale the day somebody adds a route. So the
        # engine is asked what doors it has, and any GET nobody wrote down here is
        # pressed too — which means a new screen calling a new door is covered by
        # this walk on the day it is written, not the day somebody remembers.
        doors = list(DOORS)
        try:
            _, listing = press("GET", "/openapi.json", None)
            known = {p for _, p, _ in DOORS}
            for path, methods in sorted(json.loads(listing)["paths"].items()):
                if "get" not in methods or path in known:
                    continue
                url = path
                for token, value in {"conv_id": "999", "vid": "999",
                                     "lead_id": "999", "rest": "no-such-thing"}.items():
                    url = url.replace("{%s}" % token, value)
                if "{" in url:
                    continue
                if url in known or url in LEAVE_ALONE:
                    continue
                doors.append(("GET", url, None))
        except Exception as exc:      # noqa: BLE001
            print("could not ask the engine what doors it has: %s" % exc)

        faults, rows = [], []
        for method, path, body in doors:
            status, text = press(method, path, body)
            verdict = judge(status, text)
            first = " ".join(text.split())[:110]
            rows.append((verdict, method, path, status, first))
            if verdict in ("crashed", "a Python error"):
                faults.append((method, path, verdict, first))

        width = max(len(p) for _, _, p, _, _ in rows)
        print("")
        for verdict, method, path, status, first in rows:
            print("  %-15s %-4s %-*s %3s  %s"
                  % (verdict, method, width, path, status, first))
        print("")
        if faults:
            print("%d door%s answered with something nobody can act on:"
                  % (len(faults), "" if len(faults) == 1 else "s"))
            for method, path, verdict, first in faults:
                print("  %s %s  ->  %s: %s" % (method, path, verdict, first))
            return 1
        print("Every door answered with something a person can read.")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
