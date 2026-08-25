"""Ask this computer what it is, and what LinkChat still needs from it.

    python doctor.py          (Windows)
    python3 doctor.py         (Mac)

Safe to run at any point, including before anything is installed and including
when it is broken. It only looks. It never installs, never changes a file,
never opens a browser and never sends a message to anybody.

WHY THIS EXISTS
---------------
Everything else in this folder assumes you already know which computer you are
on and how far through the install you got. Both of those are exactly what
somebody does not know when it has gone wrong. So this file works them out and
says one sentence about what to do next.

It uses nothing but what comes with Python, so it still runs on a machine where
the install failed, where it half-finished, or where it never started.
"""

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

NEEDED = ["fastapi", "uvicorn", "pydantic", "playwright", "psutil"]

next_steps = []


def say(text=""):
    print(text)


def head(text):
    say()
    say("=" * 66)
    say("  " + text)
    say("=" * 66)


def item(label, value, good=None):
    mark = "  " if good is None else ("ok" if good else "->")
    say("  %-2s %-32s %s" % (mark, label, value))


# ---------------------------------------------------------------------------
# 1. Which computer is this?
# ---------------------------------------------------------------------------
def which_computer():
    head("1. Which computer is this?")
    if IS_MAC:
        try:
            ver = platform.mac_ver()[0] or "unknown version"
        except Exception:
            ver = "unknown version"
        item("This is", "a Mac")
        item("", "macOS %s, %s chip" % (ver, platform.machine()))
        installer = "setup-mac.command"
    elif IS_WIN:
        item("This is", "a Windows computer")
        item("", "%s %s" % (platform.system(), platform.release()))
        installer = "setup.cmd"
    else:
        item("This is", "neither Windows nor a Mac")
        item("", platform.platform())
        installer = "setup-mac.command"

    say()
    say("  The installer for this computer is:  %s" % installer)

    if (HERE / installer).exists():
        item("that file is here", "yes", True)
    else:
        item("that file is here", "NO - it is missing from this folder", False)
        next_steps.append(
            "%s is missing. Either the clone did not finish or this is being run "
            "from the wrong folder. Clone it again." % installer)

    wrong = "setup.cmd" if not IS_WIN else "setup-mac.command"
    say()
    say("  Ignore %s. That is the one for the other kind of computer." % wrong)

    if not IS_WIN and not IS_MAC:
        say()
        say("  ! LinkChat has been run on Windows only. The Mac installer may work")
        say("    here, but nobody has tried it. Message Ashley first.")
    return installer


# ---------------------------------------------------------------------------
# 2. Is there a Python this can use?
# ---------------------------------------------------------------------------
def which_python():
    head("2. The Python you just ran this with")
    v = sys.version_info
    item("Version", "%d.%d.%d" % (v[0], v[1], v[2]), v >= (3, 10))
    item("Lives at", sys.executable)

    if v < (3, 10):
        next_steps.append(
            "This Python is %d.%d and LinkChat needs 3.10 or later. Install a "
            "newer one from python.org, then run this again." % (v[0], v[1]))

    # The two impostors, one per kind of computer.
    exe = (sys.executable or "").lower()
    if IS_WIN and "windowsapps" in exe:
        item("Warning", "this is the Microsoft Store stub, not Python", False)
        next_steps.append(
            "The Python you ran is the Microsoft Store stub in WindowsApps, which "
            "is not Python. Install the real one from python.org with 'Add "
            "python.exe to PATH' ticked on the first screen.")
    if IS_MAC and exe.startswith("/usr/bin/python"):
        item("Note", "this is the Python that came with the Mac", None)
        say("     That one refuses to have parts installed into it. The Mac")
        say("     installer works around it by building LinkChat a private one.")
    return v >= (3, 10)


# ---------------------------------------------------------------------------
# 3. Which Python does LinkChat itself use, and are its parts installed?
# ---------------------------------------------------------------------------
def linkchat_python():
    """On a Mac the installer builds a private Python at .venv. Ask THAT one,
    because the parts went in there and not into the one you typed."""
    sub = "Scripts" if IS_WIN else "bin"
    exe = "python.exe" if IS_WIN else "python"
    venv = HERE / ".venv" / sub / exe
    if venv.exists():
        return venv, True
    return Path(sys.executable), False


