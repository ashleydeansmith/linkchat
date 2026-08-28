"""flow_run.py — the pass: one bounded walk over everyone in a flow who is due.

WHY THIS EXISTS (Build Plan V3 §6, 2026-08-27). `sequence.tick` sent the one step a
campaign carried and marked the person finished. This module walks the program
flow_steps derived from Ashley's flow file — the whole ladder, the reply branches, the
silence rule, the re-activation section — one step per person per pass, and writes the
next wake-up from the moment the step actually completed.

Shape, and the reasons for it:
  * A person is a ROW in `journeys`: where they are (`node_key`), when they wake
    (`next_wake_at`), what they wait for (`waiting_for`), the moment the wait is measured
    from (`anchor_at`). Nothing is a live timer; the PC sleeping is a non-event.
  * Exactly-once is the DATABASE's job: `journey_sends.send_key` is UNIQUE on
    lead:lineage:node:anchor, written at 'intended' BEFORE the browser acts. A crash
    between 'intended' and 'sent' parks the person for a human on the next pass; a
    `skipped` return writes 'abandoned' and does not park (critique-of-V2 Part 2c).
  * The reply read comes FIRST, from the inbox mirror, with no browser — joined on
    `conversations.lead_id` only, never a name (critique-of-V2 Finding 1). A person
    whose thread is unlinked is parked for a human, never sent blind.
  * The mirror-age gate reads `sync_runs` (the sync's record of itself), never
    MAX(last_synced_at). Stale mirror = no sends this pass; non-LinkedIn steps still run.
  * Due people are split into one list PER ACTION TYPE and a cap refusal ends that type's
    list only — `continue`, never `break` (sequence.py:341-343 broke the whole loop).
  * Priority is Ashley's `send_priority` (2026-08-24): responders' branch moves first,
    then the ladder newest-accept first; openers keep a reserved slice of the cap.
  * The clock and the database are parameters, the sender is one function, so a test
    runs a person to day 200 in a second against a scratch file (V3 §10).

What this module does NOT do (later phases): classify a reply (Phase 2 — a reply here
pauses the journey for a human), run the withdraw / event-invite / booking / red-list /
CRM actions (Phase 3–4 — they are accepted by flow_check and marked `not_built` here),
or read the vault's command file (Phase 4).

Zero LLM. Deterministic.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import db, emit_result
from . import flow_steps as fs
from . import flows_engine as fe

# THE HOST'S HOOK (LinkChat lift, Build Plan V3 §13). When config `flow_auto_send` is off —
# absent counts as off — a due send is never carried by this file. Its words are written to
# journey_sends as 'staged', the person waits for 'approval', and this callable, set by the
# host program and never by this file, is told so it can put the words in front of a human:
#     STAGER(lead_row: dict, bubbles: list[str], send_key: str, node_key: str, ref: str) -> str | None
# The host's own send road then carries the approved words and calls release(send_key, "sent").
STAGER: Callable | None = None
# THE HOST'S SENDER (2026-08-28). A step marked `"auto_send": true` in the flow file sends
# itself even when the global switch is off (a stage the member switched on). Where the host
# has no sender of its own for the engine to call (LinkChat), it sets this to its ONE road
# out, and the engine never grows a second one:
#     SENDER(page, lead_row: dict, bubbles: list[str], rung: int, ctx: dict) -> outcome
# outcome as the sender's: 'sent' | 'replied' | 'skipped' | 'unconfirmed' | 'failed:...'
SENDER: Callable | None = None

AGENT = "linkforge-flow"
LADDER = "R0"
# per_pass 10 (was 25, 2026-08-27 evening): the daemon runs one lane at a time, so while a
# pass sends nothing else runs — including the 5-minute inbox refresh the reply read needs
# (R-K: answered within 20 minutes). Ten sends at 35-140 s is a 10-20 minute pass; passes
# chain every 5 minutes, so the day's throughput is the same and the inbox is read between.
DEFAULT_BUDGET = {"per_type": 25, "per_pass": 10, "seconds": 3600, "mirror_max_age_min": 30, "opener_reserve": 10}
# THE ATTENTION ORDER (ruling 2026-08-27 ~21:40). Every message the engine sends belongs to
# one of these kinds, and a pass works them in this order with ONE shared send budget, so a
# day full of replies starves the ladder and a quiet day fills it. Changeable in config
# (`flow_priority`) as things change.
ATTENTION_DEFAULT = ["reply", "new_accept", "event", "old_connection", "ladder"]
NOT_BUILT: set = set()                          # every step kind is built (Phase 4 closed the set)
ACT_ACTIONS = {"red_list", "withdraw_invite", "invite_to_event"}   # Phase 3, flow_actions.py
SEND_ACTIONS = {"send", "send_opener", "send_booking_link"}


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _ms_iso(ms) -> str | None:
    try:
        v = int(str(ms).strip())
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc).isoformat() if v > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# schema: one column the Phase 0 tables did not carry
# ---------------------------------------------------------------------------

# The three tables, kept in step with db.py's SCHEMA (LinkForge) — here so the engine can
# stand up in a database that has never seen them (LinkChat's), and so a scratch database
# in a test needs nothing but this file.
_JOURNEY_DDL = [
    """CREATE TABLE IF NOT EXISTS journeys (
        id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, lineage_uuid TEXT NOT NULL,
        flow_version_id INTEGER, node_key TEXT NOT NULL, ladder_cycle INTEGER NOT NULL DEFAULT 0,
        waiting_for TEXT NOT NULL DEFAULT 'clock', next_wake_at TEXT, anchor_at TEXT,
        expected_rungs INTEGER NOT NULL DEFAULT 4, sends_done INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active', arm_key TEXT, parked_from_node TEXT, join_how TEXT,
        fail_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_journeys_due ON journeys(status, waiting_for, next_wake_at)",
    "CREATE INDEX IF NOT EXISTS idx_journeys_lead ON journeys(lead_id)",
    """CREATE TABLE IF NOT EXISTS journey_sends (
        id INTEGER PRIMARY KEY AUTOINCREMENT, send_key TEXT NOT NULL UNIQUE, journey_id INTEGER NOT NULL,
        lead_id INTEGER NOT NULL, node_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'intended',
        intended_at TEXT NOT NULL, resolved_at TEXT, outcome TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_journey_sends_status ON journey_sends(status, intended_at)",
    """CREATE TABLE IF NOT EXISTS journey_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, journey_id INTEGER NOT NULL, lead_id INTEGER NOT NULL,
        at TEXT NOT NULL, kind TEXT NOT NULL, node_key TEXT, detail TEXT)""",
    # People the flow must never put in a sequence (clients, members, people Ashley names):
    # a standing list, one row per person, with the reason and who said so (2026-08-28).
    """CREATE TABLE IF NOT EXISTS leads_excluded (
        lead_id INTEGER PRIMARY KEY, reason TEXT NOT NULL, by_whom TEXT, at TEXT NOT NULL)""",
]


def ensure_schema(conn) -> None:
    for ddl in _JOURNEY_DDL:
        conn.execute(ddl)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(journeys)")}
    if "words_table" not in cols:
        # which by_arm table a transfer told the ladder to use ('cold' | 'matched')
        conn.execute("ALTER TABLE journeys ADD COLUMN words_table TEXT")
    if "branch_key" not in cols:
        # the reply branch the person landed on (kept while they sit on the silence rule)
        conn.execute("ALTER TABLE journeys ADD COLUMN branch_key TEXT")
    if "override_words" not in cols:
        # the teach loop: Ashley's own answer for THIS person, as a JSON list of bubbles,
        # sent by the next pass in place of the step's words, then cleared
        conn.execute("ALTER TABLE journeys ADD COLUMN override_words TEXT")
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(journey_sends)")}
    if "words" not in scols:
        # the words a STAGED send carries (flow_auto_send off): a JSON list of bubbles,
        # written for a human to read and release through the host's own send road
        conn.execute("ALTER TABLE journey_sends ADD COLUMN words TEXT")
    if "released_by" not in scols:
        conn.execute("ALTER TABLE journey_sends ADD COLUMN released_by TEXT")


# ---------------------------------------------------------------------------
# the program
# ---------------------------------------------------------------------------

def active_program(conn) -> tuple[dict | None, dict]:
    """(active version row, program) — a fresh read per pass, so an activation is seen
    by the very next pass and a draft is never walked."""
    v = fe.active_version(conn)
    if not v:
        return None, {"branches": {}, "sections": {}}
    return v, fs.read_program(conn, v["id"])


def ladder_rungs(program: dict) -> int:
    """How many sends the ladder promises, INCLUDING the opener (expected_rungs)."""
    steps = (program["branches"].get(LADDER) or {}).get("steps", [])
    return sum(1 for s in steps if s.get("do") in SEND_ACTIONS)


# ---------------------------------------------------------------------------
# events and sends
# ---------------------------------------------------------------------------

def _event(conn, journey_id: int, lead_id: int, kind: str, node_key: str | None,
           detail=None, at: str | None = None) -> None:
    conn.execute("INSERT INTO journey_events (journey_id, lead_id, at, kind, node_key, detail) "
                 "VALUES (?,?,?,?,?,?)",
                 (journey_id, lead_id, at or _iso(utcnow()), kind, node_key,
                  json.dumps(detail, ensure_ascii=False) if isinstance(detail, (dict, list)) else detail))


def send_key(lead_id: int, lineage: str, node_key: str, anchor_at: str | None) -> str:
    return f"{lead_id}:{lineage}:{node_key}:{anchor_at or ''}"


def answer_delay_minutes(lead_id: int, cfg=None) -> int:
    """How long after a reply arrives its matched move goes: a deterministic pick inside
    [reply_answer_min_minutes, reply_answer_max_minutes] — inside the 20-minute rule
    (R-K) and never instant (an answer 90 s after every reply is a tell)."""
    lo = int(getattr(cfg, "reply_answer_min_minutes", 4) or 4)
    hi = int(getattr(cfg, "reply_answer_max_minutes", 18) or 18)
    if hi < lo:
        lo, hi = hi, lo
    h = int(hashlib.md5(f"answer:{lead_id}".encode()).hexdigest(), 16)
    return lo + h % (hi - lo + 1)


def _wait_delta(wait: dict | None, lead_id: int, cfg=None) -> timedelta:
    """A step's wait as a timedelta. `days` as before; `minutes: "answer"` is R-K's window."""
    wait = wait or {}
    if "minutes" in wait:
        m = wait["minutes"]
        return timedelta(minutes=answer_delay_minutes(lead_id, cfg) if m == "answer" else int(m or 0))
    return timedelta(days=int(wait.get("days") or 0))


# ---------------------------------------------------------------------------
# enrolment
# ---------------------------------------------------------------------------

def arm_for_lead(lead_id: int) -> str:
    """The opener arm — the rule sequence.py has always used: md5(lead_id) % 2 -> B / C."""
    return "B" if int(hashlib.md5(str(lead_id).encode()).hexdigest(), 16) % 2 == 0 else "C"


def enrol(lead_id: int, now: datetime | None = None, accepted_at: str | None = None, *,
          arm: str | None = None, allow_connection: bool = False) -> int | None:
    """Create the journey for a freshly accepted person at R0.opener. Returns the journey
    id, or None when there is no active flow, no ladder, or the person is already in one.
    Never enrols a pre-existing connection (is_connection=1) unless `allow_connection`
    (the supply step, enrol_connections) — and then on the arm it names (D)."""
    now = now or utcnow()
    with db.connect() as conn:
        ensure_schema(conn)
        v, program = active_program(conn)
        if not v or not (program["branches"].get(LADDER) or {}).get("steps"):
            return None
        lead = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not lead or ((lead["is_connection"] or 0) == 1 and not allow_connection):
            return None
        acc = accepted_at or lead["accepted_at"]
        if not acc and allow_connection:
            acc = _lead_val(lead, "connected_on") or _iso(now)   # the day we connected, else today
        if not acc:
            return None
        # never a second opener, never a red-listed person
        if conn.execute("SELECT 1 FROM messages WHERE lead_id=? AND status='sent' LIMIT 1", (lead_id,)).fetchone():
            return None
        db.sync_red_list_from_json()
        if conn.execute("SELECT 1 FROM red_list WHERE lead_id=? OR canon_url=? OR member_urn=?",
                        (lead_id, lead["profile_url"], lead["profile_url"])).fetchone():
            return None
        if conn.execute("SELECT 1 FROM journeys WHERE lead_id=? AND lineage_uuid=?",
                        (lead_id, v["lineage_uuid"])).fetchone():
            return None
        if conn.execute("SELECT 1 FROM leads_excluded WHERE lead_id=?", (lead_id,)).fetchone():
            return None          # a client, a member, or somebody Ashley named: never in a sequence
        first = program["branches"][LADDER]["steps"][0]
        wait = int((first.get("wait") or {}).get("days") or 0)
        anchor = _parse(acc) or now
        arm_used = arm or arm_for_lead(lead_id)
        cur = conn.execute(
            "INSERT INTO journeys (lead_id, lineage_uuid, flow_version_id, node_key, ladder_cycle, "
            "waiting_for, next_wake_at, anchor_at, expected_rungs, sends_done, status, arm_key, "
            "words_table, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lead_id, v["lineage_uuid"], v["id"], f"{LADDER}.{first['key']}", 0, "clock",
             _iso(anchor + timedelta(days=wait)), _iso(anchor), ladder_rungs(program), 0,
             "active", arm_used, "cold", _iso(now), _iso(now)))
        jid = cur.lastrowid
        _event(conn, jid, lead_id, "enrolled", f"{LADDER}.{first['key']}",
               {"arm": arm_used, "expected_rungs": ladder_rungs(program),
                "existing_connection": bool(allow_connection)}, _iso(now))
        return jid


def exclude(lead_id: int, reason: str, by_whom: str = "Ashley", now: datetime | None = None) -> bool:
    """Put a person on the standing never-sequence list. An active journey is ended with a
    'retired' event so no scheduled message follows. True when newly added."""
    now = now or utcnow()
    with db.connect() as conn:
        ensure_schema(conn)
        if conn.execute("SELECT 1 FROM leads_excluded WHERE lead_id=?", (lead_id,)).fetchone():
            return False
        conn.execute("INSERT INTO leads_excluded (lead_id, reason, by_whom, at) VALUES (?,?,?,?)", (lead_id, reason, by_whom, _iso(now)))
        for j in conn.execute("SELECT id, node_key FROM journeys WHERE lead_id=? AND status IN ('active','paused_reply','needs_attention','parked')", (lead_id,)).fetchall():
            conn.execute("UPDATE journeys SET status='done', waiting_for='nothing', updated_at=? WHERE id=?", (_iso(now), j["id"]))
            _event(conn, j["id"], lead_id, "retired", j["node_key"], {"why": f"excluded: {reason}", "by": by_whom}, _iso(now))
        return True


def opener_d_ready(conn, v, program: dict) -> tuple[bool, str]:
    """True when the active flow can open a conversation with an EXISTING connection: the
    words openers.D.text exist and the ladder's first follow-up names arm D on the cold
    table. Ashley writes those words separately (ruling 2026-08-27)."""
    words = fs.words_for(conn, v["id"], "openers.D.text")
    if not words:
        return False, "the flow file has no openers.D.text — the words for someone who accepted weeks ago are still to be written"
    steps = (program["branches"].get(LADDER) or {}).get("steps") or []
    f1 = next((s for s in steps if s.get("key") == "f1"), None)
    ba = ((f1 or {}).get("words") or {}).get("by_arm") if isinstance((f1 or {}).get("words"), dict) else None
    if not ba:
        return False, "the ladder has no follow-up 1 split by arm"
    cold = ba.get("cold") if all(isinstance(x, dict) for x in ba.values()) else ba
    if "D" not in (cold or {}):
        return False, "the ladder's follow-up 1 has no words for arm D on the cold table"
    return True, "ready"


def enrol_connections(per_day: int = 200, commit: bool = False, now: datetime | None = None) -> dict:
    """THE SUPPLY STEP (ruling 2026-08-27 ~21:15): 200 new people a day from the existing
    first-degree connections we have never written to — no message of ours on record AND
    no thread at all in the inbox copy (a thread means some conversation already happened;
    those are a second pool for a later ruling) — newest first (connections-page rank where
    we have it, else the day we connected, else the day we first saw them). Real profile
    addresses only (a Sales Navigator address is not a person the sender can open).
    Refuses until opener D has words. Preview unless --commit."""
    now = now or utcnow()
    out = {"per_day": int(per_day), "commit": bool(commit), "candidates": 0, "enrolled": 0}
    with db.connect() as conn:
        ensure_schema(conn)
        v, program = active_program(conn)
        if not v:
            out["stopped"] = "no active flow"
            return out
        ok, why = opener_d_ready(conn, v, program)
        out["opener_d"] = why
        if not ok:
            out["stopped"] = why
            return out
        db.sync_red_list_from_json()
        conn.execute("ATTACH DATABASE ? AS mirror", (str(_conv_db_path()),))
        try:
            rows = conn.execute("""
                SELECT l.id FROM leads l
                WHERE l.is_connection = 1
                  AND l.profile_url LIKE '%/in/%'
                  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.lead_id = l.id)
                  AND NOT EXISTS (SELECT 1 FROM journeys j WHERE j.lead_id = l.id)
                  AND NOT EXISTS (SELECT 1 FROM red_list r WHERE r.lead_id = l.id OR r.canon_url = l.profile_url)
                  AND NOT EXISTS (SELECT 1 FROM leads_excluded e WHERE e.lead_id = l.id)
                  AND NOT EXISTS (SELECT 1 FROM mirror.conversations x WHERE x.lead_id = l.id)
                ORDER BY CASE WHEN l.connected_rank IS NULL THEN 1 ELSE 0 END, l.connected_rank ASC,
                         COALESCE(l.connected_on, l.accepted_at, l.created_at) DESC
                LIMIT ?""", (int(per_day),)).fetchall()
        finally:
            conn.execute("DETACH DATABASE mirror")
        ids = [r["id"] for r in rows]
    out["candidates"] = len(ids)
    if commit:
        for lid in ids:
            if enrol(lid, now=now, arm="D", allow_connection=True):
                out["enrolled"] += 1
    return out


# ---------------------------------------------------------------------------
# the mirror: replies and freshness
# ---------------------------------------------------------------------------

def _conv_db_path():
    try:
        from .conversations import db as cdb      # LinkForge
    except ImportError:
        from .inbox import db as cdb              # LinkChat keeps its inbox under inbox/
    return cdb.DB_PATH


def mirror_gate(now: datetime, max_age_min: int) -> tuple[bool, str]:
    """True when the newest OK sync finished inside max_age_min and reached back past the
    previous OK run's start (no activity window fell between two pages)."""
    import sqlite3
    try:
        cx = sqlite3.connect(f"file:{_conv_db_path()}?mode=ro", uri=True)
        cx.row_factory = sqlite3.Row
        try:
            rows = cx.execute("SELECT * FROM sync_runs WHERE ok=1 AND finished_at IS NOT NULL "
                              "ORDER BY finished_at DESC LIMIT 2").fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                # an inbox copy that does not record its syncs (LinkChat's): there is no
                # staleness to measure, so the pass's own reply read and the sender's checks
                # are the guard — a stage switched on there must not be refused forever
                cx.close()
                return True, "the inbox copy does not record its syncs; the reply read stands"
            raise
        cx.close()
    except Exception as e:  # noqa: BLE001
        return False, f"sync_runs unreadable: {type(e).__name__}"
    if not rows:
        return False, "no successful sync recorded"
    latest = rows[0]
    fin = _parse(latest["finished_at"])
    if not fin or (now - fin) > timedelta(minutes=max_age_min):
        return False, f"mirror is {int((now - fin).total_seconds() // 60) if fin else '?'} min old (> {max_age_min})"
    if len(rows) > 1 and latest["oldest_last_activity_seen"] and rows[1]["started_at"]:
        oldest = _ms_iso(latest["oldest_last_activity_seen"])
        if oldest and oldest > rows[1]["started_at"]:
            return False, "coverage gap: the newest sync's oldest item is later than the previous sync's start"
    return True, "fresh"


