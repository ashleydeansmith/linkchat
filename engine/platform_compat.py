r"""platform_compat — OS abstraction for LinkForge's process / window / launch primitives.

THE POINT
---------
LinkForge was Windows-only in three places — process enumeration (Toolhelp +
PowerShell-WMI + netstat + tasklist + kernel32), window hide/show + human-input
freeze (win32 user32/win32gui), and detached/no-console subprocess launch
(CREATE_NO_WINDOW / DETACHED_PROCESS). This module is the single seam that makes
those cross-platform for the macOS (Electron-shell) port WITHOUT changing Windows
behaviour:

  * PROCESS / PORT / LIVENESS  -> reimplemented on **psutil**, so ONE code path
    serves Windows, macOS and Linux. The old PowerShell/netstat/Toolhelp/tasklist
    implementations are deleted; their callers now delegate here.
  * LAUNCH FLAGS               -> Windows creationflags; POSIX uses start_new_session.
  * WINDOW CONTROL             -> the win32 code still runs on Windows (behaviour
    preserved exactly); on macOS/Linux it is a NO-OP because the keeper Chromium is
    parked off-screen via a launch arg (`--window-position=-32000,-32000`). Surfacing
    the keeper window for a manual login on macOS IS built — `browser._mac_window_state`
    drives CDP `Browser.setWindowBounds` (windowState normal/minimized), which is the
    cross-platform fix rather than an OS window API. This comment used to call it an
    open gap and no longer should: the gap named here was closed, and a stale note
    saying otherwise sends the next reader off fixing something that works. What is
    still true is that NO PART of the macOS path has ever been run on a Mac.
  * USER DATA DIR              -> %LOCALAPPDATA% (win) / ~/Library/Application Support
    (mac) / $XDG_DATA_HOME (linux), replacing the bare LOCALAPPDATA lookup.

Nothing here imports from the linkforge package, so it is safe to import at package
import time (__init__.py uses user_data_base()).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psutil

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_POSIX = not IS_WIN


# ---------------------------------------------------------------------------
# Writable user-data base (replaces the bare %LOCALAPPDATA% lookup)
# ---------------------------------------------------------------------------

def user_data_base() -> Path:
    """Per-user writable base dir for app state. LinkForge appends 'LinkForge'."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) if base else Path(os.path.expanduser("~")) / "AppData" / "Local"
    if IS_MAC:
        return Path(os.path.expanduser("~")) / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME")
                or (Path(os.path.expanduser("~")) / ".local" / "share"))


