"""server.py — the inbox / messaging API, as an APIRouter mounted into the parent program.

Stage 1 of the the inbox half merge. Originally a standalone FastAPI app on :8771; now an
APIRouter(prefix="/api/inbox") that the parent program's server include_router()s into the ONE
the parent program process on :8770. Every route therefore lives under /api/inbox/* — which also
de-collides the three that clashed with the engine API (/api/status, /api/sync,
/api/export) by namespacing them to /api/inbox/{status,sync,export}.

CRM writes (tags/notes/snooze/archive) are direct DB ops. The inbox SYNC and every
SEND is a LANE: a read-only / send Playwright keeper drive, run as a SUBPROCESS so
Playwright's sync API never touches the server's asyncio loop. The subprocess argv is
built by the frozen helper.spawn_argv so it works both in dev (`python -m engine
inbox-sync`) and frozen (`the parent program.exe inbox-sync`).
"""
from __future__ import annotations

import json
import subprocess
import time

# The conversations server runs inside the desktop app, started by pythonw (no console), so a
# console child would be handed its own console window. capture_output does not stop that.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

import csv
import io
import os
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from engine import frozen
from . import _AUTOMATION, __version__, db
from . import keeper as K

router = APIRouter(prefix="/api/inbox")
db.init()   # ensure schema + migrations are applied before the router serves


def _cx():
    return db.connect()


def _run_inbox_cmd(cmd: str, *args: str, timeout: int = 120) -> dict:
    """Run a the parent program inbox-* subcommand as a child process and return its RESULT line.

    Uses frozen.spawn_argv so the argv is correct in dev (python -m engine <cmd>) and
    in a frozen build (the parent program.exe <cmd>). cwd=_AUTOMATION keeps the dev module import
    working; it is harmless when frozen."""
    try:
        p = subprocess.run(frozen.spawn_argv(cmd, *args), cwd=str(_AUTOMATION),
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        for line in (p.stdout or "").splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[7:])
        return {"ok": False, "msg": (p.stderr or "no result").strip()[:300]}
    except Exception as e:   # noqa: BLE001
        return {"ok": False, "msg": str(e)}


# --- request bodies ----------------------------------------------------------

class TagIn(BaseModel):
    name: str
    color: str | None = None

class ConvTagIn(BaseModel):
    tag_id: int
    on: bool = True

class NoteIn(BaseModel):
    note: str = ""

class SnoozeIn(BaseModel):
    until: str | None = None      # ISO timestamp, or null to clear

class ArchiveIn(BaseModel):
    archived: bool

class PinIn(BaseModel):
    pinned: bool

class SnippetIn(BaseModel):
    name: str
    body: str

class SendIn(BaseModel):
    text: str

class FlowRecordIn(BaseModel):
    branch: str | None = None
    arm_key: str | None = None
    arm_hash: str | None = None
    suggested_body: str | None = None
    sent_body: str | None = None

class ReviewDecisionIn(BaseModel):
    decision: str                 # 'kill' | 'skip'
    reason: str | None = None
    branch: str | None = None
    arm_key: str | None = None

class ReactivationDecisionIn(BaseModel):
    decision: str                 # 'kill' | 'skip'
    reason: str | None = None
    branch: str | None = None
    arm_key: str | None = None
    lead_id: int | None = None    # a reactivation has no conv_id — decide by lead identity
    canonical_url: str | None = None
    thread_urn: str | None = None

class MicStartIn(BaseModel):
    device: int

class VoiceSendIn(BaseModel):
    duration_ms: int = 0

class TranscribeIn(BaseModel):
    u: str

class ClearIn(BaseModel):
    box: str = "focused"
    tag: int | None = None

class GifIn(BaseModel):
    url: str
    name: str = "giphy.gif"

class KeyIn(BaseModel):
    key: str


# The GIF search and GIF send used to be here. They are gone with the reply box.
# The search also carried a working key for somebody else's account, baked into
# the file, which would have shipped to every person who installed this.


# --- reading the inbox, in the background --------------------------------------
# What the Sync button is doing right now, so the screen can say so. This sat
# directly beneath the GIF key and went out with it earlier today.
_sync = {"running": False, "started": None, "result": None}


