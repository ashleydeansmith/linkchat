"""ops.py — the small amount of book-keeping LinkChat does for itself.

This file used to be a copy of the same file from the program LinkChat was built
out of, and it named one particular computer three times: a folder of activity
records, a signed-in browser folder, and a note it wrote into a private vault.
None of those exist on your machine, so on your machine every job that reached
this file failed, and on the machine it was copied from every job wrote into
somebody else's records. Both are wrong, and the second is worse.

So this is rewritten to hold two ideas apart, because they were tangled:

  THE CEILING THAT MATTERS IS YOURS, AND IT LIVES IN YOUR CRM.
  How many people LinkedIn will let you contact in a day is your business, and
  the count sits in `_engine/limits.py`, shared with Gather so both see one
  total. LinkChat asks it through `crm_bridge.py` before anything reaches a
  person. Nothing in this file may hold a second opinion about that number.

  READING IS NOT CONTACTING, SO IT IS COUNTED SEPARATELY AND HERE.
  Opening your own inbox and reading the pages is not an outward action. If it
  counted against the same ceiling, one sync of forty conversations would spend
  a day's allowance without writing to a single person. So reads have their own
  gentle ceiling, kept here, in your CRM folder, and they never touch yours.

What this file still does, and all it does:

  - a record of what LinkChat itself did, in your CRM      `activity.jsonl`
  - a gentle ceiling on how much reading it does in a day
  - a lock so two of LinkChat's OWN jobs cannot drive the browser at once

The lock BETWEEN PROGRAMS — LinkChat against Gather — is not here either. That
is your `_engine/browser_lock.py`, taken in `inbox/keeper.py` before this one, so
the two locks nest rather than compete.

Nothing here needs anything installed. It is the standard library only.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

# How much reading LinkChat will do in one day before it stops and says so.
# Generous, because reading your own inbox is ordinary use of the site; present,
# because a loop that has gone wrong should run out rather than run all night.
READ_CAPS = {
    "scrape":       400,    # opening a conversation and reading it
    "profile_view": 200,    # opening somebody's profile
}
READ_DAILY = 600            # everything LinkChat reads in a day, added up

# Names for the locks. They are labels, not folders: the folder a signed-in
# browser lives in is worked out in `linkedin_browser.py`, from your CRM.
PROFILE_BROWSER = "chromium-send"

MAX_LOCK_AGE_SEC = 1800     # a lock older than this is the wreckage of a crash
HB_STALE_SEC = 180          # ...unless it is being beaten, and then much sooner
POLL_SEC = 4.0


# --------------------------------------------------------------- where things go

def ops_dir() -> Path:
    """The folder LinkChat keeps its own record in — inside your CRM.

    Worked out each time rather than fixed when this file loads, because on the
    first run there is no CRM yet and a value captured then would send everything
    written afterwards to the wrong place for the rest of the session.
    """
    try:
        from engine import crm_bridge
        root = crm_bridge.find()
        if root:
            return Path(root) / "_state" / "linkchat" / "ops"
    except Exception:
        pass
    return Path.home() / ".linkchat" / "ops"


def _lock_dir() -> Path:
    return ops_dir() / "locks"


def _ledger_path() -> Path:
    return ops_dir() / "activity.jsonl"


def _ensure() -> None:
    _lock_dir().mkdir(parents=True, exist_ok=True)


# Kept as names because files brought over from the other program read them.
# They answer with wherever the CRM is at the moment they are asked.
class _Where(os.PathLike):
    def __init__(self, resolve):
        self._resolve = resolve

    def __fspath__(self):
        return str(self._resolve())

    def __getattr__(self, name):
        return getattr(self._resolve(), name)

    def __truediv__(self, other):
        return self._resolve() / other

    def __str__(self):
        return str(self._resolve())

    __repr__ = __str__


OPS_DIR = _Where(ops_dir)
LOCK_DIR = _Where(_lock_dir)
LEDGER_PATH = _Where(_ledger_path)
CONFIG_PATH = _Where(lambda: ops_dir() / "config.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_str() -> str:
    return date.today().isoformat()


# ------------------------------------------------------------------ the record

def log_action(agent: str, action: str, target: str | None = None,
               result: str = "ok", profile: str | None = None,
               detail: str | None = None) -> None:
    """Write down one thing LinkChat did, after it did it.

    This is LinkChat's own record of its own work — what it read and when — and
    it is the only thing the reading ceiling below counts. It deliberately does
    NOT add to the shared daily total in your CRM. That total is for things a
    person receives, and it is added to in `crm_bridge.py`, once, where the
    person is on the other end. Counting a read here as well would spend your
    day's allowance on your own inbox.
    """
    _ensure()
    entry = {"ts": _now_iso(), "date": _today_str(), "agent": agent,
             "action": action, "target": target, "result": result,
             "profile": profile, "detail": detail}
    try:
        with _ledger_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("[linkchat] could not write the activity record: %s" % exc,
              file=sys.stderr)


def _read_ledger() -> list[dict]:
    path = _ledger_path()
    if not path.exists():
        return []
    rows = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _today_rows(action: str | None = None, only_ok: bool = True,
                _ledger: list[dict] | None = None) -> list[dict]:
    today = _today_str()
    rows = [r for r in (_ledger if _ledger is not None else _read_ledger())
            if r.get("date") == today]
    if only_ok:
        rows = [r for r in rows if r.get("result", "ok") == "ok"]
    if action:
        rows = [r for r in rows if r.get("action") == action]
    return rows


# ------------------------------------------------------------ the read ceiling

# Anything not in this set reaches a person, and a person is your CRM's business.
# It is refused here on purpose rather than allowed with a smaller number,
# because two ceilings for one action is the fault this file exists to end.
READING = frozenset(READ_CAPS) | {"read"}


def check_budget(action: str, n: int = 1) -> tuple[bool, int, int, str]:
    """Is there room for one more? Returns (allowed, done_today, ceiling, why).

    Reading answers from LinkChat's own count. Anything else is refused here and
    told where the real answer lives, so no caller can accidentally get
    permission to contact somebody from this file.
    """
    if action not in READING:
        return (False, 0, 0,
                "%s is not something this part decides. Anything that reaches a "
                "person is asked of your CRM's shared daily limit, once, when the "
                "message is approved." % action)

    caps = dict(READ_CAPS)
    daily = READ_DAILY
    path = ops_dir() / "config.json"
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            caps.update(cfg.get("caps", {}))
            daily = int(cfg.get("global_daily", daily))
        except (OSError, ValueError, TypeError):
            pass

    today = _today_rows(only_ok=True)
    total = len(today)
    used = sum(1 for r in today if r.get("action") == action)
    cap = caps.get(action, 0)
    if cap and used + n > cap:
        return (False, used, cap,
                "LinkChat has read %d things today, which is as much reading as it "
                "does in a day. It starts again tomorrow." % used)
    if total + n > daily:
        return (False, total, daily,
                "LinkChat has read %d things today, which is as much reading as it "
                "does in a day. It starts again tomorrow." % total)
    return (True, used, cap, "ok")


def budget_status() -> dict:
    """What LinkChat has read today, for the screen."""
    today = _today_rows(only_ok=True)
    counts: dict[str, int] = {}
    for r in today:
        counts[r.get("action", "?")] = counts.get(r.get("action", "?"), 0) + 1
    return {"actions": {a: {"used": counts.get(a, 0), "cap": c}
                        for a, c in READ_CAPS.items()},
            "global": {"used": len(today), "cap": READ_DAILY},
            "note": "this counts reading only — messages answer to your CRM"}


# ------------------------------------------------------------------- the lock

def _pid_alive(pid) -> bool:
    """Is that process still running? Standard library only, and no window.

    A job that died holding the lock must not block LinkChat until somebody
    deletes a file by hand, so the lock is only trusted while its owner is.
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes
        SYNCHRONIZE = 0x00100000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(SYNCHRONIZE | 0x0400, False, pid)  # + QUERY_LIMITED
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True     # it opened, so something is there
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_file(profile: str) -> Path:
    return _lock_dir() / ("%s.lock" % profile)


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _age(meta: dict) -> float:
    try:
        taken = datetime.fromisoformat(str(meta.get("ts", "")).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 1e9
    if not taken.tzinfo:
        taken = taken.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - taken).total_seconds()


