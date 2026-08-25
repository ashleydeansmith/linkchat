# LinkChat — install and use

**What this is.** LinkChat is a program that runs on your own computer. It shows you
your LinkedIn conversations on one screen, and the sequences that run off them on
another. When you approve a message, it sends it.

**What it is not.** It is not a second CRM. LinkChat stores no record about any person.
Your people, your event log, your daily limit and your do-not-message list all stay in
the CRM you built in Sessions 0 to 2. LinkChat reads them from there and writes back
into them. If you delete LinkChat tomorrow, you have lost nothing.

**Read this line before anything else: approving a message sends it.** There is no
draft folder that quietly does nothing. You press approve, the message goes to that
person on LinkedIn. That is the point of it — you are meant to work through the machine
rather than beside it — but nobody should meet that fact by accident.

---

## Before you start

You need four:

1. **A Windows computer**, or a Mac — read the Mac paragraph below before you start.
2. **The CRM you built in Sessions 0 to 2**, on this same computer. LinkChat needs the
   folder it lives in.
3. **Your LinkedIn login**, the normal one. You will type it into a normal browser
   window, once.
4. **About twenty minutes**, most of it waiting for downloads.

### If you are on a Mac, read this first

**Message Ashley before the call, not during it.** There is now a Mac installer in the
LinkChat folder — it is called `setup-mac.command` and you run that instead of `setup.cmd`.
Everything after the install is identical: the same two screens, the same approval line,
the same five checks.

**Nobody has ever run LinkChat on a Mac.** It was written on 2026-08-25 and has never
been run on the computer it is for. So do the install the day before, on your own, with
Ashley on a message — not live on the call with eight people waiting, where a fault
costs you the session. If it stops, send Ashley everything the window printed. That is
useful rather than a nuisance, and it is how the Mac version gets finished.

**Three places the Mac differs**, all of them handled by `setup-mac.command`:

- Your Mac has a file called `python3` that is not Python. Typing it opens a box
  offering to install developer tools. The installer refuses that file rather than
  half-installing on top of it — the same way the Windows one refuses the Microsoft
  Store. If you need the real one it is at
  **https://www.python.org/downloads/macos/**.
- The Python that comes with a Mac refuses to have anything installed into it. So
  LinkChat builds its own private Python inside its own folder and installs there.
  Nothing outside the LinkChat folder is touched.
- The first time you double-click the LinkChat file on your desktop, your Mac may say
  it cannot be opened because it is from an unidentified developer. **Right-click it
  instead, choose Open, then choose Open again** in the box that appears. You only do
  that once.

Wherever this guide says `setup.cmd`, you run `setup-mac.command`. Wherever it says a black
window, yours is called Terminal.

---

## Step 1 — Python

LinkChat is written in Python, so Python has to be on the computer first.

Open the Start menu, type `cmd`, press Enter. A black window opens. Type this and press
Enter:

    python --version

- **If it answers `Python 3.10` or higher** — for example `Python 3.13.2` — you already
  have it. Go to Step 2.
- **If it answers a lower number**, or says it is not recognised, or the Microsoft Store
  opens: you need to install it. Read the next paragraph carefully, because one tick box
  in that installer is the difference between this working and not.

Go to **https://www.python.org/downloads/** and press the big yellow download button.
Run the file it gives you. **On the very first screen of the installer there is a tick
box that says "Add python.exe to PATH". Tick it before you press Install.** It is not
ticked by default and it is easy to miss. If you miss it, nothing later in this guide
will work and the error will not tell you why.

When it finishes, **close the black window and open a new one**, then run
`python --version` again to check it now answers properly.

> **Why the Microsoft Store sometimes opens.** Windows ships a file that is named
> python.exe but is not Python — its only job is to send you to the shop. LinkChat's
> installer knows about that file and refuses it rather than half-installing on top of
> it. If the shop opens, install from python.org as above.

---

## Step 2 — Get LinkChat onto your computer

Ashley will send you an invitation to a private page on GitHub. Accept it first — the
next command will not work until you have.

In the black window, type these two lines, pressing Enter after each:

    cd %USERPROFILE%\Documents
    git clone https://github.com/ashleydeansmith/linkchat.git LinkChat

- If it asks you to sign in to GitHub, sign in with the account Ashley invited.
- If it says `git` is not recognised, install Git from
  **https://git-scm.com/download/win**, accept every default, then close the black
  window, open a new one, and run the two lines again.

You now have a folder at `Documents\LinkChat`.

---

## Step 3 — Run the installer

Open `Documents\LinkChat` in File Explorer. Double-click **`setup.cmd`**.

A black window opens and works through four steps. It takes about five minutes, and the
second step downloads about 150 MB, so it is the slow one.

Windows may show a blue "Windows protected your PC" panel, because the file is not
signed. Click **More info**, then **Run anyway**.

When it finishes it says so, and there is now a **LinkChat icon on your desktop**.

