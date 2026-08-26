"""
linkedin_browser.py — shared LinkedIn browser layer. CHROMIUM ONLY.

Why this exists (2026-06-02):
  The LinkedIn read/scrape scripts used to run on a Playwright **Firefox**
  persistent profile (`~/.claude/browser-profiles/linkedin-firefox`, lock
  `firefox-read`). That profile launched then instantly exited (exitCode=0),
  so every posts/jobs/careers scan silently returned empty and capped every
  prospect to "Warm". Worse, Playwright's Firefox is also `firefox.exe`, so a
  name-scoped process kill could not tell it apart from a PERSONAL
  Firefox — and on 2026-06-02 a `Get-Process firefox | Stop-Process` took his
  browser down with it.

The fix / standing rule:
  - Automation is **Chromium only**. personal browsing stays **Firefox
    only**. They can never be confused again — by engine AND by path.
  - All LinkedIn browser work (reads + sends) shares ONE logged-in Chromium
    persistent profile: `the automation folder/linkedin-session` (the proven
    `chromium-send` session that already sends DMs and scrapes conversations).
  - Because every caller shares that one profile dir, every caller MUST hold the
    same `linkedin_ops` lock — READ_LOCK below ("chromium-send") — so reads and
    sends serialise instead of colliding on the profile.
  - Any process reaping is PATH-SCOPED to `*ms-playwright*` only. It will never
    touch a browser installed under Program Files (personal Firefox / Chrome).

See: AI/Failure-Log.md (2026-06-02) and memory feedback-browser-isolation.
"""

import os
import sys
from pathlib import Path

# The one logged-in LinkedIn Chromium session. Same dir the DM sender and the
# conversation scraper use (chromium-send). One profile dir => one ops lock.
#
# DEV (source tree): the shared the automation folder path — the 15+ dev-fleet importers keep using
#   the developer's logged-in session exactly as before, UNCHANGED.
# FROZEN (packaged app): a PER-USER session under the OS user-data base, so every
#   beta tester signs into THEIR OWN LinkedIn. an absolute dev path does not
#   exist on a tester's machine; without this, the keeper created an empty profile
#   and every lane saw "not logged in" (the §0a "only works for its author" defect).
#   Mirrors platform_compat.user_data_base() / __init__.py's DATA_DIR redirect, but
#   kept dependency-free — this module is shared infra dev scripts import directly.
if getattr(sys, "frozen", False):
    if sys.platform.startswith("win"):
        _ud = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        _ud = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        _ud = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    SESSION_DIR = Path(_ud) / "LinkChat" / "linkedin-session"
else:
    # The signed-in browser lives INSIDE your CRM, beside everything else LinkChat
    # keeps, so one backup covers it and there is one folder to look in.
    #
    # This line used to name one particular machine's folder. On anybody else's
    # computer that path does not exist, so the browser could never open - and the
    # failure would arrive as a puzzle rather than as a sentence.
    def _session_dir():
        try:
            from engine import crm_bridge
            root = crm_bridge.find()
            if root:
                return Path(root) / "_state" / "linkchat" / "linkedin-session"
        except Exception:
            pass
        return Path.home() / ".linkchat" / "linkedin-session"

    SESSION_DIR = _session_dir()

# Every chromium caller (read OR send) takes this lock. Same profile dir = same
# lock, so concurrent opens are impossible (chromium SingletonLock would error).
READ_LOCK = "chromium-send"

# Proven launch options (faithful to linkedin_conversation_scraper.py, which
# reads LinkedIn via chromium reliably).
_VIEWPORT = {"width": 1280, "height": 900}
_LOCALE = "en-GB"


class KeeperUnavailable(RuntimeError):
    """The keeper is RUNNING but cannot be attached, and in-place recovery failed.

    Raised instead of silently launching a competing browser on the keeper's profile.
    See open_read_context() for why that silent fallback was the single biggest source
    of lane failure in the parent program's history.
    """


def _log(msg: str) -> None:
    print(f"[linkedin_browser] {msg}", file=sys.stderr, flush=True)


