"""How LinkChat is started.

    python -m engine desktop          open LinkChat as a window
    python -m engine serve            open the engine only, no window
    python -m engine serve --port N   open it somewhere else
    python -m engine crm [path]       say what LinkChat can see of your CRM, and stop
    python -m engine inbox-sync       read your LinkedIn inbox and store what is there
    python -m engine inbox-fetch-messages N   read conversation N in full, now

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
    print("no such command: %s (try: desktop, serve, crm, inbox-sync, inbox-fetch-messages)" % command)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
