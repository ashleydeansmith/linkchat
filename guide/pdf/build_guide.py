# -*- coding: utf-8 -*-
"""build_guide.py — renders the LinkChat walkthrough to the Outliers house style.

For future Claude: this is the SAME renderer pattern as the eight CRM layer
guides in `Second Brain/Projects/Accelerator-Beta/Session 1/Build CRM HW/
outliers-crm-series/build_guides.py`, and it is deliberately a copy rather than
something new. One house style, one series. The palette, the type, the seal, the
page furniture and the footer all come from the locked brand recipe at
`Social/Assets/Outliers-Brand/Outliers-Hallmark-Brand-System.md` — Paper,
Vellum, Ink, Oxblood, Brass, and no other colour ever.

WHAT WILL CATCH YOU OUT. Every page is a fixed 296mm with `overflow:hidden`.
Content past the bottom of a page does NOT reflow onto the next one — it is
deleted, and the render looks perfectly healthy. So `check_overflow.py` runs
after this and gates the send. Add a sentence to a full page and you can silently
cost a reader the last thing on it.

    python build_guide.py
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "shots"
OUT_PDF = Path(r"C:\Users\somth\Documents\Second Brain\Projects"
               r"\Accelerator-Beta\Session 4\LinkChat - Install and Walk Through.pdf")
ALSO = HERE.parent / "LinkChat - Install and Walk Through.pdf"

SECTIONS = 11          # blocks in the position bar

STYLE = """
  @page { size: A4; margin: 0; }
  * { margin:0; padding:0; box-sizing:border-box; }
  :root { --paper:#F3EEE3; --vellum:#E7DEC9; --ink:#14110C; --ox:#6E1A18;
          --brass:#B08A3E; }
  body { font-family: Constantia, Georgia, serif; color:var(--ink);
         background:var(--paper); }
  .page { width:210mm; height:296mm; padding:20mm 22mm; background:var(--paper);
          page-break-after:always; position:relative; overflow:hidden; }
  .page:last-child { page-break-after:auto; }
  .mono { font-family: Consolas,"Courier New",monospace; letter-spacing:.16em;
          text-transform:uppercase; }
  .label { font-size:8.5pt; color:var(--brass); margin-bottom:3mm; }
  h1 { font-size:34pt; font-weight:bold; line-height:1.1; }
  h2 { font-size:19pt; font-weight:bold; margin-bottom:3mm; line-height:1.18; }
  h3 { font-family:Consolas,monospace; font-size:8pt; letter-spacing:.16em;
       text-transform:uppercase; color:var(--brass); margin:6mm 0 1.5mm 0; }
  .deck { font-size:11pt; line-height:1.45; color:var(--ox); margin-bottom:5mm; }
  p { font-size:10.5pt; line-height:1.5; margin-bottom:3mm; }
  li { font-size:10.5pt; line-height:1.5; margin-bottom:1.5mm; margin-left:5mm; }
  b { font-weight:bold; }
  figure { margin:5mm 0; }
  figure img { width:100%; display:block; border:.3mm solid rgba(20,17,12,.35); }
  figure img.narrow { width:64%; margin:0 auto; }
  .cap { font-family:Consolas,monospace; font-size:7pt; letter-spacing:.1em;
         text-transform:uppercase; color:var(--brass); text-align:center;
         margin-top:2mm; }
  .box { border-left:2mm solid var(--ox); background:var(--vellum);
         padding:3.5mm 4.5mm; margin-top:5mm; }
  .box .mono { font-size:7.5pt; color:var(--ox); display:block;
               margin-bottom:1.2mm; }
  .box p { margin:0; }
  .box p + p { margin-top:2.5mm; }
  .cmd { font-family:Consolas,monospace; font-size:9.5pt; background:var(--ink);
         color:var(--paper); padding:3mm 4mm; margin:3mm 0; line-height:1.6; }
  code { font-family:Consolas,monospace; font-size:9pt; background:var(--vellum);
         padding:0 1mm; }
  .footer { position:absolute; bottom:10mm; left:22mm; right:22mm; display:flex;
            justify-content:space-between; font-size:7pt; color:var(--brass); }
  .pgno { font-family:Consolas,monospace; font-size:7pt; letter-spacing:.1em;
          color:var(--brass); }
  .pos { position:absolute; top:19mm; right:22mm; width:48mm; }
  .rule { border:none; border-top:.3mm solid var(--ink); opacity:.25; margin:5mm 0; }
  .cover { display:flex; flex-direction:column; justify-content:space-between;
           text-align:center; padding-top:44mm; }
  .cover .word { font-size:24pt; font-weight:bold; letter-spacing:.3em;
                 margin-top:9mm; }
  .cover h1 { margin-top:14mm; }
  .cover .sub { font-size:12pt; color:var(--ox); margin-top:5mm; line-height:1.5; }
  table { width:100%; border-collapse:collapse; margin-top:3mm; }
  th { font-family:Consolas,monospace; font-size:7.5pt; letter-spacing:.14em;
       text-transform:uppercase; color:var(--brass); text-align:left;
       padding:1.8mm 2mm; border-bottom:.4mm solid var(--ink); }
  td { font-size:9.5pt; line-height:1.36; padding:1.7mm 2mm;
       border-bottom:.2mm solid rgba(20,17,12,.2); vertical-align:top; }
  td.k { width:38mm; font-weight:bold; }
  td.n { width:9mm; font-family:Consolas,monospace; font-size:7.5pt;
         letter-spacing:.1em; color:var(--brass); padding-top:2.6mm; }
  .steps td.n { width:9mm; }
  .rail { display:flex; margin-top:4mm; }
  .rail div { flex:1; border:.3mm solid rgba(20,17,12,.3); border-right:none;
              padding:2.6mm 2.8mm; }
  .rail div:last-child { border-right:.3mm solid rgba(20,17,12,.3); }
  .rail b { display:block; font-size:9.5pt; margin-bottom:1mm; }
  .rail span { font-size:8pt; line-height:1.35; }
  text { font-family:Consolas,"Courier New",monospace; }
"""

# The struck hallmark: outer thin ring, bold inner O, and the one point that
# broke from the cluster, at -46 degrees, in oxblood. Numbers from the recipe.
SEAL = ('<svg width="132" height="132" viewBox="0 0 200 200" '
        'style="display:block;margin:0 auto">'
        '<circle cx="100" cy="100" r="90" fill="none" stroke="#14110C" stroke-width="3.15"/>'
        '<circle cx="100" cy="100" r="50.4" fill="none" stroke="#14110C" stroke-width="18"/>'
        '<circle cx="162.51" cy="35.26" r="10.35" fill="#6E1A18"/></svg>')


def position_bar(n, label):
    """Where you are in the document, so a reader is never lost mid-way."""
    out = []
    w = 152 // SECTIONS
    for i in range(1, SECTIONS + 1):
        fill = "#14110C" if i == n else ("#C9BCA0" if i < n else "#E7DEC9")
        out.append('<rect x="%d" y="0" width="%d" height="7" fill="%s" '
                   'stroke="#14110C" stroke-width="0.4"/>'
                   % ((i - 1) * (w + 1), w, fill))
    out.append('<text x="0" y="17" style="font-size:6px;letter-spacing:.14em;'
               'fill:#B08A3E">%s</text>' % label.upper())
    return '<div class="pos"><svg viewBox="0 0 152 20" width="100%%">%s</svg></div>' % "".join(out)


def page(inner, foot_left, pgno, label=None, bar=None):
    head = ('<div class="label mono">%s</div>' % label) if label else ""
    head += bar or ""
    return ('<div class="page">%s%s<div class="footer"><span class="mono">%s</span>'
            '<span class="pgno">%d</span><span class="mono">Outliers</span></div></div>'
            % (head, inner, foot_left, pgno))


def shot(name, cap, narrow=False):
    src = (SHOTS / (name + ".png")).as_uri()
    cls = ' class="narrow"' if narrow else ""
    return '<figure><img src="%s"%s><div class="cap">%s</div></figure>' % (src, cls, cap)


CHECKS = [
    ("01", "The parts that do the checking are installed",
     "If the part of your CRM that checks a message is not there, nothing is "
     "sent. A check that cannot be consulted is not an absent check."),
    ("02", "They are not on your do-not-message list",
     "Asked in every way the address might be written down, not the one way it "
     "happens to be stored. Ten spellings of the same person all come back held."),
    ("03", "Nothing is left unfilled in the words",
     "A message still carrying <code>{name}</code> is the most obvious way a "
     "message announces that nobody wrote it. It is refused, and the gap named."),
    ("04", "Their own name field is not typed back at them",
     "Symbols in a name field are often put there to catch a machine. An emoji "
     "you deliberately wrote into your own words is your writing, and is left alone."),
    ("05", "It is not a copy of a message you just approved",
     "Twenty different replies to twenty people is somebody having conversations. "
     "Twenty copies of one sentence is what gets an account limited."),
]

NEEDS = [
    ("01", "The CRM you built in Sessions 0 to 2",
     "The folder with <code>_engine</code> and <code>People</code> inside it. "
     "LinkChat reads your daily limit, your do-not-message list and your records "
     "from there. No cost."),
    ("02", "Python 3.10 or later",
     "From python.org. Free. Tick the box marked <b>Add python.exe to PATH</b> on "
     "the installer's first screen; without it the LinkChat installer cannot find "
     "Python afterwards."),
    ("03", "Git",
     "From git-scm.com. Free. Used once to copy the program down, and again each "
     "time you want the newest version."),
    ("04", "About 400 MB of disk, and five minutes",
     "Most of it is the browser LinkChat reads LinkedIn with, downloaded during "
     "the install. No account and no payment at any point."),
]


def rows(items):
    return "".join('<tr><td class="n">%s</td><td class="k">%s</td><td>%s</td></tr>'
                   % (n, t, d) for n, t, d in items)


def build_html():
    p = []

    # ---- 1. cover -----------------------------------------------------------
    p.append('<div class="page cover"><div>%s<div class="word mono">Outliers</div>'
             '<h1>LinkChat</h1><div class="sub">Your LinkedIn conversations, and the '
             'sequences that run off them,<br>on your own computer — through the CRM '
             'you already built.</div></div>'
             '<div style="margin-bottom:15mm"><div class="mono" '
             'style="font-size:8.5pt;color:#B08A3E">Est. MMXXVI &nbsp;&middot;&nbsp; '
             'Outliers Accelerator</div></div></div>' % SEAL)

    # ---- 2. what it is ------------------------------------------------------
    inner = ('<h2>What it is, and what it keeps</h2>'
             '<div class="deck">Five screens over the CRM you built in Sessions 0 to 2. '
             'It reads your LinkedIn inbox onto this computer, shows you what is waiting, '
             'and carries a message into a conversation once you have approved it.</div>'
             '<h3>What it does not hold</h3>'
             '<p>LinkChat keeps no record about a person. Your people, your event log, '
             'your daily limit and your do-not-message list all stay in your CRM, and '
             'LinkChat reads them from there through one file. Point it at a different '
             'CRM and everything it knows changes, because it knew none of it.</p>'
             '<p>That is why there is nothing to export if you stop using it, and why '
             'two copies of LinkChat pointed at one CRM cannot disagree about who you '
             'have messaged today.</p>'
             '<h3>The five screens</h3>'
             '<div class="rail">'
             '<div><b>Home</b><span>What is waiting, and one next step worked out for you.</span></div>'
             '<div><b>Conversations</b><span>Your LinkedIn inbox, read onto this computer.</span></div>'
             '<div><b>Sequences</b><span>What you say when somebody replies, and to which reply.</span></div>'
             '<div><b>Campaigns</b><span>Connection requests, withdrawals, reading a saved search.</span></div>'
             '<div><b>Live</b><span>What your CRM has recorded, newest first.</span></div>'
             '</div>'
             '<div class="box"><span class="mono">Nothing happens on its own</span>'
             '<p>LinkChat runs when you press something. There is no clock in it and no '
             'job waiting to fire while you are away. A message goes when you press Send '
             'or approve one, and a copy is written into your outbox before it is carried, '
             'so a send that fails still leaves you the words.</p></div>')
    p.append(page(inner, "LinkChat &middot; What it is", 2,
                  "LinkChat &middot; What it is", position_bar(1, "Part 1 of 11")))

    # ---- 3. what you need ---------------------------------------------------
    inner = ('<h2>What you need before you start</h2>'
             '<div class="deck">Four things. Two you already have if you finished '
             'Session 2. The whole list is here so nothing arrives as a surprise '
             'halfway through.</div>'
             '<table>%s</table>'
             '<div class="box"><span class="mono">On a Mac</span>'
             '<p>Everything works the same, with one difference: you run '
             '<code>setup-mac.command</code> rather than <code>setup.cmd</code>, and '
             'where this document says <code>python</code> you type <code>python3</code>. '
             'The Mac installer builds LinkChat its own private Python inside the '
             'LinkChat folder, because the Python a Mac ships with refuses to have '
             'anything installed into it.</p></div>' % rows(NEEDS))
    p.append(page(inner, "LinkChat &middot; Before you start", 3,
                  "LinkChat &middot; Before you start", position_bar(2, "Part 2 of 11")))

    # ---- 4. getting it ------------------------------------------------------
    inner = ('<h2>Getting it onto your computer</h2>'
             '<div class="deck">Four steps. Every command below is typed into a '
             'terminal: on Windows press Start, type <code>cmd</code> and press Enter; '
             'on a Mac press Command and Space, type Terminal and press Enter.</div>'
             '<h3>One &middot; Copy the program down</h3>'
             '<div class="cmd">cd %USERPROFILE%\\Documents<br>'
             'git clone https://github.com/ashleydeansmith/linkchat.git LinkChat</div>'
             '<p>On a Mac the first line is <code>cd ~/Documents</code> instead. You now '
             'have a folder called <code>LinkChat</code> inside Documents. If that second '
             'line comes back saying the repository could not be found, the copy you were '
             'pointed at is not open to you — say so, it is one setting at the other end.</p>'
             '<h3>Two &middot; Run the installer</h3>'
             '<p>Open <code>Documents\\LinkChat</code> in File Explorer and double-click '
             '<code>setup.cmd</code>. On a Mac, double-click <code>setup-mac.command</code>. '
             'It prints four lines as it goes and takes about five minutes, most of it '
             'downloading the browser.</p>'
             '<h3>Three &middot; Double-click the icon it leaves on your desktop</h3>'
             '<p>LinkChat opens in its own window and asks where your CRM is.</p>'
             '<div class="box"><span class="mono">If any of that does not go as written</span>'
             '<p>In the terminal, move into the folder and ask the program what it can see. '
             'It reports which computer this is, which installer belongs to it, which Python '
             'you have and whether it is one of the two impostors, which parts are installed, '
             'whether the browser downloaded, and whether it has been pointed at a CRM — then '
             'one line saying what to do next. It looks only: it installs nothing, changes '
             'nothing and sends nothing.</p></div>'
             '<div class="cmd">cd %USERPROFILE%\\Documents\\LinkChat<br>python doctor.py</div>')
    p.append(page(inner, "LinkChat &middot; Install", 4,
                  "LinkChat &middot; Install", position_bar(3, "Part 3 of 11")))

    # ---- 5. pointing it -----------------------------------------------------
    inner = ('<h2>Pointing it at your CRM</h2>'
             '<div class="deck">Asked once, the first time you open it. Two boxes.</div>'
             + shot("setup", "The panel it opens with", narrow=True) +
             '<p>Your name is the box people skim past. It goes on the approval line: a '
             'sequence writes a message and cannot approve its own work, so every message '
             'that reaches your outbox carries the name of the person who said it could.</p>'
             '<h3>What it goes looking for</h3>'
             '<p>Eight parts of your CRM, by name: the part that checks a message before '
             'it goes, your do-not-message list, your daily limit, your event log, your '
             'review step, the part that works out that one person is one person, the part '
             'that knows where your folders are, and the lock that stops two jobs driving '
             'the browser at once. Any that are missing are named on the screen, and what '
             'LinkChat can do shrinks to match — rather than failing at the moment you '
             'press something.</p>'
             '<p>If your CRM has not reached Layer 6, LinkChat opens anyway and reads '
             'everything. It will not send until Layer 6 is installed, and it says so on '
             'the screen rather than refusing to open.</p>')
    p.append(page(inner, "LinkChat &middot; Your CRM", 5,
                  "LinkChat &middot; Your CRM", position_bar(4, "Part 4 of 11")))

    # ---- 6. home ------------------------------------------------------------
    inner = ('<h2>Home</h2>'
             '<div class="deck">What is waiting, and one next step — not a list of '
             'options, one step, worked out from what is actually true right now.</div>'
             + shot("home", "Home, on a copy that has read seven conversations") +
             '<p>The strip along the top is on every screen: whether you are signed in to '
             'LinkedIn, whether it is reading, how much of today\'s limit is left, and how '
             'many conversations are on this computer.</p>'
             '<p><b>Waiting on a reply</b> is the count that matters most days — '
             'conversations where they said something last and you have not answered. '
             '<b>Have agreed words</b> counts the ones your sequence recognises and has a '
             'message ready for. <b>Waiting for you to approve</b> is the review step.</p>')
    p.append(page(inner, "LinkChat &middot; Home", 6,
                  "LinkChat &middot; Home", position_bar(5, "Part 5 of 11")))

    # ---- 7. conversations, list and one open -------------------------------
    inner = ('<h2>Conversations</h2>'
             '<div class="deck">Your LinkedIn inbox, read onto this computer. Pressing '
             'Sync inbox is also what opens the browser the first time, and that is where '
             'you sign in — once, on this computer.</div>'
             + shot("inbox", "The boxes on the left narrow the same list. The names are invented")
             + shot("conversation-open", "One conversation, open: the thread, a private note, the reply box") +
             '<p>What you type in the reply box goes into the conversation when you press '
             '<b>Send</b> — and a copy is written into your outbox first, so a send that '
             'fails still leaves you the words. The note on the right is yours alone and '
             'never leaves this computer.</p>')
    p.append(page(inner, "LinkChat &middot; Conversations", 7,
                  "LinkChat &middot; Conversations", position_bar(6, "Part 6 of 11")))

    # ---- 8. the five checks, and one refusing -----------------------------
    inner = ('<h2>The five checks</h2>'
             '<div class="deck">Every message takes one road out, whoever wrote it. Five '
             'things have to be true before a single character reaches anybody. A message '
             'a sequence wrote faces a sixth: the review step in your CRM, which a sequence '
             'cannot release itself.</div>'
             + ('<table>%s</table>' % rows(CHECKS))
             + shot("refused", "The third check, refusing. Nothing was sent and nothing was lost")
             + '<p>A check that refuses is the check working. Read the sentence it gives you '
               'and fix what it names. Do not look for a way around it.</p>')
    p.append(page(inner, "LinkChat &middot; The checks", 8,
                  "LinkChat &middot; The checks", position_bar(7, "Part 7 of 11")))

    # ---- 9. sequences ------------------------------------------------------
    inner = ('<h2>Sequences</h2>'
             '<div class="deck">What you say when somebody replies, and to which kind of '
             'reply. Press Start from the shape and you get this rather than a blank '
             'canvas.</div>'
             + shot("sequences", "The starter shape: one opener, four ways back") +
             '<p>The two groups that feel like failures are the two largest. <b>No reply at '
             'all</b> and <b>a pleasantry and nothing else</b> are where most people end up, '
             'and where a written answer is worth the most, because at the moment you have '
             'none.</p>'
             '<div class="box"><span class="mono">Every message in it is a gap</span>'
             '<p>Nothing in the starter shape contains words you could send. Every message '
             'body is something for you to write, and the third check physically refuses a '
             'message with a gap left in it. Handed real copy, you could send five messages '
             'you had never read. This way you cannot.</p></div>')
    p.append(page(inner, "LinkChat &middot; Sequences", 9,
                  "LinkChat &middot; Sequences", position_bar(8, "Part 8 of 11")))

    # ---- 10. campaigns ------------------------------------------------------
    inner = ('<h2>Campaigns</h2>'
             '<div class="deck">Where conversations come from. Everything else works on '
             'people who have already replied — this is the part that reaches out.</div>'
             + shot("campaigns", "Four jobs, each with Practice and Do it") +
             '<p><b>Practice</b> does everything except the outward action and tells you '
             'what it would have done. <b>Do it</b> does it. Nothing on this screen presses '
             'Do it for you.</p>'
             '<div class="box"><span class="mono">One limit, not two</span>'
             '<p>Each job reads the same daily limit in your CRM that Gather reads. LinkedIn '
             'counts the account, not the program, and two programs each keeping their own '
             'tally is how both stay inside a limit while the account goes over it.</p>'
             '<p>One consequence worth knowing: that limit is shared across all four jobs '
             'and your messages. Message twenty people and reading a saved search will '
             'refuse too, until tomorrow.</p></div>')
    p.append(page(inner, "LinkChat &middot; Campaigns", 10,
                  "LinkChat &middot; Campaigns", position_bar(9, "Part 9 of 11")))

    # ---- 11. live -----------------------------------------------------------
    inner = ('<h2>Live</h2>'
             '<div class="deck">What LinkChat is doing now, and what your CRM has '
             'recorded. Read from your own event log — nothing on this screen sends or '
             'changes anything.</div>'
             + shot("live", "Newest first, out of your own event log") +
             '<p>These lines are written by your CRM, not by LinkChat, which is why they '
             'are still there if you stop using LinkChat entirely.</p>'
             '<p>An entry naming no person means the record could not be matched to '
             'somebody in your CRM at the moment it was written. It is shown rather than '
             'hidden, because a screen that quietly drops what it cannot explain is a '
             'screen you cannot trust about anything else.</p>')
    p.append(page(inner, "LinkChat &middot; Live", 11,
                  "LinkChat &middot; Live", position_bar(10, "Part 10 of 11")))

    # ---- 12. when something is wrong ---------------------------------------
    inner = ('<h2>When something is wrong</h2>'
             '<div class="deck">Four things go wrong more often than anything else. Each '
             'has one answer.</div>'
             '<h3>Nothing opens when you double-click the icon</h3>'
             '<p>Run the doctor. The most common cause is Python installed without '
             '<b>Add python.exe to PATH</b> ticked, and the doctor says so in those words.</p>'
             '<div class="cmd">cd %USERPROFILE%\\Documents\\LinkChat<br>python doctor.py</div>'
             '<h3>It says the browser is not started</h3>'
             '<p>Go to Conversations and press <b>Sync inbox</b>. That is what opens it. A '
             'window appears on the LinkedIn login page, and it can open behind the LinkChat '
             'window, so check your taskbar. Sign in once, come back, press Sync inbox again.</p>'
             '<h3>It says your conversations file is damaged</h3>'
             '<p>Close LinkChat, move the file it names somewhere else, and open LinkChat '
             'again. It builds a new one and reads your inbox back in. Your people, your '
             'outbox and your records are separate files and are untouched.</p>'
             '<h3>Getting the newest version</h3>'
             '<div class="cmd">cd %USERPROFILE%\\Documents\\LinkChat<br>git pull</div>'
             '<p>Your CRM is a different folder, so nothing you have written is touched.</p>'
             '<div class="box"><span class="mono">Changing it yourself</span>'
             '<p>The whole program is there and you can change any part of it. A file called '
             '<code>CLAUDE.md</code> in the folder is read by Claude Code on its own, and is '
             'mostly a list of what must never be changed: none of the five checks may be '
             'weakened, widened or worked around, and the file that talks to your CRM is not '
             'to be edited. A fix that seems to need one of those loosened is the point to '
             'stop and ask.</p></div>')
    p.append(page(inner, "LinkChat &middot; When something is wrong", 12,
                  "LinkChat &middot; When something is wrong", position_bar(11, "Part 11 of 11")))

    return ('<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8">'
            '<title>LinkChat &middot; Install and Walk Through</title>'
            '<style>%s</style></head><body>%s</body></html>'
            % (STYLE, "".join(p)))


def main():
    html_path = HERE / "guide.html"
    html_path.write_text(build_html(), encoding="utf-8")
    print("  html   %s" % html_path.name)

    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists():
        edge = Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists():
        print("  FAILED  Edge not found; the series renders with headless Edge")
        return 1

    for dest in (OUT_PDF, ALSO):
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([str(edge), "--headless=new", "--disable-gpu",
                        "--no-pdf-header-footer",
                        "--print-to-pdf=%s" % dest, html_path.as_uri()],
                       capture_output=True, timeout=180)
        print("  pdf    %-52s %s" % (dest.name, "built" if dest.exists() else "FAILED"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