def which_parts():
    head("3. Are the parts LinkChat needs installed?")
    py, private = linkchat_python()
    if private:
        item("LinkChat has its own Python", "yes, at .venv", True)
        say("     These answers are about that one, not the one you typed.")
    else:
        item("LinkChat has its own Python", "no - it uses the one you typed", None)

    missing = []
    for mod in NEEDED:
        try:
            r = subprocess.run([str(py), "-c", "import " + mod],
                               capture_output=True, timeout=60,
                               creationflags=NO_WINDOW)
            ok = r.returncode == 0
        except Exception:
            ok = False
        item(mod, "installed" if ok else "MISSING", ok)
        if not ok:
            missing.append(mod)

    # The window is genuinely optional. Without it LinkChat opens in the
    # ordinary browser instead, and everything still works.
    try:
        r = subprocess.run([str(py), "-c", "import webview"],
                           capture_output=True, timeout=60,
                           creationflags=NO_WINDOW)
        win_ok = r.returncode == 0
    except Exception:
        win_ok = False
    item("webview (its own window)",
         "installed" if win_ok else "missing - it will use your browser instead",
         None)

    if missing:
        next_steps.append(
            "These parts are missing: %s. Run the installer again."
            % ", ".join(missing))
    return not missing


# ---------------------------------------------------------------------------
# 4. Is the browser it reads with downloaded?
# ---------------------------------------------------------------------------
def which_browser():
    head("4. The browser LinkChat reads LinkedIn with")
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA")
        cache = (Path(base) / "ms-playwright") if base else None
    elif IS_MAC:
        cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        cache = Path(base) / "ms-playwright"

    if cache and cache.exists():
        chromium = sorted(cache.glob("chromium-*"))
        if chromium:
            item("Downloaded", "yes (%s)" % chromium[-1].name, True)
            return True
        item("Downloaded", "the folder is there but no browser in it", False)
    else:
        item("Downloaded", "no", False)
    next_steps.append(
        "The browser is not downloaded. Run the installer again - that step is "
        "about 150 MB and it is the slow one.")
    return False


# ---------------------------------------------------------------------------
# 5. Has it been pointed at a CRM yet?
# ---------------------------------------------------------------------------
def which_crm():
    head("5. Has LinkChat been pointed at your CRM?")
    cfg = HERE / "linkchat.json"
    if not cfg.exists():
        item("Pointed at a CRM", "not yet - it asks the first time you open it",
             None)
        return False
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception as exc:
        item("Pointed at a CRM", "the settings file cannot be read (%s)" % exc,
             False)
        next_steps.append(
            "linkchat.json cannot be read. Delete it and open LinkChat again - "
            "it will ask you for the folder afresh.")
        return False

    root = data.get("crm_root") or data.get("crm") or ""
    if not root:
        item("Pointed at a CRM", "the settings file names no folder", False)
        return False
    p = Path(root)
    item("CRM folder", str(p))
    there = p.exists()
    item("That folder is there", "yes" if there else "NO", there)
    if not there:
        next_steps.append(
            "LinkChat is pointed at %s and that folder is not there. Open "
            "LinkChat and point it at your CRM again." % p)
        return False
    for part in ("_engine", "People"):
        sub = p / part
        item("  contains " + part, "yes" if sub.exists() else "no", sub.exists())
    return True


# ---------------------------------------------------------------------------
def main():
    say()
    say("  LinkChat - what this computer is, and what is still needed")
    say("  Looking only. Nothing is installed, changed or sent.")

    installer = which_computer()
    py_ok = which_python()
    parts_ok = which_parts()
    browser_ok = which_browser()
    which_crm()

    head("What to do next")
    if next_steps:
        for i, step in enumerate(next_steps, 1):
            say("  %d. %s" % (i, step))
        say()
        say("  The installer for this computer is:  %s" % installer)
        if IS_MAC:
            say()
            say("  LinkChat has never been run on a Mac before, so if the installer")
            say("  stops that is worth reporting rather than working around. Open")
            say("  Claude Code in this folder and say:")
            say()
            say("      run doctor.py and fix what it finds")
            say()
            say("  CLAUDE.md in this folder tells Claude what it may change and")
            say("  what it must never touch. Send Ashley what Claude changed.")
    elif py_ok and parts_ok and browser_ok:
        say("  Nothing. LinkChat is installed on this computer.")
        say()
        say("  Open it with the LinkChat icon on your desktop, or type:")
        say("      python -m engine desktop")
    else:
        say("  Run %s and then run this again." % installer)
    say()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
