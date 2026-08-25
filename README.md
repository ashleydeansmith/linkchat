# LinkChat

Your LinkedIn conversations, and the sequences that run off them, on your own computer.

**Start here: [`guide/LinkChat — Install and Use.md`](guide/LinkChat%20%E2%80%94%20Install%20and%20Use.md)**

That guide is written to be followed start to finish with nothing assumed. Everything
below is the short version for somebody who has already read it.

---

## Install, in four lines

    cd %USERPROFILE%\Documents
    git clone https://github.com/ashleydeansmith/linkchat.git LinkChat

Then double-click `setup.cmd` in the folder it made, and double-click the LinkChat icon
it puts on your desktop.

Needs Python 3.10 or later from python.org, with **"Add python.exe to PATH"** ticked on
the installer's first screen.

**Not sure which computer you are on, or how far it got?** Run `python doctor.py`
(`python3 doctor.py` on a Mac) in this folder. It works it out and says what to do next.
It only looks — it installs nothing and sends nothing.

**On a Mac** you run `setup-mac.command` instead, and Python comes from
https://www.python.org/downloads/macos/. **Nobody has ever run LinkChat on a Mac** — do
the install the day before, with Ashley on a message, rather than live on the call. The
Mac paragraph in the guide says what differs and what to do when it stops.

---

## What it does, and what it deliberately does not

LinkChat keeps **no record about any person**. Your people, your event log, your daily
limit and your do-not-message list all stay in the CRM you built in Sessions 0 to 2.
One file, `engine/crm_bridge.py`, is the only door between the two, so there is one
place to look rather than a dozen.

**Approving a message sends it.** There is no draft folder that quietly does nothing.

Every message that reaches a person — one a sequence wrote, or one you typed yourself —
goes through the same function, `_carry` in `engine/server.py`, and faces the same five
checks. There is deliberately no second road out: the moment there are two, one of them
stops being maintained, and it is the unmaintained one that reaches somebody.

Connecting, withdrawing invitations and scraping are **out of scope**. The Session 2
Gather kit already does those. A second copy would store results your CRM cannot see.

## If you have not reached Layer 6

The checks are not LinkChat's rules — they are the parts installed in Layer 6 of your own
CRM. Without them the part that would grant permission is absent, and an absent guard
must never read as an open door. So LinkChat opens, reads and shows you everything, and
cannot send. Reaching Layer 6 turns sending on with nothing to reinstall.

To see what your copy can do:

    python -m engine crm

---

## Running it from a terminal

    python -m engine desktop     open LinkChat as a window (what the icon runs)
    python -m engine serve       the engine only, no window
    python -m engine crm         what LinkChat can see of your CRM, then stop
    python -m engine inbox-sync  read your LinkedIn inbox and store what is there

## Checking it still works

    python tests/run_all.py

Four checks: it installs on a computer that is not the one it was built on · nobody's
name is typed back at them · nothing reaches a person unapproved · and the walk, which
starts the engine, presses every door and judges the shape of the answer.

---

## If you use Claude Code

Open it in this folder and say **"run doctor.py and fix what it finds"**. `CLAUDE.md` here
loads on its own and tells Claude what it may change and what it must never touch — the
checks that stop a message reaching the wrong person, your CRM records, and the tests.
You have read access to this repository and not write access, so a fix stays on your
machine: send Ashley the patch.