def _run_sync(max_deep: int) -> None:
    import threading  # noqa: F401  (imported by caller; kept local for clarity)
    _sync.update(running=True, started=time.strftime("%Y-%m-%d %H:%M:%S"))
    result = None
    try:
        # FAST path: the Voyager list sync (whole inbox in seconds). max_deep is ignored here.
        p = subprocess.run(frozen.spawn_argv("inbox-sync"),
                           cwd=str(_AUTOMATION), capture_output=True, text=True, timeout=600,
                           creationflags=_NO_WINDOW)
        for line in (p.stdout or "").splitlines():
            if line.startswith("RESULT "):
                result = json.loads(line[7:])
        if result is None:
            result = {"ok": False, "msg": (p.stderr or "no RESULT line").strip()[:400]}
    except Exception as e:   # noqa: BLE001
        result = {"ok": False, "msg": str(e)}
    finally:
        _sync.update(running=False, result=result)


# --- status ------------------------------------------------------------------

@router.get("/status")
def status():
    cx = _cx()
    try:
        # WHETHER YOU ARE SIGNED IN, not just whether a browser is open.
        #
        # The keeper already worked this out - it watches for LinkedIn's login
        # page and puts a flag on disk - but nothing ever told a screen, so the
        # program knew a member was locked out and said nothing. They pressed
        # Sync, a browser appeared somewhere behind the window, and LinkChat
        # carried on as though it were reading.
        signed_in, needs_login = None, None
        try:
            from engine import browser as _B
            needs_login = _B.NEEDS_LOGIN_FLAG.exists()
            signed_in = _B.LOGGED_IN_FLAG.exists()
        except Exception:
            pass
        return {"version": __version__, "keeper": K.keeper_running(),
                "needs_login": needs_login, "signed_in": signed_in,
                **db.counts(cx), "boxes": db.box_counts(cx), "sync": _sync}
    finally:
        cx.close()


@router.post("/sync")
def start_sync(max: int = 20):
    import threading
    if _sync["running"]:
        return {"ok": False, "msg": "sync already running", "sync": _sync}
    threading.Thread(target=_run_sync, args=(max,), daemon=True).start()
    return {"ok": True, "msg": f"sync started (max {max})"}


# --- inbox read --------------------------------------------------------------

@router.get("")
def inbox(box: str = "focused", tag: int | None = None, q: str | None = None,
          limit: int = 200):
    cx = _cx()
    try:
        return {"box": box, "tag": tag,
                "conversations": db.list_conversations(cx, box=box, tag_id=tag, q=q, limit=limit)}
    finally:
        cx.close()


@router.post("/unarchive-all")
def unarchive_all_ep():
    cx = _cx()
    try:
        return {"ok": True, "restored": db.unarchive_all(cx)}
    finally:
        cx.close()


@router.post("/clear")
def clear_inbox(body: ClearIn):
    """Archive everything in the current box/label (Inbox-Zero). Local only."""
    cx = _cx()
    try:
        return {"ok": True, "archived": db.archive_box(cx, box=body.box, tag_id=body.tag)}
    finally:
        cx.close()


# NOTE: the bare GET /{conv_id} (open a conversation) is declared at the BOTTOM of this
# file, AFTER every literal GET route (/status, /tags, /gif, /mics, /audio, /export, …).
# FastAPI/Starlette match routes in declaration order and the int validation on conv_id
# happens AFTER the regex match, so a /{conv_id} declared first would shadow /tags etc.
# (return 422 int_parsing). Two-segment routes like /{conv_id}/note are safe to keep here.


# --- CRM writes --------------------------------------------------------------
#
# Each of these used to answer "done" whatever number it was given, including a
# conversation that does not exist. Nothing broke, and that is the problem: a
# note typed against the wrong number vanished and the screen said it saved. So
# the conversation is looked up first, and an unknown one is said out loud.

def _must_exist(cx, conv_id: int) -> None:
    row = cx.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="there is no conversation with that number")