def open_read_context(pw, headless: bool = False, shared: bool = True):
    """Open the logged-in LinkedIn Chromium context (sync API).

    Caller MUST already hold the `READ_LOCK` ops lock. Returns the BrowserContext;
    caller drives `context.pages[0]` / `context.new_page()` and closes via
    `the parent program.safe_close()` in a finally block.

    SINGLE-INSTANCE (LH2 model, 2026-06-13): by default this ATTACHES over CDP to the
    one long-lived 'keeper' Chromium (see the parent program/browser.py) instead of launching a
    fresh browser per lane — so the browser stays alive and we navigate within it like
    a human, rather than open/close/open/close. Pass shared=False to force a private
    launch.

    ⚠ THE FALLBACK IS NOT FREE (root-caused 2026-07-11). The old code, on ANY keeper
    attach failure, silently fell through to launch_persistent_context() on the SAME
    profile dir a live keeper still holds. That launch cannot succeed (chromium
    SingletonLock), so it hit the retry path, which called reap_playwright_chromium()
    — a PATH-scoped kill of every `*ms-playwright*` chrome.exe. The keeper IS an
    ms-playwright chrome.exe. So the "self-heal" killed the keeper mid-batch, and every
    remaining lead died with "Target page, context or browser has been closed": 199 of
    248 all-time connect failures (80%), and the bulk of the historical 45% connect /
    34% withdraw success rates.

    The rule now: a private launch and the reaper are only ever allowed when NO keeper
    is running. If a keeper is up but unattachable (wedged — stale READ_LOCK / stale
    heartbeat / parked page), recover it IN PLACE (stop -> restart -> re-attach; the
    manual fix proven on 2026-07-10 — cookies are on disk, so the login survives) and,
    failing that, raise KeeperUnavailable so the lane stops honestly instead of
    reaping the keeper and burning the rest of the batch.
    """
    if shared:
        from the parent program import browser as _lfb
        ctx = None
        try:
            ctx = _lfb.connect(pw)        # NB: returns None on CDP failure, does not raise
        except Exception as e:
            _log(f"keeper attach raised ({type(e).__name__}: {e})")
        if ctx is not None:
            return ctx

        if _lfb.keeper_running():
            # Wedged keeper. NEVER launch alongside it — recover it in place.
            _log("keeper is UP but unattachable (wedged) — restarting it in place")
            try:
                _lfb.stop_keeper()
                if _lfb.ensure_keeper(wait_sec=120):
                    ctx = _lfb.connect(pw)
                    if ctx is not None:
                        _log("keeper recovered — attached")
                        return ctx
                    _log("keeper restarted but still unattachable")
                else:
                    _log("keeper restart failed (ensure_keeper returned no CDP url)")
            except Exception as e:
                _log(f"keeper recovery failed ({type(e).__name__}: {e})")
            raise KeeperUnavailable(
                "The keeper browser is running but cannot be attached, and restarting it "
                "in place failed. Refusing to launch a second browser on the keeper's "
                "profile — that reaps the keeper and fails every remaining lead in the "
                "batch. Close the keeper Chromium window and re-run the lane."
            )

        _log("no keeper running — falling back to a private launch")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    def _launch():
        return pw.chromium.launch_persistent_context(
            str(SESSION_DIR),
            headless=headless,
            viewport=_VIEWPORT,
            locale=_LOCALE,
        )

    try:
        return _launch()
    except Exception:
        # The launch failed — classically because an ORPHANED automation Chromium (a
        # previous lane that crashed and left its headful browser open) still holds this
        # persistent profile, so a second open can't acquire it (TargetClosedError /
        # exitCode 21). Reap it (path-scoped; never touches personal browsers) and retry
        # once so the lane self-heals.
        #
        # GUARD (2026-07-11): the reaper cannot tell a zombie from the KEEPER — both are
        # `*ms-playwright*` chrome.exe. If a keeper came up in the gap, reaping here would
        # kill it and re-open the exact death spiral above. No keeper => nothing to protect.
        try:
            from the parent program import browser as _lfb
            if _lfb.keeper_running():
                raise KeeperUnavailable(
                    "A keeper browser is running, so the private-launch reaper is refused "
                    "(it would kill the keeper and fail the rest of the batch). Re-run the "
                    "lane — it will attach to the keeper."
                )
        except KeeperUnavailable:
            raise
        except Exception:
            pass       # engine.browser unimportable => no keeper can exist => reap is safe
        reap_playwright_chromium()
        import time as _t
        _t.sleep(1.5)
        return _launch()


def reap_playwright_chromium():
    """Reap orphaned Playwright Chromium + clear the chromium singleton locks.

    PATH-SCOPED: only kills a browser whose program file sits under
    `*ms-playwright*` — never a personal Chrome, never Firefox, never Safari.
    Safe to call on the failure/exit path while this process holds READ_LOCK (no
    other LinkedIn automation can be on the profile). Then clears the chromium
    Singleton* lock files a force-kill can leave behind so the next launch is clean.

    Windows does the kill through PowerShell, exactly as it has since 2026-07-11.
    macOS and Linux do it through psutil. The lock-file clearing runs on EVERY
    computer — until 2026-08-25 it sat behind a Windows-only return, so a keeper
    that died on a Mac left a lock nothing ever cleared and every launch after it
    was refused for a reason the member could not see.
    """
    if sys.platform.startswith("win"):
        # Windows: unchanged since 2026-07-11. Deliberately NOT routed through the
        # psutil path below — this is send-path-adjacent code and the Windows
        # behaviour is the proven one.
        import subprocess
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Where-Object { $_.Name -eq 'chrome.exe' -and $_.ExecutablePath -like '*ms-playwright*' } | "
                 "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }"],
                timeout=30, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),   # no flashing window
            )
        except Exception:
            pass
    else:
        # macOS and Linux: psutil, which is already installed for everything else.
        # Same rule as the Windows line above and it is the whole safety of this
        # function — only kill a browser whose program file sits inside the
        # Playwright folder. A personal Chrome or Safari is never a candidate.
        try:
            import psutil
            for proc in psutil.process_iter(["name", "exe"]):
                try:
                    exe = (proc.info.get("exe") or "")
                    name = (proc.info.get("name") or "").lower()
                    if "ms-playwright" not in exe.lower():
                        continue
                    if "chrom" not in name and "chrom" not in os.path.basename(exe).lower():
                        continue
                    proc.kill()
                except Exception:
                    continue
        except Exception:
            pass

    # Clearing the lock files is NOT a Windows job and used to sit behind the
    # Windows-only return above. A keeper that dies on a Mac leaves SingletonLock
    # behind, and every launch afterwards is refused with the profile already in
    # use — a fault with no visible cause. This now runs on every computer.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            f = SESSION_DIR / name
            if f.exists():
                f.unlink()
        except Exception:
            pass


# Back-compat alias: scripts historically called reap_playwright_firefox().
# Keep the name working but route it to the chromium reaper.
reap_playwright_firefox = reap_playwright_chromium
