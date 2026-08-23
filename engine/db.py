"""db.py — LinkForge operational state (SQLite).

This is the ENGINE's own state, not the CRM. The human-readable CRM is Obsidian
(People notes), written by the CRM lane in a later phase. This DB tracks who's
been invited, invite ages (for the withdraw lane), where each lead sits in a
drip sequence, and an app-local audit trail.

One file: linkforge/data/linkforge.db. Schema is created idempotently
(CREATE TABLE IF NOT EXISTS), so init-db is safe to re-run.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import DB_PATH, DATA_DIR, RED_LIST_PATH
from .canon import canon_in

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'connect',   -- connect | connect+drip
    targeting     TEXT,                              -- JSON: salesnav/search urls + filters
    note_template TEXT,                              -- optional spintax connect note
    sequence_id   INTEGER,                           -- FK -> sequences.id (drip)
    daily_connect_cap INTEGER,                       -- per-campaign cap (<= global)
    status        TEXT NOT NULL DEFAULT 'paused',    -- paused | active | done
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_url   TEXT UNIQUE NOT NULL,              -- canonical /in/ url = identity
    sales_nav_url TEXT,
    full_name     TEXT,
    headline      TEXT,
    company       TEXT,
    title         TEXT,
    location      TEXT,
    source        TEXT,                              -- salesnav | search | csv
    campaign_id   INTEGER,
    status        TEXT NOT NULL DEFAULT 'new',
        -- new | queued_connect | invited | accepted | in_sequence
        -- | replied | done | skipped
    raw_json      TEXT,                              -- observed profile data as-seen
    crm_note_path TEXT,                              -- Obsidian People note (when synced)
    invited_at    TEXT,
    accepted_at   TEXT,
    last_action_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id)
);
CREATE INDEX IF NOT EXISTS idx_leads_status   ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id);

CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    sent_at     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',     -- pending | accepted | withdrawn | expired
    withdrawn_at TEXT,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);
CREATE INDEX IF NOT EXISTS idx_invites_status ON invites(status);

CREATE TABLE IF NOT EXISTS sequences (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence_id INTEGER NOT NULL,
    step_index  INTEGER NOT NULL,                    -- 0-based order
    wait_days   INTEGER NOT NULL DEFAULT 0,          -- delay after prior step / accept
    template    TEXT NOT NULL,                       -- spintax message body
    FOREIGN KEY (sequence_id) REFERENCES sequences(id)
);

CREATE TABLE IF NOT EXISTS sequence_state (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id      INTEGER NOT NULL,
    sequence_id  INTEGER NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    next_due_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'active',     -- active | stopped_reply | completed
    last_sent_at TEXT,
    FOREIGN KEY (lead_id) REFERENCES leads(id),
    FOREIGN KEY (sequence_id) REFERENCES sequences(id)
);
CREATE INDEX IF NOT EXISTS idx_seqstate_status ON sequence_state(status);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL,
    step_index INTEGER,
    body       TEXT NOT NULL,
    sent_at    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'sent',         -- sent | failed
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    lead_id INTEGER,
    kind    TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA_READY = False
_SCHEMA_INITING = False


def _ensure_schema() -> None:
    """Create/migrate the schema before any query runs. Fresh installs used to open an
    empty linkforge.db (sqlite creates the file on connect) without ever running init_db,
    which broke /api/safety and POST /api/campaign with 'no such table'. Idempotent."""
    global _SCHEMA_READY, _SCHEMA_INITING
    if _SCHEMA_READY or _SCHEMA_INITING:
        return
    _SCHEMA_INITING = True
    try:
        init_db()
        _SCHEMA_READY = True
    finally:
        _SCHEMA_INITING = False


@contextmanager
def connect():
    """Yield a sqlite3 connection with row access by name. Commits on clean exit."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _SCHEMA_INITING:
        _ensure_schema()
    # F0.5b (2026-07-15): 15s busy wait + WAL. Default rollback-journal mode let ANY
    # reader block the writer with a 5s fuse — and the send path records a message
    # AFTER the irreversible LinkedIn click, so a lock hit in that window lost the
    # record of a message a human really received (the resume logic then misread our
    # own unrecorded send as their reply). WAL makes readers never block the writer;
    # journal_mode is persistent per-DB, re-issuing it is a cheap no-op.
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass   # locked right now: existing mode still works; next connect retries
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the original schema shipped. Each is applied with a guarded
# ALTER TABLE (PRAGMA check) so init_db stays idempotent and never errors on a DB
# that already has them — the Stage-2 migration rule (never drop, never blind-add).
_LEAD_MIGRATIONS = {
    # CRM Sync (Stage 3): write-back cursor + routing fields pulled from People notes.
    "crm_synced_at":   "TEXT",   # high-water mark: last interaction ts written to the note
    "dm_track":        "TEXT",   # scaled | small — routes the AI-DM opener agent
    "cso_phase":       "TEXT",   # CSO dm-phase (curiosity | messy-middle | ...)
    "cso_chess_angle": "TEXT",   # CSO chess-angle line (context for the opener)
    "cso_synced_at":   "TEXT",   # when routing fields were last pulled from the note
    # ICP scorer (the no-SN qualification layer) — advisory, never touches `status`.
    "icp_score":       "INTEGER", # 0-100 ICP fit (NULL = unscored)
    "icp_tier":        "TEXT",    # prime | strong | fit | reject
    "icp_reason":      "TEXT",    # short why: +role / +ind / -excl signals matched
    # Already a 1st-degree connection. HARD safety flag, not a status: `status` is
    # pipeline state and gets rewritten by every lane, but you can never *un-connect*
    # from someone. The connect lane refuses to invite these (see connect._queue_leads).
    # Set on any import whose source is the connection roster (csv_import(is_connection=True)).
    "is_connection":   "INTEGER DEFAULT 0",
    # v5 (connect-lane rebuild, 2026-07-13): when a live search walk last SAW this lead
    # on its campaign's Sales Nav search. NULL = never seen since stamping began. Powers
    # honest staleness ('collected' rows the dynamic search no longer serves) and the
    # `connect --audit` composition report. Stamped by the sweep and the audit walk.
    "last_seen_on_search_at": "TEXT",
    # v9 (event invites, 2026-08-20): the day this person became a connection, and where
    # they sat in the connections list when it was last read. LinkedIn sorts that page
    # newest first, so `connected_rank` 1 is your most recent connection — which is the
    # order event invitations are meant to walk. The archive export carries an exact
    # "Connected On" date and the import used to discard it; the page itself often only
    # says "3 weeks ago", which is why the position is kept alongside the date.
    "connected_on":    "TEXT",
    "connected_rank":  "INTEGER",
}


