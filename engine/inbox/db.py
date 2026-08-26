"""db.py — the inbox half's own local SQLite (no cloud, ever).

The inbox unit is the CONVERSATION (keyed by LinkedIn thread URN), optionally linked to
a the parent program lead post-merge (lead_id, dark until then). Tags/notes/snooze/archive live
on the conversation so threads with no campaign lead are full CRM citizens. Schema is
byte-compatible with the parent program/KONDO-STANDALONE-BUILD-PLAN.md §V3.3 so the later merge
into the parent program is an INSERT…SELECT, not a reconciliation.

v0.1 uses CREATE TABLE IF NOT EXISTS only (all tables are net-new). Column additions
later follow the parent program's guarded-ALTER pattern (PRAGMA table_info → ALTER if absent).
"""
from __future__ import annotations

import json as _json
import sqlite3
import time

from . import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_urn    TEXT UNIQUE NOT NULL,
    lead_id       INTEGER,                       -- FK -> the parent program leads, post-merge (nullable)
    participant_name        TEXT,
    participant_headline    TEXT,
    participant_profile_url TEXT,
    last_preview  TEXT,
    last_msg_dir  TEXT,                          -- 'in' | 'out' (drives reply-pending)
    last_msg_at   TEXT,
    list_hash     TEXT,                          -- md5(urn|preview): unchanged-skip signal
    note          TEXT,
    follow_up_at  TEXT,                          -- snooze/resurface (NULL = not snoozed)
    follow_up_fired INTEGER NOT NULL DEFAULT 0,  -- a due snooze fires its toast once
    archived_at   TEXT,                          -- local triage flag; never a LinkedIn action
    pinned        INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_followup ON conversations(follow_up_at);
CREATE INDEX IF NOT EXISTS idx_conv_archived ON conversations(archived_at);
-- the inbox list sorts by (pinned, last_msg_at) every load; index it so the sort stays fast as the inbox grows
CREATE INDEX IF NOT EXISTS idx_conv_lastmsg ON conversations(pinned, last_msg_at);

CREATE TABLE IF NOT EXISTS conversation_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    direction       TEXT,                        -- 'in' | 'out'
    body            TEXT,
    seq             INTEGER,                     -- order within the thread (timestamps are fragile)
    created_at      TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON conversation_messages(conversation_id);

CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    color      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_tags (
    conversation_id INTEGER NOT NULL,
    tag_id          INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, tag_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id)          REFERENCES tags(id)          ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS snippets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE NOT NULL,
    body       TEXT NOT NULL,                    -- {{first_name}} substituted at insert time
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(DB_PATH, timeout=15.0)   # F0.5b: WAL + busy wait (see db.py)
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    cx.execute("PRAGMA busy_timeout = 15000")
    try:
        cx.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return cx


def init() -> None:
    cx = connect()
    try:
        cx.executescript(SCHEMA)
        # guarded ALTERs for columns added after v0.1 (the parent program's migration pattern)
        cols = {r["name"] for r in cx.execute("PRAGMA table_info(conversations)")}
        if "participant_avatar" not in cols:
            cx.execute("ALTER TABLE conversations ADD COLUMN participant_avatar TEXT")
        if "unread" not in cols:
            cx.execute("ALTER TABLE conversations ADD COLUMN unread INTEGER NOT NULL DEFAULT 0")
        mcols = {r["name"] for r in cx.execute("PRAGMA table_info(conversation_messages)")}
        if "audio" not in mcols:
            cx.execute("ALTER TABLE conversation_messages ADD COLUMN audio TEXT")
        if "attach" not in mcols:
            cx.execute("ALTER TABLE conversation_messages ADD COLUMN attach TEXT")
        cx.commit()
    finally:
        cx.close()


