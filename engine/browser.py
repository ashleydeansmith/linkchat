"""browser.py — ONE persistent LinkedIn Chromium for the parent program (the LH2 model).

THE PROBLEM this solves
-----------------------
Every lane used to call `linkedin_browser.open_read_context()`, which runs
`launch_persistent_context()` — i.e. it LAUNCHED a fresh Chromium, did its work,
and CLOSED it. So a normal run was: open → scan → close → open → connect → close
→ open → message → close … That open/close/open/close churn is slow AND it looks
nothing like a human (a person keeps ONE browser open and clicks around in it).
It is very likely part of what tripped LinkedIn's throttle on 2026-06-13.

THE MODEL (how LH2 does it)
---------------------------
Keep ONE Chromium ALIVE for the whole session and navigate inside it. Here that
is a detached "keeper" process that owns the `linkedin-session` persistent profile
and exposes a CDP debugging port. Every lane CONNECTS to that already-running
browser over CDP (`connect_over_cdp`) instead of launching its own, drives the
existing page, and DISCONNECTS without closing it. The browser never churns.

  keeper (this module, --keep)  ── launches the ONE Chromium, stays alive ──┐
  lane salesnav  ─ connect_over_cdp ─ drive page ─ disconnect (don't close) ─┤  same
  lane connect   ─ connect_over_cdp ─ drive page ─ disconnect (don't close) ─┤  browser
  lane drip      ─ connect_over_cdp ─ drive page ─ disconnect (don't close) ─┘  throughout

Lanes still serialise on the `linkedin_ops` READ_LOCK (one set of hands on the
one browser at a time). The keeper is the ONLY process that opens the profile dir,
so there is no SingletonLock collision.

Safe by construction: if the keeper can't be reached, callers fall back to the old
launch-per-lane behaviour, so nothing hard-breaks.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import linkedin_browser as lb   # SESSION_DIR / viewport / locale / reaper live here

from . import DATA_DIR          # writable state dir (redirected to %LOCALAPPDATA% when frozen)
from . import platform_compat as pc   # OS abstraction (process/window/launch) — Win + macOS
from .frozen import keeper_argv

PKG_DIR = Path(__file__).resolve().parent
STATE_FILE = DATA_DIR / "browser.json"
STOP_FLAG = DATA_DIR / "browser.stop"
FRAME_FILE = DATA_DIR / "keeper_frame.jpg"   # latest live screenshot for the in-app viewer
SHOW_FLAG = DATA_DIR / "browser.show"        # present = surface the real window (for manual login)
NEEDS_LOGIN_FLAG = DATA_DIR / "browser.needslogin"   # keeper sets this on a login/checkpoint page
LOGGED_IN_FLAG = DATA_DIR / "browser.loggedin"       # keeper sets when feed/session is live
HEARTBEAT_FILE = DATA_DIR / "keeper.heartbeat"       # active driving lane touches this while attached
PORT = 9334                       # LinkChat's own door onto its own browser.
#
# THIS NUMBER HAS TO BE DIFFERENT FROM EVERY OTHER PROGRAM ON THE MACHINE.
# It was the same as the one the program LinkChat was cut out of, and both were
# running on one particular computer at once. LinkChat asked for "the browser at that
# door" and got the OTHER program's browser, signed into the OTHER account, and
# drove it - opened conversations in it, and would have typed into it. It only
# shows up on a machine where both are installed, which is exactly the machine
# the live install is demonstrated from.

# Keeper-liveness tuning (the 2026-07-06 keeper-stability fix). A busy keeper answers its
# CDP port SLOWLY while driving a heavy page, so a dead-looking port alone must NOT trigger
# a reap — that force-killed the in-use browser mid-operation (connect 45% / withdraw 34%).
# The driving lane refreshes HEARTBEAT_FILE on a background thread; liveness = (CDP probe
# responds) OR (heartbeat fresh). We only reap when BOTH are stale past a grace window.
HEARTBEAT_INTERVAL = 3.0   # the driving lane re-touches the heartbeat this often
HEARTBEAT_TTL = 15.0       # heartbeat counts as "fresh" (a lane is driving) within this many seconds
KEEPER_GRACE_SEC = 20.0    # in-use + unresponsive port: re-probe this long before giving up (never kill)


def _cookies_have_li_at(cookies_path: Path) -> bool:
    import sqlite3
    import shutil
    import tempfile

    def _query(path: Path) -> bool:
        uri = f"file:{path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = con.execute(
                "SELECT 1 FROM cookies WHERE name='li_at' "
                "AND (host_key LIKE '%.linkedin.com' OR host_key LIKE 'linkedin.com') LIMIT 1"
            ).fetchone()
            return bool(row)
        finally:
            con.close()

    try:
        return _query(cookies_path)
    except Exception:
        pass
    # Chrome locks the DB while running — copy to a temp file and read that.
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".cookies") as tmp:
            tmp_path = Path(tmp.name)
        try:
            shutil.copy2(cookies_path, tmp_path)
            return _query(tmp_path)
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    except Exception:
        return False


def linkedin_logged_in() -> bool:
    """True only when the keeper profile has a LinkedIn auth cookie (li_at).

    Chromium creates an empty Cookies DB on first launch — checking file existence alone
    falsely hides the Connect prompt on a fresh install."""
    if LOGGED_IN_FLAG.exists():
        return True
    try:
        sd = lb.SESSION_DIR
        for cookies_path in (sd / "Default" / "Network" / "Cookies", sd / "Default" / "Cookies"):
            if not cookies_path.is_file() or cookies_path.stat().st_size < 512:
                continue
            if _cookies_have_li_at(cookies_path):
                return True
    except Exception:
        pass
    return False
CDP_URL = f"http://127.0.0.1:{PORT}"

# Stealth: strip the automation tells LinkedIn reads on page load. The big one is
# navigator.webdriver — Playwright sets it true via the default --enable-automation flag.
# We remove it at source (drop --enable-automation + --disable-blink-features=
# AutomationControlled) and belt-and-braces in JS before any page script runs. This is a
# real headed Chrome profile, so most other tells are already absent; the patch is kept
# MINIMAL on purpose — faking plugins/mimetypes with wrong values is itself a detectable
# inconsistency, so we only correct the things that are genuinely wrong under automation.
_STEALTH_JS = """
// navigator.webdriver -> undefined (a normal browser has no such own-property)
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// window.chrome present (real Chrome exposes it; some automated contexts don't)
if (!window.chrome) { window.chrome = { runtime: {} }; }
// permissions.query: kill the classic 'notifications denied while Notification granted' mismatch
try {
  const _q = window.navigator.permissions && window.navigator.permissions.query;
  if (_q) {
    window.navigator.permissions.query = (p) => (
      p && p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _q(p)
    );
  }
} catch (e) {}
"""


# ---------------------------------------------------------------------------
# State + liveness
# ---------------------------------------------------------------------------

def _state_write(pid: int) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(
        {"pid": pid, "port": PORT, "started": time.time()}), encoding="utf-8")


def _state_read() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    return pc.pid_alive(pid)


def _port_alive(timeout: float = 1.5, retries: int = 1) -> bool:
    """The keeper is up iff its CDP endpoint answers. `retries`>1 re-checks before
    concluding it's dead, so a BUSY keeper (slow to answer while driving a lane) is not
    misjudged dead — that misjudgement is what spawned duplicate keepers (2026-06-15)."""
    for i in range(max(1, retries)):
        try:
            with urllib.request.urlopen(CDP_URL + "/json/version", timeout=timeout) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        if i + 1 < retries:
            time.sleep(0.6)
    return False


_KEEPER_CACHE = {"t": 0.0, "up": False}


def keeper_running(max_age: float = 2.0) -> bool:
    """True iff the keeper's CDP endpoint is live — probed with a SHORT timeout and cached briefly.

    /inbox/status calls this on every inbox load, every conversation open, and every CRM action,
    so a DOWN keeper used to cost a full ~1.5s socket timeout EACH time — the "inbox loads slowly"
    bug (status was 1.5s; the conversation list itself is ~30ms). A live local CDP answers in a few
    ms, so 0.3s is ample, and the short cache stops rapid polling (the sync loop, click-throughs)
    from re-probing. STATUS READ ONLY — the keeper SPAWN decision uses _port_alive(retries=4)
    directly (ensure_keeper), so it is unaffected by this timeout/cache."""
    now = time.time()
    if now - _KEEPER_CACHE["t"] < max_age:
        return _KEEPER_CACHE["up"]
    up = _port_alive(timeout=0.3)
    _KEEPER_CACHE.update(t=now, up=up)
    return up


def _set_keeper_cache(up: bool) -> None:
    """Record a known keeper state after a start/stop so keeper_running() reflects it immediately."""
    _KEEPER_CACHE.update(t=time.time(), up=up)


# ---------------------------------------------------------------------------
# Keeper heartbeat + in-use guard (2026-07-06 keeper-stability fix)
# ---------------------------------------------------------------------------
# The dominant engine-reliability failure was the self-heal reaper force-killing a keeper
# that a connect/withdraw lane was mid-operation on: a concurrent ensure_keeper() (the
# :8770 inbox poll, or another lane) lost the 0.3s CDP port race, concluded "dead", and
# reaped the in-use browser. The lane's next Playwright call then threw
# "Target page, context or browser has been closed". READ_LOCK serialised DRIVING but not
# the process vs the reaper. The fix: an attached lane refreshes a heartbeat file on a
# background thread, and the reaper treats (heartbeat fresh) OR (READ_LOCK held by ANOTHER
# live process) as "in use — do not kill", combining that with the CDP probe rather than
# replacing it. A lane that dies takes its heartbeat thread + lock with it, so both signals
# go stale and the keeper becomes reapable again (no deadlock).

_HB_LOCK = threading.Lock()
_HB_STOP: "threading.Event | None" = None
_HB_THREAD: "threading.Thread | None" = None


def _write_heartbeat() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_FILE.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def _start_heartbeat() -> None:
    """Begin refreshing the keeper heartbeat on a background daemon thread — called when a
    lane attaches (connect()). Idempotent per process (a second attach reuses the thread).
    The heartbeat is written IMMEDIATELY (before the first tick) so the just-attached lane
    reads as in-use at once. The thread dies with the process, so a crashed lane's heartbeat
    ages out within HEARTBEAT_TTL and the keeper becomes reapable — this cannot deadlock."""
    global _HB_STOP, _HB_THREAD
    with _HB_LOCK:
        _write_heartbeat()
        if _HB_THREAD is not None and _HB_THREAD.is_alive():
            return
        _HB_STOP = threading.Event()
        stop = _HB_STOP

        def _loop(stop=stop):
            while not stop.wait(HEARTBEAT_INTERVAL):
                _write_heartbeat()

        _HB_THREAD = threading.Thread(target=_loop, name="lf-keeper-heartbeat", daemon=True)
        _HB_THREAD.start()


def _stop_heartbeat() -> None:
    """Stop refreshing the heartbeat (lane detached). The last timestamp simply ages out
    past HEARTBEAT_TTL, so a clean release also frees the keeper for a later reap."""
    global _HB_STOP, _HB_THREAD
    with _HB_LOCK:
        if _HB_STOP is not None:
            _HB_STOP.set()
        _HB_STOP = None
        _HB_THREAD = None


def _heartbeat_fresh(ttl: float = HEARTBEAT_TTL) -> bool:
    """True iff HEARTBEAT_FILE was touched within `ttl` seconds — i.e. a lane is actively
    driving the keeper right now (its background thread is alive). This is what lets a BUSY
    keeper (slow CDP port) survive a concurrent liveness check instead of being reaped."""
    try:
        epoch = float(HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return (time.time() - epoch) < ttl


def _read_lock_held_by_other() -> bool:
    """True iff the LinkedIn READ_LOCK is held by a LIVE owner that is NOT this process —
    i.e. ANOTHER lane is driving the keeper, so we must never reap it. The lock held by
    OURSELVES doesn't count: when WE are the one calling ensure_keeper (to first-start or
    reattach), our own lock must not block our own spawn. A lane that died holding the lock
    reads as stale (same staleness rules as the lock), so this never deadlocks the reaper."""
    try:
        from engine import ops as _ops
        import linkedin_browser as _lb
        me = os.getpid()
        for lk in _ops.held_locks():
            if (lk.get("_profile") == _lb.READ_LOCK and not lk.get("_stale")
                    and lk.get("pid") not in (me, None)):
                return True
    except Exception:
        pass
    return False


def _keeper_in_use() -> bool:
    """A lane is actively driving the keeper iff its heartbeat is fresh OR another live
    process holds the READ_LOCK. Either signal means DO NOT REAP — the dead-looking CDP
    port is a busy keeper answering slowly, not a dead one."""
    return _heartbeat_fresh() or _read_lock_held_by_other()


def status() -> dict:
    st = _state_read()
    return {"running": keeper_running(), "pid": st.get("pid"),
            "pid_alive": _pid_alive(st.get("pid", 0)), "cdp": CDP_URL,
            "state_file": str(STATE_FILE)}


# ---------------------------------------------------------------------------
# Start / stop
# ---------------------------------------------------------------------------

def _list_keeper_pids() -> list[int]:
    """PIDs of every running keeper process (command line carries '--keep'). Matches DEV
    (`-m engine.browser --keep`) and FROZEN (`the parent program.exe browser --keep`) alike, so
    reap/single-instance still works frozen. Now psutil-based (cross-platform)."""
    return pc.list_keeper_pids()


def _listening_pid(port: int) -> int | None:
    """PID of the process LISTENING on `port` (a Chromium child of the real keeper)."""
    return pc.pid_on_port(port)


def _parent_map() -> dict[int, int]:
    """pid -> parent-pid for every process (one pass), for ancestor walks."""
    return pc.parent_map()


def _port_owner_keeper_pid() -> int | None:
    """The python keeper PID that owns the live CDP port (the ONE real keeper). The
    process listening on PORT is a Chromium descendant, so walk its parent chain up to
    the first ancestor that is an actual `--keep` keeper. None if no live keeper."""
    lp = _listening_pid(PORT)
    if not lp:
        return None
    keepers = set(_list_keeper_pids())
    pm = _parent_map()
    cur, seen = lp, set()
    for _ in range(16):
        if cur in keepers:
            return cur
        if cur in seen or not cur:
            break
        seen.add(cur)
        cur = pm.get(cur, 0)
    return None


def reap_keepers(except_pid: int | None = None) -> list[int]:
    """Force-kill every keeper process (and its Chromium child tree via /T) EXCEPT
    `except_pid` and ourselves. Returns the PIDs reaped. The fix for keeper accumulation
    (12 keepers / 11 Chromiums observed 2026-06-15 — duplicate-spawn with no reaping)."""
    me = os.getpid()
    killed: list[int] = []
    for pid in _list_keeper_pids():
        if pid in (except_pid, me):
            continue
        try:
            pc.kill_tree(pid)          # terminate the keeper + its Chromium child tree
            killed.append(pid)
        except Exception:
            pass
    return killed


def ensure_keeper(wait_sec: float = 35.0, force: bool = False) -> str | None:
    """Return the CDP url of a LIVE keeper, starting EXACTLY ONE if needed. None on failure.

    Two guards added 2026-06-15 to stop keeper accumulation:
      1. reuse-with-retry — a busy keeper that's slow to answer is NOT misjudged dead
         (the misjudgement spawned duplicates);
      2. spawn behind a lock + reap orphans — two lanes can't both launch, and any
         leftover keepers/Chromiums (which hold the profile SingletonLock and would make
         a fresh launch half-attach) are reaped before starting one.

    Third guard added 2026-07-06 (keeper-stability): never reap a keeper that a lane is
    DRIVING. Before the reap, if a keeper process exists AND a lane is in use (heartbeat
    fresh OR another live process holds READ_LOCK), grace-probe instead of killing —
    returning the CDP url if the busy keeper answers, else None ("busy, retry"). This is
    the fix for the connect/withdraw browser-closed cascade. `force=True` (the reattach
    path, which KNOWS the browser just died) bypasses this in-use guard so a lane can
    respawn past its own stale lock/heartbeat.
    """
    if _port_alive(retries=4):
        _set_keeper_cache(True)
        return CDP_URL

    # No live keeper. Serialise the spawn so concurrent lanes can't both launch one.
    ops = got = None
    try:
        from engine import ops as _ops
        ops = _ops
        got = ops.acquire_lock("keeper-spawn", "linkchat-browser", wait_sec=45)
    except Exception:
        ops, got = None, True   # lock unavailable — proceed best-effort
    try:
        if _port_alive(retries=2):
            _set_keeper_cache(True)
            return CDP_URL       # another lane won the race and started it
        # IN-USE GUARD (2026-07-06): a keeper PROCESS exists and a lane is driving it, but
        # its CDP port answered slow/dead. Do NOT reap — a busy keeper answers slowly, and
        # killing it here is the dominant browser-closed cascade. Grace-probe; hand back the
        # url if it recovers, else surface busy (None) so the caller retries. `force` skips
        # this (the reattach path knows the browser is truly dead).
        if not force and _list_keeper_pids() and _keeper_in_use():
            grace_deadline = time.time() + KEEPER_GRACE_SEC
            while time.time() < grace_deadline:
                if _port_alive(retries=2):
                    _set_keeper_cache(True)
                    return CDP_URL
                if not (_list_keeper_pids() and _keeper_in_use()):
                    break        # keeper gone OR lane released -> fall through to reap+spawn
                time.sleep(1.0)
            if _port_alive(retries=2):
                _set_keeper_cache(True)
                return CDP_URL
            if _list_keeper_pids() and _keeper_in_use():
                # A live lane is still driving an existing keeper whose port is unresponsive.
                # Refuse to kill it (that IS the bug). Surface busy; the caller retries.
                _set_keeper_cache(False)
                return None
        # Clean the slate: orphan keepers hold the session profile open and would make a
        # fresh launch half-attach. Port is dead so nothing is being driven — safe to reap.
        reap_keepers()
        # reap_keepers() is a process-TREE kill, but the keeper's REAL Chrome (channel="chrome")
        # REPARENTS away from the keeper python process, so the tree kill MISSES it: it stays alive
        # holding the linkedin-session SingletonLock and makes the fresh launch half-attach, which
        # loops as "many Chrome windows + endless flashing + can't log in" (laptop beta, 2026-06-26).
        # Reap the orphaned profile-holder by profile match (never the user's personal Chrome) and
        # clear any stale Singleton* locks a force-kill leaves behind — the SAME cleanup stop_keeper()
        # already does, now applied on the SPAWN/connect path so a stuck profile self-heals without
        # the user having to hit Stop first.
        try:
            reap_keeper_chrome()
        except Exception:
            pass
        try:
            lb.reap_playwright_chromium()
        except Exception:
            pass
        try:
            STOP_FLAG.unlink()
        except Exception:
            pass
        try:
            subprocess.Popen(
                keeper_argv(),                             # frozen: [exe, browser, --keep]; dev: [pythonw, -m, engine.browser, --keep]
                cwd=str(PKG_DIR.parent),                   # dev: so `-m engine.browser` resolves; ignored when frozen
                close_fds=True,
                **pc.detached_no_window_popen_kwargs(),    # win: DETACHED|NO_WINDOW; posix: start_new_session
            )
        except Exception as e:  # noqa: BLE001
            print(f"[keeper] failed to spawn: {e}", file=sys.stderr)
            return None
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if _port_alive():
                _set_keeper_cache(True)
                return CDP_URL
            time.sleep(0.7)
        return None
    finally:
        if ops is not None and got:
            try:
                ops.release_lock("keeper-spawn", "linkchat-browser")
            except Exception:
                pass


def reap_keeper_chrome() -> int:
    """Kill the KEEPER's Chrome/Chromium ONLY - identified by the LinkedIn-session profile dir in
    its command line, never the user's personal Chrome (which uses a different profile). Real
    Chrome launched via channel='chrome' REPARENTS away from the keeper process, so a plain
    process-tree kill misses it and leaves orphaned, input-FROZEN windows the user can't even
    close with the X. Matching on the profile catches it safely, leaving personal browsing
    untouched (the 2026-06-02 'never kill browsers by name' rule)."""
    import psutil
    needle = lb.SESSION_DIR.name.lower()   # 'linkedin-session'
    killed = 0
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if "chrom" not in (p.info.get("name") or "").lower():
                continue
            if needle in " ".join(p.cmdline()).lower():
                pc.kill_tree(p.info["pid"])
                killed += 1
        except Exception:
            pass
    return killed


def stop_keeper() -> None:
    """Ask the keeper to close gracefully (stop-flag), then force-reap as a backstop."""
    _set_keeper_cache(False)   # reflect 'down' at once so status doesn't lag the stop
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        STOP_FLAG.write_text("stop", encoding="utf-8")
    except Exception:
        pass
    # give the keeper a moment to close its context cleanly
    for _ in range(10):
        if not _port_alive():
            break
        time.sleep(0.5)
    st = _state_read()
    pid = st.get("pid")
    if pid and _pid_alive(pid):
        try:
            pc.kill_tree(pid)          # graceful terminate + SIGKILL backstop on the tree
        except Exception:
            pass
    # stop means stop ALL — reap any other keeper processes (there should never be more
    # than one; this clears strays left by the old duplicate-spawn bug).
    reap_keepers()
    try:
        lb.reap_playwright_chromium()
    except Exception:
        pass
    try:
        reap_keeper_chrome()   # the keeper's REAL Chrome (channel='chrome') reparents away from
    except Exception:          # the keeper process tree - reap it by profile, safely
        pass
    for f in (STATE_FILE, STOP_FLAG):
        try:
            f.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Lane-side connection (the drop-in for open_read_context)
# ---------------------------------------------------------------------------

class BrowserBusy(RuntimeError):
    """The shared browser stayed busy past the wait ceiling — a genuinely wedged queue."""


@contextmanager
def browser_job(agent: str, wait_sec: float = 3600, ensure: bool = True):
    """The queue primitive: WAIT your turn on the shared keeper, then run.

    Instead of failing on 'busy' (the old per-lane behaviour behind today's collisions), a
    lane WAITS up to `wait_sec` for the READ_LOCK — "you're next, I'll run you". While it holds
    the lock a daemon timer heartbeats it every 30s (ops.renew_lock), so a healthy long run
    (a ~45-min connect walk) is never judged stale and stolen, while a job that dies stops
    beating and is reclaimed within ~3 min. `ensure` brings the keeper up first, so a dead
    keeper auto-recovers instead of failing the job (the 2026-07-23 Cockpit-send failure).

    Yields True. Raises BrowserBusy only if the wait ceiling elapses (a wedged queue).
    NOTE: additive primitive — lanes are migrated onto it one at a time; until a lane uses it
    the lock stays legacy (30-min age rule), so migration is safe lane-by-lane."""
    from . import ops
    if not ops.acquire_lock(lb.READ_LOCK, agent, wait_sec=wait_sec):
        raise BrowserBusy(f"shared browser still busy after {wait_sec:.0f}s")
    ops.renew_lock(lb.READ_LOCK, agent)     # mark heartbeated -> tight staleness from now on
    _stop = threading.Event()

    def _beat():
        while not _stop.wait(30):
            ops.renew_lock(lb.READ_LOCK, agent)
    _hb = threading.Thread(target=_beat, name=f"browser-hb-{agent}", daemon=True)
    _hb.start()
    try:
        if ensure:
            ensure_keeper()
        yield True
    finally:
        _stop.set()
        ops.release_lock(lb.READ_LOCK, agent)


def connect(pw):
    """Attach to the ONE keeper browser over CDP and return its live context.
    The returned context is marked shared so `safe_close` leaves the browser ALIVE.
    Returns None if no keeper could be reached (caller then falls back to launch)."""
    url = ensure_keeper()
    if not url:
        return None
    try:
        browser = pw.chromium.connect_over_cdp(url)
    except Exception:
        return None
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    try:
        ctx._lf_shared = True          # read by safe_close -> do NOT close
        ctx._lf_browser = browser
    except Exception:
        _SHARED_IDS.add(id(ctx))       # fallback if attributes are rejected
    # a lane is now attached and about to drive — refresh the keeper heartbeat so a
    # concurrent ensure_keeper() (the :8770 poll, another lane) never misjudges this
    # busy keeper dead and reaps it out from under us (the keeper-stability fix).
    try:
        _start_heartbeat()
    except Exception:
        pass
    # a lane is about to drive the browser — freeze human input so it can't be disturbed
    try:
        lock_window()
    except Exception:
        pass
    return ctx


_SHARED_IDS: set[int] = set()


def is_shared(ctx) -> bool:
    return bool(getattr(ctx, "_lf_shared", False)) or id(ctx) in _SHARED_IDS


def release(ctx) -> None:
    """Detach from the keeper WITHOUT closing it (the whole point). Disconnecting the
    CDP connection leaves the keeper's Chromium running for the next lane."""
    # lane detaching — stop refreshing the heartbeat so the keeper is reapable once idle
    try:
        _stop_heartbeat()
    except Exception:
        pass
    browser = getattr(ctx, "_lf_browser", None)
    if browser is not None:
        try:
            browser.close()    # closes only THIS CDP CONNECTION, not the keeper process
        except Exception:
            pass
    # a lane just finished driving — release the human lock (unless a manual lock is set)
    try:
        unlock_window()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Keeper-death recovery for batch lanes (2026-07-06 keeper-stability fix)
# ---------------------------------------------------------------------------
# Batch ops (connect/withdraw/drip/inmail) open ONE context and drive the whole batch. If
# the keeper dies mid-batch there was no reattach, so every remaining lead failed with the
# same "Target … has been closed" — one death cascaded the run (withdraw was 98.8%
# browser-closed for exactly this reason). These helpers let a lane DETECT that death,
# reattach a fresh context, and retry the CURRENT lead — mirroring the death-tolerance the
# sync path already has via conversations/keeper.py K.drive().

_CLOSED_SIGNATURES = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "target closed",
    "connection closed",
    "websocket",  # CDP transport dropped when the keeper process died
)