@router.post("/{conv_id}/note")
def set_note(conv_id: int, body: NoteIn):
    cx = _cx()
    try:
        _must_exist(cx, conv_id)
        db.set_note(cx, conv_id, body.note)
        return {"ok": True}
    finally:
        cx.close()


@router.post("/{conv_id}/snooze")
def set_snooze(conv_id: int, body: SnoozeIn):
    cx = _cx()
    try:
        _must_exist(cx, conv_id)
        db.set_snooze(cx, conv_id, body.until)
        return {"ok": True, "until": body.until}
    finally:
        cx.close()


@router.post("/{conv_id}/archive")
def set_archive(conv_id: int, body: ArchiveIn):
    cx = _cx()
    try:
        _must_exist(cx, conv_id)
        db.set_archive(cx, conv_id, body.archived)
        return {"ok": True, "archived": body.archived}
    finally:
        cx.close()


@router.post("/{conv_id}/pin")
def set_pin(conv_id: int, body: PinIn):
    cx = _cx()
    try:
        _must_exist(cx, conv_id)
        db.set_pinned(cx, conv_id, body.pinned)
        return {"ok": True, "pinned": body.pinned}
    finally:
        cx.close()


# --- cockpit send QUEUE (ruled 2026-07-24: queue, don't block) --------------------------
# --- the background sender that used to live here ----------------------------
#
# It claimed jobs off a queue and ran a sending command, one at a time, waiting
# its turn for the browser. It is gone.
#
# It mattered more than the other send paths removed today, because it was the
# one still WIRED. The route in front of it refuses, and the command behind it
# names a module that no longer exists, so nothing was reaching LinkedIn - but
# that is an accident of a broken path, not a decision. A guarantee that rests
# on something being broken stops being a guarantee the moment somebody fixes it.
#
# LinkChat drafts. You approve. It lands in your outbox unsent. You send it.


def _ensure_send_queue_runner() -> None:
    """Kept as a name only, so nothing that calls it breaks. It starts nothing."""
    return


@router.get("/queue")
def send_queue_status():
    """What the cockpit send-queue is doing — queued / running / recently done."""
    from engine import ops
    jobs = [j for j in ops.list_queue() if j.get("action") == "cockpit-send"]
    running = [j for j in jobs if j.get("status") == "running"]
    queued = [j for j in jobs if j.get("status") == "queued"]
    return {"ok": True, "queued": len(queued), "running": len(running),
            "jobs": sorted(jobs, key=lambda j: j.get("enqueued_at", ""))[-40:]}


@router.post("/{conv_id}/send")
def reply_moved(conv_id: int, body: SendIn):
    """Replying from this screen moved, and it works. It is one door along.

    It used to refuse, and the reason was right at the time: a reply typed here
    would have gone out without facing the checks every other message faces. That
    is fixed now, by sending it down the same road rather than by opening a second
    one. The road is POST /api/crm/reply, because the checks live where your CRM
    is open, and this half of the program does not hold that door.
    """
    raise HTTPException(
        status_code=308,
        detail="replying works - it goes through /api/crm/reply, which runs the "
               "same checks every other message runs")


@router.post("/{conv_id}/fetch-messages")
def fetch_messages(conv_id: int):
    """Lazily pull a thread's full messages via Voyager (called on open when none stored yet)."""
    return _run_inbox_cmd("inbox-fetch-messages", str(conv_id), timeout=120)


# --- suggestion bridge + learning capture (the DM reply cockpit) -------------
# NOTE: these routes NEVER send. The frontend fires the existing POST /{conv_id}/send
# once per bubble to actually send a chosen give — the send path is untouched here.