# messages gains a source marker so the CRM write-back can label who wrote a send
# (mechanical drip vs the AI-DM plugin). Default 'drip' preserves existing rows.
_MESSAGE_MIGRATIONS = {"source": "TEXT DEFAULT 'drip'"}

# sequence_state gains an A/B variant marker (which message variant a lead is enrolled in).
# Default 'A' preserves existing single-sequence enrolments.
_SEQSTATE_MIGRATIONS = {
    "variant": "TEXT DEFAULT 'A'",
    # v5: consecutive live-send failures for this journey. 3 strikes -> the tick parks the
    # row at status='needs_attention' instead of retrying a broken send forever (the
    # 2026-07-13 Yasmin Z. grind: one un-messageable lead re-drove a 45s Sales Nav goto
    # every ~2-3 minutes for 3+ hours on a throttle-scarred account).
    "fail_count": "INTEGER DEFAULT 0",
}


# Target schema version. BUMP THIS whenever _migrate adds or changes anything. The init
# runner (init_db) reads/writes PRAGMA user_version and, before applying a migration to a
# NON-fresh DB, copies linkforge.db -> linkforge.db.bak.v{old}. A failed migration is then
# auto-restored from that copy; the .bak files also allow a manual roll-back after a bad
# auto-update. This is the hard prerequisite the auto-update phase (P4) edges on.
SCHEMA_VERSION = 9   # v9: leads.connected_on / leads.connected_rank — when someone
                     #     became a connection and where they sit in the newest-first
                     #     connections list, so event invites can walk it in order.
                     # v8: event_invites — one row per person invited to one LinkedIn
                     #     event, so a re-run never invites the same person twice and the
                     #     same person can still be invited to a different event.
                     # v7: red_list — the global do-not-contact table (RED-LIST-BUILD-PLAN
                     #     2026-07-22). A cache of LinkForge/red-list.json, re-synced at the
                     #     start of every lane run. Keyed on BOTH URL namespaces (vanity
                     #     /in/<slug> + member-URN /in/ACoAA…) + lead_id + name so a
                     #     person red-listed by any identifier is caught on every lane.
                     # v6: ConversationForge F1 data spine — flow_versions/nodes/edges/
                     #     arms + the flow_stamps append-only analytic ledger + sticky
                     #     arm assignments (plan §5.1 + §6c hardening, 2026-07-15).
                     # v5: leads.last_seen_on_search_at + sequence_state.fail_count
                     #     (connect-lane rebuild + sequence dead-letter, 2026-07-13).
                     # v4: bridge_notes + bridge_field_state (the Nexus CRM bridge).
                     # v3: leads.is_connection — the connect lane must never invite a
                     #     1st-degree connection.
                     # v2: ICP scorer columns icp_score/icp_tier/icp_reason.