def read_replies(conn, now: datetime, program: dict | None = None) -> dict:
    """The mirror read. For every active journey: a thread with `last_msg_dir='in'` and
    `last_msg_at` later than our anchor (our last send) is a reply -> paused for a human
    (Phase 2 classifies). No thread linked -> parked as unlinked. Joined on lead_id only."""
    import sqlite3
    rep = {"replies": 0, "unlinked": 0}
    # 'approval' too: a reply that arrives while the words sit with a human is still a reply
    rows = conn.execute("SELECT * FROM journeys WHERE status='active' AND waiting_for IN ('clock','reply','approval')").fetchall()
    if not rows:
        return rep
    if program is None:
        program = active_program(conn)[1]
    try:
        cx = sqlite3.connect(f"file:{_conv_db_path()}?mode=ro", uri=True)
        cx.row_factory = sqlite3.Row
    except Exception:
        return rep
    try:
        # the newest successful sync: a thread can only be expected in the mirror once a
        # sync has run AFTER our last send (plus the backfill's chance to link it)
        try:
            last_sync = cx.execute("SELECT finished_at FROM sync_runs WHERE ok=1 AND finished_at IS NOT NULL "
                                   "ORDER BY finished_at DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            last_sync = None      # an inbox that does not record its syncs (LinkChat's): never park as unlinked
        last_sync_at = _parse(last_sync["finished_at"]) if last_sync else None
        ccols = {r[1] for r in cx.execute("PRAGMA table_info(conversations)")}
        jh_col = "join_how" if "join_how" in ccols else "NULL AS join_how"
        for j in rows:
            th = cx.execute(f"SELECT last_msg_dir, last_msg_at, {jh_col}, thread_urn FROM conversations "
                            "WHERE lead_id=? ORDER BY last_msg_at DESC LIMIT 1", (j["lead_id"],)).fetchone()
            if th is None:
                anchor = _parse(j["anchor_at"])
                mirror_had_a_chance = bool(last_sync_at and anchor and last_sync_at > anchor + timedelta(minutes=30))
                if j["sends_done"] > 0 and mirror_had_a_chance:
                    # we have written to this person, the mirror has synced since, and still no
                    # thread is linked to them: their reply could not be seen, so never send blind
                    conn.execute("UPDATE journeys SET waiting_for='human', updated_at=? WHERE id=?", (_iso(now), j["id"]))
                    _event(conn, j["id"], j["lead_id"], "unlinked_thread", j["node_key"], None, _iso(now))
                    rep["unlinked"] += 1
                continue
            inbound = _ms_iso(th["last_msg_at"]) if th["last_msg_dir"] == "in" else None
            if th["last_msg_dir"] == "out" and j["sends_done"] > 0:
                # OUR message is the newest on the thread. If the engine did not send it,
                # Ashley stepped in ("sometimes I'll jump in ... that's absolutely fine",
                # 2026-08-27). The engine stands back: no scheduled rung lands on top of
                # his words. The person moves to the stall rule, measured from HIS message.
                ours = _ms_iso(th["last_msg_at"])
                if ours and _by_hand(conn, j, ours, now):
                    _stand_back_for_human_message(conn, program, j, ours, now)
                    rep["by_hand"] = rep.get("by_hand", 0) + 1
                    continue
            if inbound and inbound > (j["anchor_at"] or ""):
                _event(conn, j["id"], j["lead_id"], "reply_seen", j["node_key"],
                       {"at": inbound, "thread": th["thread_urn"], "join_how": th["join_how"]}, _iso(now))
                rep["replies"] += 1
                text = _reply_text(cx, th, j)
                r = classify_reply(conn, program, j, text, inbound, now)
                rep[r] = rep.get(r, 0) + 1
    finally:
        cx.close()
    return rep


def _by_hand(conn, j, ours: str, now: datetime) -> bool:
    """True when an outbound message at `ours` (ISO) is later than this journey's anchor by
    more than ten minutes and no engine send for this person resolved within fifteen minutes
    of it — that is, somebody typed it. The engine's own sends are on journey_sends; the old
    engine's are the anchor itself (the migration anchored on them)."""
    anchor = _parse(j["anchor_at"])
    at = _parse(ours)
    if not at or not anchor or at <= anchor + timedelta(minutes=10):
        return False
    lo, hi = _iso(at - timedelta(minutes=15)), _iso(at + timedelta(minutes=15))
    engine = conn.execute("SELECT 1 FROM journey_sends WHERE lead_id=? AND status='sent' AND resolved_at BETWEEN ? AND ? LIMIT 1",
                          (j["lead_id"], lo, hi)).fetchone()
    if engine:
        return False
    already = conn.execute("SELECT 1 FROM journey_events WHERE journey_id=? AND kind='by_hand' AND detail LIKE ? LIMIT 1",
                           (j["id"], f'%"at": "{ours}"%')).fetchone()
    return already is None


def _stand_back_for_human_message(conn, program: dict, j, ours: str, now: datetime) -> None:
    """Ashley wrote to this person himself: their journey leaves whatever rung it stood on
    and waits on the stall rule from his message (👀 at three days, then the ladder). The
    branch they were on is kept, so a reply is still read against that stage's words."""
    from .config import Config
    cfg = Config.load()
    node = f"{fs.PARENT_KEY}.eyes"
    try:
        steps, idx = fs.steps_for(program, node)
        due = _parse(ours) + _wait_delta(steps[idx].get("wait"), j["lead_id"], cfg)
    except Exception:  # noqa: BLE001 — a flow with no stall rule: the person simply waits for him
        conn.execute("UPDATE journeys SET waiting_for='human', updated_at=? WHERE id=?", (_iso(now), j["id"]))
        _event(conn, j["id"], j["lead_id"], "by_hand", j["node_key"], {"at": ours, "to": "human", "why": "no stall rule in the flow"}, _iso(now))
        return
    conn.execute("UPDATE journeys SET node_key=?, anchor_at=?, next_wake_at=?, waiting_for='clock', status='active', updated_at=? WHERE id=?",
                 (node, ours, _iso(due), _iso(now), j["id"]))
    _event(conn, j["id"], j["lead_id"], "by_hand", j["node_key"], {"at": ours, "to": node, "due": _iso(due)}, _iso(now))


def _reply_text(cx, th, j) -> str:
    """Their newest message(s) after our last send: the stored bodies when the mirror has
    them with a time, else the thread's last preview. Never our own words."""
    conv = cx.execute("SELECT id, last_preview FROM conversations WHERE thread_urn=?", (th["thread_urn"],)).fetchone()
    if conv is None:
        return ""
    try:
        rows = cx.execute("SELECT body FROM conversation_messages WHERE conversation_id=? AND direction='in' "
                          "AND sent_at IS NOT NULL AND sent_at > ? ORDER BY sent_at", (conv["id"], j["anchor_at"] or "")).fetchall()
    except sqlite3.OperationalError:
        rows = []                 # no sent_at column (LinkChat's inbox): the thread's last preview stands
    if rows:
        return "\n".join(r["body"] or "" for r in rows)
    return conv["last_preview"] or ""


def _words_only_verdict(text: str, branches: list[dict]):
    """The reader LinkChat has: the first branch, in priority order, whose word list appears
    in the reply. Sends only when that branch has words of its own. Same shape as
    reply_read.Verdict so the caller cannot tell the two apart."""
    from types import SimpleNamespace
    hit = fe.classify_ordered(text, [(b["id"], b.get("patterns") or []) for b in branches])
    has_words = bool(hit) and any(b["id"] == hit and b.get("templates") for b in branches)
    return SimpleNamespace(branch=hit, send=has_words, word_list=hit, read=None,
                           reasons=[f"word list matched {hit}" if hit else "no word list matched",
                                    "no reader installed: word lists only"])


def classify_reply(conn, program: dict, j, text: str, reply_at: str, now: datetime) -> str:
    """R-C / R-J: match the reply against THIS stage's closed list of branches — word lists
    first, a judge only when config says so and only to choose from that list. A clean
    match with words of its own -> the branch's move, due `answer` minutes after the reply
    arrived, sent by the next pass. Anything else -> paused for Ashley with the candidates
    (`unmatched_reply`), and his answer becomes a branch (the teach loop).
    Returns 'matched' | 'unmatched'."""
    from .config import Config
    try:
        from . import reply_read
    except ImportError:
        reply_read = None         # LinkChat carries no reader: the branches' word lists alone decide
    cfg = Config.load()
    cands = fs.candidates_for(program, j["node_key"], j["branch_key"])
    lead = conn.execute("SELECT * FROM leads WHERE id=?", (j["lead_id"],)).fetchone()   # member_urn: LinkForge only
    verdict = None
    if cands and text.strip() and reply_read is None:
        verdict = _words_only_verdict(text, fs.branch_dicts(program, cands))
    elif cands and text.strip():
        verdict = reply_read.decide([text], branches=fs.branch_dicts(program, cands),
                                    use_reader=(str(getattr(cfg, "reply_judge", "off")).lower() in ("haiku", "local")),
                                    name=_lead_val(lead, "full_name"), url=_lead_val(lead, "profile_url"),
                                    urn=_lead_val(lead, "member_urn"), lead_id=j["lead_id"])
    detail = {"reply_at": reply_at, "text": (text or "")[:500], "candidates": cands,
              "word_list": getattr(verdict, "word_list", None), "read": getattr(verdict, "read", None),
              "branch": getattr(verdict, "branch", None), "reasons": getattr(verdict, "reasons", None),
              "judge": str(getattr(cfg, "reply_judge", "off"))}
    _event(conn, j["id"], j["lead_id"], "classified", j["node_key"], detail, _iso(now))
    branch = verdict.branch if (verdict and verdict.send) else None
    steps = (program["branches"].get(branch) or {}).get("steps") if branch else None
    if branch and steps and steps[0].get("do") == "send":
        anchor = _parse(reply_at) or now
        due = anchor + _wait_delta(steps[0].get("wait"), j["lead_id"], cfg)
        conn.execute("UPDATE journeys SET node_key=?, branch_key=?, anchor_at=?, next_wake_at=?, waiting_for='clock', "
                     "status='active', updated_at=? WHERE id=?",
                     (f"{branch}.{steps[0]['key']}", branch, _iso(anchor), _iso(due), _iso(now), j["id"]))
        _event(conn, j["id"], j["lead_id"], "transferred", f"{branch}.{steps[0]['key']}",
               {"from": j["node_key"], "why": "reply matched", "due": _iso(due)}, _iso(now))
        return "matched"
    # the branch the reader recognised is kept even when it refused to send (R4 "not for
    # me" has no words of its own) — Ashley sees what it was read as
    recognised = getattr(verdict, "branch", None) or getattr(verdict, "word_list", None)
    conn.execute("UPDATE journeys SET status='paused_reply', waiting_for='human', branch_key=COALESCE(?, branch_key), updated_at=? WHERE id=?",
                 (recognised, _iso(now), j["id"]))
    _event(conn, j["id"], j["lead_id"], "unmatched_reply", j["node_key"],
           {"reply_at": reply_at, "text": (text or "")[:500], "candidates": cands,
            "reasons": getattr(verdict, "reasons", None) or (["no candidates at this stage"] if not cands else ["empty reply"])}, _iso(now))
    return "unmatched"


# ---------------------------------------------------------------------------
# words
# ---------------------------------------------------------------------------

def _name_tools():
    """first_name_of and personalise: drip's where the host has a working drip (LinkForge);
    names.first_name_of plus a plain {var} fill where it does not (LinkChat's drip is a
    dead file). A gap the fill cannot resolve is LEFT IN — the host's send gate refuses it."""
    try:
        from . import drip
        return drip.first_name_of, drip.personalise
    except Exception:  # noqa: BLE001 — an ImportError from drip's own imports, not only a missing module
        from . import names

        def _personalise(text, fields):
            for k, v in (fields or {}).items():
                if v:
                    text = text.replace("{" + k + "}", str(v))
            return text
        return names.first_name_of, _personalise


def _lead_val(row, key: str):
    """A column that one host's leads table has and the other's may not (greet_name)."""
    try:
        return row[key] if row is not None and key in row.keys() else None
    except Exception:  # noqa: BLE001
        return None


def resolve_words(conn, version_id: int, step: dict, journey) -> tuple[list[str], str | None]:
    """The bubbles a step sends for this person, personalised. (bubbles, ref)."""
    first_name_of, personalise = _name_tools()
    w = step.get("words")
    ref = None
    if journey["override_words"]:
        try:
            ob = json.loads(journey["override_words"])
            if isinstance(ob, list) and ob:
                lead = conn.execute("SELECT * FROM leads WHERE id=?", (journey["lead_id"],)).fetchone()
                fn = (_lead_val(lead, "greet_name") or first_name_of(_lead_val(lead, "full_name"))) or "there"
                return [personalise(str(b).replace("{name}", fn), {"first_name": fn}) for b in ob if str(b).strip()], "override"
        except Exception:  # noqa: BLE001
            pass
    if isinstance(w, str):
        ref = w
    elif isinstance(w, dict):
        ba = w.get("by_arm") or {}
        if ba and all(isinstance(v, dict) for v in ba.values()):
            table = journey["words_table"] or w.get("table") or "cold"
            ref = (ba.get(table) or ba.get(w.get("table") or "cold") or {}).get(journey["arm_key"])
        else:
            ref = ba.get(journey["arm_key"])
    if not ref:
        return [], None
    bubbles = fs.words_for(conn, version_id, ref)
    if step.get("do") == "send_booking_link" or any("{booking_link}" in b for b in bubbles):
        from . import flow_actions as FA
        url, _rule = FA.booking_link(conn, version_id)
        if step.get("do") == "send_booking_link" and url and not any("{booking_link}" in b for b in bubbles):
            bubbles = bubbles + [url]
        bubbles = FA.fill_booking_link(bubbles, url)
    lead = conn.execute("SELECT * FROM leads WHERE id=?",
                        (journey["lead_id"],)).fetchone()
    fn = (_lead_val(lead, "greet_name") or first_name_of(_lead_val(lead, "full_name"))) or "there"
    fields = {"first_name": fn, "company": (lead["company"] if lead else "") or "",
              "title": (lead["title"] if lead else "") or "", "location": (lead["location"] if lead else "") or ""}
    out = [personalise(b.replace("{name}", fn), fields) for b in bubbles]
    return [b for b in out if b.strip()], ref


# ---------------------------------------------------------------------------
# advancing
# ---------------------------------------------------------------------------

def _apply_advance(conn, j, program: dict, steps: list[dict], idx: int, now: datetime,
                   completed_at: datetime, outcome_kind: str) -> None:
    """Move the row to the step after `idx`, anchored at completed_at. The last step of an
    owner leaves the person `done` unless it transferred or parked (those set their own)."""
    nxt = idx + 1
    owner = j["node_key"].split(".")[0]
    if j["override_words"] and outcome_kind == "sent":
        # a taught answer (R-C): Ashley's words went in place of the step's. The person
        # is now in conversation on the taught branch — which may live only in a draft
        # version until he activates it — so they take the shared silence rule directly.
        sil = ((program["sections"].get(fs.PARENT_KEY) or {}).get("silence") or {}).get("steps") or []
        if sil:
            due = completed_at + _wait_delta(sil[0].get("wait"), j["lead_id"])
            conn.execute("UPDATE journeys SET node_key=?, anchor_at=?, next_wake_at=?, waiting_for='clock', override_words=NULL, updated_at=? WHERE id=?",
                         (f"{fs.PARENT_KEY}.{sil[0]['key']}", _iso(completed_at), _iso(due), _iso(now), j["id"]))
        else:
            conn.execute("UPDATE journeys SET waiting_for='human', anchor_at=?, next_wake_at=NULL, override_words=NULL, updated_at=? WHERE id=?",
                         (_iso(completed_at), _iso(now), j["id"]))
        return
    if nxt >= len(steps):
        # the end of an owner's steps. A reply BRANCH hands the person to the silence rule
        # it inherits (in_conversation.eyes, measured from this send), waiting for their
        # reply; a terminal branch (no silence rule) goes to Ashley — R7 wants a call, and
        # nothing after a "no" is the machine's to say. The ladder and the sections end
        # in their own transfer/park steps and never reach here in a v8 flow.
        branch = j["branch_key"] if owner in program["sections"] else owner
        sil = fs.silence_steps_for(program, branch) if branch in program["branches"] else []
        parent = (program["branches"].get(branch) or {}).get("parent")
        if owner in program["branches"] and owner != LADDER and sil and parent:
            first = sil[0]
            due = completed_at + _wait_delta(first.get("wait"), j["lead_id"])
            conn.execute("UPDATE journeys SET node_key=?, branch_key=?, anchor_at=?, next_wake_at=?, waiting_for='clock', "
                         "override_words=NULL, updated_at=? WHERE id=?",
                         (f"{parent}.{first['key']}", owner, _iso(completed_at), _iso(due), _iso(now), j["id"]))
            return
        if owner in program["branches"] and owner != LADDER:
            conn.execute("UPDATE journeys SET status='active', waiting_for='human', branch_key=?, anchor_at=?, next_wake_at=NULL, "
                         "override_words=NULL, updated_at=? WHERE id=?", (owner, _iso(completed_at), _iso(now), j["id"]))
            _event(conn, j["id"], j["lead_id"], "handed_to_human", j["node_key"], {"why": f"{owner} is a terminal branch"}, _iso(now))
            return
        conn.execute("UPDATE journeys SET status='done', waiting_for='nothing', anchor_at=?, next_wake_at=NULL, updated_at=? WHERE id=?",
                     (_iso(completed_at), _iso(now), j["id"]))
        return
    ns = steps[nxt]
    wait = ns.get("wait") or {"days": 0, "from": "previous_send"}
    base = completed_at
    if wait.get("from") == "accepted":
        acc = conn.execute("SELECT accepted_at FROM leads WHERE id=?", (j["lead_id"],)).fetchone()
        base = _parse(acc["accepted_at"] if acc else None) or completed_at
    due = base + _wait_delta(wait, j["lead_id"])
    conn.execute("UPDATE journeys SET node_key=?, anchor_at=?, next_wake_at=?, waiting_for='clock', override_words=NULL, updated_at=? WHERE id=?",
                 (f"{owner}.{ns['key']}", _iso(base), _iso(due), _iso(now), j["id"]))


def _do_transfer(conn, j, program: dict, step: dict, now: datetime) -> None:
    to = step.get("to")
    owner, _, key = to.partition(".")
    steps, idx = fs.steps_for(program, to)
    target = steps[idx]
    cycle = j["ladder_cycle"] + (1 if step.get("ladder_cycle") == "+1" else 0)
    table = step.get("table") or j["words_table"]
    wait = target.get("wait") or {"days": 0, "from": "previous_send"}
    base = _parse(j["anchor_at"]) or now
    due = base + _wait_delta(wait, j["lead_id"])
    conn.execute("UPDATE journeys SET node_key=?, ladder_cycle=?, words_table=?, next_wake_at=?, "
                 "waiting_for='clock', flow_version_id=flow_version_id, updated_at=? WHERE id=?",
                 (to, cycle, table, _iso(due), _iso(now), j["id"]))
    _event(conn, j["id"], j["lead_id"], "transferred", to, {"from": j["node_key"], "ladder_cycle": cycle, "table": table}, _iso(now))


def _do_park(conn, j, step: dict, now: datetime) -> None:
    conn.execute("UPDATE journeys SET status='parked', waiting_for='human', parked_from_node=?, next_wake_at=NULL, updated_at=? WHERE id=?",
                 (j["node_key"], _iso(now), j["id"]))
    _event(conn, j["id"], j["lead_id"], "parked", j["node_key"], {"reason": step.get("reason"), "reactivate": step.get("reactivate")}, _iso(now))


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------

def _attention_kind(j, step: dict) -> str:
    """Which of the attention kinds this due step is.
    reply           a branch's answer to THEIR reply, or the stall nudge in a conversation
    new_accept      the opener to somebody who just accepted (arm B/C)
    event           a message or invite about an event
    old_connection  the opener to an existing connection we never wrote to (arm D)
    ladder          a follow-up rung, or re-activation — the people who have not replied"""
    do = step.get("do")
    owner = j["node_key"].split(".")[0]
    if do == "invite_to_event":
        return "event"
    if owner not in (LADDER, fs.REACT_KEY):
        return "reply"
    if owner == LADDER and do == "send_opener":
        return "old_connection" if (j["arm_key"] or "") == "D" else "new_accept"
    return "ladder"


def _attention_order(cfg) -> list[str]:
    order = [str(k) for k in (getattr(cfg, "flow_priority", None) or []) if str(k) in ATTENTION_DEFAULT]
    return order + [k for k in ATTENTION_DEFAULT if k not in order]


def _priority(row, program: dict) -> tuple:
    """Ashley's send_priority: responders' branch moves first (newest reply first), then
    the ladder newest accept first. Lower sorts first."""
    owner = row["node_key"].split(".")[0]
    is_ladder = owner == LADDER
    return (1 if is_ladder else 0, -(int((_parse(row["created_at"]) or utcnow()).timestamp())))


def pass_(now: datetime | None = None, *, live: bool = False, budget: dict | None = None,
          sender: Callable | None = None, page=None, pace: Callable | None = None,
          can_act: Callable | None = None, stager: Callable | None = None) -> dict:
    """One pass. `live=False` never calls the sender for real: a stub records what would
    have gone. `sender(page, lead_row, bubbles, step_index)` returns
    'sent' | 'replied' | 'skipped' | 'unconfirmed' | 'failed'. `pace()` is called between
    sends and returns False to stop cleanly (LaneLock.pause). `can_act(action)` returns
    (ok, why)."""
    now = now or utcnow()
    b = dict(DEFAULT_BUDGET, **(budget or {}))
    started = utcnow()
    summary = {"replies": 0, "unlinked": 0, "node_missing": 0, "due": 0, "sent": 0, "skipped": 0,
               "abandoned": 0, "parked": 0, "transferred": 0, "stale_mirror": False, "cap_stops": [],
               "would_send": 0, "not_built": 0, "stopped": None, "staged": 0}
    from .config import Config
    _cfg = Config.load()
    msg_cap = int((getattr(_cfg, "daily_caps", None) or {}).get("message", 120))
    # NEW people a day (openers), not counting the sequence — ruling 2026-08-27: 200
    opener_daily = int(getattr(_cfg, "flow_openers_per_day", 200) or 200)
    # off (or absent) = every due send is STAGED for a human and carried by the host's own
    # send road; on = this file carries it. LinkForge's config says True; LinkChat's has no
    # such field, so there it can only ever be off.
    auto = bool(getattr(_cfg, "flow_auto_send", False))
    if can_act is None:
        from . import safety
        can_act = lambda action: safety.can_act(action, _cfg)  # noqa: E731

    with db.connect() as conn:
        ensure_schema(conn)
        v, program = active_program(conn)
        if not v:
            summary["stopped"] = "no active flow"
            return summary
        lineage = v["lineage_uuid"]

        # 1. refresh the version pointer; a node the active version no longer has -> human
        for j in conn.execute("SELECT * FROM journeys WHERE status='active' AND lineage_uuid=?", (lineage,)).fetchall():
            try:
                fs.steps_for(program, j["node_key"])
                if j["flow_version_id"] != v["id"]:
                    conn.execute("UPDATE journeys SET flow_version_id=?, updated_at=? WHERE id=?", (v["id"], _iso(now), j["id"]))
            except KeyError:
                conn.execute("UPDATE journeys SET waiting_for='human', flow_version_id=?, updated_at=? WHERE id=?", (v["id"], _iso(now), j["id"]))
                _event(conn, j["id"], j["lead_id"], "node_missing", j["node_key"], {"version": v["id"]}, _iso(now))
                summary["node_missing"] += 1

        # 1b. the vault's command file — park / revive / transfer, read from a saved offset
        if bool(getattr(_cfg, "flow_crm_enabled", False)):
            try:
                from . import flow_crm as FC
                cmd = FC.apply_commands(conn, program, _iso(now), _cfg, event=_event)
                summary["commands"] = cmd
            except Exception as e:  # noqa: BLE001 — the vault must never stop the pass
                summary["commands"] = {"error": f"{type(e).__name__}: {e}"[:120]}

        # 2. the reply read, from the mirror, no browser
        rr = read_replies(conn, now, program)
        summary.update(rr)   # replies, unlinked, matched, unmatched
        conn.commit()

        # 3. the mirror gate — stale means no LinkedIn sends this pass
        fresh, why = mirror_gate(now, int(b["mirror_max_age_min"]))
        summary["stale_mirror"] = not fresh
        summary["mirror"] = why

        # 4. crashed intents from a previous pass -> park for a human
        for s in conn.execute("SELECT * FROM journey_sends WHERE status='intended' AND intended_at < ?",
                              (_iso(now - timedelta(minutes=5)),)).fetchall():
            conn.execute("UPDATE journey_sends SET status='abandoned', resolved_at=?, outcome='crashed' WHERE id=?", (_iso(now), s["id"]))
            conn.execute("UPDATE journeys SET status='needs_attention', waiting_for='human', updated_at=? WHERE id=?", (_iso(now), s["journey_id"]))
            _event(conn, s["journey_id"], s["lead_id"], "parked", s["node_key"], {"reason": "intended send never confirmed"}, _iso(now))
            summary["parked"] += 1

        # 5. the due, one list per action type
        due_rows = conn.execute(
            "SELECT * FROM journeys WHERE status='active' AND waiting_for='clock' AND next_wake_at <= ? "
            "AND lineage_uuid=?", (_iso(now), lineage)).fetchall()
        summary["due"] = len(due_rows)
        by_type: dict[str, list] = {}
        for j in due_rows:
            try:
                steps, idx = fs.steps_for(program, j["node_key"])
            except KeyError:
                continue
            do = steps[idx].get("do")
            kind = _attention_kind(j, steps[idx]) if (do in SEND_ACTIONS or do == "invite_to_event") else do
            by_type.setdefault(kind, []).append((j, steps, idx))
        # the attention order first (one shared send budget), then the book-keeping steps
        order = _attention_order(_cfg) + ["red_list", "withdraw_invite", "transfer", "park", "wait"]
        order += sorted(k for k in by_type if k not in order)
        send_budget = int(b.get("per_pass", 25))
        sends_attempted = 0

        # the opener's reserved slice: ladder sends may not take the message cap below accepts-today + reserve
        accepts_today = conn.execute("SELECT COUNT(*) FROM leads WHERE accepted_at >= ?", (now.date().isoformat(),)).fetchone()[0]
        sent_today = conn.execute("SELECT COUNT(*) FROM journey_sends WHERE status='sent' AND resolved_at >= ?", (now.date().isoformat(),)).fetchone()[0]
        openers_today = conn.execute("SELECT COUNT(*) FROM journey_sends WHERE status='sent' AND node_key LIKE '%.opener' AND resolved_at >= ?",
                                     (now.date().isoformat(),)).fetchone()[0]
        reserve_needed = accepts_today + int(b["opener_reserve"])

        for kind in order:
            items = by_type.get(kind) or []
            items.sort(key=lambda t: _priority(t[0], program))
            n_done = 0
            for j, steps, idx in items:
                action = steps[idx].get("do")
                # COMMIT BEFORE EVERY ITEM. The sender (drip.send_to_lead) and the actions open
                # their own connections and write BEFORE the irreversible click; a write lock
                # held here made every one of them wait 15 s and fail (first live pass,
                # 2026-08-27 20:42 — nothing went out). Committing also makes each intent
                # durable before the browser acts, which is what the intent record is for.
                conn.commit()
                if bool(getattr(Config.load(), "flow_paused", False)):
                    # Ashley's stop ("stop messages ... until I say so", 2026-08-27 22:1x): read
                    # fresh between items so a running pass halts after the send in flight
                    summary["stopped"] = "paused by Ashley (config flow_paused)"
                    return summary
                if n_done >= int(b["per_type"]) or (utcnow() - started).total_seconds() > int(b["seconds"]):
                    summary["stopped"] = summary["stopped"] or f"budget at {kind}"
                    break
                if action in SEND_ACTIONS and sends_attempted >= send_budget:
                    # the shared send budget is spent on the kinds above this one — by design
                    summary["stopped"] = summary["stopped"] or f"this pass's {send_budget} sends are spent (stopped in {kind})"
                    break
                step = steps[idx]
                node = j["node_key"]
                # staleness: a step too late to send is skipped, and the person moves on
                late = int(step.get("max_late_days") or 0)
                if late and _parse(j["next_wake_at"]) and (now - _parse(j["next_wake_at"])) > timedelta(days=late):
                    _event(conn, j["id"], j["lead_id"], "skipped_stale", node, {"late_days": late}, _iso(now))
                    _apply_advance(conn, j, program, steps, idx, now, now, "skipped_stale")
                    summary["skipped"] += 1
                    n_done += 1
                    continue
                if action in ACT_ACTIONS:
                    from . import flow_actions as FA
                    cap_action = FA.CAP_ACTION.get(action)
                    if cap_action:
                        if not fresh and action != "red_list":
                            continue                  # a LinkedIn act waits for a fresh mirror too
                        ok, why = can_act(cap_action)
                        if not ok:
                            summary["cap_stops"].append(f"{action}: {why}")
                            break                     # this TYPE's list only
                    # one act per (person, node, anchor): the event log is the record
                    already = conn.execute("SELECT 1 FROM journey_events WHERE journey_id=? AND node_key=? AND kind IN "
                                           "('withdrawn','event_invited','red_listed') AND at >= ? LIMIT 1",
                                           (j["id"], node, j["anchor_at"] or "")).fetchone()
                    if already:
                        _apply_advance(conn, j, program, steps, idx, now, now, "already")
                        n_done += 1
                        continue
                    lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (j["lead_id"],)).fetchone())
                    conn.commit()   # the action writes through its own connection
                    if action == "red_list":
                        outcome, why = FA.red_list_one(lead, step.get("reason"), live=live)
                    elif action == "withdraw_invite":
                        outcome, why = ("would", "shadow") if not live else (("failed", "no page") if page is None else FA.withdraw_one(page, lead, live=True))
                    else:
                        outcome, why = ("would", "shadow") if not live else (("failed", "no page") if page is None else
                                                                              FA.invite_one(page, lead, str(step.get("event_id") or ""), live=True))
                    kind = {"red_list": "red_listed", "withdraw_invite": "withdrawn", "invite_to_event": "event_invited"}[action]
                    if outcome in ("done", "would"):
                        _event(conn, j["id"], j["lead_id"], kind if outcome == "done" else f"would_{kind}", node, {"why": why}, _iso(now))
                        _apply_advance(conn, j, program, steps, idx, now, now, outcome)
                        if outcome == "would":
                            summary["would_send"] += 1
                        else:
                            summary["acts"] = summary.get("acts", 0) + 1
                    elif outcome == "skipped":
                        _event(conn, j["id"], j["lead_id"], "skipped", node, {"action": action, "why": why}, _iso(now))
                        _apply_advance(conn, j, program, steps, idx, now, now, "skipped")
                        summary["skipped"] += 1
                    else:
                        conn.execute("UPDATE journeys SET fail_count=fail_count+1, updated_at=? WHERE id=?", (_iso(now), j["id"]))
                        fc = conn.execute("SELECT fail_count FROM journeys WHERE id=?", (j["id"],)).fetchone()[0]
                        _event(conn, j["id"], j["lead_id"], "failed", node, {"action": action, "why": why, "fail_count": fc}, _iso(now))
                        if fc >= 3:
                            conn.execute("UPDATE journeys SET status='needs_attention', waiting_for='human', updated_at=? WHERE id=?", (_iso(now), j["id"]))
                            _event(conn, j["id"], j["lead_id"], "parked", node, {"reason": f"three failures on {action}"}, _iso(now))
                            summary["parked"] += 1
                    n_done += 1
                    if live and pace is not None and cap_action and not pace():
                        summary["stopped"] = "lock not re-acquired after the pacing gap"
                        return summary
                elif action in SEND_ACTIONS:
                    step_auto = auto or bool(step.get("auto_send"))   # the stage's own switch
                    if not fresh and step_auto:
                        continue                      # the gate: classify, never send, on a stale mirror
                    # (staging for a human needs no fresh mirror: they read the thread before releasing)
                    cap_action = "message"
                    ok, why = can_act(cap_action)
                    if not ok:
                        summary["cap_stops"].append(f"{action}: {why}")
                        break                         # this TYPE's list only
                    if action == "send_opener" and openers_today >= opener_daily:
                        summary["cap_stops"].append(f"send_opener: {openers_today} new people today; the day's target is {opener_daily}")
                        break
                    if auto and action == "send" and (msg_cap - sent_today) <= reserve_needed:
                        # (staging spends no allowance: the host's send road counts the carry)
                        # the opener's own lane (send_priority line 3): ladder sends may not
                        # take the day's message cap below today's accepts plus a margin
                        summary["cap_stops"].append(f"send: {msg_cap - sent_today} left, reserved for openers ({reserve_needed})")
                        break
                    bubbles, ref = resolve_words(conn, v["id"], step, j)
                    if not bubbles:
                        _event(conn, j["id"], j["lead_id"], "parked", node, {"reason": f"no words for {ref}"}, _iso(now))
                        conn.execute("UPDATE journeys SET status='needs_attention', waiting_for='human', updated_at=? WHERE id=?", (_iso(now), j["id"]))
                        summary["parked"] += 1
                        n_done += 1
                        continue
                    key = send_key(j["lead_id"], lineage, node, j["anchor_at"])
                    try:
                        conn.execute("INSERT INTO journey_sends (send_key, journey_id, lead_id, node_key, status, intended_at) "
                                     "VALUES (?,?,?,?, 'intended', ?)", (key, j["id"], j["lead_id"], node, _iso(now)))
                    except Exception:  # sqlite3.IntegrityError — the database refused a second send
                        _event(conn, j["id"], j["lead_id"], "skipped_duplicate", node, {"send_key": key}, _iso(now))
                        _apply_advance(conn, j, program, steps, idx, now, now, "duplicate")
                        summary["skipped"] += 1
                        n_done += 1
                        continue
                    conn.commit()   # the intent row is on disk and the lock is released before the click
                    sends_attempted += 1
                    if not step_auto:
                        # STAGED, not sent: the words wait for a human; the host's send road
                        # carries them and calls release(). The person is no longer 'due'.
                        conn.execute("UPDATE journey_sends SET status='staged', words=? WHERE send_key=?",
                                     (json.dumps(bubbles, ensure_ascii=False), key))
                        conn.execute("UPDATE journeys SET waiting_for='approval', updated_at=? WHERE id=?", (_iso(now), j["id"]))
                        note = None
                        hook = stager or STAGER
                        if live and hook is not None:   # a shadow pass never reaches the host's queue
                            lead = dict(conn.execute("SELECT * FROM leads WHERE id=?", (j["lead_id"],)).fetchone())
                            try:
                                note = hook(lead, bubbles, key, node, ref)
                            except Exception as e:  # noqa: BLE001 — the host's queue failing must not lose the stage
                                note = f"stager failed: {type(e).__name__}: {e}"[:160]
                        _event(conn, j["id"], j["lead_id"], "staged", node,
                               {"ref": ref, "bubbles": len(bubbles), "send_key": key, "note": note}, _iso(now))
                        summary["staged"] += 1
                        n_done += 1
                        continue
                    if not live:
                        outcome = "would_send"
                        summary["would_send"] += 1
                    else:
                        lead = conn.execute("SELECT * FROM leads WHERE id=?", (j["lead_id"],)).fetchone()
                        rung = j["sends_done"]
                        try:
                            if sender is not None:
                                outcome = sender(page, dict(lead), bubbles, rung)
                            elif SENDER is not None:
                                # the host's one road out (LinkChat), for a stage switched on
                                outcome = SENDER(page, dict(lead), bubbles, rung,
                                                 {"send_key": key, "node_key": node, "ref": ref, "auto_send": bool(step.get("auto_send"))})
                            else:
                                fctx = dict(_stamp_ctx(conn, v["id"], ref) or {})
                                if kind == "reply":
                                    fctx["answering_reply"] = True    # their message on the thread is the reason, not a stop
                                outcome = _default_sender(page, dict(lead), bubbles, rung, flow_ctx=fctx or None)
                        except Exception as e:  # noqa: BLE001
                            outcome = f"failed:{type(e).__name__}"
                        # one line per send, flushed: the daemon's supervisor reads progress off
                        # this stream, and a silent 40-minute pass aged the heartbeat (21:38)
                        print(f"[flow] {outcome} {lead['full_name']} at {node} ({ref})", flush=True)
                    # the pass's clock: in production `now` is the pass start, minutes at most from the click
                    if _record_send_outcome(conn, j, program, steps, idx, node, key, ref, len(bubbles), outcome, now, summary):
                        sent_today += 1
                        if action == "send_opener":
                            openers_today += 1
                    conn.commit()   # the outcome is on disk before the pacing gap
                    n_done += 1
                    if live and pace is not None and not pace():
                        summary["stopped"] = "lock not re-acquired after the pacing gap"
                        return summary
                elif action == "transfer":
                    try:
                        _do_transfer(conn, j, program, step, now)
                        summary["transferred"] += 1
                    except KeyError:
                        conn.execute("UPDATE journeys SET waiting_for='human', updated_at=? WHERE id=?", (_iso(now), j["id"]))
                        _event(conn, j["id"], j["lead_id"], "node_missing", node, {"to": step.get("to")}, _iso(now))
                        summary["node_missing"] += 1
                    n_done += 1
                elif action == "park":
                    _do_park(conn, j, step, now)
                    summary["parked"] += 1
                    n_done += 1
                elif action == "wait":
                    _apply_advance(conn, j, program, steps, idx, now, now, "wait")
                    n_done += 1
                elif action == "crm_write":
                    # write this person's place into their People note now (the end-of-pass
                    # sync would do it anyway; a step makes the moment Ashley's to choose)
                    from . import flow_crm as FC
                    note = conn.execute("SELECT crm_note_path FROM leads WHERE id=?", (j["lead_id"],)).fetchone()
                    wrote = False
                    if note and note["crm_note_path"] and live:
                        try:
                            wrote = FC.write_note(note["crm_note_path"], j)
                        except Exception as e:  # noqa: BLE001
                            _event(conn, j["id"], j["lead_id"], "failed", node, {"action": "crm_write", "why": str(e)[:120]}, _iso(now))
                    _event(conn, j["id"], j["lead_id"], "crm_written" if wrote else ("would_crm_written" if not live else "skipped"), node,
                           {"note": note["crm_note_path"] if note else None, "linked": bool(note and note["crm_note_path"])}, _iso(now))
                    _apply_advance(conn, j, program, steps, idx, now, now, "crm_write")
                    summary["acts"] = summary.get("acts", 0) + (1 if wrote else 0)
                    n_done += 1
                elif action in NOT_BUILT:
                    _event(conn, j["id"], j["lead_id"], "skipped", node, {"reason": f"{action} not built yet"}, _iso(now))
                    _apply_advance(conn, j, program, steps, idx, now, now, "not_built")
                    summary["not_built"] += 1
                    n_done += 1
    # 9. the vault shows every change: three keys per changed journey, the ledger appended.
    # Live passes only — a shadow pass on a copy must never write into Ashley's notes.
    if live and bool(getattr(_cfg, "flow_crm_enabled", False)):
        try:
            from . import flow_crm as FC
            summary["crm_sync"] = FC.sync_to_vault(commit=True, cfg=_cfg)
        except Exception as e:  # noqa: BLE001
            summary["crm_sync"] = {"error": f"{type(e).__name__}: {e}"[:120]}
    return summary