@router.get("/{conv_id}/suggest")
def suggest(conv_id: int):
    """Read-only: classify the latest inbound reply and return the branch's give library
    (arms split into bubbles) for the cockpit to surface. Sends nothing.

    A missing conversation still 404s; any UNEXPECTED/DB error falls to a graceful empty
    payload (HTTP 200) — the cockpit must never see a 500 from the read-only suggest path
    (finding 5, 2026-07-21 review)."""
    latest = None
    thread_urn = None
    try:
        cx = _cx()
        try:
            conv = db.get_conversation(cx, conv_id)
        finally:
            cx.close()
        if not conv:
            raise HTTPException(status_code=404, detail="no such conversation")
        thread_urn = conv.get("thread_urn")
        for m in conv.get("messages", []):
            if m.get("direction") == "in" and m.get("body"):
                latest = m["body"]
        from engine import flows_engine as fe
        s = fe.suggest_for_text(latest)
        s["latest_inbound"] = latest
        s["thread_urn"] = thread_urn
        return s
    except HTTPException:
        raise
    except Exception:   # noqa: BLE001 — read-only path, never surface a 500 to the cockpit
        return {"version": None, "branch": None, "label": None, "gives": [],
                "latest_inbound": latest, "thread_urn": thread_urn}


@router.post("/{conv_id}/flow-record")
def flow_record(conv_id: int, body: FlowRecordIn):
    """Capture-only: record what a suggested give became on send (as-is vs edited) as a
    learning stamp on the main DB. NEVER sends; capture must never raise into the caller."""
    if not body.branch:
        return {"ok": False, "msg": "no branch"}
    try:
        cx = _cx()
        try:
            conv = db.get_conversation(cx, conv_id)
        finally:
            cx.close()
        thread_urn = conv.get("thread_urn") if conv else None
        # identity so the second_exchange/booked sensors can join an outcome to this send
        # (finding 2, 2026-07-21 review): lead_id + the CANONICAL participant profile URL.
        lead_id = conv.get("lead_id") if conv else None
        from engine.canon import canon_in
        purl = conv.get("participant_profile_url") if conv else None
        canonical_url = (canon_in(purl) or purl) if purl else None
        from engine import flows_engine as fe
        from engine import db as maindb
        with maindb.connect() as mc:
            gv = fe.give_version(mc)
            lin = None
            if gv is not None:
                r = mc.execute("SELECT lineage_uuid FROM flow_versions WHERE id=?",
                               (gv,)).fetchone()
                lin = r["lineage_uuid"] if r else None
            recorded = fe.record_suggested_send(
                mc, thread_urn=thread_urn, canonical_url=canonical_url, lead_id=lead_id,
                branch=body.branch, arm_key=body.arm_key, arm_hash=body.arm_hash,
                version_id=gv, lineage_uuid=lin,
                suggested_body=body.suggested_body or "",
                sent_body=body.sent_body or "")
        return {"ok": True, "recorded": recorded}
    except Exception as e:   # noqa: BLE001 — capture must never break the cockpit
        return {"ok": False, "msg": str(e)}


class ImproveIn(BaseModel):
    text: str
    branch: str | None = None
    arm_key: str | None = None
    context: str | None = None


@router.post("/{conv_id}/improve")
def improve(conv_id: int, body: ImproveIn):
    """Capture a free-text IMPROVEMENT the operator wrote ('here's how I'd handle this') as an
    'improve' learning stamp. Never sends; capture must never raise into the cockpit."""
    text = (body.text or "").strip()
    if not text:
        return {"ok": False, "msg": "empty improvement"}
    try:
        cx = _cx()
        try:
            conv = db.get_conversation(cx, conv_id)
        finally:
            cx.close()
        thread_urn = conv.get("thread_urn") if conv else None
        lead_id = conv.get("lead_id") if conv else None
        from engine.canon import canon_in
        purl = conv.get("participant_profile_url") if conv else None
        canonical_url = (canon_in(purl) or purl) if purl else None
        from engine import flows_engine as fe
        from engine import db as maindb
        with maindb.connect() as mc:
            gv = fe.give_version(mc)
            lin = None
            if gv is not None:
                r = mc.execute("SELECT lineage_uuid FROM flow_versions WHERE id=?",
                               (gv,)).fetchone()
                lin = r["lineage_uuid"] if r else None
            recorded = fe.record_improvement(
                mc, thread_urn=thread_urn, canonical_url=canonical_url, lead_id=lead_id,
                branch=body.branch, arm_key=body.arm_key, version_id=gv, lineage_uuid=lin,
                text=text, context=body.context or "")
        return {"ok": True, "recorded": recorded}
    except Exception as e:   # noqa: BLE001
        return {"ok": False, "msg": str(e)}


