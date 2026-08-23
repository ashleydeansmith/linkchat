"""LinkChat — your LinkedIn conversations, and the sequences that run off them.

Two screens. One reads your inbox. The other runs message sequences against the
people already in your CRM.

WHERE THINGS ARE KEPT. LinkChat holds no record about a person. Your people, your
event log, your daily ceiling and your hold list all stay in the CRM you built in
Sessions 0 to 2, and LinkChat reaches them through one file, `crm_bridge.py`.

What LinkChat does keep is its own working state: the shape of your sequences, who
is at which step, and a copy of your inbox so the screen is quick. That lives in
`_state/linkchat/` INSIDE your CRM rather than off in a folder of its own, so it
is backed up when your CRM is, and there is one place to look rather than two.
"""
from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

PKG_DIR = Path(__file__).resolve().parent
APP_DIR = PKG_DIR.parent

# Kept for the files brought over from LinkForge, which expect it.
_AUTOMATION = APP_DIR


def crm_root():
    """The CRM LinkChat is pointed at, or None if it has not been told yet."""
    from . import crm_bridge
    return crm_bridge.find()


def data_dir():
    """Where LinkChat keeps its own working state.

    Inside your CRM when there is one, so one backup covers everything. Beside the
    program only when LinkChat has not been pointed at a CRM yet, which is the
    first run and nothing else.
    """
    root = crm_root()
    target = (Path(root) / "_state" / "linkchat") if root else (APP_DIR / "_state")
    target.mkdir(parents=True, exist_ok=True)
    return target


class _LazyPath(os.PathLike):
    """A path that answers with wherever the CRM is at the moment it is asked.

    The files brought over from LinkForge read DATA_DIR when they load, which is
    before you have chosen a CRM. A fixed value captured at that moment would send
    everything written afterwards to the wrong folder for the rest of the session.
    """

    def __init__(self, resolve, *parts):
        self._resolve = resolve
        self._parts = parts

    def _now(self):
        p = Path(self._resolve())
        for part in self._parts:
            p = p / part
        return p

    def __fspath__(self):
        return str(self._now())

    def __truediv__(self, other):
        return _LazyPath(self._resolve, *(self._parts + (other,)))

    def __getattr__(self, name):
        return getattr(self._now(), name)

    def __str__(self):
        return str(self._now())

    def __repr__(self):
        return "LinkChat path -> %s" % self._now()


DATA_DIR = _LazyPath(data_dir)
DB_PATH = DATA_DIR / "linkchat.db"
CONFIG_PATH = DATA_DIR / "config.json"


def emit_result(lane, ok, msg, **extra):
    """One line saying how a job went, for the log pane on the screen."""
    import json
    import sys
    line = {"lane": lane, "ok": bool(ok), "msg": str(msg)}
    line.update(extra)
    sys.stdout.write("RESULT " + json.dumps(line) + "\n")
    sys.stdout.flush()


def safe_close(ctx) -> None:
    """Close a browser context without letting the closing itself raise."""
    try:
        if ctx is not None:
            ctx.close()
    except Exception:
        pass


# The do-not-contact list that came with the files brought over from LinkForge.
# It is NOT the list LinkChat obeys. The list that matters is `holds.py` in your
# own CRM, which you wrote when you installed Layer 6, and which crm_bridge.py
# checks before every message. This path exists only because the database file
# brought across expects it, and it starts empty.
RED_LIST_PATH = DATA_DIR / "unused-red-list.json"