def is_browser_closed_error(exc) -> bool:
    """True if `exc` is the keeper-death signature — the browser/page/context being closed
    out from under a driving lane. This (not a selector timeout) is what the per-lead
    reattach+retry recovers from; other errors are left to the caller's normal handling."""
    s = str(exc).lower()
    return any(sig in s for sig in _CLOSED_SIGNATURES)


def reattach(pw, ctx=None):
    """Recover from a mid-batch keeper death: drop the dead context, FORCE a fresh keeper
    (the lane KNOWS the browser died, so bypass the in-use guard — its own stale lock/
    heartbeat would otherwise read 'in use'), and return a new live shared context. Returns
    None if a keeper can't be brought back (the caller then logs the lead failed as before).
    Only used by the batch ops' per-lead retry — the sync path is untouched."""
    if ctx is not None:
        try:
            release(ctx)
        except Exception:
            pass
    _stop_heartbeat()
    if not ensure_keeper(force=True):
        return None
    try:
        return connect(pw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Window lock — freeze HUMAN input to the keeper window while a lane drives it
# ---------------------------------------------------------------------------
# The keeper shows ONE real Chromium window. Lanes serialise against EACH OTHER on the
# ops lock, but nothing stops the HUMAN clicking into that same window mid-lane and
# breaking the automation (a cause of the 2026-06-13 TargetClosed/throttle messes). We
# freeze human input by disabling the OS window (win32 EnableWindow False): the window
# stays VISIBLE so you can watch, but ignores mouse/keyboard. CDP-driven automation is
# UNAFFECTED — Playwright injects input straight into the renderer, not via the OS window
# message queue. A "manual" lock (the app's button) stays on until cleared by hand; an
# AUTO lock (a running lane) clears itself when the lane releases.

LOCKED_FLAG = DATA_DIR / "browser.locked"
MANUAL_LOCK_FLAG = DATA_DIR / "browser.lock.manual"


def _descendant_pids(root_pid: int) -> set[int]:
    """Every pid whose ancestry chains up to root_pid (plus root_pid itself)."""
    return pc.descendant_pids(root_pid)


def _set_enabled(enabled: bool) -> int:
    """Freeze/unfreeze human input to the keeper's windows. Windows: win32 EnableWindow.
    macOS/Linux: no-op (0) — the on-page CSS overlay is the visible 'locked' signal."""
    keeper_pid = _state_read().get("pid")
    if not keeper_pid:
        return 0
    return pc.set_keeper_input_enabled(int(keeper_pid), enabled)


# The on-page lock overlay, applied as `page.evaluate(_OVERLAY_JS, locked)` by the keeper's
# own reconcile loop (NOT localStorage — LinkedIn silently drops localStorage writes). It's
# idempotent and pointer-events:none, so it's purely visual and never blocks a running lane.
_OVERLAY_JS = r"""
(locked) => {
  var ID = 'lf-lock-overlay';
  var el = document.getElementById(ID);
  if (locked && !el && (document.body || document.documentElement)) {
    el = document.createElement('div'); el.id = ID;
    // Light GREY wash (no blur) so the page stays fully readable — you can watch the parent program
    // type and click — plus a small corner toast. Click-through (pointer-events:none).
    el.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;'
      + 'background:rgba(64,72,72,.22);font-family:Inter,Segoe UI,system-ui,sans-serif';
    el.innerHTML = '<div style="position:absolute;right:20px;bottom:20px;display:flex;align-items:center;gap:11px;'
      + 'max-width:290px;background:rgba(11,110,108,.97);color:#fff;padding:13px 16px;border-radius:13px;'
      + 'box-shadow:0 10px 30px rgba(0,0,0,.35)">'
      + '<div style="font-size:23px;line-height:1">&#128274;</div>'
      + '<div><div style="font-size:14px;font-weight:700;letter-spacing:-.01em">the parent program is working</div>'
      + '<div style="font-size:12px;opacity:.85;margin-top:2px;line-height:1.4">Browser locked &middot; unlock in the app to take over</div>'
      + '</div></div>';
    (document.body || document.documentElement).appendChild(el);
  } else if (!locked && el) { el.remove(); }
}
"""


def lock_window(manual: bool = False) -> int:
    """Freeze human input to the keeper window(s). Returns # windows frozen."""
    n = _set_enabled(False)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if n:                       # only record a lock if a window was actually frozen
        try:
            LOCKED_FLAG.write_text("locked", encoding="utf-8")
        except Exception:
            pass
        if manual:
            try:
                MANUAL_LOCK_FLAG.write_text("manual", encoding="utf-8")
            except Exception:
                pass
    return n   # the keeper's reconcile loop paints the overlay from LOCKED_FLAG


def unlock_window(manual: bool = False) -> int:
    """Re-enable human input. An AUTO unlock (manual=False) is IGNORED while a manual
    lock is in force, so a finishing lane never unlocks a window you locked by hand."""
    if MANUAL_LOCK_FLAG.exists() and not manual:
        return 0
    n = _set_enabled(True)
    for f in (LOCKED_FLAG, MANUAL_LOCK_FLAG):
        try:
            f.unlink()
        except Exception:
            pass
    return n   # the keeper's reconcile loop clears the overlay when LOCKED_FLAG is gone


def lock_state() -> dict:
    # cheap: flag files only (the app polls this on every refresh — no process enum here)
    return {"locked": LOCKED_FLAG.exists(), "manual": MANUAL_LOCK_FLAG.exists()}


# ---------------------------------------------------------------------------
# Login / OAuth URL detection — used by the keeper visibility loop so we never
# hide the window mid sign-in (Google/Microsoft popups leave linkedin.com).
# ---------------------------------------------------------------------------

_LOGIN_URL_MARKERS = (
    "/login", "/checkpoint", "/uas/", "authwall",
    "accounts.google.com", "google.com/o/oauth", "google.com/signin",
    "consent.google.com", "google.com/gsi", "myaccount.google.com",
    "login.microsoftonline.com", "login.live.com",
    "linkedin.com/oauth", "linkedin.com/uas",
)

_LOGGED_IN_URL_MARKERS = (
    "linkedin.com/feed", "linkedin.com/in/", "linkedin.com/mynetwork",
    "linkedin.com/notifications", "linkedin.com/messaging", "linkedin.com/jobs",
)


def _url_needs_login(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _LOGIN_URL_MARKERS)


def _context_needs_login(ctx) -> bool:
    """True if ANY open page/tab/popup is on a login or OAuth wall."""
    try:
        for pg in list(ctx.pages):
            try:
                if _url_needs_login(pg.url):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _url_logged_in(url: str) -> bool:
    u = (url or "").lower()
    if _url_needs_login(u):
        return False
    return any(m in u for m in _LOGGED_IN_URL_MARKERS)


def _context_logged_in(ctx) -> bool:
    """True when a keeper page has landed on the authenticated LinkedIn app."""
    try:
        for pg in list(ctx.pages):
            try:
                if _url_logged_in(pg.url):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _keeper_chrome_pids() -> list[int]:
    """Chrome PIDs whose windows we show/hide. Real Chrome reparents away from the
    keeper's child tree, so the CDP port owner is the authoritative browser PID."""
    pids: list[int] = []
    port_pid = _pid_on_port(PORT)
    if port_pid:
        pids.append(port_pid)
    for p in _descendant_chrome_pids(os.getpid()):
        if p not in pids:
            pids.append(p)
    return pids


# ---------------------------------------------------------------------------
# Keeper process entry point
# ---------------------------------------------------------------------------

def _keeper_main() -> None:
    """Launch the ONE persistent Chromium with a CDP port and keep it alive until a
    stop-flag appears. This process holds the linkedin-session profile open."""
    # STEALTH: prefer patchright (patches the CDP/Runtime automation leaks Playwright exposes)
    # over vanilla Playwright; fall back if it isn't installed.
    try:
        from patchright.sync_api import sync_playwright
        _stealth_driver = "patchright"
    except Exception:
        from playwright.sync_api import sync_playwright
        _stealth_driver = "playwright"
    try:
        STOP_FLAG.unlink()
    except Exception:
        pass
    lb.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        _kw = dict(
            headless=False,
            # Enable Chromium's sandbox. Playwright/patchright default it OFF (adds --no-sandbox),
            # which triggers Chrome's "unsupported command-line flag: --no-sandbox — stability and
            # security will suffer" warning bar. A normal user desktop runs sandboxed fine, so
            # turning it ON removes the banner AND is more secure + more normal-looking (stealth).
            chromium_sandbox=True,
            viewport=lb._VIEWPORT,
            locale=lb._LOCALE,
            args=[
                f"--remote-debugging-port={PORT}",
                "--disable-blink-features=AutomationControlled",
                # keep the automation running full-speed even when the window is
                # locked/occluded/minimised (no background throttling of timers/renderer)
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                # Park the window OFF-SCREEN so it never pops up — the only view of the session is
                # the in-app Live panel (screenshots still render off-screen). show_window() brings
                # it back on-screen when a manual step (login / checkpoint) needs the real window.
                "--window-position=-32000,-32000",
                "--window-size=1280,900",
            ],
            ignore_default_args=["--enable-automation"],
        )
        # channel="chrome" runs the REAL installed Chrome (genuine fingerprint — harder for LinkedIn
        # to flag). BUT on macOS, where the tester's everyday browser is often Chrome TOO, driving
        # the real Chrome collides with their personal Chrome (shared processes, windows, automation
        # flags) and makes their browser buggy. So on macOS use the ISOLATED bundled Chromium — it's
        # a different binary from Google Chrome.app, so the parent program never touches the personal browser
        # (the browser-isolation rule). Real Chrome stays the stealth default on Windows.
        try:
            if pc.IS_MAC:
                ctx = pw.chromium.launch_persistent_context(str(lb.SESSION_DIR), **_kw)
                print(f"[keeper] stealth: {_stealth_driver} + bundled Chromium (isolated — macOS)")
            else:
                ctx = pw.chromium.launch_persistent_context(str(lb.SESSION_DIR), channel="chrome", **_kw)
                print(f"[keeper] stealth: {_stealth_driver} + real Chrome")
        except Exception as e:  # noqa: BLE001
            print(f"[keeper] preferred channel unavailable ({e}); using bundled Chromium ({_stealth_driver})")
            ctx = pw.chromium.launch_persistent_context(str(lb.SESSION_DIR), **_kw)
        # stealth: apply to every page in this context BEFORE we navigate anywhere
        try:
            ctx.add_init_script(_STEALTH_JS)
        except Exception:
            pass
        # OAuth popups are new pages/windows — surface once when they appear, not on a timer.
        try:
            ctx.on("page", lambda _pg: _surface_keeper_windows())
        except Exception:
            pass
        # always have a page parked on the feed so lanes have something to drive
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # macOS: the off-screen --window-position park is ignored and the win32 hide path is a
        # no-op, so the keeper Chrome would sit on-screen flashing/mis-sized. Drive it via CDP
        # (Browser.setWindowBounds) instead — set up the session NOW so the initial hide below
        # and every _set_all_visibility call from this keeper loop can minimise/restore it.
        want_show = SHOW_FLAG.exists()
        if pc.IS_MAC:
            global _MAC_CDP, _MAC_WIN_ID
            try:
                _MAC_CDP = ctx.new_cdp_session(page)
                _MAC_WIN_ID = _MAC_CDP.send("Browser.getWindowForTarget")["windowId"]
            except Exception:
                _MAC_CDP, _MAC_WIN_ID = None, None
            # macOS has neither the off-screen --window-position park NOR the win32 hide path,
            # and windows_for_pid() returns [] here so the win32 initial-hide loop below never
            # fires — the window used to sit on-screen flashing until the reconcile loop caught
            # it seconds later. Hide it via CDP the INSTANT it exists, BEFORE navigating, so a
            # logged-in session never shows its window. Leave it on-screen if a login is pending.
            _mac_window_state(want_show)
        else:
            # Windows: hide the window the INSTANT it exists — BEFORE navigating — so it never
            # lingers in the taskbar. Fast process-tree scan (no netstat), tight poll. Targets
            # only OUR chrome.
            for _ in range(60):                 # up to ~3.6s, but breaks the moment it's hidden
                chrome_pids = _keeper_chrome_pids()
                if chrome_pids and any(_windows_for_pid(p) for p in chrome_pids):
                    try:
                        _set_all_visibility(chrome_pids, want_show)
                    except Exception:
                        pass
                    break
                time.sleep(0.06)
        try:
            page.goto("https://www.linkedin.com/feed/",
                      wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass
        _state_write(os.getpid())
        had_show_flag = want_show
        known_chrome_pids: set[int] = set()
        tick = 0
        try:
            while not STOP_FLAG.exists():
                # Reconcile the lock overlay against LOCKED_FLAG on EVERY page each tick — this
                # survives navigation and needs no in-page flag (LinkedIn drops localStorage).
                # pointer-events:none, so it never blocks a running lane. Best-effort per page.
                locked = LOCKED_FLAG.exists()
                for _pg in list(ctx.pages):
                    try:
                        _pg.evaluate(_OVERLAY_JS, locked)
                    except Exception:
                        pass
                tick += 1
                shot = ctx.pages[-1] if ctx.pages else page
                user_surfaced = _should_surface()
                # Live-view frames only while hidden — screenshots on a visible login window
                # fight the human for the display and can flash the OAuth popup.
                if not user_surfaced:
                    try:
                        tmp = FRAME_FILE.with_suffix(".tmp.jpg")
                        shot.screenshot(path=str(tmp), type="jpeg", quality=85)
                        os.replace(tmp, FRAME_FILE)
                    except Exception:
                        pass
                # ~every 2s: login flags for the UI. NEVER periodic auto-hide (flash bug).
                # Surface on auth walls; when login succeeds, clear flags + hide the window.
                if tick % 4 == 0:
                    try:
                        chrome_pids = _keeper_chrome_pids()
                        on_wall = _context_needs_login(ctx)
                        logged_in = _context_logged_in(ctx) or linkedin_logged_in()

                        if on_wall:
                            try:
                                NEEDS_LOGIN_FLAG.write_text("1", encoding="utf-8")
                            except Exception:
                                pass
                            if not _should_surface():
                                try:
                                    SHOW_FLAG.write_text("show", encoding="utf-8")
                                except Exception:
                                    pass
                        elif logged_in:
                            try:
                                LOGGED_IN_FLAG.write_text("1", encoding="utf-8")
                            except Exception:
                                pass
                            for f in (NEEDS_LOGIN_FLAG, SHOW_FLAG):
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                            _set_all_visibility(chrome_pids, False)
                            had_show_flag = False
                        else:
                            try:
                                NEEDS_LOGIN_FLAG.write_text("1", encoding="utf-8")
                            except Exception:
                                pass
                            try:
                                LOGGED_IN_FLAG.unlink()
                            except Exception:
                                pass

                        pid_set = set(chrome_pids)
                        user_surfaced = _should_surface()
                        if user_surfaced and (not had_show_flag or pid_set - known_chrome_pids):
                            _set_all_visibility(chrome_pids, True)
                        had_show_flag = user_surfaced
                        known_chrome_pids = pid_set
                    except Exception:
                        pass
                time.sleep(0.5)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            for f in (STATE_FILE, FRAME_FILE, SHOW_FLAG, NEEDS_LOGIN_FLAG, LOGGED_IN_FLAG):
                try:
                    f.unlink()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Window visibility (Windows). The keeper Chrome is parked OFF-SCREEN with a TOOL-WINDOW
# style so it has NO taskbar button and never appears — yet it still composites, so the
# in-app Live view keeps getting frames. show_window() summons it on-screen for a manual
# login/checkpoint; hide_window() sends it back. We target ONLY the process that owns the
# keeper's debug port (PORT) — never the user's personal Chrome.
# ---------------------------------------------------------------------------

def _descendant_chrome_pids(root_pid: int):
    """Chrome/Chromium PIDs descended from `root_pid` (this keeper). Used at startup to hide
    the window the instant it appears. Playwright launches the browser a grandchild via its
    node driver, so the whole tree is walked. psutil-based (cross-platform)."""
    return pc.descendant_chrome_pids(root_pid)


def _pid_on_port(port: int):
    """PID of the process LISTENING on `port` (the keeper's Chrome browser process)."""
    return pc.pid_on_port(port)


def _windows_for_pid(pid: int):
    """Top-level, titled windows owned by `pid`. Windows only ([] elsewhere)."""
    return pc.windows_for_pid(pid)


def _set_visibility(pid: int, show: bool) -> None:
    """show=True → on-screen; show=False → off-screen tool-window. Prefer _set_all_visibility."""
    pc.set_window_visibility(pid, show)


# macOS keeper-window control via CDP — set up by _keeper_main. On macOS the win32 hide path
# below AND the off-screen --window-position park are both no-ops, so without this the keeper
# Chrome sits on-screen, flashing and mis-sized (the bug Eliza saw). Module-level so
# _set_all_visibility can drive it from the keeper loop.
_MAC_CDP = None
_MAC_WIN_ID = None
_MAC_WIN_SHOWN = None   # last visibility we APPLIED (None = unknown). Makes _mac_window_state
                        # IDEMPOTENT so the keeper loop's ~2s reconcile can call it every tick
                        # (self-heal) WITHOUT re-issuing a minimise to an already-minimised
                        # window. Re-issuing setWindowBounds minimized every tick re-fired the
                        # dock minimise animation endlessly — the macOS "flashing" bug. Mirrors
                        # the win32 idempotency in _apply_window_visibility ("re-applying show
                        # every tick must not flash").


def _mac_window_state(show: bool) -> None:
    """macOS: restore (show=True) or minimise (show=False) the keeper window via CDP
    Browser.setWindowBounds. Best-effort; no-op off macOS or before the keeper set up its
    CDP session. IDEMPOTENT — only sends when the requested state differs from the last
    state we applied, so the keeper loop can call it every ~2s (self-heal) without the
    minimise animation re-firing (the flashing). To re-surface for a manual login the loop
    transitions hidden->shown, which is a real state change and therefore still sent."""
    global _MAC_WIN_SHOWN
    if not pc.IS_MAC or _MAC_CDP is None or _MAC_WIN_ID is None:
        return
    if _MAC_WIN_SHOWN == show:
        return   # already in the target state — do NOT re-issue (that was the flash)
    try:
        _MAC_CDP.send("Browser.setWindowBounds",
                      {"windowId": _MAC_WIN_ID,
                       "bounds": {"windowState": "normal" if show else "minimized"}})
        _MAC_WIN_SHOWN = show
    except Exception:
        pass


def _set_all_visibility(pids: list[int], show: bool) -> None:
    """Show/hide every keeper Chromium window (main + OAuth popups), once per HWND."""
    pc.set_windows_visibility(pids, show)   # Windows path
    _mac_window_state(show)                 # macOS path (CDP); each is a no-op on the other OS


def _should_surface() -> bool:
    """True while the human may be signing in — never auto-hide in this window."""
    return SHOW_FLAG.exists()


def _surface_keeper_windows() -> None:
    """Bring ALL keeper Chromium windows on-screen (main + OAuth popups)."""
    if not _should_surface():
        return
    try:
        _set_all_visibility(_keeper_chrome_pids(), True)
    except Exception:
        pass


def show_window() -> bool:
    """Surface the keeper window on-screen for a manual login/checkpoint."""
    try:
        SHOW_FLAG.write_text("show", encoding="utf-8")
    except Exception:
        pass
    _surface_keeper_windows()
    return True


def hide_window() -> bool:
    """Send the keeper window back off-screen (no taskbar)."""
    try:
        SHOW_FLAG.unlink()
    except Exception:
        pass
    try:
        _set_all_visibility(_keeper_chrome_pids(), False)
        return True
    except Exception:
        pass
    return False


def main() -> None:
    if "--keep" in sys.argv:
        _keeper_main()
    elif "--stop" in sys.argv:
        stop_keeper()
        print("keeper stopped")
    elif "--status" in sys.argv:
        print(json.dumps(status(), indent=2))
    elif "--start" in sys.argv:
        print("keeper at", ensure_keeper())
    elif "--reap" in sys.argv:
        keep = _port_owner_keeper_pid()
        killed = reap_keepers(except_pid=keep)
        print(f"kept keeper pid {keep} (owns port {PORT}); reaped {len(killed)} stray keeper(s): {killed}")
    elif "--lock" in sys.argv:
        print(f"locked {lock_window(manual=True)} window(s)")
    elif "--unlock" in sys.argv:
        print(f"unlocked {unlock_window(manual=True)} window(s)")
    elif "--lock-state" in sys.argv:
        print(json.dumps(lock_state()))
    else:
        print("usage: python -m engine.browser "
              "--keep | --start | --stop | --status | --reap | --lock | --unlock | --lock-state")


if __name__ == "__main__":
    main()