# --- DM Approvals Queue (batch review, 2026-07-22) ---------------------------
# A read-only BATCH of every conversation awaiting our reply, each with the engine's
# proposed give bubbles, plus a kill/skip decision that stamps the conv off the queue.
# Neither route sends: Approve is done by the frontend firing the existing /send once per
# bubble + /flow-record (the send path is untouched); kill/skip only write a stamp.
# review-queue is a LITERAL GET, declared BEFORE the catch-all /{conv_id} (see the note near
# the top: /{conv_id} lives last so a literal segment is never int-parsed as a conv id).

@router.get("/review-queue")
def review_queue(limit: int = 50, days: int = 21):
    """The single cockpit window (V3.1): BOTH awaiting-reply drafts AND reactivation drafts.
    Reply items (kind='reply') — every conversation awaiting OUR reply (last inbound, not
    archived) not yet decided (no sent/killed/skipped stamp newer than its last inbound),
    each with the give library for its classified branch. Reactivation items
    (kind='reactivation') — no-reply leads gone silent past the window who never got a nudge,
    each carrying its ACTUAL resolved message text (never an opener code). Off-map (branch
    None) and the R7 booking-intent ESCALATION are FLAGGED, never hidden. Ordering:
    escalations first, reply give-branches, reactivations, off-map last. Read-only — sends
    nothing. Any unexpected error degrades to an empty queue (the cockpit must never see a
    500 from this read path)."""
    import time as _t
    from engine.canon import canon_in
    from engine import flows_engine as fe
    from engine import db as maindb
    try:
        # Window the awaiting-reply candidates to the recent past (default ~3 weeks) so the
        # cockpit is a WORKABLE list, not the entire all-time backlog (~800+), and so the
        # folded-in reactivations are actually reachable within the limit. days<=0 = no window.
        _cut_ms = int(_t.time() * 1000) - days * 86400 * 1000 if days and days > 0 else None
        # A conversation with NO timestamp must stay in the queue.
        #
        # The list the inbox is read from carries "10:42 AM" or "Aug 14", never
        # an epoch, so last_msg_at is NULL on every row a sync writes. The
        # window compared NULL against the cut, NULL is never >=, and the whole
        # queue came back empty: 31 conversations, 5 of them waiting on a reply,
        # nothing shown. Found 2026-08-25 on a real inbox.
        #
        # Keeping an undated row is the safe direction. This window decides what
        # is SHOWN for a human to approve, not what may be sent - every check
        # still runs at the send, and nothing goes without a tap. Showing a
        # conversation that turns out to be old costs a moment; hiding one that
        # is waiting for a reply loses the person.
        _q = ("SELECT * FROM conversations WHERE last_msg_dir='in' AND archived_at IS NULL "
              + ("AND (last_msg_at IS NULL OR CAST(last_msg_at AS INTEGER) >= ?) "
                 if _cut_ms is not None else "")
              + "ORDER BY last_msg_at DESC, updated_at DESC")
        _args = (_cut_ms,) if _cut_ms is not None else ()
        cx = _cx()
        try:
            cands = [dict(r) for r in cx.execute(_q, _args)]
        finally:
            cx.close()
        now_epoch = _t.time()
        items = []
        with maindb.connect() as mc:
            for conv in cands:
                cid = conv["id"]
                thread_urn = conv.get("thread_urn")
                purl = conv.get("participant_profile_url")
                canonical_url = (canon_in(purl) or purl) if purl else None
                lead_id = conv.get("lead_id")
                last_inbound_at = conv.get("last_msg_at")
                if fe.is_decided(mc, last_inbound=last_inbound_at, thread_urn=thread_urn,
                                 canonical_url=canonical_url, lead_id=lead_id):
                    continue
                # latest NON-EMPTY inbound text — the exact predicate /suggest uses
                fcx = _cx()
                try:
                    full = db.get_conversation(fcx, cid)
                finally:
                    fcx.close()
                latest = None
                for m in (full.get("messages", []) if full else []):
                    if m.get("direction") == "in" and m.get("body"):
                        latest = m["body"]
                s = fe.suggest_for_text(latest, mc)
                branch = s.get("branch")
                days_waiting = None
                e = fe._to_epoch(last_inbound_at)
                if e is not None:
                    days_waiting = round(max(0.0, (now_epoch - e)) / 86400.0, 1)
                items.append({
                    "conv_id": cid,
                    "kind": "reply",
                    "participant_name": conv.get("participant_name"),
                    "headline": conv.get("participant_headline"),
                    "profile_url": purl,
                    "canonical_url": canonical_url,
                    "thread_urn": thread_urn,
                    "lead_id": lead_id,
                    "latest_inbound": latest,
                    "their_last": latest,
                    "context": None,
                    "days_waiting": days_waiting,
                    "version": s.get("version"),
                    "branch": branch,
                    "label": s.get("label"),
                    "gives": s.get("gives", []),
                    "off_map": branch is None,
                    "escalation": branch == "R7",
                })
            # --- fold in the reactivation candidates (V3.1: one complete window) ---------
            # No-reply leads who went silent past the window and never got their nudge. Each
            # carries its ACTUAL resolved message text (kind='reactivation'), not an opener
            # code. Additive + best-effort: a failure here must never break the reply queue.
            try:
                from engine import flows_sensors as _fsx
                rq = _fsx.build_reactivate_queue(limit=limit)
                ra_branch = rq.get("branch")
                for e in rq.get("entries", []):
                    ra_lead = e.get("lead_id")
                    ra_url = e.get("canonical_url")
                    if fe.reactivation_decided(mc, lead_id=ra_lead, canonical_url=ra_url):
                        continue          # a past kill/skip stands until a fresh inbound
                    # resolve a conv_id for display/open ONLY (decisions route by identity)
                    ra_conv_id, ra_urn = None, None
                    fcx = _cx()
                    try:
                        row = None
                        if ra_lead is not None:
                            row = fcx.execute("SELECT id, thread_urn FROM conversations "
                                              "WHERE lead_id=? LIMIT 1", (ra_lead,)).fetchone()
                        if row is None and ra_url and "/in/" in ra_url:
                            slug = ra_url.rsplit("/in/", 1)[-1].strip("/")
                            if slug:
                                row = fcx.execute(
                                    "SELECT id, thread_urn FROM conversations WHERE "
                                    "participant_profile_url LIKE ? LIMIT 1",
                                    (f"%/in/{slug}%",)).fetchone()
                        if row:
                            ra_conv_id, ra_urn = row["id"], row["thread_urn"]
                    finally:
                        fcx.close()
                    days = e.get("days_silent")
                    ctx = (f"No reply to the opener · silent {days}d" if days is not None
                           else "No reply to the opener")
                    items.append({
                        "conv_id": ra_conv_id,
                        "kind": "reactivation",
                        "participant_name": e.get("name"),
                        "headline": None,
                        "profile_url": e.get("profile_url"),
                        "canonical_url": ra_url,
                        "thread_urn": ra_urn,
                        "lead_id": ra_lead,
                        "latest_inbound": None,
                        "their_last": None,
                        "context": ctx,
                        "days_waiting": days,
                        "version": None,
                        "branch": ra_branch,
                        "label": "Re-activation nudge",
                        "gives": [{"arm_key": "reactivation", "arm_hash": None,
                                   "bubbles": e.get("bubbles") or []}],
                        "off_map": False,
                        "escalation": False,
                    })
            except Exception:   # noqa: BLE001 — reactivation is additive, never fatal
                pass
        # escalations first (0), reply give-branches (1), reactivations (2), off-map last (3);
        # within a group the longest-waiting first so nothing rots at the bottom.
        def _order(it):
            if it["escalation"]:
                g = 0
            elif it["off_map"]:
                g = 3
            elif it.get("kind") == "reactivation":
                g = 2
            else:
                g = 1
            return (g, -(it["days_waiting"] or 0.0))
        items.sort(key=_order)
        return {"queue": items[:limit], "count": len(items[:limit]), "total_awaiting": len(items)}
    except Exception:   # noqa: BLE001 — read-only path, never surface a 500 to the cockpit
        return {"queue": [], "count": 0, "total_awaiting": 0}