def _is_stale(meta: dict | None) -> bool:
    if not meta:
        return True
    if not _pid_alive(meta.get("pid")):
        return True
    beat = meta.get("hb")
    if beat:
        try:
            last = datetime.fromisoformat(str(beat).replace("Z", "+00:00"))
            if not last.tzinfo:
                last = last.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last).total_seconds() > HB_STALE_SEC
        except (ValueError, AttributeError):
            return True
    return _age(meta) > MAX_LOCK_AGE_SEC


def acquire_lock(profile: str, agent: str, wait_sec: float = 0.0) -> bool:
    """Take the lock for one of LinkChat's own jobs. True if it was taken."""
    _ensure()
    path = _lock_file(profile)
    deadline = time.time() + max(0.0, float(wait_sec))
    while True:
        meta = _read_lock(path)
        if meta is None or _is_stale(meta):
            if meta is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    json.dump({"profile": profile, "agent": agent,
                               "pid": os.getpid(), "ts": _now_iso()}, fh)
                return True
        elif meta.get("agent") == agent and meta.get("pid") == os.getpid():
            return True         # already ours; taking it twice is not an error
        if time.time() >= deadline:
            return False
        time.sleep(POLL_SEC)


def renew_lock(profile: str, agent: str) -> bool:
    """Say the job is still alive, so a long run is never judged as wreckage."""
    path = _lock_file(profile)
    meta = _read_lock(path)
    if not meta or meta.get("pid") != os.getpid():
        return False
    meta["hb"] = _now_iso()
    # A unique temp name per write. A fixed one is the race that cost messages
    # in the outbox: two writers open it at once and Windows refuses the second.
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    try:
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, path)      # never open the real lock file for writing
    except OSError:
        return False
    return True


