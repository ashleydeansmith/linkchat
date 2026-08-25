"""ship_check.py — is LinkChat actually safe to hand to nine people?

Every other test in this folder checks the program against what it was meant to
do. This one checks the DELIVERY, and it does it by introducing somebody who is
not the person who built it: it clones the address the members will clone, and
runs the program from that clone rather than from this folder.

That distinction is the whole point. A program checked in the folder it was
written in passes because the machine it was written on already has everything.
Every fault worth catching here is a fault about the machine, not the logic:

  - a part that was never on the list of parts to install
  - a file that names the folder it was copied from
  - a screen that was never rebuilt after its words were fixed
  - work that was committed and never pushed, so the clone gets yesterday
  - a setting file belonging to the author that shipped with the program

Three verdicts, on purpose:

  SAFE TO SEND   every check ran and every check passed
  DO NOT SEND    a check ran and failed
  NOT PROVEN     a check could not run at all

NOT PROVEN exists so a half-finished run can never read as a pass. A check that
quietly skips is worse than one that fails, because nobody goes looking.

Run:  python tests/ship_check.py
      python tests/ship_check.py --quick     (skips the clone and the live start)
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
UNRUN: list[tuple[str, str]] = []


def ok(label: str) -> None:
    PASSED.append(label)
    print("  PASS      " + label)


def bad(label: str, why: str) -> None:
    FAILED.append((label, why))
    print("  FAIL      " + label)
    print("            " + why)


def unrun(label: str, why: str) -> None:
    UNRUN.append((label, why))
    print("  NOT RUN   " + label)
    print("            " + why)


def run(args, cwd=None, timeout=600):
    """Run something and hand back what it said. No console window, ever."""
    return subprocess.run(args, cwd=str(cwd or ROOT), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW)


def git(*args, cwd=None):
    return run(["git"] + list(args), cwd=cwd)


# ===========================================================================
print("=" * 72)
print("  1. Is what a member would clone the same as what is on this computer?")
print("=" * 72)

r = git("status", "--porcelain")
if r.returncode != 0:
    unrun("nothing is left uncommitted", "git would not answer: " + r.stderr.strip())
else:
    dirty = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if dirty:
        bad("nothing is left uncommitted",
            "%d file(s) changed and not committed, so the clone gets the old "
            "version: %s" % (len(dirty), ", ".join(d[3:] for d in dirty[:4])))
    else:
        ok("nothing is left uncommitted")

r = git("remote", "get-url", "origin")
ORIGIN = r.stdout.strip() if r.returncode == 0 else ""
if not ORIGIN:
    bad("there is somewhere for members to clone from",
        "no origin remote. Nobody can get this program.")
else:
    ok("there is somewhere for members to clone from (%s)" % ORIGIN)

if ORIGIN:
    git("fetch", "--quiet", "origin")
    here = git("rev-parse", "HEAD").stdout.strip()
    there = git("rev-parse", "@{u}").stdout.strip()
    if not there:
        unrun("what is pushed matches what is here",
              "this branch is not tracking anything on the remote")
    elif here != there:
        ahead = git("rev-list", "--count", "@{u}..HEAD").stdout.strip()
        bad("what is pushed matches what is here",
            "%s commit(s) exist here that were never pushed. A member cloning "
            "now gets the version before them." % ahead)
    else:
        ok("what is pushed matches what is here")


# ===========================================================================
print()
print("=" * 72)
print("  2. Does anything belonging to the author ship with it?")
print("=" * 72)

r = git("ls-files")
TRACKED = [ln for ln in r.stdout.splitlines() if ln.strip()]
if not TRACKED:
    unrun("the shipped files could be listed", "git ls-files said nothing")
else:
    ok("the shipped files could be listed (%d of them)" % len(TRACKED))

TEXT = {".py", ".cmd", ".md", ".txt", ".json", ".js", ".jsx", ".css", ".html", ".plist"}
readable = [p for p in TRACKED if Path(p).suffix.lower() in TEXT
            and "package-lock" not in p and "/dist/" not in p.replace("\\", "/")]

home_hits = []
for rel in readable:
    try:
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for m in re.finditer(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9._-]+|/(?:home|Users)/[A-Za-z0-9._-]+",
                         text):
        # A placeholder telling a member what THEIR path looks like is fine.
        window = text[max(0, m.start() - 60):m.end() + 20].lower()
        if any(w in window for w in ("yourname", "you\\", "you/", "placeholder",
                                     "for example", "%userprofile%", "e.g.")):
            continue
        home_hits.append("%s: %s" % (rel, m.group(0)))
if home_hits:
    bad("no shipped file names a folder on somebody's computer",
        "%d place(s): %s" % (len(home_hits), "; ".join(home_hits[:3])))
else:
    ok("no shipped file names a folder on somebody's computer")

SETTINGS_THAT_MUST_NOT_SHIP = ["linkchat.json"]
leaked = [f for f in SETTINGS_THAT_MUST_NOT_SHIP if f in TRACKED]
if leaked:
    bad("the author's own settings are not in the shipped files",
        "these would go to every member: " + ", ".join(leaked))
else:
    ok("the author's own settings are not in the shipped files")

r = git("log", "--all", "--pretty=format:", "--name-only", "--diff-filter=A")
ever = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
sensitive = sorted(f for f in ever
                   if f in SETTINGS_THAT_MUST_NOT_SHIP
                   or f.endswith((".db", ".log"))
                   or f.startswith(("_state/", "_retired/")))
if sensitive:
    bad("nothing sensitive is anywhere in the history",
        "a clone carries all of history: " + ", ".join(sensitive[:5]))
else:
    ok("nothing sensitive is anywhere in the history")


# ===========================================================================
print()
print("=" * 72)
print("  3. Are the screens a member gets the current ones?")
print("=" * 72)

dist_index = ROOT / "web" / "dist" / "index.html"
if not dist_index.exists():
    bad("the built screens ship with the program",
        "web/dist is missing. A member would be told to install Node and build "
        "the screens themselves, which is not a thing to ask of them.")
else:
    ok("the built screens ship with the program")
    built_at = dist_index.stat().st_mtime
    newer = []
    for p in list((ROOT / "web" / "src").rglob("*")) + [ROOT / "web" / "index.html"]:
        if p.is_file() and p.stat().st_mtime > built_at:
            newer.append(str(p.relative_to(ROOT)))
    if newer:
        bad("the built screens are newer than the words in them",
            "%d source file(s) changed after the last build, so a fix you made "
            "is not in what ships: %s" % (len(newer), ", ".join(newer[:3])))
    else:
        ok("the built screens are newer than the words in them")

    # Every built file the page asks for must actually be there.
    page = dist_index.read_text(encoding="utf-8", errors="replace")
    missing = [a for a in re.findall(r'(?:src|href)="/([^"]+)"', page)
               if not (ROOT / "web" / "dist" / a).exists()]
    if missing:
        bad("every file the page asks for is present",
            "the page asks for these and they are not there: " + ", ".join(missing[:4]))
    else:
        ok("every file the page asks for is present")


# ===========================================================================
print()
print("=" * 72)
print("  4. Is there a guide, and does it describe this program?")
print("=" * 72)

guides = list((ROOT / "guide").glob("*.md"))
if not guides:
    bad("there is a guide", "the guide folder is empty. Nine people install "
                            "this tomorrow with nothing to follow.")
else:
    ok("there is a guide (%s)" % guides[0].name)
    text = guides[0].read_text(encoding="utf-8", errors="replace")

    # Every command the guide tells a member to type must be a real command.
    real = set(re.findall(r'command == "([a-z-]+)"',
                          (ROOT / "engine" / "__main__.py").read_text(encoding="utf-8")))
    told = set(re.findall(r"python -m engine ([a-z-]+)", text))
    unreal = sorted(told - real)
    if unreal:
        bad("every command the guide names is a real command",
            "the guide tells a member to run: " + ", ".join(unreal))
    else:
        ok("every command the guide names is a real command (%s)"
           % ", ".join(sorted(told)))

    # The one fact nobody should meet by accident.
    if re.search(r"approv\w+ (a message )?sends? it", text, re.I):
        ok("the guide says plainly that approving sends")
    else:
        bad("the guide says plainly that approving sends",
            "a member could press approve believing it only saves a draft")

    if re.search(r"\bmac\b", text, re.I):
        ok("the guide says what a Mac member should do")
    else:
        bad("the guide says what a Mac member should do",
            "a Mac member will follow Windows steps until something breaks")

    if (ROOT / "README.md").exists():
        ok("there is a README pointing at the guide")
    else:
        bad("there is a README pointing at the guide",
            "the first page a member sees on the repository is blank")


# ===========================================================================
print()
print("=" * 72)
print("  5. Words a member should never be shown")
print("=" * 72)

BANNED = re.compile(r"\b(things?|bites?|biting)\b", re.I)
member_visible = [ROOT / "setup.cmd", ROOT / "setup-mac.command", ROOT / "README.md"] + guides \
    + sorted((ROOT / "web" / "src").rglob("*.jsx"))
hits = []
for p in member_visible:
    if not p.exists():
        continue
    body = p.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(body.splitlines(), 1):
        # Only what a person reads on screen, not what a programmer reads.
        if p.suffix == ".jsx" and not re.search(r">[^<>{]*\b(things?|bites?)\b", line, re.I):
            continue
        if p.suffix in (".cmd", ".command", ".py") and line.strip().startswith(("rem", "#")):
            continue
        if BANNED.search(line):
            hits.append("%s:%d %s" % (p.relative_to(ROOT), i, line.strip()[:60]))
if hits:
    bad("no banned word appears where a member can read it",
        "%d place(s): %s" % (len(hits), " | ".join(hits[:3])))
else:
    ok("no banned word appears where a member can read it")


# ===========================================================================
print()
print("=" * 72)
print("  6. Somebody who is not the author: clone it, and run it")
print("=" * 72)

if "--quick" in sys.argv:
    unrun("a fresh clone passes its own tests", "--quick was passed")
    unrun("the program starts from a fresh clone and serves its screens",
          "--quick was passed")
elif not ORIGIN:
    unrun("a fresh clone passes its own tests", "there is no address to clone")
    unrun("the program starts from a fresh clone and serves its screens",
          "there is no address to clone")
else:
    tmp = Path(tempfile.mkdtemp(prefix="linkchat-shipcheck-"))
    clone = tmp / "LinkChat"
    try:
        r = run(["git", "clone", "--quiet", ORIGIN, str(clone)], cwd=tmp, timeout=600)
        if r.returncode != 0:
            bad("a member can clone the address they are given",
                "git clone failed: " + (r.stderr.strip()[:200] or "no reason given"))
            unrun("a fresh clone passes its own tests", "the clone did not happen")
            unrun("the program starts from a fresh clone and serves its screens",
                  "the clone did not happen")
        else:
            ok("a member can clone the address they are given")

            # --- What a Mac member actually receives -----------------------
            # Every one of these is about the journey out of git, not about the
            # file sitting here. A shell file is refused by a Mac on its first
            # line if git rewrote its line endings on the way out, and the error
            # it gives names nothing the member can act on.
            mac_setup = clone / "setup-mac.command"
            if not mac_setup.exists():
                bad("a Mac member gets an installer they can run",
                    "setup-mac.command is not in the clone at all")
            else:
                raw = mac_setup.read_bytes()
                if b"\r\n" in raw:
                    bad("a Mac member gets an installer they can run",
                        "the clone's setup-mac.command has Windows line endings - a Mac "
                        "refuses it on line one, and the message it gives explains "
                        "nothing the member can act on")
                elif not raw.startswith(b"#!/bin/bash"):
                    bad("a Mac member gets an installer they can run",
                        "setup-mac.command does not start with a line saying what runs it")
                else:
                    ok("a Mac member gets an installer they can run")

                r = run(["git", "ls-files", "-s", "setup-mac.command"], cwd=clone)
                mode = (r.stdout or "").split(" ", 1)[0].strip()
                if mode == "100755":
                    ok("git records the Mac installer as runnable")
                elif mode:
                    bad("git records the Mac installer as runnable",
                        "git records it as %s, so a Mac refuses to run it until the "
                        "member types a command nobody has written down for them"
                        % mode)
                else:
                    unrun("git records the Mac installer as runnable",
                          "git did not report a mode for it")

                bash = shutil.which("bash")
                if not bash:
                    unrun("the Mac installer is a valid shell file",
                          "there is no bash on this computer to check it with")
                else:
                    r = run([bash, "-n", str(mac_setup)], cwd=clone, timeout=60)
                    if r.returncode == 0:
                        ok("the Mac installer is a valid shell file")
                    else:
                        bad("the Mac installer is a valid shell file",
                            (r.stderr or r.stdout).strip()[:200] or "bash refused it")

                # --- Does the clone tell the member which computer they are on?
                doc = clone / "doctor.py"
                if not doc.exists():
                    bad("the clone can work out which computer it is on",
                        "doctor.py is not in the clone")
                else:
                    r = run([sys.executable, "doctor.py"], cwd=clone, timeout=300)
                    out = (r.stdout or "") + (r.stderr or "")
                    if r.returncode != 0:
                        bad("the clone can work out which computer it is on",
                            "doctor.py stopped: " + out.strip()[-200:])
                    elif "setup.cmd" not in out or "setup-mac.command" not in out:
                        bad("the clone can work out which computer it is on",
                            "doctor.py ran but never named which installer to use")
                    else:
                        ok("the clone can work out which computer it is on")

                # --- Are a member's own Claude instructions there, and safe? ---
                # This is the check that matters most of the five here. That file
                # is read by an agent with edit access on the member's machine,
                # and the one change it must never make is the one that would let
                # a message past a check.
                cmd_md = clone / "CLAUDE.md"
                if not cmd_md.exists():
                    bad("a member's Claude is told what it must never touch",
                        "CLAUDE.md is not in the clone")
                else:
                    txt = cmd_md.read_text(encoding="utf-8", errors="replace").lower()
                    needed = {
                        "it forbids weakening the checks": "never weaken",
                        "it names the send gate file": "crm_bridge.py",
                        "it forbids sending while diagnosing": "never send a message",
                        "it forbids editing the tests": "do not edit the test",
                        "it says a Mac is unproven": "nobody has ever run linkchat",
                    }
                    absent = [k for k, v in needed.items() if v not in txt]
                    if absent:
                        bad("a member's Claude is told what it must never touch",
                            "CLAUDE.md no longer says: " + "; ".join(absent))
                    else:
                        ok("a member's Claude is told what it must never touch")

                guide_txt = ""
                for g in (clone / "guide").glob("*.md"):
                    guide_txt += g.read_text(encoding="utf-8", errors="replace")
                if "setup-mac.command" in guide_txt:
                    ok("the guide tells a Mac member which file to run")
                else:
                    bad("the guide tells a Mac member which file to run",
                        "no guide in the clone names setup-mac.command, so the installer "
                        "exists and nobody is told about it")

            r = run([sys.executable, "tests/run_all.py"], cwd=clone, timeout=900)
            if r.returncode == 0 and "Clean." in r.stdout:
                ok("a fresh clone passes its own tests")
            else:
                tail = (r.stdout or r.stderr).strip().splitlines()[-6:]
                bad("a fresh clone passes its own tests", " / ".join(tail))

            # Actually start it, on a door nothing else is using, and ask it
            # for the page a member's window would ask for.
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            proc = subprocess.Popen(
                [sys.executable, "-m", "engine", "serve", "--port", str(port)],
                cwd=str(clone), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=NO_WINDOW)
            try:
                page = None
                for _ in range(60):
                    try:
                        with urllib.request.urlopen(
                                "http://127.0.0.1:%d/" % port, timeout=1) as resp:
                            page = resp.read().decode("utf-8", "replace")
                            break
                    except Exception:
                        time.sleep(0.5)
                if page is None:
                    bad("the program starts from a fresh clone and serves its screens",
                        "it never answered on its own door within 30 seconds")
                else:
                    asset = re.search(r'src="/(assets/[^"]+\.js)"', page)
                    if not asset:
                        bad("the program starts from a fresh clone and serves its screens",
                            "it answered, but the page asks for no screens at all")
                    else:
                        with urllib.request.urlopen(
                                "http://127.0.0.1:%d/%s" % (port, asset.group(1)),
                                timeout=10) as resp:
                            size = len(resp.read())
                        if size > 10_000:
                            ok("the program starts from a fresh clone and serves its "
                               "screens (%d kB)" % (size // 1024))
                        else:
                            bad("the program starts from a fresh clone and serves its screens",
                                "the screens came back only %d bytes" % size)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
print()
print("=" * 72)
if FAILED:
    verdict = "DO NOT SEND"
elif UNRUN:
    verdict = "NOT PROVEN"
else:
    verdict = "SAFE TO SEND"

print("  %d passed, %d failed, %d could not run" % (len(PASSED), len(FAILED), len(UNRUN)))
print()
print("  VERDICT: " + verdict)
if FAILED:
    print()
    print("  Fix these before anybody clones it:")
    for label, why in FAILED:
        print("    - %s" % label)
        print("      %s" % why)
if UNRUN:
    print()
    print("  These did not run, so nothing here is proven:")
    for label, why in UNRUN:
        print("    - %s (%s)" % (label, why))
print("=" * 72)

sys.exit(0 if verdict == "SAFE TO SEND" else 1)