@router.post("/reactivation-decision")
def reactivation_decision(body: ReactivationDecisionIn):
    """KILL or SKIP a REACTIVATION candidate (a no-reply lead folded into the cockpit).
    Reactivations have no inbound and may have no conversation row, so this decides by LEAD
    IDENTITY (lead_id / canonical_url / thread_urn), not a conv_id — the send-path twin of
    /{conv_id}/review-decision. NEVER sends; a kill/skip stands until a fresh inbound moves
    the person into the reply lane. Literal path, declared before the catch-all /{conv_id}."""
    decision = (body.decision or "").strip().lower()
    if decision not in ("kill", "skip"):
        return {"ok": False, "msg": "decision must be 'kill' or 'skip'"}
    if body.lead_id is None and not body.canonical_url and not body.thread_urn:
        return {"ok": False, "msg": "no reactivation identity (lead_id / canonical_url / thread_urn)"}
    try:
        from engine import flows_engine as fe
        from engine import db as maindb
        with maindb.connect() as mc:
            gv = fe.give_version(mc)
            lin = None
            if gv is not None:
                r = mc.execute("SELECT lineage_uuid FROM flow_versions WHERE id=?",
                               (gv,)).fetchone()
                lin = r["lineage_uuid"] if r else None
            recorded = fe.record_review_decision(
                mc, decision=decision, thread_urn=body.thread_urn,
                canonical_url=body.canonical_url, lead_id=body.lead_id,
                branch=body.branch, arm_key=body.arm_key,
                version_id=gv, lineage_uuid=lin, reason=body.reason)
        return {"ok": True, "recorded": recorded, "decision": decision}
    except Exception as e:   # noqa: BLE001 — capture must never break the cockpit
        return {"ok": False, "msg": str(e)}