# --- Nexus CRM bridge (Build Plan V3, 2026-07-10) --------------------------------
# Identity is the CANONICAL URL, never leads.id: `id` is an AUTOINCREMENT surrogate
# reassigned on any DB merge or restore (the two installs were merged 2026-07-08),
# while db.py's own schema comment calls profile_url "the identity".
#
# `bridge_field_state` holds the three-way-merge ANCESTOR — the exact value the bridge
# last wrote for a field. It is NOT speculative generality for a future round-trip:
# `headline`/`company`/`role`/`location` have THREE live writers today (this bridge,
# crm.py:run_sync's last-writer-wins frontmatter refresh, and linkedin_search.py).
# The ancestor is the only thing that distinguishes "unchanged since I wrote it"
# (safe to update) from "an agent or human edited it" (surface a conflict, never
# clobber). It cannot be reconstructed after the fact, so it is stored from day one.
_BRIDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_notes (
    canonical_url TEXT PRIMARY KEY,
    lead_id       INTEGER,
    note_path     TEXT,
    pushed_at     TEXT,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS bridge_field_state (
    canonical_url     TEXT NOT NULL,
    field             TEXT NOT NULL,
    last_synced_value TEXT,
    last_synced_at    TEXT,
    PRIMARY KEY (canonical_url, field)
);
"""


# --- ConversationForge F1 (Build Plan V3, 2026-07-14; built 2026-07-15) ----------
# The flow data spine. NEW TABLES ONLY — no ALTER of hot tables (plan §5.1). Design
# carries the §6c adversarial-review hardening from day one:
#   lineage_uuid   — immutable identity ACROSS version clones, so activating a typo-fix
#                    never forks the stats (finding 7). Stats aggregate by lineage.
#   scope_campaign_id — active-per-scope: one flow per campaign, NULL = the default
#                    flow. Enforced in the activate transaction (finding 11).
#   priority       — classification precedence is explicit and first-match-wins,
#                    rendered on the canvas at F2 (finding 5).
#   typed edges    — cond_type in (pattern_ref|timeout_days|outcome|label); 'label' is
#                    display prose and NEVER a condition the engine acts on (finding 4).
#   flow_stamps    — APPEND-ONLY analytic ledger keyed by canonical profile URL (leads.id
#                    is a surrogate reassigned on merges — finding 10), with a natural
#                    event_key UNIQUE so re-runs can never double-count (finding 6),
#                    account_id from day one (§6b-19) and a cohort tag so fresh-accept
#                    vs backfill populations never silently mix (§6b-17).
#   arm assignments— sticky per-lead arm choice bound to arm content-hash lineage, stored
#                    at first exposure so arm edits don't reshuffle cohorts (finding 7).
_FLOWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lineage_uuid  TEXT NOT NULL,
    name          TEXT NOT NULL,
    scope_campaign_id INTEGER,                      -- NULL = default scope
    status        TEXT NOT NULL DEFAULT 'draft',    -- draft | active | retired
    meta          TEXT,                             -- JSON: escalation/give_bank/drafting_rules/notes
    source        TEXT NOT NULL DEFAULT 'editor',   -- editor | import
    created_at    TEXT NOT NULL,
    activated_at  TEXT,
    retired_at    TEXT,
    updated_at    TEXT NOT NULL                     -- optimistic-lock stamp (PUT graph)
);
CREATE INDEX IF NOT EXISTS idx_flowver_status ON flow_versions(status);

CREATE TABLE IF NOT EXISTS flow_nodes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    node_key   TEXT NOT NULL,                       -- 'R1', 'R1-move', 'opener-B', …
    kind       TEXT NOT NULL,                       -- opener | branch | move | terminal | escalation
    label      TEXT,
    read       TEXT,
    color      TEXT,
    patterns   TEXT,                                -- JSON array (branch kind only)
    priority   INTEGER NOT NULL DEFAULT 100,        -- classification order, lower first
    body       TEXT,                                -- opener/move copy (arms may override)
    meta       TEXT,                                -- JSON: never[], notes, …
    canvas_x   REAL,
    canvas_y   REAL,
    UNIQUE (version_id, node_key),
    FOREIGN KEY (version_id) REFERENCES flow_versions(id)
);

CREATE TABLE IF NOT EXISTS flow_edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    from_node  TEXT NOT NULL,                       -- node_key (stable across clones)
    to_node    TEXT NOT NULL,
    cond_type  TEXT NOT NULL DEFAULT 'label',       -- pattern_ref | timeout_days | outcome | label
    cond_value TEXT,
    UNIQUE (version_id, from_node, to_node, cond_type, cond_value),
    FOREIGN KEY (version_id) REFERENCES flow_versions(id)
);

CREATE TABLE IF NOT EXISTS flow_arms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id   INTEGER NOT NULL,
    node_key     TEXT NOT NULL,
    arm_key      TEXT NOT NULL,                     -- 'a', 'b', …
    body         TEXT NOT NULL,
    content_hash TEXT NOT NULL,                     -- lineage binding for arm stats
    enabled      INTEGER NOT NULL DEFAULT 1,
    retired_at   TEXT,
    UNIQUE (version_id, node_key, arm_key),
    FOREIGN KEY (version_id) REFERENCES flow_versions(id)
);

CREATE TABLE IF NOT EXISTS flow_stamps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL DEFAULT 'default',
    canonical_url TEXT,                             -- identity; lead_id is advisory
    lead_id       INTEGER,
    thread_urn    TEXT,
    version_id    INTEGER,
    lineage_uuid  TEXT,
    node_key      TEXT NOT NULL,
    arm_key       TEXT,
    arm_hash      TEXT,
    cohort        TEXT NOT NULL DEFAULT 'fresh',    -- fresh | backfill
    event         TEXT NOT NULL,                    -- matched | sent | second_exchange | booked | edge_traversed
    event_key     TEXT NOT NULL UNIQUE,             -- natural key: idempotent re-runs
    stamped_at    TEXT NOT NULL,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_stamps_event   ON flow_stamps(event);
CREATE INDEX IF NOT EXISTS idx_stamps_url     ON flow_stamps(canonical_url);
CREATE INDEX IF NOT EXISTS idx_stamps_lineage ON flow_stamps(lineage_uuid, node_key);

CREATE TABLE IF NOT EXISTS flow_arm_assignments (
    canonical_url TEXT NOT NULL,
    lineage_uuid  TEXT NOT NULL,
    node_key      TEXT NOT NULL,
    arm_key       TEXT NOT NULL,
    arm_hash      TEXT,
    assigned_at   TEXT NOT NULL,
    PRIMARY KEY (canonical_url, lineage_uuid, node_key)
);
"""


# --- Global red-list (do-not-contact) — RED-LIST-BUILD-PLAN V3 (2026-07-22) -------
# The `red_list` table is a CACHE of LinkForge/red-list.json (the human-editable SoT),
# re-imported at the start of every lane run so it can never drift from the file.
#
# The two-namespace trap it defends against: the same person carries TWO URL strings —
# the vanity `/in/<slug>` (linkforge.db `leads.profile_url`) and the member-URN
# `/in/ACoAA…` (conversations.db `participant_profile_url`). A red list keyed on only
# one silently misses the inbox/conversation lane, so BOTH forms are stored + matched,
# alongside lead_id and name. Adding a person once, by ANY identifier, is enough forever.
_REDLIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS red_list (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name   TEXT,
    canon_url   TEXT,          -- vanity  https://www.linkedin.com/in/<slug>
    member_urn  TEXT,          -- member  https://www.linkedin.com/in/ACoAA…
    lead_id     INTEGER,       -- linkforge.db leads.id when known (advisory; surrogate)
    reason      TEXT,
    category    TEXT NOT NULL DEFAULT 'other',   -- friend | client | anti-fit | other
    added_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_redlist_canon ON red_list(canon_url);
CREATE INDEX IF NOT EXISTS idx_redlist_urn   ON red_list(member_urn);
CREATE INDEX IF NOT EXISTS idx_redlist_lead  ON red_list(lead_id);
CREATE INDEX IF NOT EXISTS idx_redlist_name  ON red_list(full_name);
"""



_EVENTS_SCHEMA = """
-- v8: one row per person invited to one LinkedIn event. Keyed by (event, profile) so
-- re-running an invite never sends the same person twice, and so inviting the same
-- person to a DIFFERENT event is still allowed. leads.status can only hold one state,
-- which is why per-event history lives here rather than on the lead row.
CREATE TABLE IF NOT EXISTS event_invites (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT    NOT NULL,
    profile_url  TEXT    NOT NULL,
    lead_id      INTEGER,
    full_name    TEXT,
    campaign_id  INTEGER,
    invited_at   TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_invites_uniq  ON event_invites(event_id, profile_url);
CREATE INDEX        IF NOT EXISTS idx_event_invites_event ON event_invites(event_id);
"""


def event_invited_urls(conn, event_id: str) -> set:
    """Every profile already invited to this event, as canonical /in/ URLs."""
    return {r["profile_url"] for r in conn.execute(
        "SELECT profile_url FROM event_invites WHERE event_id=?", (str(event_id),))}


def record_event_invite(conn, *, event_id: str, profile_url: str, full_name: str | None = None,
                        lead_id: int | None = None, campaign_id: int | None = None) -> None:
    """Write one invite to the per-event history. Idempotent — a repeat is ignored."""
    conn.execute(
        "INSERT OR IGNORE INTO event_invites (event_id, profile_url, lead_id, full_name, "
        "campaign_id, invited_at) VALUES (?,?,?,?,?,?)",
        (str(event_id), profile_url, lead_id, full_name, campaign_id, _now()))


def event_invite_counts(conn, event_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM event_invites WHERE event_id=?",
                        (str(event_id),)).fetchone()[0]


def _migrate(conn) -> None:
    """Add any missing post-ship columns. Guarded per column via PRAGMA table_info."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(leads)")}
    for col, decl in _LEAD_MIGRATIONS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {decl}")
    have_m = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for col, decl in _MESSAGE_MIGRATIONS.items():
        if col not in have_m:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {decl}")
    have_s = {r["name"] for r in conn.execute("PRAGMA table_info(sequence_state)")}
    for col, decl in _SEQSTATE_MIGRATIONS.items():
        if col not in have_s:
            conn.execute(f"ALTER TABLE sequence_state ADD COLUMN {col} {decl}")
    # v4: the bridge tables. CREATE TABLE IF NOT EXISTS, so this is idempotent and
    # safe on a fresh DB (where SCHEMA runs first) and on an upgrade alike.
    conn.executescript(_BRIDGE_SCHEMA)
    # v6: the ConversationForge flow spine — same idempotent pattern, new tables only.
    conn.executescript(_FLOWS_SCHEMA)
    # v7: the global red-list (do-not-contact) cache table — idempotent, new table only.
    conn.executescript(_REDLIST_SCHEMA)
    # v8: per-event invite history — idempotent, new table only.
    conn.executescript(_EVENTS_SCHEMA)


def init_db() -> None:
    """Create the schema if absent, then apply guarded migrations behind a versioned,
    backup-before-migrate runner (PRAGMA user_version). Idempotent and safe to re-run.

    P3 (migration safety) — the hard prerequisite for auto-update: a schema change shipped
    in an update can never brick a tester's data. A NON-fresh DB that is behind SCHEMA_VERSION
    is COPIED to linkforge.db.bak.v{old} before any ALTER; if the migration throws, the copy
    is restored automatically and the error re-raised. Fresh DBs need no backup."""
    import shutil
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fresh = not DB_PATH.exists()

    # 1) Ensure tables exist (CREATE TABLE IF NOT EXISTS — always safe), read the version.
    with connect() as conn:
        conn.executescript(SCHEMA)
        cur = conn.execute("PRAGMA user_version").fetchone()[0]

    # 2) Already current: _migrate is a cheap idempotent no-op — run it for belt-and-braces.
    if cur >= SCHEMA_VERSION:
        with connect() as conn:
            _migrate(conn)
        return

    # 3) Behind: back up an EXISTING db before touching it. No backup => no migration.
    bak = None
    if not fresh and DB_PATH.exists():
        bak = DB_PATH.with_name(DB_PATH.name + f".bak.v{cur}")
        try:
            shutil.copy2(DB_PATH, bak)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"refusing to migrate without a backup ({bak.name}): {e}")

    # 4) Migrate; on ANY failure restore the backup so the user is never left half-migrated.
    try:
        with connect() as conn:
            _migrate(conn)
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
    except Exception:
        if bak and bak.exists():
            try:
                shutil.copy2(bak, DB_PATH)   # automatic one-copy restore
            except Exception:
                pass
        raise


