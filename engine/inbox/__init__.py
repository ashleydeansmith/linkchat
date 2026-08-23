"""LinkChat inbox — your LinkedIn conversations, read from the pages themselves.

This subpackage is InboxForge folded INTO LinkForge (Stage 1 of the merge plan at
Second Brain/Projects/LinkForge-InboxForge-Merge/Build-Plan-V3.md). It is a local,
no-cloud Kondo-style 3-pane inbox: own SQLite, own UI, NO Voyager-as-a-separate-engine —
it reuses the ONE shared LinkedIn keeper (linkforge.browser) and the shared governance
ledger (linkforge.ops / linkedin_browser.READ_LOCK), exactly as the standalone did.

STAGE 1 (co-process): the inbox runs INSIDE the LinkForge process and UI but keeps its
own DB file — `conversations.db`, beside linkforge.db in LinkForge's frozen-aware
DATA_DIR. The `messages` table has been renamed to `conversation_messages` here (it
collides with LinkForge's campaign send-log `messages` table) so that Stage 2's graft
into linkforge.db is a clean INSERT…SELECT, not a reconciliation.

The standalone `inboxforge` package is left intact as a fallback until Stage 3 retires it.
"""
from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"

# Nexus/automation must be importable regardless of cwd so `from linkforge import …`
# and `import linkedin_browser` resolve. linkforge/__init__ already does this, but we
# repeat it so this subpackage is import-safe even if loaded in isolation.
_AUTOMATION = Path(__file__).resolve().parent.parent.parent  # …/Nexus/automation
if str(_AUTOMATION) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION))

PKG_DIR = Path(__file__).resolve().parent

# Share LinkForge's writable data dir — it is already frozen-aware (redirects to
# %LOCALAPPDATA%\LinkForge in a PyInstaller build). The inbox DB, audio cache, and
# voice/attachment staging files all land here beside linkforge.db.
from engine import DATA_DIR as DATA_DIR  # noqa: E402  (re-export for the inbox modules)

DB_PATH = DATA_DIR / "conversations.db"

AGENT = "inboxforge"   # keep the SAME ledger/lock identity in the shared linkedin_ops layer