@router.post("/{conv_id}/review-decision")
def review_decision(conv_id: int, body: ReviewDecisionIn):
    """Record a review-queue KILL or SKIP so the conversation leaves the queue. NEVER sends.
    Approve is NOT here — the frontend approves by firing the existing /send + /flow-record.
    Capture must never raise into the caller (mirrors /flow-record)."""
    decision = (body.decision or "").strip().lower()
    if decision not in ("kill", "skip"):
        return {"ok": False, "msg": "decision must be 'kill' or 'skip'"}
    try:
        cx = _cx()
        try:
            conv = db.get_conversation(cx, conv_id)
        finally:
            cx.close()
        if not conv:
            raise HTTPException(status_code=404, detail="no such conversation")
        thread_urn = conv.get("thread_urn")
        lead_id = conv.get("lead_id")
        from engine.canon import canon_in
        purl = conv.get("participant_profile_url")
        canonical_url = (canon_in(purl) or purl) if purl else None
        from engine import flows_engine as fe
        from engine import db as maindb
        with maindb.connect() as mc:
            gv = fe.give_version(mc)
            lin = None
            if gv is not None:
                r = mc.execute("SELECT lineage_uuid FROM flow_versions WHERE id=?",
                               (gv,)).fetchone()
                lin = r["lineage_uuid"] if r else None
            recorded = fe.record_review_decision(
                mc, decision=decision, thread_urn=thread_urn, canonical_url=canonical_url,
                lead_id=lead_id, branch=body.branch, arm_key=body.arm_key,
                version_id=gv, lineage_uuid=lin, reason=body.reason)
        return {"ok": True, "recorded": recorded, "decision": decision}
    except HTTPException:
        raise
    except Exception as e:   # noqa: BLE001 — capture must never break the cockpit
        return {"ok": False, "msg": str(e)}


# ---------------------------------------------------------------------------
# Voice notes, attachments and playback: not built, and saying so.
#
# These came over half-finished from the program LinkChat was cut out of. Two of
# them called for code that was left behind, so asking for one used to end in a
# page of Python rather than an answer. The rule here is the same one the rest of
# the program follows: a thing that cannot happen says so in a sentence.
#
# None of this is on the critical path. Reading a conversation and replying to it
# are, and both work.
# ---------------------------------------------------------------------------