# ---------------------------------------------------------------------------
# Global red-list (do-not-contact) — RED-LIST-BUILD-PLAN V3 (2026-07-22)
# ---------------------------------------------------------------------------
# Two layers sit on top of this: Layer A (NOT EXISTS filters on every selection query,
# so a red-listed person is invisible to the queues + cockpit) and Layer B (a send/action
# guard right before each irreversible action — the hard stop, and the ONLY layer that
# can see the conversations.db URN namespace). The SoT is a plain JSON file; the table is
# a re-synced cache. FAIL-SAFE: a corrupt/unreadable SoT RAISES (never silently disables a
# guard); a merely-absent SoT keeps the last-known cache. Matching is on POSITIVE identity
# only — a person is blocked iff one of their stored identifiers matches.

class RedListError(RuntimeError):
    """The red-list SoT is present but unreadable/malformed. Raised, never swallowed —
    a guard must never silently pass because the do-not-contact list could not be read."""


# Re-sync bookkeeping for the bulk intake path (upsert_lead): the SoT is re-imported only
# when the file's mtime changes, so a 6,000-row import syncs once, not per row, yet a fresh
# `redlist add` in another process is still picked up on its next mtime change.
_RED_LIST_MTIME = None


def _load_red_list_records(path=None) -> list:
    """Read the SoT JSON into a list of records. Missing file -> [] (a valid empty list on
    a fresh install). Present-but-malformed -> RedListError (fail-safe: never silent)."""
    p = path or RED_LIST_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise RedListError(f"red-list SoT unreadable ({p}): {e}") from e
    if not isinstance(raw, list):
        raise RedListError(f"red-list SoT must be a JSON list, got {type(raw).__name__}")
    return raw


def _apply_red_list(conn, records: list) -> int:
    """Replace the cache table to EXACTLY match the SoT records (the table is a cache, so a
    person removed from the SoT is dropped here too). Runs inside the caller's transaction."""
    conn.execute("DELETE FROM red_list")
    for r in records:
        cu = canon_in(r.get("canon_url")) or r.get("canon_url")
        conn.execute(
            "INSERT INTO red_list (full_name, canon_url, member_urn, lead_id, reason, "
            "category, added_at) VALUES (?,?,?,?,?,?,?)",
            (r.get("name") or r.get("full_name"), cu, r.get("member_urn"), r.get("lead_id"),
             r.get("reason"), (r.get("category") or "other"), r.get("added_at") or _now()))
    return len(records)


def sync_red_list_from_json(path=None) -> int:
    """Import the SoT into the cache table (called at the start of every lane run and by
    every Layer-B guard). Absent SoT -> keep the last-known cache untouched. Malformed SoT
    -> RedListError. Returns the number of people on the list."""
    global _RED_LIST_MTIME
    p = path or RED_LIST_PATH
    records = _load_red_list_records(p)   # raises on a malformed SoT
    if not p.exists():
        with connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM red_list").fetchone()[0]
    with connect() as conn:
        n = _apply_red_list(conn, records)
    try:
        _RED_LIST_MTIME = p.stat().st_mtime
    except OSError:
        _RED_LIST_MTIME = None
    return n


def _ensure_red_list_synced(conn) -> None:
    """Cheap freshness check for the bulk intake path: re-import the SoT into the table
    only when the file changed (mtime), using the caller's connection. Malformed SoT still
    raises (fail-safe). Absent SoT is a no-op that keeps whatever the table already holds."""
    global _RED_LIST_MTIME
    p = RED_LIST_PATH
    try:
        mtime = p.stat().st_mtime if p.exists() else 0
    except OSError:
        mtime = 0
    if mtime == _RED_LIST_MTIME:
        return
    records = _load_red_list_records(p)   # raises on a malformed SoT
    if p.exists():
        _apply_red_list(conn, records)
    _RED_LIST_MTIME = mtime