def _record_send_outcome(conn, j, program: dict, steps: list, idx: int, node: str, key: str, ref,
                         n_bubbles: int, outcome: str, now: datetime, summary: dict) -> bool:
    """What a send's outcome does to the record — one place, used by the live pass and by
    release() when the host's send road reports on a staged send. True when it was sent."""
    done_at = now
    if outcome in ("sent", "would_send"):
        conn.execute("UPDATE journey_sends SET status='sent', resolved_at=?, outcome=? WHERE send_key=?", (_iso(done_at), outcome, key))
        conn.execute("UPDATE journeys SET sends_done=sends_done+1, updated_at=? WHERE id=?", (_iso(now), j["id"]))
        j = conn.execute("SELECT * FROM journeys WHERE id=?", (j["id"],)).fetchone()
        _event(conn, j["id"], j["lead_id"], "sent", node, {"ref": ref, "bubbles": n_bubbles, "outcome": outcome}, _iso(done_at))
        _apply_advance(conn, j, program, steps, idx, now, done_at, "sent")
        summary["sent"] = summary.get("sent", 0) + 1
        return True
    if outcome == "replied":
        conn.execute("UPDATE journey_sends SET status='abandoned', resolved_at=?, outcome='replied' WHERE send_key=?", (_iso(done_at), key))
        conn.execute("UPDATE journeys SET status='paused_reply', waiting_for='human', updated_at=? WHERE id=?", (_iso(now), j["id"]))
        _event(conn, j["id"], j["lead_id"], "race_lost", node, {"ref": ref}, _iso(done_at))
        summary["abandoned"] = summary.get("abandoned", 0) + 1
        return False
    if outcome == "unconfirmed":
        # leave the intent standing: the next pass parks it for a human (step 4)
        summary["abandoned"] = summary.get("abandoned", 0) + 1
        return False
    # A refusal or a failure FREES the key: the next pass must try this person again as a
    # real attempt, not find its own refusal on the row and walk them past the step as a
    # "duplicate" (three people were walked to follow-up 1 that way, 2026-08-27 21:04).
    # Only a SENT key may refuse a second send of the same step.
    conn.execute("UPDATE journey_sends SET status='abandoned', resolved_at=?, outcome=?, send_key=send_key || ':try' || id WHERE send_key=?",
                 (_iso(done_at), str(outcome)[:80], key))
    conn.execute("UPDATE journeys SET fail_count=fail_count+1, updated_at=? WHERE id=?", (_iso(now), j["id"]))
    fc = conn.execute("SELECT fail_count FROM journeys WHERE id=?", (j["id"],)).fetchone()[0]
    _event(conn, j["id"], j["lead_id"], "skipped", node, {"outcome": str(outcome)[:80], "fail_count": fc}, _iso(done_at))
    if fc >= 3:
        conn.execute("UPDATE journeys SET status='needs_attention', waiting_for='human', updated_at=? WHERE id=?", (_iso(now), j["id"]))
        _event(conn, j["id"], j["lead_id"], "parked", node, {"reason": "three consecutive failures"}, _iso(now))
        summary["parked"] = summary.get("parked", 0) + 1
    summary["abandoned"] = summary.get("abandoned", 0) + 1
    return False