If it stops with a message instead, it tells you what to do about it. Send Ashley the
last few lines in that window — they name the actual cause.

---

## Step 4 — Open it and point it at your CRM

Double-click the LinkChat icon on your desktop. A window opens.

The first screen asks one question: **where is the CRM you already built.**

Type the full path to the folder — for example `C:\Users\yourname\CRM`. It is the folder
with **`_engine`** and **`People`** inside it, the one Layer 1 made. If you point it at
the wrong folder it says so rather than pretending, so a wrong guess costs you nothing.

It also asks for **your name**. This goes on the approval line. A sequence writes a
message and is not allowed to approve its own work, so every message that reaches your
outbox carries the name of the person who said it could go: you.

Press **Use this CRM**.

---

## Step 5 — Sign into LinkedIn, once

**There is no button that starts the browser, and you do not need one.** The browser
opens by itself the first time something needs it, which is when you press **Sync** on
the Conversations screen.

So: open **Conversations** and press **Sync**. A browser window opens — an ordinary
browser, on your own computer. Sign into LinkedIn in it exactly as you normally would,
including any code it texts you.

**Give it up to two minutes to settle after you have signed in**, and leave that browser
window open while you finish. The line across the top of LinkChat reads **LinkedIn
browser: not started** until it is up, and **LinkedIn browser: running** once it is.
When it says running, press **Sync** once more — the first press was what opened the
browser, and this is the one that reads your inbox.

Once it says running you can close the browser window. It remembers you on this
computer and you are not asked again.

Your password is typed into LinkedIn's own page in a normal browser. LinkChat never sees
it, never stores it, and never asks you for it.

---

## The three screens

**Conversations.** Every LinkedIn conversation you have, read into one list. Press
**Sync** to go and read your inbox again; that opens pages and reads them, which takes a
while on a large inbox, and it only ever reads. You can reply to anybody here in your own
words.

**Sequences.** The branches — what gets sent when somebody replies a particular way, and
what is waiting for your approval right now. This is where a message a sequence wrote is
shown to you, and where you approve it.

**Find people.** Search across the people your CRM already knows about.

---

## What happens when you approve

Approving sends. Before it does, the message passes five checks. Every message goes
through the same five, whether a sequence wrote it or you typed it, because there is only
one road out — a second road would be the one nobody maintained, and that is the one that
would reach somebody.

The five, in the order they run:

1. **Your CRM has the parts that do the checking.** If they are missing, no message goes.
2. **The person is not on your do-not-message list** — the list your CRM calls the hold
   list, of people nothing automatic may ever contact. If LinkChat cannot find that list,
   it treats everybody as being on it.
3. **Nothing in the words was left unfilled.** A message still reading `Hi {first_name}`
   is refused.
4. **Their own name field is not typed back at them.** People put symbols and emoji in
   their LinkedIn name on purpose, to catch messages nobody read before sending. Greeting
   somebody with their decorated name walks straight into that. Greet them by name
   instead.
5. **It is not near enough to the last message to be a copy.**

A message a **sequence** wrote faces a sixth: it cannot approve its own work, so your own
review step has to release it and record who did. A reply **you typed** does not face that
one, and the reason is worth saying plainly: the rule was never that a person must retype
what a machine wrote. It is that a machine must not decide somebody should hear from you.
When you type the words, the deciding was already yours.

When a check refuses, it says which one and why, in a sentence. Nothing is hidden.

---

## Your first sequence

A sequence is an opening message, plus what you say back depending on how the person
replies. You write it once. After that it decides who is next and what they get, and
every message it writes waits for you to approve it.

The Sequences screen starts empty, and a blank canvas is a hard place to start from. So
you are offered a shape to work on top of.

### Load it

Open **Sequences**. Because you have not built one yet, the screen offers two buttons:

- **Start from the shape** — loads the sequence described below.
- **Start from nothing** — an empty canvas, if you would rather.

Press **Start from the shape**. It arrives as a draft, and a draft changes nothing until
you press **Activate**, so you can open it, read it and change your mind with nothing at
stake.

### What you get

One opening message, and the four ways a person comes back from it:

| | | |
|---|---|---|
| **No reply at all** | after four days | 309 of 812 people |
| **A pleasantry only** | "thanks for connecting!" | 350 of 812 |
| **Not now, or no** | | 57 of 812 |
| **Interested, or asks you something** | | 42 of 812 |

Those counts are from 812 real conversations, and the reason they are printed on the
canvas is that they say something most people get backwards. **The two largest groups
are the two that feel like failures.** The biggest group of all is the people who never
replied — 309 of them — and not one message has ever been written for that group. The
second biggest said something polite that answers nothing. Between them that is 659 of
812 people, and almost all of the unworked ground is there.

### Every message in it is empty on purpose

Open any step and you will find something like this where the words should be:

    {what you say to somebody who was only being polite - one question they
     can answer in a sentence, about them and not about you}

