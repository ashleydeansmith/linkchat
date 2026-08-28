"""How LinkChat is started.

    python -m engine desktop          open LinkChat as a window
    python -m engine serve            open the engine only, no window
    python -m engine serve --port N   open it somewhere else

    python -m engine connect --commit --max N     ask people to connect
    python -m engine withdraw --commit            take back old requests
    python -m engine accept-sync                  work out who said yes
    python -m engine search --probe               look at your saved searches
    python -m engine crm [path]       say what LinkChat can see of your CRM, and stop
    python -m engine inbox-sync       read your LinkedIn inbox and store what is there
    python -m engine inbox-fetch-messages N   read conversation N in full, now
    python -m engine flow --pass --commit     walk your sequences: read replies, STAGE what is due
    python -m engine flow --staged            what is waiting for you to release
    python -m engine flow --enrol --commit    put everyone who accepted and never heard from you on the opener
    python -m engine flow                     where everybody is

The window you double-click starts `serve` for you and then opens onto it. This
is the same program either way, so anything that works in the window can be
checked from a terminal.
"""
from __future__ import annotations

import sys

PORT = 8790          # not the port the parent program uses, so both can run at once


def _arg(argv, name, fallback=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return fallback


def serve(argv):
    import uvicorn
    port = int(_arg(argv, "--port", PORT))
    host = _arg(argv, "--host", "127.0.0.1")
    from .server import app
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def desktop(argv):
    """Open LinkChat as a window. This is what the shortcut runs."""
    from .server import run_desktop
    run_desktop(port=int(_arg(argv, "--port", PORT)))
    return 0


def lane(command, argv):
    """Run one acquisition lane. Its own argv is passed through untouched."""
    import sys as _sys
    mod = {"connect": "connect", "withdraw": "withdraw",
           "accept-sync": "accept_sync", "search": "salesnav",
           "import-csv": "csv_import"}[command]
    # The lanes read sys.argv directly, so hand them their own flags with the
    # command word taken off the front.
    _sys.argv = [command] + list(argv[2:])
    import importlib
    importlib.import_module("engine." + mod).main()
    return 0


def inbox_sync(argv):
    """Read the LinkedIn inbox and store what is there. Opens pages, reads them.

    Run as its own process by the Sync button, so a long read cannot block the
    screen. It only ever reads: there is no code in LinkChat that writes into a
    conversation.
    """
    from .inbox import sync as S
    S.sync(max_deep=int(_arg(argv, "--max", 20)))
    return 0


def inbox_fetch_one(argv):
    """Read ONE conversation in full. Run when you open a conversation the
    backfill has not reached yet, so the screen shows what is really there rather
    than an empty thread that looks like silence."""
    from .inbox import sync as S
    rest = [a for a in argv[2:] if not a.startswith("-")]
    if not rest:
        print("which conversation? pass its number")
        return 2
    result = S.fetch_one(int(rest[0]))
    return 0 if result.get("ok") else 1


def flow(argv):
    """The sequence walker, shared with the parent program - on LinkChat's terms.

    HERE IT NEVER CARRIES A MESSAGE. The parent program has a setting that lets the
    engine send what is due; LinkChat's settings have no such field, so in LinkChat it
    is off and cannot be turned on. What the walker does instead, each pass:

      1. reads your inbox copy for replies and matches them against the branch the
         person is standing on (only that branch's word lists - never a later stage's)
      2. finds everybody whose next step is due
      3. writes each due message DOWN - into the engine's own record as 'staged', and
         into your CRM's review queue (Layer 6) as a message the sequence wrote

    Then it stops. The message sits on your Cockpit until you read it and press
    approve, and pressing approve carries it through the one road out - the same five
    checks as everything else, plus the sixth (a sequence cannot release its own work).
    When it has actually gone, the road tells the engine, and only then is the person
    walked on to their next step. A refused or failed carry leaves them where they were.

    A rehearsal (`--pass` without `--commit`) is refused on your real database, because
    a rehearsal walks people on as if the words had gone. Use `--shadow` for a copy.
    """
    from . import flow_run as FR
    FR.STAGER = _stage_in_your_crm
    if "--commit" in argv and "--pass" in argv:
        _link_inbox_to_people()
    import sys as _sys
    _sys.argv = ["flow"] + list(argv[2:])
    FR.main()
    return 0


def _link_inbox_to_people():
    """Tie each inbox conversation to the person it belongs to, where that is certain.

    The engine sees a reply only on a conversation linked to the person (their row in
    `leads`), and the review queue needs the conversation's address to carry an approved
    message. A conversation is linked by the profile address when the inbox recorded
    one, and by name only when exactly ONE person in your records has that name. Two
    people with the same name are left unlinked, on purpose: the wrong link would send
    one person's follow-up to the other.
    """
    from . import db
    from .inbox import db as cvdb
    try:
        cx = cvdb.connect()
    except Exception:
        return
    try:
        rows = cx.execute("SELECT id, participant_name, participant_profile_url FROM conversations "
                          "WHERE lead_id IS NULL").fetchall()
        if not rows:
            return
        with db.connect() as conn:
            by_url, by_name = {}, {}
            for l in conn.execute("SELECT id, profile_url, full_name FROM leads"):
                if l["profile_url"]:
                    by_url[l["profile_url"].rstrip("/").lower()] = l["id"]
                if l["full_name"]:
                    by_name.setdefault(" ".join(l["full_name"].split()).lower(), []).append(l["id"])
        for r in rows:
            lid = None
            url = (r["participant_profile_url"] or "").rstrip("/").lower()
            if url and url in by_url:
                lid = by_url[url]
            else:
                ids = by_name.get(" ".join((r["participant_name"] or "").split()).lower()) or []
                if len(ids) == 1:
                    lid = ids[0]
            if lid:
                cx.execute("UPDATE conversations SET lead_id=? WHERE id=? AND lead_id IS NULL", (lid, r["id"]))
        cx.commit()
    except Exception:
        pass      # linking is a convenience; a fault here must never stop the pass
    finally:
        try:
            cx.close()
        except Exception:
            pass


def _stage_in_your_crm(lead, bubbles, send_key, node_key, ref):
    """The engine has a message that is due. Put it in YOUR review queue, as the
    sequence's own work, and say nothing to anybody. Returns a note for the engine's log."""
    from . import crm_bridge
    from .inbox import db as cvdb
    bridge = crm_bridge.open_crm()
    thread_urn = ""
    try:
        cx = cvdb.connect()
        row = cx.execute("SELECT thread_urn FROM conversations WHERE lead_id=? "
                         "ORDER BY last_msg_at DESC LIMIT 1", (lead["id"],)).fetchone()
        thread_urn = (row["thread_urn"] if row else "") or ""
        cx.close()
    except Exception:
        thread_urn = ""
    body = "\n\n".join(b for b in bubbles if b)
    to = lead.get("full_name") or ""
    identifier = lead.get("profile_url") or to
    summary = "%s: %s" % (node_key, " ".join(body.split())[:80])
    bridge.propose(send_key, body, summary=summary, to=to, identifier=identifier,
                   thread_urn=thread_urn)
    return ("in your review queue" if thread_urn else
            "in your review queue - no conversation is linked to this person yet, so sync "
            "your inbox before approving or the carry has nowhere to go")


def crm(argv):
    from . import crm_bridge
    # argv[0] is python, argv[1] is "crm" — a path, if there is one, starts at 2.
    rest = [a for a in argv[2:] if not a.startswith("-")]
    return crm_bridge.main(["crm"] + rest)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    command = argv[1]
    if command == "serve":
        return serve(argv)
    if command == "desktop":
        return desktop(argv)
    if command == "crm":
        return crm(argv)
    if command == "inbox-sync":
        return inbox_sync(argv)
    if command == "flow":
        return flow(argv)

    # THE ACQUISITION LANES.
    #
    # Each runs as its own process, the way the inbox read does, because they
    # drive a browser through Playwright's blocking interface and that must
    # never happen inside the web server's own loop.
    #
    # Every one of them asks engine.safety first, which asks YOUR CRM's daily
    # ceiling - the same one Gather answers to. There is no second set of
    # counts here: LinkedIn counts the account, not the program.
    if command in ("connect", "withdraw", "accept-sync", "search", "import-csv"):
        return lane(command, argv)
    if command == "inbox-fetch-messages":
        return inbox_fetch_one(argv)
    if command in ("inbox-send", "inbox-send-media", "inbox-voice-send",
                   "inbox-audio-fetch"):
        # Named so the failure is a sentence rather than "no such command".
        #
        # Sending is not done this way. A sequence writes a message, you approve it
        # on the screen, and it goes from there — through the five checks — in the
        # browser that is already open. There is no second way out, on purpose.
        #
        # Attachments, voice notes and playing back a voice somebody sent you are
        # not built. They were half-carried over and they are named here so asking
        # for one gets a sentence rather than a crash.
        print("LinkChat does not do that. A message goes when you approve it under "
              "Sequences, and nothing else carries anything to anybody.")
        return 2
    print("no such command: %s (try: desktop, serve, crm, inbox-sync, inbox-fetch-messages, flow)" % command)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