def is_red_listed(conn, *, url: str | None = None, urn: str | None = None,
                  lead_id: int | None = None, name: str | None = None) -> dict | None:
    """Pure table read on the given connection: return the matching red_list row (as a dict)
    or None. Any identifier is sufficient. Because a caller may hold a URL string that is
    EITHER namespace, `url` and `urn` are each tested against BOTH URL columns."""
    clauses, params = [], []
    cu = canon_in(url) or url
    if cu:
        clauses += ["canon_url = ?", "member_urn = ?"]; params += [cu, cu]
    if urn:
        clauses += ["member_urn = ?", "canon_url = ?"]; params += [urn, urn]
    if lead_id is not None:
        clauses += ["lead_id = ?"]; params += [lead_id]
    if name:
        clauses += ["LOWER(full_name) = LOWER(?)"]; params += [name]
    if not clauses:
        return None
    row = conn.execute(
        "SELECT id, full_name, canon_url, member_urn, lead_id, reason, category "
        f"FROM red_list WHERE {' OR '.join(clauses)} LIMIT 1", params).fetchone()
    return dict(row) if row else None


def red_list_match(*, url: str | None = None, urn: str | None = None,
                   lead_id: int | None = None, name: str | None = None) -> dict | None:
    """Layer-B guard entry point: re-sync the cache from the SoT (fail-safe — raises on a
    malformed SoT, never silently passes), then return the matching row or None. Opens its
    own connection, so it is safe to call from any lane (including the conversations lane,
    whose own DB is a different file)."""
    sync_red_list_from_json()
    with connect() as conn:
        return is_red_listed(conn, url=url, urn=urn, lead_id=lead_id, name=name)


def _write_red_list(records: list, path=None) -> None:
    p = path or RED_LIST_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def add_to_red_list(*, name: str | None = None, canon_url: str | None = None,
                    member_urn: str | None = None, lead_id: int | None = None,
                    reason: str | None = None, category: str = "other",
                    path=None) -> dict:
    """Add (or update) one person in the SoT, then re-sync the cache. De-dupes on any of
    canon_url / member_urn / lead_id — a second add of the same person updates in place."""
    p = path or RED_LIST_PATH
    records = _load_red_list_records(p)
    cu = canon_in(canon_url) or canon_url
    def _same(r):
        return ((cu and (canon_in(r.get("canon_url")) or r.get("canon_url")) == cu)
                or (member_urn and r.get("member_urn") == member_urn)
                or (lead_id is not None and r.get("lead_id") == lead_id))
    existing = next((r for r in records if _same(r)), None)
    if existing is not None:
        for k, v in {"name": name, "canon_url": cu, "member_urn": member_urn,
                     "lead_id": lead_id, "reason": reason, "category": category}.items():
            if v is not None:
                existing[k] = v
        rec, verb = existing, "updated"
    else:
        rec = {"name": name, "canon_url": cu, "member_urn": member_urn, "lead_id": lead_id,
               "reason": reason, "category": category or "other", "added_at": _now()}
        records.append(rec)
        verb = "added"
    _write_red_list(records, p)
    sync_red_list_from_json(p)
    return {"verb": verb, "record": rec}


def remove_from_red_list(*, canon_url: str | None = None, member_urn: str | None = None,
                         lead_id: int | None = None, name: str | None = None,
                         path=None) -> dict:
    """Remove everyone matching any identifier from the SoT, then re-sync the cache."""
    p = path or RED_LIST_PATH
    records = _load_red_list_records(p)
    cu = canon_in(canon_url) or canon_url
    def _match(r):
        return ((cu and (canon_in(r.get("canon_url")) or r.get("canon_url")) == cu)
                or (member_urn and r.get("member_urn") == member_urn)
                or (lead_id is not None and r.get("lead_id") == lead_id)
                or (name and (r.get("name") or "").lower() == name.lower()))
    kept = [r for r in records if not _match(r)]
    removed = len(records) - len(kept)
    _write_red_list(kept, p)
    sync_red_list_from_json(p)
    return {"removed": removed}


def list_red_list(path=None) -> list:
    """The SoT contents (the human-authoritative list), newest-add last."""
    return _load_red_list_records(path)


def log_event(kind: str, lead_id: int | None = None, detail: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
            (_now(), lead_id, kind, detail),
        )


def upsert_lead(conn, *, profile_url: str, full_name: str | None = None,
                company: str | None = None, title: str | None = None,
                headline: str | None = None, location: str | None = None,
                source: str | None = None, status: str = "new",
                raw_json: str | None = None, crm_note_path: str | None = None) -> str:
    """Insert a lead keyed by canonical profile_url, or update light fields if it
    already exists. Returns 'inserted' | 'updated'. Identity is profile_url, so the
    same person appearing in multiple folders de-duplicates here. Does NOT overwrite
    an existing status (pipeline state is owned by the engine, not the importer).

    RED-LIST INTAKE GUARD (2026-07-22): this is the single choke point every source flows
    through (salesnav / search / engagers / scrape-connections / csv_import / viewers), so
    a red-listed person is refused HERE — never re-collected, never resurrected from a prior
    'skipped'. Returns 'blocked'. The SoT is re-synced on any file change (cheap for bulk)."""
    _ensure_red_list_synced(conn)
    if is_red_listed(conn, url=profile_url):
        return "blocked"
    now = _now()
    row = conn.execute("SELECT id FROM leads WHERE profile_url = ?", (profile_url,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO leads (profile_url, full_name, company, title, headline, location,"
            " source, status, raw_json, crm_note_path, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (profile_url, full_name, company, title, headline, location, source,
             status, raw_json, crm_note_path, now, now),
        )
        return "inserted"
    conn.execute(
        "UPDATE leads SET full_name=COALESCE(?,full_name), company=COALESCE(?,company),"
        " title=COALESCE(?,title), headline=COALESCE(?,headline), location=COALESCE(?,location),"
        " crm_note_path=COALESCE(?,crm_note_path), updated_at=? WHERE id=?",
        (full_name, company, title, headline, location, crm_note_path, now, row["id"]),
    )
    return "updated"


def pipeline_summary() -> dict:
    """Lead counts by pipeline stage overall and per campaign — the 'where is each
    list in the pipeline' monitor."""
    out: dict = {"by_status": {}, "by_campaign": []}
    if not DB_PATH.exists():
        return out
    with connect() as conn:
        for r in conn.execute("SELECT status, COUNT(*) c FROM leads GROUP BY status"):
            out["by_status"][r["status"]] = r["c"]
        out["by_campaign"] = [dict(r) for r in conn.execute(
            "SELECT COALESCE(c.name, '(unassigned)') AS name, l.status AS status, COUNT(*) AS c "
            "FROM leads l LEFT JOIN campaigns c ON l.campaign_id = c.id "
            "GROUP BY name, l.status ORDER BY name, l.status")]
    return out


def campaigns_list() -> list[dict]:
    """Every campaign with its stored source, total leads, and per-stage counts —
    powers the multi-campaign view + metrics."""
    out: list[dict] = []
    if not DB_PATH.exists():
        return out
    with connect() as conn:
        for c in conn.execute("SELECT id, name, targeting, status, created_at FROM campaigns ORDER BY id"):
            stages = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) n FROM leads WHERE campaign_id = ? GROUP BY status", (c["id"],))}
            source = ""
            source_type = ""
            try:
                tgt = (json.loads(c["targeting"]) or {}) if c["targeting"] else {}
                source = tgt.get("ref", "")
                source_type = tgt.get("source_type", "")
            except Exception:
                source = source_type = ""
            # FT-5: `campaigns.status` is a LIFECYCLE marker (active|paused|done) — NOT the run-state.
            # A bare `status:"active"` let federation/API consumers misread a drafted-but-off campaign
            # as "running". Expose the raw value as `lifecycle`, a distinct `run_state:null` (run-state
            # is derived by the client from engine + daemon + enabled steps, exactly as the Campaigns
            # UI does — this endpoint cannot assert it), and relabel the ambiguous "active" in the
            # back-compat `status` alias to "enabled" so it can't be read as "currently sending".
            _lifecycle = c["status"]
            _status_alias = "enabled" if _lifecycle == "active" else _lifecycle
            out.append({"id": c["id"], "name": c["name"], "source": source,
                        "source_type": source_type,
                        "lifecycle": _lifecycle, "run_state": None,
                        "status": _status_alias, "stages": stages, "total": sum(stages.values())})
    return out