def staged_list(conn=None, limit: int = 200) -> list[dict]:
    """Every send waiting for a human to release it, oldest first, with the words."""
    def _q(c):
        ensure_schema(c)
        out = []
        for r in c.execute("SELECT s.send_key, s.lead_id, s.node_key, s.intended_at, s.words, l.full_name, l.profile_url "
                           "FROM journey_sends s JOIN leads l ON l.id=s.lead_id WHERE s.status='staged' "
                           "ORDER BY s.intended_at LIMIT ?", (limit,)):
            d = dict(r)
            d["words"] = json.loads(d["words"] or "[]")
            out.append(d)
        return out
    if conn is not None:
        return _q(conn)
    with db.connect() as c:
        return _q(c)


def release(send_key: str, outcome: str, *, by: str | None = None, at: datetime | None = None) -> dict:
    """The host's send road reporting what happened to a STAGED send. 'sent' records it and
    walks the person on exactly as the live pass would; anything else leaves them waiting
    with the reason on the event log (the words are still in the host's outbox)."""
    now = at or utcnow()
    with db.connect() as conn:
        ensure_schema(conn)
        s = conn.execute("SELECT * FROM journey_sends WHERE send_key=?", (send_key,)).fetchone()
        if s is None:
            return {"ok": False, "why": "no staged send with that key"}
        if s["status"] != "staged":
            return {"ok": False, "why": f"that send is already {s['status']}"}
        j = conn.execute("SELECT * FROM journeys WHERE id=?", (s["journey_id"],)).fetchone()
        if outcome != "sent":
            _event(conn, j["id"], j["lead_id"], "carry_failed", s["node_key"], {"outcome": str(outcome)[:120], "by": by}, _iso(now))
            return {"ok": False, "why": str(outcome)[:120]}
        conn.execute("UPDATE journey_sends SET released_by=? WHERE send_key=?", (by, send_key))
        v, program = active_program(conn)
        if not v or j["status"] != "active" or j["node_key"] != s["node_key"]:
            # the person moved on while the words waited (a reply came in, a park): the send is
            # recorded against them, but they are not walked from a step they no longer stand on
            conn.execute("UPDATE journey_sends SET status='sent', resolved_at=?, outcome='sent_off_node' WHERE send_key=?", (_iso(now), send_key))
            _event(conn, j["id"], j["lead_id"], "sent", s["node_key"], {"outcome": "sent_off_node", "released_by": by}, _iso(now))
            return {"ok": True, "advanced": False, "lead_id": j["lead_id"], "node": s["node_key"]}
        steps, idx = fs.steps_for(program, j["node_key"])
        n = len(json.loads(s["words"] or "[]"))
        summary: dict = {}
        _record_send_outcome(conn, j, program, steps, idx, s["node_key"], send_key, None, n, "sent", now, summary)
        return {"ok": True, "advanced": True, "lead_id": j["lead_id"], "node": s["node_key"]}


