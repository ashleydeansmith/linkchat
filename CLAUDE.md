# LinkChat — instructions for Claude

You are working inside a copy of **LinkChat** on somebody's own computer. This file loads on its own when they open Claude Code in this folder.

**Read this before you change anything.** LinkChat sends real messages to real people on
LinkedIn under this person's name. Most of what follows is about what you must not do.

---

## First, always

Run this and read it before forming any opinion:

    python doctor.py        (Windows)
    python3 doctor.py       (Mac)

It says which computer this is, which installer belongs to it, how far the install got,
and what to do next. It only looks — it installs nothing and changes nothing. Its
**What to do next** section is the work. Do that, then run it again.

If the person has not said what is wrong, the doctor's output IS the answer. Do not go
reading the codebase first.

---

## The line you must not cross

LinkChat has one road out for every message, and on that road are five checks:

1. their CRM has the parts that do the checking
2. the person is not on their do-not-message list
3. nothing in the words was left unfilled — no `Hi {first_name}`
4. their own LinkedIn name field is not typed back at them
5. it is not near enough to the last message to be a copy

A sequence's message faces a sixth: it cannot approve its own work.

**⛔ You must never weaken, widen, bypass, disable or "temporarily" skip any of these, and
you must never edit `engine/crm_bridge.py`.** Not to get a test passing. Not to unblock
somebody. Not because a check looks over-cautious.

**A check that refuses is not a fault. It is the check working.** If something is refused
and the person thinks it should not be, the answer is to change the message or the record
— never the check. The failure this rule exists to stop is the quiet one: a check widened
to let one message through then lets everything through, and nobody finds out until a
message reaches somebody it should not have.

If a fix seems to require touching a check: **stop, change nothing, and tell them to send
what you found upstream.** That is the correct outcome, not a failure to solve it.

### Also never

- **Never send a message, or approve one, while diagnosing.** Approving sends. There is
  no draft folder that quietly does nothing. If you need to prove the send path works,
  use the tests — they press it with words that must be refused, so what is proven is the
  refusal.
- **Never change anything inside their CRM folder.** That folder is their own records
  from Sessions 0 to 2: people, the event log, the daily limit, the do-not-message list.
  LinkChat reads and writes it through `engine/crm_bridge.py`, and neither you nor
  LinkChat owns it. Never delete a file in it.
- **Never put an absolute path from this machine into a shipped file.** This code was cut
  out of another program and shipped with somebody else's folder names hard-coded three
  times. There is a test that refuses any absolute path under a home folder — do not work
  around it.
- **Never commit or push.** This person has read-only access to the repository, so a push
  will fail anyway. See *Getting a fix back upstream* below.

---

## Which computer, and what that means

The doctor prints this, but so you know what it is deciding between:

| | Windows | Mac |
|---|---|---|
| Installer | `setup.cmd` (double-click) | `setup-mac.command` (double-click) |
| Python comes from | python.org, **tick "Add python.exe to PATH"** | python.org/downloads/macos |
| Where the parts go | the Python that is on PATH | a private Python at `.venv` in this folder |
| Proven? | yes | **no — see below** |

**On Windows this has been installed and run.** If it fails on Windows, something is
unusual about that machine — read the doctor and follow it, and do not start rewriting.

**On a Mac, nobody has ever run LinkChat.** It was made installable on 2026-08-25 and has
never been run on the computer it is for. So on a Mac you are the first, and a fault you
hit is probably real rather than local to this person.

---

## What is fair to change on a Mac

The engine is already written for both — `engine/platform_compat.py` is the seam, and
`engine/browser.py` and `linkedin_browser.py` already have Mac branches. So a Mac fault is
much more likely to be in the **way in** than in the program.

Look here first, in this order:

1. **`setup-mac.command`** — the installer. Most likely place for a fault.
2. **`doctor.py`** — if it says something untrue about this Mac, that is a fault worth
   fixing on its own, because the next person reads it too.
3. **`engine/platform_compat.py`** — the one place that knows about the differences
   between kinds of computer. A Mac fix usually belongs here rather than scattered.
4. **`requirements.txt`** — but note the window parts for a Mac (`pywebview[cocoa]`) are
   installed by `setup-mac.command`, not listed here, because a Windows machine has no
   use for them.

**Windows behaviour must not change.** When you add a Mac path, add a branch — do not
re-route the Windows one through your new code, however much tidier that would be. The
Windows path is the proven one and it is a day from being used by nine people.

### Mac faults that are expected, and are not faults

- **No LinkChat window, and it opens in the ordinary browser instead.** That is the
  designed fallback for when the window parts are missing. Everything works. Not urgent.
- **"cannot be opened because it is from an unidentified developer"** on the desktop file.
  Right-click it, choose Open, then Open again. Once only.
- **The LinkedIn sign-in window.** On a Mac the browser window is shown and hidden through
  the browser's own remote control rather than through macOS, in
  `browser._mac_window_state`. It is written and it has never been run. If the sign-in
  window never appears, that is the first place to look — and it is a real finding worth
  sending upstream.

---

## After any change

Run the tests. All of them, not the one you think you touched:

    python tests/run_all.py

Five checks: it installs on a clean machine · nobody's LinkedIn name is typed back ·
nothing reaches a person unapproved · the starter sequence carries no words of its own ·
and the walk, which starts the engine and presses every door.

**If a test fails after your change, your change is wrong.** Do not edit the test to
match. The tests are what stands between a member and a message they did not mean to
send.

---

## Getting a fix back upstream

This person can read the repository, not write to it. So a fix lives on their machine
until it is merged upstream. Give them this, ready to send:

1. **What was wrong**, in one or two plain sentences — what they saw, not what you infer.
2. **The exact output** of `doctor.py` from before the fix.
3. **The change**, as a patch they can paste:

       git diff > linkchat-fix.patch

4. **Whether `python tests/run_all.py` passes** after it.

Say plainly if you could not fix it. An accurate description of a fault the maintainer can
reproduce is worth more than a workaround nobody can check — and on a Mac, being the
first person to hit something is useful information rather than a nuisance.

---

## What LinkChat is, briefly

Two screens over the CRM this person built in Sessions 0 to 2. **Conversations** — every
LinkedIn conversation in one list, where they can reply. **Sequences** — an opening
message and what follows depending on how somebody replies. Plus **Find people**.

It keeps no record about any person. People, the event log, the daily limit and the
do-not-message list all stay in their own CRM and are reached through one file,
`engine/crm_bridge.py`.

**Approving sends.** That is the whole design: the gate is the approval, not a retype.
Full detail for them, in plain English, is in `guide/LinkChat — Install and Use.md`.