_NOT_BUILT = ("LinkChat does not handle voice notes or attachments yet. You can "
              "read every conversation and reply to any of them; anything with a "
              "recording or a file in it is opened in LinkedIn itself.")


@router.get("/audio")
def audio_proxy(u: str):
    """Playing back a voice note somebody sent you. Not built."""
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.post("/transcribe")
def transcribe_audio(body: TranscribeIn):
    """Writing out what a voice note says. Not built."""
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.post("/{conv_id}/attach")
def attach(conv_id: int):
    """Sending a file. Not built, and it would be a second way out of the program.

    Every message LinkChat carries passes five checks first. A file-sending route
    that skipped them would be exactly the sort of quiet second door this whole
    design exists to avoid, so it stays shut until it goes through the same gate.
    """
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.get("/mics")
def mics():
    """Which microphones are on this computer. Nothing records, so: none."""
    return {"mics": [], "default": None, "why": _NOT_BUILT}


@router.post("/voice/start")
def voice_start(body: MicStartIn):
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.post("/{conv_id}/voice-stop")
def voice_stop(conv_id: int):
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.get("/{conv_id}/voice-preview")
def voice_preview(conv_id: int):
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.post("/{conv_id}/voice-send")
def voice_send_ep(conv_id: int):
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.post("/{conv_id}/voice")
def send_voice_note(conv_id: int):
    raise HTTPException(status_code=501, detail=_NOT_BUILT)


@router.post("/{conv_id}/tags")
def set_conv_tag(conv_id: int, body: ConvTagIn):
    cx = _cx()
    try:
        db.set_conversation_tag(cx, conv_id, body.tag_id, body.on)
        return {"ok": True, "tags": db.conversation_tags(cx, conv_id)}
    finally:
        cx.close()


# --- tags --------------------------------------------------------------------

@router.get("/tags")
def tags():
    cx = _cx()
    try:
        return {"tags": db.list_tags(cx)}
    finally:
        cx.close()


@router.post("/tags")
def create_tag(body: TagIn):
    cx = _cx()
    try:
        return {"ok": True, "tag": db.create_tag(cx, body.name, body.color)}
    finally:
        cx.close()


@router.delete("/tags/{tag_id}")
def delete_tag(tag_id: int):
    cx = _cx()
    try:
        db.delete_tag(cx, tag_id)
        return {"ok": True}
    finally:
        cx.close()


# --- snippets ----------------------------------------------------------------

@router.get("/snippets")
def snippets():
    cx = _cx()
    try:
        return {"snippets": db.list_snippets(cx)}
    finally:
        cx.close()


@router.post("/snippets")
def upsert_snippet(body: SnippetIn):
    cx = _cx()
    try:
        return {"ok": True, "snippet": db.upsert_snippet(cx, body.name, body.body)}
    finally:
        cx.close()


@router.delete("/snippets/{snippet_id}")
def delete_snippet(snippet_id: int):
    cx = _cx()
    try:
        db.delete_snippet(cx, snippet_id)
        return {"ok": True}
    finally:
        cx.close()


# --- export (the privacy wedge: local CSV, no cloud) -------------------------

@router.get("/export")
def export(format: str = "csv"):
    cx = _cx()
    try:
        rows = db.export_rows(cx)
    finally:
        cx.close()
    if format != "csv":
        return {"rows": rows}
    buf = io.StringIO()
    cols = ["name", "headline", "profile_url", "last_dir", "tags", "note",
            "snooze", "archived", "messages", "thread_urn"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=the inbox half-export.csv"})


# --- open a conversation (declared LAST: see the note near the top) ----------

@router.get("/{conv_id}")
def conversation(conv_id: int):
    cx = _cx()
    try:
        c = db.get_conversation(cx, conv_id)
        if not c:
            raise HTTPException(status_code=404, detail="conversation not found")
        if c.get("unread"):
            db.mark_read(cx, conv_id)   # opening a conversation marks it read (local)
            c["unread"] = 0
        return c
    finally:
        cx.close()