def approval_pass(now: datetime | None = None) -> dict:
    """The pass when flow_auto_send is off: no browser, no lane lock, no LinkedIn. Replies are
    read from the mirror, due steps are STAGED for a human, and nothing is carried by this
    file. Staging spends no allowance — the host's send road counts the carry."""
    return pass_(now, live=True, page=None, sender=None, pace=None,
                 can_act=lambda action: (True, "staging spends nothing"))


def _is_live_db() -> bool:
    """True when db.DB_PATH is the program's real data file (not a test's scratch path)."""
    try:
        from . import DB_PATH as _pkg_db
        return Path(str(db.DB_PATH)).resolve() == Path(str(_pkg_db)).resolve()
    except Exception:
        return True


def _stamp_ctx(conn, version_id: int, ref: str | None) -> dict | None:
    """The canvas node, arm and arm hash a words reference belongs to, so the sender's stamp
    (flow_stamps) names the arm exactly and the chart is not blind to engine sends."""
    if not ref or ref == "override":
        return None
    try:
        nk, ak = fs._arm_key_for(ref)
        row = conn.execute("SELECT content_hash FROM flow_arms WHERE version_id=? AND node_key=? AND arm_key=?",
                           (version_id, nk, ak)).fetchone()
        if not row:
            return None
        return {"node_key": nk, "arm_key": ak, "arm_hash": row["content_hash"]}
    except Exception:  # noqa: BLE001 — a stamp is book-keeping; never let it stop a send
        return None