def upsert_conversation(cx, *, thread_urn, name=None, preview=None, last_dir=None,
                        profile_url=None, list_hash=None, avatar=None, headline=None,
                        last_at=None, unread=None) -> int:
    """Insert or update a conversation by thread_urn. COALESCE keeps existing values
    when a field is not supplied this pass. Returns the conversation id."""
    now = _now()
    row = cx.execute("SELECT id FROM conversations WHERE thread_urn=?", (thread_urn,)).fetchone()
    if row:
        cx.execute(
            """UPDATE conversations SET
                 participant_name=COALESCE(?, participant_name),
                 last_preview=COALESCE(?, last_preview),
                 last_msg_dir=COALESCE(?, last_msg_dir),
                 participant_profile_url=COALESCE(?, participant_profile_url),
                 participant_avatar=COALESCE(?, participant_avatar),
                 participant_headline=COALESCE(?, participant_headline),
                 last_msg_at=COALESCE(?, last_msg_at),
                 unread=COALESCE(?, unread),
                 list_hash=COALESCE(?, list_hash),
                 updated_at=?
               WHERE id=?""",
            (name, preview, last_dir, profile_url, avatar, headline, last_at, unread, list_hash, now, row["id"]))
        cx.commit()
        return row["id"]
    cur = cx.execute(
        """INSERT INTO conversations
             (thread_urn, participant_name, last_preview, last_msg_dir,
              participant_profile_url, participant_avatar, participant_headline,
              last_msg_at, unread, list_hash, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (thread_urn, name, preview, last_dir, profile_url, avatar, headline,
         last_at, (unread or 0), list_hash, now, now))
    cx.commit()
    return cur.lastrowid


def set_avatar_by_name(cx, name: str, avatar: str) -> int:
    """Update the avatar for all conversations matching a participant name. Returns rows hit.
    Used by the list-only avatar backfill (the list shows photos without opening threads)."""
    cur = cx.execute("UPDATE conversations SET participant_avatar=? WHERE participant_name=?",
                     (avatar, name))
    cx.commit()
    return cur.rowcount


def replace_messages(cx, conversation_id: int, msgs: list[dict]) -> None:
    """Re-write a thread's messages (idempotent re-sync) and stamp last_synced_at."""
    now = _now()
    cx.execute("DELETE FROM conversation_messages WHERE conversation_id=?", (conversation_id,))
    cx.executemany(
        "INSERT INTO conversation_messages (conversation_id, direction, body, seq, audio, attach, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [(conversation_id, m["dir"], m["text"], i, m.get("audio") or None,
          (_json.dumps(m["file"]) if m.get("file") else None), now)
         for i, m in enumerate(msgs)])
    cx.execute("UPDATE conversations SET last_synced_at=? WHERE id=?", (now, conversation_id))
    cx.commit()


def append_message(cx, conversation_id: int, direction: str, body: str) -> None:
    """Append a single message to a thread (e.g. one we just SENT) and bump the
    conversation's last-* fields so it shows immediately + bubbles to the top of the list,
    without waiting for the next full sync. seq continues from the current max."""
    now = _now()
    seq = cx.execute("SELECT COALESCE(MAX(seq), -1) + 1 AS s FROM conversation_messages "
                     "WHERE conversation_id = ?", (conversation_id,)).fetchone()["s"]
    cx.execute("INSERT INTO conversation_messages (conversation_id, direction, body, seq, created_at) "
               "VALUES (?,?,?,?,?)", (conversation_id, direction, body, seq, now))
    cx.execute("UPDATE conversations SET last_preview=?, last_msg_dir=?, last_msg_at=?, "
               "updated_at=? WHERE id=?",
               ((body or "")[:200], direction, str(int(time.time() * 1000)), now, conversation_id))
    cx.commit()


def counts(cx) -> dict:
    c = cx.execute("SELECT COUNT(*) n FROM conversations").fetchone()["n"]
    m = cx.execute("SELECT COUNT(*) n FROM conversation_messages").fetchone()["n"]
    return {"conversations": c, "messages": m}


# ---------------------------------------------------------------------------
# CRM layer — the Kondo features (tags, notes, snooze, archive) the API serves.
# All local; archive is a LOCAL triage flag, NEVER a LinkedIn action.
# ---------------------------------------------------------------------------

def conversation_tags(cx, conv_id: int) -> list[dict]:
    return [dict(r) for r in cx.execute(
        "SELECT t.id, t.name, t.color FROM tags t "
        "JOIN conversation_tags ct ON ct.tag_id = t.id "
        "WHERE ct.conversation_id = ? ORDER BY t.name", (conv_id,))]


# split-inbox boxes -> the WHERE clause that defines each rail filter.
_BOX_WHERE = {
    "focused":   "c.archived_at IS NULL",
    "unread":    "c.archived_at IS NULL AND c.unread > 0",
    "needs-you": "c.archived_at IS NULL AND c.last_msg_dir = 'in'",
    "snoozed":   "c.archived_at IS NULL AND c.follow_up_at IS NOT NULL",
    "archived":  "c.archived_at IS NOT NULL",
    "all":       "1=1",
}