# ---------------------------------------------------------------------------
# Process liveness / tree / port  (psutil — cross-platform)
# ---------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    """True if pid is a running (non-zombie) process. Replaces the tasklist check
    (browser) and the kernel32 OpenProcess/GetExitCodeProcess check (scheduler)."""
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def pid_create_time(pid: int) -> float | None:
    """Epoch seconds at which `pid` was created, or None if it cannot be read.

    A pid is only a NUMBER, and the OS recycles numbers. (pid, create_time) is the
    pair that identifies a process, so a pid file that records the create time can
    tell its own daemon apart from whatever inherited the number after it died —
    see scheduler.daemon_pid(). None means "unknown", never "mismatch": callers
    must fail SAFE on None and keep trusting the pid."""
    if not pid:
        return None
    try:
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


def parent_map() -> dict[int, int]:
    """pid -> parent-pid for every process (one pass). Replaces the PowerShell-WMI map."""
    pm: dict[int, int] = {}
    for p in psutil.process_iter(["pid", "ppid"]):
        try:
            pm[int(p.info["pid"])] = int(p.info["ppid"])
        except Exception:
            pass
    return pm


def descendant_pids(root_pid: int) -> set[int]:
    """root_pid + every process descended from it."""
    out = {int(root_pid)}
    try:
        for c in psutil.Process(int(root_pid)).children(recursive=True):
            out.add(c.pid)
    except Exception:
        pass
    return out


def descendant_chrome_pids(root_pid: int) -> list[int]:
    """Chrome/Chromium PIDs descended from root_pid (the keeper). Playwright launches
    the browser as a grandchild via its node driver, so we walk the whole tree.
    Matches 'chrom' to catch chrome.exe, 'Google Chrome' and 'Chromium' alike."""
    out: list[int] = []
    try:
        for c in psutil.Process(int(root_pid)).children(recursive=True):
            try:
                if "chrom" in (c.name() or "").lower():
                    out.append(c.pid)
            except Exception:
                pass
    except Exception:
        pass
    return out


def pid_on_port(port: int) -> int | None:
    """PID LISTENING on `port` (the keeper's Chromium browser process). psutil first
    (cross-platform); lsof fallback on POSIX where net_connections may need privileges."""
    try:
        for c in psutil.net_connections(kind="inet"):
            try:
                if (c.status == psutil.CONN_LISTEN and c.laddr
                        and c.laddr.port == port and c.pid):
                    return int(c.pid)
            except Exception:
                pass
    except Exception:
        pass
    if IS_POSIX:
        try:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=10).stdout
            for tok in out.split():
                if tok.strip().isdigit():
                    return int(tok)
        except Exception:
            pass
    return None


def list_keeper_pids() -> list[int]:
    """PIDs of running keeper processes (command line carries '--keep'). Matches dev
    (`-m linkforge.browser --keep`) and frozen (`LinkForge[.exe] browser --keep`).
    Replaces the PowerShell-WMI CommandLine scan."""
    out: list[int] = []
    for p in psutil.process_iter(["pid"]):
        try:
            cl = p.cmdline()
        except Exception:
            continue
        if not cl:
            continue
        s = " ".join(cl)
        low = s.lower()
        if "--keep" in s and ("linkforge.browser" in low
                              or ("linkforge" in low and "browser" in low)):
            out.append(p.pid)
    return out


def kill_tree(pid: int, timeout: float = 10.0) -> bool:
    """Terminate a process AND its whole child tree (SIGTERM, then SIGKILL backstop).
    Replaces `taskkill /PID <pid> /T /F`. Lets the tree close gracefully first so
    Chromium can flush cookies (the 2026-06-02 'force-kill discards session' lesson)."""
    try:
        parent = psutil.Process(int(pid))
    except Exception:
        return False
    procs: list[psutil.Process] = []
    try:
        procs = parent.children(recursive=True)
    except Exception:
        pass
    procs.append(parent)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Subprocess launch flags  (no-console / detached daemon)
# ---------------------------------------------------------------------------

def no_window_popen_kwargs() -> dict:
    """Popen kwargs so a child never flashes a console window. Windows-only flag."""
    if IS_WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def detached_popen_kwargs() -> dict:
    """Popen kwargs to fully detach a long-lived daemon from this process.
    Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP. POSIX: start_new_session
    (the safe Popen-native equivalent of preexec_fn=os.setsid)."""
    if IS_WIN:
        return {"creationflags": (getattr(subprocess, "DETACHED_PROCESS", 0)
                                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))}
    return {"start_new_session": True}


def detached_no_window_popen_kwargs() -> dict:
    """Detached AND no-console (the keeper spawn). Windows merges both flags; POSIX
    just needs the new session (no console concept)."""
    if IS_WIN:
        return {"creationflags": (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                  | getattr(subprocess, "DETACHED_PROCESS", 0))}
    return {"start_new_session": True}


# ---------------------------------------------------------------------------
# Window control  (win32 on Windows; no-op on macOS/Linux)
# ---------------------------------------------------------------------------

def windows_for_pid(pid: int) -> list[int]:
    """Top-level CHROMIUM BROWSER windows owned by `pid` (window class 'Chrome_WidgetWin_1').
    [] on non-Windows. Filtering by class is essential: every Chrome child process (GPU,
    renderer, utility) owns invisible IME helper windows ('Default IME', 'MSCTFIME UI') that
    ALSO carry a title - surfacing those opened a pile of blank/IME windows and made the real
    login window flash. We want only the one real browser window."""
    if not IS_WIN:
        return []
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    hwnds: list[int] = []

    def _cb(hwnd, _lp):
        p = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value != pid or u.GetWindowTextLengthW(hwnd) <= 0:
            return True
        buf = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, buf, 64)
        if buf.value == "Chrome_WidgetWin_1":   # the real Chromium top-level window only
            hwnds.append(hwnd)
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(_cb)
    u.EnumWindows(proc, 0)
    return hwnds


def _apply_window_visibility(hwnd: int, show: bool) -> None:
    """Toggle one Chromium top-level window. Idempotent — skips SW_HIDE when already
    in the target state (re-applying show every tick must not flash)."""
    if not IS_WIN:
        return
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    SW_HIDE, SW_RESTORE, SW_SHOWNA = 0, 9, 8
    SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x4, 0x10
    try:
        rect = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rect))
        off_screen = rect.left < -30000 or rect.top < -30000
        if show and not off_screen:
            return
        if not show and off_screen:
            return
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.ShowWindow(hwnd, SW_HIDE)
        if show:
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
            u.SetWindowPos(hwnd, 0, 80, 60, 1280, 860, SWP_NOZORDER)
            u.ShowWindow(hwnd, SW_RESTORE)
        else:
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW)
            u.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            u.ShowWindow(hwnd, SW_SHOWNA)
    except Exception:
        pass


def set_window_visibility(pid: int, show: bool) -> None:
    """show=True -> on-screen; show=False -> off-screen tool-window. See set_windows_visibility
    when multiple Chrome PIDs may own keeper windows (OAuth popups)."""
    for hwnd in windows_for_pid(pid):
        _apply_window_visibility(hwnd, show)


def set_windows_visibility(pids: list[int], show: bool) -> None:
    """Apply show/hide once per unique top-level Chromium window across many PIDs.
    OAuth popups often land on a different Chrome process than the CDP port owner."""
    if not IS_WIN:
        return
    seen: set[int] = set()
    for pid in pids:
        for hwnd in windows_for_pid(pid):
            if hwnd not in seen:
                seen.add(hwnd)
                _apply_window_visibility(hwnd, show)


def set_keeper_input_enabled(keeper_pid: int, enabled: bool) -> int:
    """Freeze (enabled=False) / unfreeze HUMAN input to the keeper's visible windows via
    win32 EnableWindow — CDP-driven automation is unaffected. Returns # windows toggled.
    NO-OP (0) on macOS/Linux: the on-page CSS overlay is the visible 'locked' signal
    there; OS-level input-freeze of another process's window is the degraded bit."""
    if not IS_WIN or not keeper_pid:
        return 0
    try:
        import win32gui
        import win32process
    except Exception:
        return 0
    pids = descendant_pids(int(keeper_pid))
    n = [0]

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if not win32gui.GetWindowText(hwnd):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in pids:
                win32gui.EnableWindow(hwnd, enabled)
                n[0] += 1
        except Exception:
            pass

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return 0
    return n[0]