def _default_sender(page, lead: dict, bubbles: list[str], rung: int, flow_ctx: dict | None = None) -> str:
    """The real sender: drip.send_to_lead with the resolved bubbles. Its own thread read is
    the last line of defence and returns 'replied' when a message from them is on the thread.
    `flow_ctx` names the canvas arm so the send is stamped exactly (F1 attribution)."""
    from . import drip
    return drip.send_to_lead(page, lead, [], drip._recent_hashes(), step_index=rung,
                             override_bubbles=bubbles, source="flow", flow_ctx=flow_ctx)


# ---------------------------------------------------------------------------
# status, for people and for the CLI
# ---------------------------------------------------------------------------

def _status_from(conn) -> dict:
    out = {"by_status": {}, "by_node": {}, "human": 0}
    for r in conn.execute("SELECT status, COUNT(*) n FROM journeys GROUP BY status"):
        out["by_status"][r["status"]] = r["n"]
    for r in conn.execute("SELECT node_key, COUNT(*) n FROM journeys WHERE status='active' GROUP BY node_key"):
        out["by_node"][r["node_key"]] = r["n"]
    out["human"] = conn.execute("SELECT COUNT(*) FROM journeys WHERE waiting_for='human'").fetchone()[0]
    return out


def status(conn=None) -> dict:
    if conn is not None:
        return _status_from(conn)
    with db.connect() as c:
        return _status_from(c)


# ---------------------------------------------------------------------------
# the migration — Build Plan V3 §9.1, as ruled (R-B, R-L)
# ---------------------------------------------------------------------------