That is a gap, not a draft. **Check three refuses any message with a gap left in it**, so
this sequence physically cannot send anything until you have replaced every gap with your
own words. If you press approve on one before you have written it, it stops and tells you:
*this still has a gap in it that nothing filled in. Write the words in yourself.*

This is deliberate, and it is the reason you are given a shape rather than a script.
Nobody else's words go out under your name, and you cannot get past it by not reading
carefully. There are five gaps. Fill all five and the sequence runs.

### Fill one in

1. Click a step on the canvas. The panel on the right shows its words.
2. Delete everything between the curly brackets, **and the brackets too**.
3. Write what you would actually say. One or two sentences.
4. Do the same for the other four.
5. Press **Activate**.

Under each branch there is a short note headed **never** — two lines saying what not to do
in that particular reply. They are notes to yourself. Nothing sends them and nothing
checks them.

### Add a way back of your own

Each branch is matched by the words people actually use. The **patterns** box holds those
words — `thanks for connecting`, `not right now`, `how much`. A reply is matched against
each branch in order and lands in the first one that fits.

To add a fifth way back, add a branch and give it three or four phrases you have genuinely
seen people write. Use the **preview** to see which of your real conversations it would
have caught. A pattern must be at least three characters, because a shorter one matches
nearly everything and swallows the whole inbox.

Two rules worth keeping:

- **Write the pattern you have seen, not the one you can imagine.** Read ten real replies
  first and take the phrases out of them.
- **Order matters.** The first branch that fits wins, so put the narrow ones above the
  wide ones.


---

## If your copy can read but not send

LinkChat's checks are not LinkChat's rules. They are the parts you installed in **Layer 6**
of your own CRM. If you have not reached Layer 6 yet, the part that would grant permission
is not there — and an absent guard must never read as an open door. So LinkChat opens,
shows you every conversation, and lets you read everything. It just cannot send.

That is deliberate. It is better than the program refusing to open, and far better than it
sending without the checks.

To see exactly what your copy can and cannot do, open the black window and type:

    cd %USERPROFILE%\Documents\LinkChat
    python -m engine crm

It prints your CRM's location, how many people are in it, your daily limit and how much of
it you have used, then a `yes` or `no` for reading, syncing and drafting, then a line for
each part that is missing and what that part is for. Send Ashley that output if you are
not sure what it is telling you — it names the cause precisely.

Reaching Layer 6 turns sending on. Nothing needs reinstalling.

---

## When something goes wrong

### Start here, whatever it is

    python doctor.py        (Windows)
    python3 doctor.py       (Mac)

Type that in the LinkChat folder. It works out which computer you are on, which installer
belongs to it, how far the install got, whether the parts and the browser are there, and
whether it is pointed at your CRM. Then it says what to do next, in one line.

It only looks. It installs nothing, changes nothing, opens no browser and sends no
message to anybody, so it is safe to run at any point — including before you have
installed anything and including when it is broken.

**If you are stuck, run it and send Ashley what it printed.** That is more useful than
describing what happened, because it names the actual cause.

### Getting your own Claude to fix it

You have Claude Code. Open it **in the LinkChat folder** and say:

    run doctor.py and fix what it finds

There is a `CLAUDE.md` in that folder which Claude reads on its own. It tells Claude what
it may change, and — more to the point — what it must never touch: the checks that stop a
message going to the wrong person, your CRM records, and the tests. If a fix would need
one of those weakened, Claude is told to stop and send you to Ashley instead.

**This matters most on a Mac**, where you are the first person ever to run LinkChat.
Something that stops you is likely to be real rather than your fault, and Claude has what
it needs to find it. Send Ashley whatever it changed.

### The usual ones

**The desktop icon does nothing when I double-click it.**
Open the black window, then:

    cd %USERPROFILE%\Documents\LinkChat
    python -m engine desktop

That runs the same program with its errors visible, and the message it prints is the
answer. Send Ashley that message.

**It says a part is missing when I open it.**
Run the installer again — `setup.cmd` on Windows, `setup-mac.command` on a Mac. It is
safe to run as many times as you like.

**Sync finds nothing, or the top line stays on "LinkedIn browser: not started".**
Press **Sync** — that is what opens the browser. Sign into LinkedIn in the window that
appears, wait for the top line to read **LinkedIn browser: running**, then press **Sync**
again. The first press opens the browser; the second one reads your inbox.

**It is asking for my CRM folder again.**
It is pointed somewhere that no longer has `_engine` and `People` inside it. Give it the
path again.

**Anything else.** Message Ashley with what you did and what it said back. The messages
this program prints are written to name the actual cause, so quoting one is usually
enough.

---

## What LinkChat never does

- It never types your password, sees it, or stores it.
- It never writes into a conversation without you approving it first.
- It never contacts anybody on your do-not-message list.
- It never keeps its own copy of a person. Your records stay yours, in your CRM.
