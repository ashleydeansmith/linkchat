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

1. **A Windows computer.** See the next paragraph if you are on a Mac.
2. **The CRM you built in Sessions 0 to 2**, on this same computer. LinkChat needs the
   folder it lives in.
3. **Your LinkedIn login**, the normal one. You will type it into a normal browser
   window, once.
4. **About twenty minutes**, most of it waiting for downloads.

**If you are on a Mac: stop here and message Ashley before you install anything.** The
Mac version has not been built or tested. Everything below is written for Windows and
the installer file will not run for you. This is not a small difference you can work
around — half-installing it will cost you more time than waiting will.

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

At the top of the window there is a line reading **LinkedIn browser — stopped**, with a
**Start browser** button next to it.

Press **Start browser**. A Chromium browser window opens — an ordinary browser, on your
own computer. Sign into LinkedIn in it exactly as you normally would, including any code
it texts you.

**Give it up to two minutes to settle after you have signed in**, and leave that browser
window open while you finish signing in. The line at the top of LinkChat changes to
**LinkedIn browser — running** when it is ready. Once it says running you can close the
browser window; it remembers you on this computer and you are not asked again.

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

**The desktop icon does nothing when I double-click it.**
Open the black window, then:

    cd %USERPROFILE%\Documents\LinkChat
    python -m engine desktop

That runs the same program with its errors visible, and the message it prints is the
answer. Send Ashley that message.

**It says a part is missing when I open it.**
Run `setup.cmd` again. It is safe to run as many times as you like.

**Sync finds nothing, or the browser line stays on "stopped".**
Press **Start browser**, sign into LinkedIn in the window that opens, and wait for the
line to read **running** before pressing Sync.

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