def migrate_completed(now: datetime | None = None, commit: bool = False, repliers_since: str | None = None) -> dict:
    """The 633 `sequence_state` rows marked completed after one message become journeys
    at R0.f1, due four days after their last send, on the cold table — when they pass
    every exclusion. Previews by default; `commit=True` writes. The rule, as run:
      a  enrolment guard: is_connection=0 AND accepted_at (R-B: the 307 are left alone)
      b  not red-listed
      c  an inbox thread linked to the person (else: parked as unlinked, for a human)
      d  no reply after our last send: last_msg_dir='in' AND last_msg_at later
           (else: the reply pass, never the ladder)
      e  no matched/second_exchange stamp (else: they were answered by hand — the silence rule)
      A  the three on opener A are parked by rule (no ladder ruled for A)
    R-L: a thread matched to exactly one person by name is a good link — they migrate."""
    import sqlite3
    now = now or utcnow()
    out = {"start": 0, "guard_failed": 0, "red": 0, "unlinked": 0, "replied": 0, "by_hand": 0,
           "opener_a": 0, "migrate": 0, "already": 0, "written": 0, "commit": commit,
           "by_arm": {}, "by_join": {}}
    with db.connect() as conn:
        ensure_schema(conn)
        v, program = active_program(conn)
        if not v or not (program["branches"].get(LADDER) or {}).get("steps"):
            out["stopped"] = "no active flow with a ladder"
            return out
        lineage = v["lineage_uuid"]
        f1_idx = next((i for i, s in enumerate(program["branches"][LADDER]["steps"]) if s.get("key") == "f1"), None)
        if f1_idx is None:
            out["stopped"] = "the ladder has no f1"
            return out
        f1_wait = int((program["branches"][LADDER]["steps"][f1_idx].get("wait") or {}).get("days") or 4)
        cx = sqlite3.connect(f"file:{_conv_db_path()}?mode=ro", uri=True)
        cx.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT s.lead_id, s.variant, l.is_connection, l.accepted_at,
                       (SELECT MAX(sent_at) FROM messages m WHERE m.lead_id=s.lead_id AND m.status='sent') AS last_sent,
                       (SELECT COUNT(*) FROM red_list r WHERE r.lead_id=l.id OR r.canon_url=l.profile_url OR r.member_urn=l.profile_url) AS red,
                       (SELECT COUNT(*) FROM flow_stamps fs WHERE fs.lead_id=s.lead_id AND fs.event IN ('matched','second_exchange')) AS reply_stamps
                FROM sequence_state s JOIN leads l ON l.id=s.lead_id
                WHERE s.status='completed'""").fetchall()
            seen = set()
            for r in rows:
                if r["lead_id"] in seen:
                    continue
                seen.add(r["lead_id"]); out["start"] += 1
                if conn.execute("SELECT 1 FROM journeys WHERE lead_id=? AND lineage_uuid=?", (r["lead_id"], lineage)).fetchone():
                    out["already"] += 1
                    continue
                if not ((r["is_connection"] or 0) == 0 and r["accepted_at"]):
                    out["guard_failed"] += 1
                    continue
                if r["red"]:
                    out["red"] += 1
                    continue
                th = cx.execute("SELECT last_msg_dir, last_msg_at, join_how FROM conversations WHERE lead_id=? "
                                "ORDER BY last_msg_at DESC LIMIT 1", (r["lead_id"],)).fetchone()
                arm = (r["variant"] or "").strip().upper() or None
                last_sent = r["last_sent"]
                if th is None:
                    out["unlinked"] += 1
                    if commit:
                        _write_migrated(conn, r["lead_id"], lineage, v["id"], "R0.f1", arm, last_sent, f1_wait, now,
                                        status="active", waiting="human", why="unlinked_thread", join_how="none",
                                        expected=ladder_rungs(program))
                    continue
                inbound = _ms_iso(th["last_msg_at"]) if th["last_msg_dir"] == "in" else None
                if inbound and inbound > (last_sent or ""):
                    out["replied"] += 1
                    if repliers_since and inbound >= repliers_since:
                        # they answered the old engine's opener and nobody answered them (2026-08-28):
                        # stand them at follow-up 1, anchored on the opener, and the next pass's
                        # reply read sees their message, matches it, and answers it
                        out["repliers_in"] = out.get("repliers_in", 0) + 1
                        if commit:
                            _write_migrated(conn, r["lead_id"], lineage, v["id"], "R0.f1", arm, last_sent, f1_wait, now,
                                            status="active", waiting="clock", why="migrated_replier", join_how=th["join_how"],
                                            expected=ladder_rungs(program))
                    continue
                if r["reply_stamps"]:
                    out["by_hand"] += 1
                    continue
                if arm == "A":
                    out["opener_a"] += 1
                    if commit:
                        _write_migrated(conn, r["lead_id"], lineage, v["id"], "R0.f1", arm, last_sent, f1_wait, now,
                                        status="parked", waiting="human", why="opener_A_no_ladder", join_how=th["join_how"],
                                        expected=ladder_rungs(program))
                    continue
                out["migrate"] += 1
                out["by_arm"][arm or "?"] = out["by_arm"].get(arm or "?", 0) + 1
                jh = th["join_how"] or "unknown"
                out["by_join"][jh] = out["by_join"].get(jh, 0) + 1
                if commit:
                    _write_migrated(conn, r["lead_id"], lineage, v["id"], "R0.f1", arm, last_sent, f1_wait, now,
                                    status="active", waiting="clock", why="migrated", join_how=th["join_how"],
                                    expected=ladder_rungs(program))
                    out["written"] += 1
        finally:
            cx.close()
    return out


def _write_migrated(conn, lead_id, lineage, version_id, node, arm, last_sent, wait_days, now, *,
                    status, waiting, why, join_how, expected) -> None:
    anchor = _parse(last_sent) or now
    due = anchor + timedelta(days=wait_days)
    cur = conn.execute(
        "INSERT INTO journeys (lead_id, lineage_uuid, flow_version_id, node_key, ladder_cycle, waiting_for, "
        "next_wake_at, anchor_at, expected_rungs, sends_done, status, arm_key, words_table, join_how, "
        "parked_from_node, created_at, updated_at) VALUES (?,?,?,?,0,?,?,?,?,1,?,?,'cold',?,?,?,?)",
        (lead_id, lineage, version_id, node, waiting, _iso(due), _iso(anchor), expected, status, arm, join_how,
         node if status == "parked" else None, _iso(now), _iso(now)))
    _event(conn, cur.lastrowid, lead_id, "enrolled", node, {"migrated": True, "why": why, "arm": arm, "join_how": join_how,
                                                            "last_sent": last_sent}, _iso(now))


def enrol_new_accepts(now: datetime | None = None, commit: bool = False) -> dict:
    """Every accepted lead (guard-passing, accepted_at set) with no journey in the active
    lineage -> enrolled at R0.opener. Previews by default."""
    now = now or utcnow()
    out = {"waiting": 0, "enrolled": 0, "commit": commit}
    with db.connect() as conn:
        v, program = active_program(conn)
        if not v:
            out["stopped"] = "no active flow"
            return out
        # A FRESH accept: guard-passing, no journey, no message EVER sent to them (a person
        # who already had an opener belongs to the migration or the reply pass, never to a
        # second opener — the shadow run of 2026-08-27 would have enrolled 57 such people),
        # and not on the red list.
        db.sync_red_list_from_json()
        ids = [r[0] for r in conn.execute(
            "SELECT l.id FROM leads l WHERE l.status='accepted' AND l.is_connection=0 AND l.accepted_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM leads_excluded e WHERE e.lead_id=l.id) "
            "AND l.id NOT IN (SELECT lead_id FROM journeys WHERE lineage_uuid=?) "
            "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.lead_id=l.id AND m.status='sent') "
            "AND NOT EXISTS (SELECT 1 FROM red_list r WHERE r.lead_id=l.id OR r.canon_url=l.profile_url OR r.member_urn=l.profile_url)",
            (v["lineage_uuid"],))]
    out["waiting"] = len(ids)
    if commit:
        for lid in ids:
            if enrol(lid, now=now):
                out["enrolled"] += 1
    return out


# ---------------------------------------------------------------------------
# the shadow run — the real pass, on a COPY of the live database, sender stubbed
# ---------------------------------------------------------------------------

def shadow_run(now: datetime | None = None) -> dict:
    """Copy linkforge.db to data/shadow/, migrate + enrol on the COPY, run one pass with the
    sender stubbed, and report what WOULD have gone and to whom. The live database is never
    opened for writing; the live mirror is read for replies. Build Plan V3 §10."""
    import shutil
    from . import DATA_DIR
    now = now or utcnow()
    shadow_dir = DATA_DIR / "shadow"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    src = DATA_DIR / "linkforge.db"
    dst = shadow_dir / "linkforge-shadow.db"
    shutil.copy2(src, dst)
    saved = db.DB_PATH
    db.DB_PATH = dst
    would: list[dict] = []
    try:
        mig = migrate_completed(now, commit=True)
        enr = enrol_new_accepts(now, commit=True)

        def stub(page, lead, bubbles, rung):
            would.append({"lead_id": lead["id"], "name": lead.get("full_name"), "rung": rung, "first_bubble": (bubbles or [""])[0][:80]})
            return "sent"
        s = pass_(now, live=True, sender=stub, can_act=lambda a: (True, "ok"), budget={"per_type": 200})
        rep = {"at": _iso(now), "migration": mig, "enrolment": enr, "pass": s, "would_send": would}
        (shadow_dir / f"shadow-{now.strftime('%Y%m%d-%H%M')}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        return rep
    finally:
        db.DB_PATH = saved


# ---------------------------------------------------------------------------
# the live wrapper — lock, own tab, pacing. Nothing calls this until Phase 1's gate.
# ---------------------------------------------------------------------------

def _refresh_mirror_if_older_than(minutes: int = 5) -> None:
    """One page of the inbox, refreshed, lockless, own tab — when the newest good sync is
    older than `minutes`. Never raises: a failed refresh leaves the gate to decide."""
    try:
        from .conversations import db as cdb
        from .conversations.sync import voyager_sync
        cx = cdb.connect()
        try:
            last = cdb.latest_sync_run(cx)
        finally:
            cx.close()
        fin = _parse(last["finished_at"]) if last else None
        if fin and (utcnow() - fin) < timedelta(minutes=minutes):
            return
        voyager_sync(max_pages=1, mode="refresh", own_tab=True, shared_lock=False, action="inbox_read")
    except Exception as e:  # noqa: BLE001
        print(f"[flow] inbox refresh before the pass failed: {type(e).__name__}: {e}", flush=True)


def live_pass(now: datetime | None = None) -> dict:
    """The real pass: LaneLock (released across every pacing gap), the keeper's browser over
    CDP, a tab of our OWN so no other lane's page is ever moved, the real sender, and the
    human-length gap from safety.next_delay between sends."""
    from .config import Config
    from . import safety, traffic
    import linkedin_browser as lb
    from playwright.sync_api import sync_playwright
    cfg = Config.load()
    if not cfg.enabled or cfg.dry_run:
        return {"stopped": "engine not armed"}
    # A FRESH INBOX READ FIRST. The daemon runs one lane at a time, so the 5-minute inbox
    # lane cannot run while a pass sends; by the next pass the mirror could be past the
    # 30-minute gate (it was 38 min old at 09:09 on 2026-08-28, a watchdog run in between).
    # The read is lockless and in its own tab, so nobody waits on it.
    _refresh_mirror_if_older_than(minutes=5)
    lane = traffic.LaneLock(agent=AGENT, wait_sec=300)
    if not lane.acquire():
        return {"stopped": "another LinkedIn lane is busy"}
    idx = {"n": 0}

    def pace() -> bool:
        idx["n"] += 1
        return lane.pause(safety.next_delay(cfg, idx["n"]))

    try:
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            page = ctx.new_page()
            try:
                return pass_(now, live=True, page=page, pace=pace)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                from . import safe_close
                safe_close(ctx)
    finally:
        lane.release()


# ---------------------------------------------------------------------------
# the teach loop (R-C) and the human list
# ---------------------------------------------------------------------------

def human_list(conn=None, limit: int = 200) -> list[dict]:
    """Everyone waiting for Ashley, with the reply that put them there."""
    def _q(c):
        out = []
        for j in c.execute("SELECT * FROM journeys WHERE waiting_for='human' ORDER BY updated_at DESC LIMIT ?", (limit,)):
            ev = c.execute("SELECT kind, detail, at FROM journey_events WHERE journey_id=? AND kind IN "
                           "('unmatched_reply','unlinked_thread','node_missing','parked','handed_to_human') ORDER BY id DESC LIMIT 1",
                           (j["id"],)).fetchone()
            lead = c.execute("SELECT full_name, profile_url FROM leads WHERE id=?", (j["lead_id"],)).fetchone()
            d = {}
            try:
                d = json.loads(ev["detail"]) if ev and ev["detail"] else {}
            except Exception:  # noqa: BLE001
                pass
            out.append({"journey_id": j["id"], "lead_id": j["lead_id"], "name": lead["full_name"] if lead else None,
                        "profile_url": lead["profile_url"] if lead else None, "node": j["node_key"], "branch": j["branch_key"],
                        "status": j["status"], "why": ev["kind"] if ev else None, "since": ev["at"] if ev else j["updated_at"],
                        "reply": d.get("text"), "candidates": d.get("candidates"), "reasons": d.get("reasons")})
        return out
    if conn is not None:
        return _q(conn)
    with db.connect() as c:
        return _q(c)


def teach(lead_id: int, words: list[str], *, as_branch: str | None = None, new_label: str | None = None,
          patterns: list[str] | None = None, now: datetime | None = None) -> dict:
    """Ashley's answer to a reply nothing matched. Two things happen, in this order:
    1. The answer is SAVED into a new DRAFT version of the flow (same lineage): as a new
       template on an existing branch (`as_branch`), or as a new branch (`new_label`) whose
       patterns are `patterns` — or, if none are given, the reply's own distinctive words.
       The draft is never activated here; Ashley activates it (`flows-activate`). From
       then, the next person who replies that way matches and is answered without him.
    2. THIS person gets his words now: the journey carries them as `override_words`,
       due at once, and the next pass sends them in place of the step's words, then hands
       the person to the silence rule as any branch move would.
    Never invents: the words are his, the patterns are the reply's."""
    now = now or utcnow()
    words = [w.strip() for w in (words or []) if str(w).strip()]
    if not words:
        raise ValueError("teach needs words")
    with db.connect() as conn:
        ensure_schema(conn)
        v, program = active_program(conn)
        if not v:
            raise ValueError("no active flow")
        j = conn.execute("SELECT * FROM journeys WHERE lead_id=? AND lineage_uuid=? ORDER BY id DESC LIMIT 1",
                         (lead_id, v["lineage_uuid"])).fetchone()
        if j is None:
            raise ValueError(f"lead {lead_id} has no journey")
        ev = conn.execute("SELECT detail FROM journey_events WHERE journey_id=? AND kind='unmatched_reply' ORDER BY id DESC LIMIT 1",
                          (j["id"],)).fetchone()
        reply_text = ""
        try:
            reply_text = (json.loads(ev["detail"]) if ev and ev["detail"] else {}).get("text") or ""
        except Exception:  # noqa: BLE001
            pass
        # 1. the draft
        doc = fe.export_flows_json(v["id"])
        doc["lineage"] = v["lineage_uuid"]
        bid = as_branch
        if bid:
            b = next((x for x in doc["branches"] if x["id"] == bid), None)
            if b is None:
                raise ValueError(f"branch {bid} is not in the active flow")
            b.setdefault("templates", []).append(words)
            b.pop("steps", None)   # let the translation rebuild the move from the templates
        else:
            used = {x["id"] for x in doc["branches"]}
            n = 15
            while f"R{n}" in used:
                n += 1
            bid = f"R{n}"
            pats = [p.strip() for p in (patterns or []) if p.strip()] or _patterns_from(reply_text)
            doc["branches"].append({"id": bid, "label": new_label or f"taught {now.date().isoformat()}",
                                    "read": f"Taught by Ashley on {now.date().isoformat()} from a reply nothing matched.",
                                    "patterns": pats, "templates": [words], "never": [], "forward": [],
                                    "parent": "in_conversation", "_taught": {"lead_id": lead_id, "at": _iso(now), "reply": reply_text[:300]}})
    draft_id = fe.import_flows_json(doc, name=f"taught {now.strftime('%Y-%m-%d %H:%M')} {bid}", activate=False,
                                    lineage=v["lineage_uuid"])
    # 2. this person, now
    with db.connect() as conn:
        conn.execute("UPDATE journeys SET override_words=?, branch_key=?, status='active', waiting_for='clock', "
                     "anchor_at=?, next_wake_at=?, updated_at=? WHERE id=?",
                     (json.dumps(words, ensure_ascii=False), bid, _iso(now), _iso(now), _iso(now), j["id"]))
        _event(conn, j["id"], lead_id, "taught", j["node_key"],
               {"branch": bid, "draft_version": draft_id, "words": words, "patterns": patterns}, _iso(now))
    return {"journey_id": j["id"], "branch": bid, "draft_version": draft_id, "new_branch": as_branch is None}


