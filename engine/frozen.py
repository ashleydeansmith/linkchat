r"""frozen.py — single source of truth for PyInstaller-frozen vs dev behaviour.

LinkForge runs two ways:
  * DEV: `python -m linkforge <cmd>` (source tree, CWD = Nexus/automation).
  * FROZEN: `LinkForge.exe <cmd>` (a PyInstaller onedir bundle; sys.executable is
    LinkForge.exe itself and `-m linkforge` has no meaning — there is no source
    tree to import a module from at runtime).

Three things differ when frozen and they ALL funnel through here so the change is
made in exactly one place per concern:

  1. SUBPROCESS ARGV.  A frozen exe cannot re-invoke itself with `-m linkforge`.
     The frozen entry routes sys.argv[1:] straight into __main__.main(), so
     `LinkForge.exe connect --max 5` works — therefore the right re-invocation is
     `[sys.executable, "connect", "--max", "5"]`. In dev it stays
     `[sys.executable, "-u", "-m", "linkforge", "connect", ...]`.

  2. THE KEEPER.  browser.ensure_keeper() spawns `-m linkforge.browser --keep`.
     Frozen, that becomes `LinkForge.exe browser --keep` — __main__ dispatches the
     `browser` subcommand which calls browser.main(), and "--keep" in sys.argv still
     routes to _keeper_main(). So the keeper is just another subcommand spawn.

  3. BUNDLED DATA ROOT.  Read-only bundled data (web/dist, config.json template,
     openers.txt) lands under sys._MEIPASS in a onedir build. bundle_root() returns
     it when frozen, else the package dir.

DATA_DIR redirection (writable state -> %LOCALAPPDATA%\LinkForge) lives in
__init__.py because it must be set at import time before any submodule reads it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# True inside a PyInstaller bundle (onedir or onefile).
IS_FROZEN = bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Root of bundled (read-only) data. sys._MEIPASS when frozen, else the package dir."""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent  # Nexus/automation (parent of the package)


def spawn_argv(cmd: str, *args: str) -> list[str]:
    """argv to re-invoke a LinkForge CLI SUBCOMMAND as a child process.

    frozen:  [LinkForge.exe, cmd, *args]
    dev:     [python, -u, -m, engine, cmd, *args]
    """
    if IS_FROZEN:
        return [sys.executable, cmd, *args]
    return [sys.executable, "-u", "-m", "engine", cmd, *args]


def keeper_argv() -> list[str]:
    """argv to spawn the persistent browser keeper.

    frozen:  [LinkForge.exe, browser, --keep]   (dispatched via __main__ -> browser.main)
    dev:     [pythonw|python, -m, engine.browser, --keep]
    """
    if IS_FROZEN:
        return [sys.executable, "browser", "--keep"]
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else sys.executable
    return [exe, "-m", "engine.browser", "--keep"]
