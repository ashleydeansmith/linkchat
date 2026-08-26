"""LinkChat inbox — your LinkedIn conversations, read from the pages themselves.

This subpackage is the inbox half folded INTO the parent program (Stage 1 of the merge plan at
Second Brain/Projects/the parent program-the inbox half-Merge/Build-Plan-V3.md). It is a local,
no-cloud Kondo-style 3-pane inbox: own SQLite, own UI, NO Voyager-as-a-separate-engine —
it reuses the ONE shared LinkedIn keeper (engine.browser) and the shared governance
ledger (the parent program.ops / linkedin_browser.READ_LOCK), exactly as the standalone did.

STAGE 1 (co-process): the inbox runs INSIDE the the parent program process and UI but keeps its
own DB file — `conversations.db`, beside the parent program.db in the parent program's frozen-aware
DATA_DIR. The `messages` table has been renamed to `conversation_messages` here (it
collides with the parent program's campaign send-log `messages` table) so that Stage 2's graft
into the parent program.db is a clean INSERT…SELECT, not a reconciliation.

The standalone `the inbox half` package is left intact as a fallback until Stage 3 retires it.
"""
from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"

# the automation folder must be importable regardless of cwd so `from the parent program import …`
# and `import linkedin_browser` resolve. the parent program/__init__ already does this, but we
# repeat it so this subpackage is import-safe even if loaded in isolation.
_AUTOMATION = Path(__file__).resolve().parent.parent.parent  # …/the automation folder
if str(_AUTOMATION) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION))

PKG_DIR = Path(__file__).resolve().parent

# Share the parent program's writable data dir — it is already frozen-aware (redirects to
# %LOCALAPPDATA%\the parent program in a PyInstaller build). The inbox DB, audio cache, and
# voice/attachment staging files all land here beside the parent program.db.
from engine import DATA_DIR as DATA_DIR  # noqa: E402  (re-export for the inbox modules)

DB_PATH = DATA_DIR / "conversations.db"

AGENT = "the inbox half"   # keep the SAME ledger/lock identity in the shared linkedin_ops layer