def _patterns_from(text: str) -> list[str]:
    """The reply's own distinctive words as patterns: the longest few, lower-cased, no
    names or numbers. A guess only in the sense every word list is; Ashley can edit it."""
    import re as _re
    words = [w.lower() for w in _re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text or "")]
    stop = {"that", "this", "with", "have", "your", "just", "what", "from", "they", "them", "there", "here",
            "will", "would", "could", "about", "been", "were", "when", "then", "than", "into", "some", "more",
            "very", "much", "thanks", "thank", "hello", "cheers", "really", "sure", "yeah", "okay"}
    seen, out = set(), []
    for w in sorted(words, key=len, reverse=True):
        if w in stop or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 3:
            break
    return out


def main() -> None:
    args = sys.argv[1:]
    if "--human" in args:
        print(json.dumps(human_list(), ensure_ascii=False, indent=1))
        return
    if "--staged" in args:
        print(json.dumps(staged_list(), ensure_ascii=False, indent=1))
        return
    if "--exclude" in args:
        # flow --exclude --lead N --reason "client" [--by Ashley]
        def _arg(flag):
            return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
        lid = int(_arg("--lead") or 0)
        added = exclude(lid, _arg("--reason") or "named by Ashley", by_whom=_arg("--by") or "Ashley")
        emit_result("flow", True, f"lead {lid} {'added to' if added else 'already on'} the never-sequence list")
        return
    if "--crm-link" in args:
        from . import flow_crm as FC
        r = FC.link_leads_to_notes(commit="--commit" in args)
        print(json.dumps(r, ensure_ascii=False))
        emit_result("flow", True, f"CRM link {'WRITTEN' if r['commit'] else 'preview'}: {r['linked']} linked, {r['ambiguous']} ambiguous, "
                    f"{r['unmatched']} unmatched, {r['already']} already", **r)
        return
    if "--crm-sync" in args:
        from . import flow_crm as FC
        r = FC.sync_to_vault(commit="--commit" in args)
        print(json.dumps(r, ensure_ascii=False))
        emit_result("flow", True, f"CRM sync {'WRITTEN' if r['commit'] else 'preview'}: {r['notes_written']} notes, {r['ledger_lines']} ledger lines, "
                    f"{r['notes_unlinked']} journeys with no linked note", **r)
        return
    if "--teach" in args:
        def _arg(flag):
            return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None
        lead = int(_arg("--lead") or 0)
        words = [w.strip() for w in (_arg("--words") or "").split("||") if w.strip()]
        pats = [p.strip() for p in (_arg("--patterns") or "").split("|") if p.strip()] or None
        r = teach(lead, words, as_branch=_arg("--as"), new_label=_arg("--new"), patterns=pats)
        print(json.dumps(r, ensure_ascii=False))
        emit_result("flow", True, f"Taught: {'new branch' if r['new_branch'] else 'template on'} {r['branch']} saved as draft "
                    f"version {r['draft_version']}; lead {lead} will get the words on the next pass", **r)
        return
    if "--migrate" in args:
        since = args[args.index("--repliers-since") + 1] if "--repliers-since" in args else None
        r = migrate_completed(commit="--commit" in args, repliers_since=since)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        emit_result("flow", "stopped" not in r,
                    f"Migration {'WRITTEN' if r.get('commit') else 'preview'}: {r.get('migrate', 0)} to R0.f1 "
                    f"({r.get('unlinked', 0)} unlinked, {r.get('replied', 0)} replied, {r.get('by_hand', 0)} by hand, "
                    f"{r.get('opener_a', 0)} on opener A, {r.get('guard_failed', 0)} left alone)", **{k: r.get(k) for k in ("migrate", "written", "by_join")})
    elif "--enrol-connections" in args:
        per = int(args[args.index("--per-day") + 1]) if "--per-day" in args else 200
        r = enrol_connections(per_day=per, commit="--commit" in args)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        emit_result("flow", "stopped" not in r,
                    (f"Supply refused: {r['stopped']}" if "stopped" in r else
                     f"Supply {'WRITTEN' if r['commit'] else 'preview'}: {r['candidates']} existing connections, {r['enrolled']} enrolled on opener D"), **r)
    elif "--enrol" in args:
        r = enrol_new_accepts(commit="--commit" in args)
        print(json.dumps(r, ensure_ascii=False))
        emit_result("flow", True, f"Enrol {'WRITTEN' if r['commit'] else 'preview'}: {r['waiting']} waiting, {r['enrolled']} enrolled")
    elif "--shadow" in args:
        r = shadow_run()
        s = r["pass"]
        print(json.dumps({k: v for k, v in r.items() if k != "would_send"}, ensure_ascii=False, indent=1))
        emit_result("flow", True, f"Shadow: {len(r['would_send'])} would send, {s['replies']} replies seen, "
                    f"{s['due']} due — on a copy, nothing sent", would_send=len(r["would_send"]))
    elif "--pass" in args:
        if "--commit" not in args and _is_live_db():
            # a rehearsal on the real database walks people on as if the words had gone
            # (the 2026-08-27 morning slip, on sequence --tick): a copy, or the real pass
            print("refused: a rehearsal pass on the real database advances people as if the words "
                  "had gone. Use --shadow (runs on a copy) or --commit.")
            emit_result("flow", False, "Flow pass refused: a rehearsal on the live database. Use --shadow or --commit.")
            return
        if "--commit" in args:
            from .config import Config
            s = live_pass() if bool(getattr(Config.load(), "flow_auto_send", False)) else approval_pass()
        else:
            s = pass_(live=False)
        print(json.dumps(s, ensure_ascii=False))
        emit_result("flow", not str(s.get("stopped") or "").startswith("no active"),
                    f"Flow pass ({'LIVE' if '--commit' in args else 'shadow'}): {s.get('sent', 0)} sent, "
                    f"{s.get('staged', 0)} staged for you, {s.get('would_send', 0)} would send, "
                    f"{s.get('replies', 0)} replies seen, {s.get('due', 0)} due",
                    **{k: s.get(k) for k in ("sent", "staged", "would_send", "replies", "due", "parked", "transferred", "stopped")})
    else:
        print(json.dumps(status(), indent=1))


if __name__ == "__main__":
    main()