def recent_activity(limit: int = 20) -> list[dict]:
    """Human-readable activity feed for the Home dashboard — newest first, drawn
    from the events table (lane actions) joined to lead names where present."""
    if not DB_PATH.exists():
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.ts, e.kind, e.detail, l.full_name "
            "FROM events e LEFT JOIN leads l ON e.lead_id = l.id "
            "ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def lead_history(lead_id: int) -> dict:
    """Everything known about one lead for the timeline drawer: the lead row, its
    event trail, and its message history (oldest first)."""
    if not DB_PATH.exists():
        return {}
    with connect() as conn:
        lead = conn.execute(
            "SELECT l.*, COALESCE(c.name,'(unassigned)') AS campaign "
            "FROM leads l LEFT JOIN campaigns c ON l.campaign_id=c.id "
            "WHERE l.id=?", (lead_id,)).fetchone()
        if lead is None:
            return {}
        events = [dict(r) for r in conn.execute(
            "SELECT ts, kind, detail FROM events WHERE lead_id=? ORDER BY id", (lead_id,))]
        msgs = [dict(r) for r in conn.execute(
            "SELECT sent_at, step_index, body, status FROM messages WHERE lead_id=? "
            "ORDER BY id", (lead_id,))]
        seq = conn.execute(
            "SELECT current_step, next_due_at, status FROM sequence_state "
            "WHERE lead_id=? ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
    return {"lead": dict(lead), "events": events, "messages": msgs,
            "sequence": dict(seq) if seq else None}


def leads_list(campaign_id: int | None = None, status: str | None = None,
               name_like: str | None = None, limit: int = 300) -> dict:
    """Individual leads for the app's lead browser — WHO exactly is held, filterable
    by campaign/status/name. Returns {'total': N, 'rows': [...]} so the UI can say
    'showing X of N'."""
    if not DB_PATH.exists():
        return {"total": 0, "rows": []}
    where, args = "WHERE 1=1", []
    if campaign_id is not None:
        where += " AND l.campaign_id = ?"
        args.append(campaign_id)
    if status:
        where += " AND l.status = ?"
        args.append(status)
    if name_like:
        where += " AND l.full_name LIKE ?"
        args.append(f"%{name_like}%")
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM leads l {where}", args).fetchone()["c"]
        rows = [dict(r) for r in conn.execute(
            "SELECT l.id, l.full_name, l.headline, l.status, l.profile_url, "
            "l.icp_score, l.icp_tier, l.icp_reason, "
            "COALESCE(c.name, '(unassigned)') AS campaign "
            f"FROM leads l LEFT JOIN campaigns c ON l.campaign_id = c.id {where} "
            # scored leads first (best fit on top), unscored after, then alphabetical
            "ORDER BY l.icp_score IS NULL, l.icp_score DESC, l.full_name COLLATE NOCASE LIMIT ?",
            [*args, limit])]
    return {"total": total, "rows": rows}


def skip_lead(lead_id: int) -> str:
    """Take a lead out of every send lane (connect/inmail/drip all select on
    status) WITHOUT deleting the row — the row is what stops a re-collect from
    resurrecting them. Prior status is kept in the event log for restore."""
    with connect() as conn:
        row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return "not found"
        prior = row["status"]
        if prior == "skipped":
            return "already skipped"
        now = _now()
        conn.execute("UPDATE leads SET status='skipped', updated_at=? WHERE id=?", (now, lead_id))
        conn.execute("INSERT INTO events (ts, lead_id, kind, detail) VALUES (?,?,?,?)",
                     (now, lead_id, "lead-skipped", prior))
    return prior


def restore_lead(lead_id: int) -> str:
    """Undo skip_lead — puts the lead back at the status it held when skipped
    (from the event log; defaults to 'collected' if no record)."""
    with connect() as conn:
        if conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone() is None:
            return "not found"
        ev = conn.execute(
            "SELECT detail FROM events WHERE lead_id=? AND kind='lead-skipped' "
            "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        prior = (ev["detail"] if ev and ev["detail"] else "collected")
        conn.execute("UPDATE leads SET status=?, updated_at=? WHERE id=?",
                     (prior, _now(), lead_id))
    return prior


# ---------------------------------------------------------------------------
# Composite campaigns — a campaign is an ordered chain of COMPONENTS, stored in
# campaigns.targeting JSON. Each component is one lane the leads flow through.
# The single source of truth for which lead statuses belong to which component
# (the flow view and any future executor both read this map, so they agree):
# ---------------------------------------------------------------------------

# Human-readable names for every internal lead status — the UI shows THESE, never
# the raw codes. One source of truth (flow boxes, lead rows, filters, cards).
STATUS_DISPLAY = {
    "new": "Imported",
    "collected": "Collected",
    "queued_connect": "Waiting to invite",
    "invited": "Invited — awaiting accept",
    "accepted": "Connected — they accepted our invite",
    # A person we were ALREADY connected to (imported roster). Deliberately NOT 'accepted':
    # 'accepted' is a connect-funnel outcome (we invited, they said yes) and feeds the
    # accept-rate + safety posture. These never went through the funnel.
    "connection": "Connection (already had)",
    "queued_message": "Waiting to message",
    "messaged": "Messaged",
    "replied": "Replied 🎉",
    "inmailed": "InMailed",
    "in_sequence": "In sequence",
    "event_invited": "Event invite sent",
    "event_invited_no_response": "Event — invited, no reply",
    "event_attending": "Event — attending 🎟",
    "event_messaged": "Event — reminded",
    "done": "Done",
    "skipped": "Removed",
}


def status_label(code: str) -> str:
    return STATUS_DISPLAY.get(code, code.replace("_", " ").title())


COMPONENT_TYPES = ["collect", "connect", "message", "inmail"]

COMPONENT_STAGES = {
    "collect": ["collected"],
    "connect": ["queued_connect", "invited", "accepted"],
    "message": ["queued_message", "messaged", "replied"],
    "inmail":  ["inmailed"],
}

COMPONENT_LABEL = {
    "collect": "Collect",
    "connect": "Connect",
    "message": "Message (drip)",
    "inmail":  "InMail",
}


def _targeting(conn, campaign_id: int) -> dict:
    row = conn.execute("SELECT targeting FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not row or not row["targeting"]:
        return {}
    try:
        return json.loads(row["targeting"]) or {}
    except Exception:
        return {}


def campaign_get(campaign_id: int) -> dict | None:
    """One campaign with its parsed targeting (source + components)."""
    if not DB_PATH.exists():
        return None
    with connect() as conn:
        c = conn.execute("SELECT id, name, type, targeting, status, created_at "
                         "FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not c:
            return None
        tgt = {}
        try:
            tgt = json.loads(c["targeting"]) if c["targeting"] else {}
        except Exception:
            tgt = {}
        return {"id": c["id"], "name": c["name"], "type": c["type"],
                "status": c["status"], "source": tgt.get("ref", ""),
                "source_type": tgt.get("source_type", ""),
                "components": tgt.get("components", [])}


def create_composite_campaign(name: str, source_type: str, source_ref: str,
                              components: list[dict]) -> dict:
    """Create (or update) a campaign with an ordered component chain. Idempotent
    on name — re-running with the same name updates the source + components rather
    than duplicating, so the builder and salesnav --collect converge on one row."""
    now = _now()
    has_drip = any(c.get("type") == "message" for c in components)
    ctype = "connect+drip" if has_drip else "connect"
    targeting = json.dumps({"source_type": source_type, "ref": source_ref,
                            "components": components})
    with connect() as conn:
        row = conn.execute("SELECT id FROM campaigns WHERE name = ?", (name,)).fetchone()
        if row:
            conn.execute("UPDATE campaigns SET type=?, targeting=?, status='active' WHERE id=?",
                         (ctype, targeting, row["id"]))
            return {"id": row["id"], "created": False}
        cur = conn.execute(
            "INSERT INTO campaigns (name, type, targeting, status, created_at) VALUES (?,?,?,?,?)",
            (name, ctype, targeting, "active", now))
        return {"id": cur.lastrowid, "created": True}


def _clean_steps(steps) -> list[dict]:
    return [{"template": (s.get("template") or "Openers"),
             "wait_days": max(0, int(s.get("wait_days", 0) or 0))} for s in (steps or [])]


def campaign_variants(campaign_id: int) -> list[dict]:
    """Read the message component's A/B VARIANTS (normalised): [{name, steps:[{template,
    wait_days}]}]. Backward compatible: a component with a single `steps`/`template` (no
    variants) reads as one variant "A". None reads as a single empty variant "A"."""
    c = campaign_get(campaign_id)
    if not c:
        return [{"name": "A", "steps": []}]
    for comp in c["components"]:
        if comp.get("type") == "message":
            vs = comp.get("variants")
            if vs:
                return [{"name": v.get("name") or chr(65 + i), "steps": _clean_steps(v.get("steps"))}
                        for i, v in enumerate(vs)]
            if comp.get("steps"):
                return [{"name": "A", "steps": _clean_steps(comp["steps"])}]
            if comp.get("template"):
                return [{"name": "A", "steps": [{"template": comp["template"],
                                                 "wait_days": int(comp.get("wait_days", 0) or 0)}]}]
            return [{"name": "A", "steps": []}]
    return [{"name": "A", "steps": []}]


def set_campaign_variants(campaign_id: int, variants: list[dict]) -> bool:
    """Set the A/B variants on the campaign's `message` component (adds the component if
    absent). Canonical component order preserved. Variants supersede a legacy single `steps`."""
    c = campaign_get(campaign_id)
    if not c:
        return False
    clean = [{"name": (v.get("name") or chr(65 + i)), "steps": _clean_steps(v.get("steps"))}
             for i, v in enumerate(variants)] or [{"name": "A", "steps": []}]
    comps = list(c["components"])
    msg = next((comp for comp in comps if comp.get("type") == "message"), None)
    if msg is not None:
        msg["variants"] = clean
        msg.pop("steps", None); msg.pop("template", None); msg.pop("wait_days", None)
    else:
        comps.append({"type": "message", "variants": clean})
    order = {t: i for i, t in enumerate(COMPONENT_TYPES)}
    comps.sort(key=lambda comp: order.get(comp.get("type"), 99))
    create_composite_campaign(c["name"], c["source_type"], c["source"], comps)
    return True


def set_campaign_source(campaign_id: int, source_type: str, source_ref: str = "") -> bool:
    """Change a campaign's lead source (salesnav / search / csv / engagers / events / viewers)."""
    c = campaign_get(campaign_id)
    if not c:
        return False
    comps = list(c["components"])
    if not any(x.get("type") == "collect" for x in comps):   # ensure a collect step exists
        comps.append({"type": "collect"})
        order = {t: i for i, t in enumerate(COMPONENT_TYPES)}
        comps.sort(key=lambda x: order.get(x.get("type"), 99))
    create_composite_campaign(c["name"], source_type, source_ref, comps)
    return True


def set_campaign_component(campaign_id: int, comp_type: str, present: bool = True) -> bool:
    """Add or remove a campaign step (collect / connect / inmail). Preserves an existing
    component's config when re-asserting it; message variants are managed separately."""
    c = campaign_get(campaign_id)
    if not c or comp_type not in COMPONENT_TYPES:
        return False
    existing = next((x for x in c["components"] if x.get("type") == comp_type), None)
    comps = [x for x in c["components"] if x.get("type") != comp_type]
    if present:
        comps.append(existing or {"type": comp_type})
    order = {t: i for i, t in enumerate(COMPONENT_TYPES)}
    comps.sort(key=lambda x: order.get(x.get("type"), 99))
    create_composite_campaign(c["name"], c["source_type"], c["source"], comps)
    return True


def campaign_message_steps(campaign_id: int, variant: str | None = None) -> list[dict]:
    """The steps for one variant (default: the first, "A"). Variant-aware; back-compatible."""
    vs = campaign_variants(campaign_id)
    if variant:
        m = next((v for v in vs if v["name"] == variant), None)
        return m["steps"] if m else (vs[0]["steps"] if vs else [])
    return vs[0]["steps"] if vs else []


def set_campaign_message_steps(campaign_id: int, steps: list[dict], variant: str = "A") -> bool:
    """Set one variant's steps (default variant "A"), preserving the others. The canvas /
    sequence editor save through this; A/B just targets a named variant."""
    vs = campaign_variants(campaign_id)
    found = next((v for v in vs if v["name"] == variant), None)
    if found:
        found["steps"] = _clean_steps(steps)
    else:
        vs.append({"name": variant, "steps": _clean_steps(steps)})
    return set_campaign_variants(campaign_id, vs)


def campaign_flow(campaign_id: int) -> dict | None:
    """The visual-flow model: each component box with its live per-stage lead
    counts + a total, in chain order, PLUS any lead statuses present in the
    campaign that no component claims (so the view never hides leads)."""
    c = campaign_get(campaign_id)
    if c is None:
        return None
    with connect() as conn:
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) n FROM leads WHERE campaign_id = ? GROUP BY status",
            (campaign_id,))}
    boxes, claimed = [], set()
    for comp in c["components"]:
        t = comp.get("type")
        stages = COMPONENT_STAGES.get(t, [])
        claimed.update(stages)
        counts = [(s, by_status.get(s, 0)) for s in stages]
        boxes.append({"type": t, "label": COMPONENT_LABEL.get(t, t),
                      "config": comp, "stages": counts,
                      "total": sum(n for _, n in counts)})
    other = {s: n for s, n in by_status.items()
             if s not in claimed and s != "skipped" and n}
    skipped = by_status.get("skipped", 0)
    return {"campaign": c, "boxes": boxes, "other": other,
            "skipped": skipped, "total": sum(by_status.values())}


def delete_campaign(campaign_id: int) -> dict:
    """Delete a campaign and all its leads (plus their invites / sequence / messages)."""
    with connect() as conn:
        lead_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM leads WHERE campaign_id = ?", (campaign_id,))]
        for lid in lead_ids:
            conn.execute("DELETE FROM invites WHERE lead_id = ?", (lid,))
            conn.execute("DELETE FROM sequence_state WHERE lead_id = ?", (lid,))
            conn.execute("DELETE FROM messages WHERE lead_id = ?", (lid,))
        conn.execute("DELETE FROM leads WHERE campaign_id = ?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    return {"leads_deleted": len(lead_ids)}


def reset_all(keep_campaigns: bool = False) -> dict:
    """Wipe operational state to a clean slate (back up the DB file first).
    Clears leads / invites / messages / sequence_state / events — and campaigns too
    unless keep_campaigns. Config + templates are untouched (separate files). Returns
    counts removed + the backup path. This is engine state only, fully re-derivable
    (re-import the vault, re-collect from sources)."""
    import shutil
    from datetime import datetime
    backup = None
    if DB_PATH.exists():
        backup = DB_PATH.with_name(f"linkforge-backup-{datetime.now():%Y%m%d-%H%M%S-%f}.db")
        shutil.copy2(DB_PATH, backup)
    removed = {}
    with connect() as conn:
        for t in ("leads", "invites", "messages", "sequence_state", "sequence_steps", "events"):
            removed[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            conn.execute(f"DELETE FROM {t}")
        if not keep_campaigns:
            removed["campaigns"] = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
            conn.execute("DELETE FROM campaigns")
            conn.execute("DELETE FROM sequences")
        conn.execute("DELETE FROM sqlite_sequence")   # reset AUTOINCREMENT counters
    return {"removed": removed, "backup": str(backup) if backup else None}


def clear_messages() -> dict:
    """Delete all sent-message history (the messages table). Leaves leads/campaigns.
    Useful to reset the drip's no-repeat memory or clear test sends."""
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        conn.execute("DELETE FROM messages")
    return {"messages_deleted": n}


def delete_leads_by_status(status: str) -> dict:
    """Delete every lead at one pipeline stage (and its invites/messages/seq rows).
    Lets the app clear, e.g., all 'collected' or all 'queued_message' without nuking
    the whole DB."""
    with connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM leads WHERE status = ?", (status,))]
        for lid in ids:
            conn.execute("DELETE FROM invites WHERE lead_id = ?", (lid,))
            conn.execute("DELETE FROM messages WHERE lead_id = ?", (lid,))
            conn.execute("DELETE FROM sequence_state WHERE lead_id = ?", (lid,))
        conn.execute("DELETE FROM leads WHERE status = ?", (status,))
    return {"leads_deleted": len(ids), "status": status}


def status_list() -> list[tuple[str, int]]:
    """Distinct lead statuses present + their counts — drives the Manage-tab picker."""
    if not DB_PATH.exists():
        return []
    with connect() as conn:
        return [(r["status"], r["n"]) for r in conn.execute(
            "SELECT status, COUNT(*) n FROM leads GROUP BY status ORDER BY n DESC")]


def counts() -> dict:
    """At-a-glance state for the status command."""
    out: dict = {"leads_by_status": {}, "invites_by_status": {}, "campaigns": 0, "sequences": 0}
    if not DB_PATH.exists():
        return out
    with connect() as conn:
        for row in conn.execute("SELECT status, COUNT(*) c FROM leads GROUP BY status"):
            out["leads_by_status"][row["status"]] = row["c"]
        for row in conn.execute("SELECT status, COUNT(*) c FROM invites GROUP BY status"):
            out["invites_by_status"][row["status"]] = row["c"]
        out["campaigns"] = conn.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0]
        out["sequences"] = conn.execute("SELECT COUNT(*) FROM sequences").fetchone()[0]
    return out