def list_conversations(cx, *, box="focused", tag_id=None, q=None, limit=200) -> list[dict]:
    where = [_BOX_WHERE.get(box, _BOX_WHERE["focused"])]
    params: list = []
    if tag_id:
        where.append("c.id IN (SELECT conversation_id FROM conversation_tags WHERE tag_id = ?)")
        params.append(tag_id)
    if q:
        where.append("(c.participant_name LIKE ? OR c.last_preview LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    # sort by REAL last-activity time (last_msg_at, ms epoch as text — equal-length numeric
    # strings sort correctly; NULLs from old DOM syncs fall last in DESC). Most-recent first.
    sql = ("SELECT c.* FROM conversations c WHERE " + " AND ".join(where) +
           " ORDER BY c.pinned DESC, c.last_msg_at DESC, c.updated_at DESC LIMIT ?")
    params.append(limit)
    rows = [dict(r) for r in cx.execute(sql, params)]
    for r in rows:
        r["tags"] = conversation_tags(cx, r["id"])
    return rows


def get_conversation(cx, conv_id: int) -> dict | None:
    r = cx.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["tags"] = conversation_tags(cx, conv_id)
    msgs = []
    for m in cx.execute("SELECT direction, body, seq, audio, attach FROM conversation_messages "
                        "WHERE conversation_id = ? ORDER BY seq", (conv_id,)):
        row = dict(m)
        if row.get("attach"):
            try:
                row["attach"] = _json.loads(row["attach"])
            except Exception:
                row["attach"] = None
        msgs.append(row)
    d["messages"] = msgs
    return d


def box_counts(cx) -> dict:
    out = {b: cx.execute(f"SELECT COUNT(*) n FROM conversations c WHERE {w}").fetchone()["n"]
           for b, w in _BOX_WHERE.items()}
    return out


def archive_box(cx, box="focused", tag_id=None) -> int:
    """Archive (clear) every active conversation in a box/label — the Inbox-Zero button.
    Scope is derived from the SAME _BOX_WHERE that list_conversations + box_counts use, so
    'Clear' can never archive more than the count shown in the confirm (the Unread-box footgun:
    the old code only narrowed needs-you/snoozed, so clearing Unread archived the WHOLE inbox
    while the dialog quoted the unread count). Returns how many were archived. Local only —
    never a LinkedIn action."""
    where = [_BOX_WHERE.get(box, _BOX_WHERE["focused"])]
    params: list = []
    if tag_id:
        where.append("c.id IN (SELECT conversation_id FROM conversation_tags WHERE tag_id = ?)")
        params.append(tag_id)
    now = _now()
    # only ever stamp rows not already archived (keeps 'archived'/'all' a no-op on cleared rows)
    sql = ("UPDATE conversations SET archived_at = ?, updated_at = ? "
           "WHERE archived_at IS NULL AND id IN "
           "(SELECT c.id FROM conversations c WHERE " + " AND ".join(where) + ")")
    cur = cx.execute(sql, [now, now] + params)
    cx.commit()
    return cur.rowcount


def mark_read(cx, conv_id: int, unread: int = 0) -> None:
    cx.execute("UPDATE conversations SET unread = ? WHERE id = ?", (unread, conv_id))
    cx.commit()


def unarchive_all(cx) -> int:
    """Restore every archived conversation back to the inbox (undo a Clear)."""
    cur = cx.execute("UPDATE conversations SET archived_at = NULL, updated_at = ? "
                     "WHERE archived_at IS NOT NULL", (_now(),))
    cx.commit()
    return cur.rowcount


def set_note(cx, conv_id: int, note: str) -> None:
    cx.execute("UPDATE conversations SET note = ?, updated_at = ? WHERE id = ?",
               (note, _now(), conv_id))
    cx.commit()


def set_snooze(cx, conv_id: int, until: str | None) -> None:
    # setting/clearing a snooze resets the fired guard so it can toast again when due
    cx.execute("UPDATE conversations SET follow_up_at = ?, follow_up_fired = 0, "
               "updated_at = ? WHERE id = ?", (until, _now(), conv_id))
    cx.commit()


def set_archive(cx, conv_id: int, archived: bool) -> None:
    cx.execute("UPDATE conversations SET archived_at = ?, updated_at = ? WHERE id = ?",
               (_now() if archived else None, _now(), conv_id))
    cx.commit()


def set_pinned(cx, conv_id: int, pinned: bool) -> None:
    cx.execute("UPDATE conversations SET pinned = ?, updated_at = ? WHERE id = ?",
               (1 if pinned else 0, _now(), conv_id))
    cx.commit()


# --- tags -------------------------------------------------------------------

def list_tags(cx) -> list[dict]:
    out = []
    for t in cx.execute("SELECT * FROM tags ORDER BY name"):
        d = dict(t)
        d["count"] = cx.execute(
            "SELECT COUNT(*) n FROM conversation_tags ct JOIN conversations c "
            "ON c.id = ct.conversation_id WHERE ct.tag_id = ? AND c.archived_at IS NULL",
            (t["id"],)).fetchone()["n"]
        out.append(d)
    return out


def create_tag(cx, name: str, color: str | None = None) -> dict:
    cx.execute("INSERT OR IGNORE INTO tags (name, color, created_at) VALUES (?,?,?)",
               (name, color, _now()))
    cx.commit()
    return dict(cx.execute("SELECT * FROM tags WHERE name = ?", (name,)).fetchone())


def delete_tag(cx, tag_id: int) -> None:
    cx.execute("DELETE FROM tags WHERE id = ?", (tag_id,))   # cascades conversation_tags
    cx.commit()


def set_conversation_tag(cx, conv_id: int, tag_id: int, on: bool) -> None:
    if on:
        cx.execute("INSERT OR IGNORE INTO conversation_tags (conversation_id, tag_id) "
                   "VALUES (?,?)", (conv_id, tag_id))
    else:
        cx.execute("DELETE FROM conversation_tags WHERE conversation_id = ? AND tag_id = ?",
                   (conv_id, tag_id))
    cx.commit()


# --- snippets ---------------------------------------------------------------

def list_snippets(cx) -> list[dict]:
    return [dict(r) for r in cx.execute("SELECT * FROM snippets ORDER BY name")]


def upsert_snippet(cx, name: str, body: str) -> dict:
    cx.execute("INSERT INTO snippets (name, body, updated_at) VALUES (?,?,?) "
               "ON CONFLICT(name) DO UPDATE SET body = excluded.body, "
               "updated_at = excluded.updated_at", (name, body, _now()))
    cx.commit()
    return dict(cx.execute("SELECT * FROM snippets WHERE name = ?", (name,)).fetchone())


def delete_snippet(cx, snippet_id: int) -> None:
    cx.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
    cx.commit()


def conversations_due(cx, now_iso: str) -> list[dict]:
    """Snoozes that are due and not yet fired (for the snooze daemon, P6)."""
    return [dict(r) for r in cx.execute(
        "SELECT * FROM conversations WHERE follow_up_at IS NOT NULL "
        "AND follow_up_at <= ? AND follow_up_fired = 0 AND archived_at IS NULL", (now_iso,))]


def mark_followup_fired(cx, conv_id: int) -> None:
    cx.execute("UPDATE conversations SET follow_up_fired = 1 WHERE id = ?", (conv_id,))
    cx.commit()


def export_rows(cx) -> list[dict]:
    """Flat rows for CSV/Obsidian export (the privacy wedge — your data, local, portable)."""
    out = []
    for c in cx.execute("SELECT * FROM conversations ORDER BY participant_name"):
        d = dict(c)
        tags = ", ".join(t["name"] for t in conversation_tags(cx, d["id"]))
        n = cx.execute("SELECT COUNT(*) n FROM conversation_messages WHERE conversation_id = ?",
                       (d["id"],)).fetchone()["n"]
        out.append({
            "name": d["participant_name"] or "",
            "headline": d["participant_headline"] or "",
            "profile_url": d["participant_profile_url"] or "",
            "last_dir": d["last_msg_dir"] or "",
            "tags": tags,
            "note": d["note"] or "",
            "snooze": d["follow_up_at"] or "",
            "archived": "yes" if d["archived_at"] else "",
            "messages": n,
            "thread_urn": d["thread_urn"],
        })
    return out