def release_lock(profile: str, agent: str) -> bool:
    """Give it back. Only the holder can, so one job cannot free another's."""
    path = _lock_file(profile)
    meta = _read_lock(path)
    if not meta:
        return False
    if meta.get("pid") != os.getpid():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def held_locks() -> list[dict]:
    """Every lock LinkChat currently holds, and whether each still means anything."""
    folder = _lock_dir()
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.lock")):
        meta = _read_lock(path)
        if meta is None:
            continue
        meta["_profile"] = path.stem
        meta["_stale"] = _is_stale(meta)
        meta["_age_sec"] = int(_age(meta))
        out.append(meta)
    return out


class _LockCtx:
    """`with ops.lock(...) as got:` — got is False if it stayed busy.

    heartbeat=True keeps saying the job is still alive for as long as the lock
    is held. A long run - walking a search, sending a batch of requests - takes
    many minutes, and without this it eventually looks abandoned and another job
    reclaims the lock underneath it. Two jobs then drive one signed-in browser,
    which is the fault that does not announce itself.
    """

    def __init__(self, profile: str, agent: str, wait_sec: float = 0.0,
                 heartbeat: bool = False):
        self.profile, self.agent, self.wait_sec = profile, agent, wait_sec
        self.heartbeat = heartbeat
        self.got = False
        self._stop = None
        self._thread = None

    def __enter__(self) -> bool:
        self.got = acquire_lock(self.profile, self.agent, self.wait_sec)
        if self.got and self.heartbeat:
            import threading
            self._stop = threading.Event()

            def beat():
                while not self._stop.wait(30.0):
                    try:
                        renew_lock(self.profile, self.agent)
                    except Exception:
                        return
            self._thread = threading.Thread(target=beat, daemon=True)
            self._thread.start()
        return self.got

    def __exit__(self, *exc):
        if self._stop is not None:
            self._stop.set()
        if self.got:
            release_lock(self.profile, self.agent)
        return False


def lock(profile: str, agent: str = "linkchat", wait_sec: float = 0.0,
         heartbeat: bool = False):
    return _LockCtx(profile, agent, wait_sec, heartbeat)


@contextmanager
def held(profile: str, agent: str = "linkchat", wait_sec: float = 0.0):
    got = acquire_lock(profile, agent, wait_sec)
    try:
        yield got
    finally:
        if got:
            release_lock(profile, agent)


# ---------------------------------------------------------------- the queue

def list_queue(status: str | None = None) -> list[dict]:
    """There is no queue of work waiting to be sent, and there is not meant to be.

    The program this was copied from ran a background worker that picked jobs off
    a list and typed them into LinkedIn. LinkChat has no such worker: a message
    goes when you approve it, in front of you, and nowhere else. This answers
    with an empty list so the screen that asks gets an answer rather than an
    error, and so nothing can quietly start filling one.
    """
    return []
