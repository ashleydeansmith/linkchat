"""server.py — the engine behind the two screens.

LinkChat runs as one process: this serves the screens and answers what they ask.

There are three groups of routes.

  /api/crm/*      your CRM: what LinkChat can see, your people, what is waiting
                  for you to approve, and approving it
  /api/flows/*    the sequences: their shape, their branches, how they are doing
  /api/inbox/*    your conversations, brought over whole from the inbox that was
                  already built

Nothing here sends a message. The furthest any route goes is writing a file into
the outbox in your CRM, marked unsent, for you to send yourself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import __version__, DATA_DIR
from . import db
from . import crm_bridge
from . import gather
from .config import Config

app = FastAPI(title="LinkChat", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# The CRM, held open for as long as the program runs.
# ---------------------------------------------------------------------------

_BRIDGE = None


def crm(required=True):
    """The open door into your CRM.

    Opened once and kept, because opening it reloads every part of your CRM and
    doing that on each request would be slow and would reset their state.
    """
    global _BRIDGE
    if _BRIDGE is None:
        try:
            _BRIDGE = crm_bridge.open_crm()
        except crm_bridge.NoCRM as exc:
            if required:
                raise HTTPException(409, str(exc))
            return None
    return _BRIDGE


def forget_crm():
    global _BRIDGE
    _BRIDGE = None



_RECENT_BODIES = []


def _too_similar_to_recent(bridge, body, keep=25):
    """Refuse a message that is nearly the last one, and say so.

    This replaces a daily count, which was the wrong check for a conversation.
    LinkedIn does not publish a cap on messages to people you are connected to; what
    it watches is whether people reply and whether they report you. Twenty different
    replies to twenty different people is somebody having conversations. Twenty
    copies of one sentence is the thing that gets an account limited - and the count
    cannot tell those apart, because it never looks at the words.
    """
    import re
    text = re.sub(r"\s+", " ", str(body or "")).strip().lower()
    if len(text) < 25:
        return None
    for previous in _RECENT_BODIES:
        if _sameness(text, previous) >= 0.9:
            return ("that is almost word-for-word a message you just approved. "
                    "Change it so it speaks to this person, then approve it again.")
    _RECENT_BODIES.append(text)
    del _RECENT_BODIES[:-keep]
    return None


def _sameness(a, b):
    """How much of the shorter message appears, in order, in the longer one."""
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def _unfilled(body):
    """Every {placeholder} still sitting in a finished message.

    A sequence writes "Hi {first_name}" and fills it from the record. When the
    record has no first name the gap survives, and "Hi {first_name}" is the single
    most obvious way a message can announce that nobody wrote it. Empty means it
    is safe.
    """
    from .flows_sensors import unresolved
    return unresolved(body)


def _their_decoration(body, raw_name):
    """The symbols out of THEIR name field, if the message typed them back.

    Never your own: an emoji you deliberately wrote into your own words is your
    writing and is left alone. This only fires when the characters in the message
    are the ones in the name on their profile.
    """
    from . import names
    return names.leaked_decoration(body, raw_name)


@app.get("/api/health")
def health() -> dict:
    fault = globals().get("CONVERSATIONS_FAULT") or ""
    return {"ok": not fault, "version": __version__, "app": "LinkChat",
            "conversations_fault": fault}


# ---------------------------------------------------------------------------
# Your CRM
# ---------------------------------------------------------------------------

@app.get("/api/crm/state")
def crm_state() -> dict:
    """What LinkChat can see, and what it cannot do yet, in one answer.

    This is what the screen reads to decide whether to show the sequences as
    workable or as reading-only. It never raises when there is no CRM: a member
    on the call who has not pointed LinkChat anywhere yet must see a screen that
    explains that, not an error.
    """
    bridge = crm(required=False)
    if bridge is None:
        return {"connected": False,
                "looked_in": str(Path.home() / "CRM"),
                "reading_only": True,
                "can": {"read": False, "sync": False, "draft": False},
                "missing": {}, "people": 0,
                "conversations_fault": globals().get("CONVERSATIONS_FAULT") or ""}
    state = bridge.state()
    state["connected"] = True
    state["people"] = len(bridge.people())
    state["you"] = bridge.you()
    state["conversations_fault"] = globals().get("CONVERSATIONS_FAULT") or ""
    return state


class ChooseCRM(BaseModel):
    path: str
    you: str | None = None


@app.post("/api/crm/choose")
def crm_choose(body: ChooseCRM) -> dict:
    """Point LinkChat at your CRM. Asked once, on the first run."""
    try:
        bridge = crm_bridge.Bridge(body.path)
    except crm_bridge.NoCRM as exc:
        raise HTTPException(422, str(exc))
    crm_bridge.remember(bridge.root)
    if body.you:
        settings = {}
        try:
            settings = json.loads(crm_bridge.SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        settings["you"] = body.you
        tmp = crm_bridge.SETTINGS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        import os
        os.replace(tmp, crm_bridge.SETTINGS)
    forget_crm()
    # Build the inbox tables in the CRM that was just chosen.
    #
    # Why this line exists: the inbox creates its tables when it loads, which is
    # BEFORE anyone has said where the CRM is. At that moment they land beside the
    # program. The moment a CRM is chosen the folder changes, a fresh empty file is
    # made there, and nothing ever creates the tables in it - so every screen that
    # reads a conversation fails, and the screen paints the failure as an empty
    # inbox. It worked on the machine where the folder was already set and failed
    # on every other one, which is the worst way for a fault to behave on a call.
    try:
        from .inbox import db as inbox_db
        inbox_db.init()
    except Exception as exc:
        print("[linkchat] could not prepare the inbox in %s: %s" % (bridge.root, exc),
              file=sys.stderr)
    return crm_state()


@app.get("/api/crm/people")
def crm_people(q: str | None = None, limit: int = 200) -> dict:
    """Your people, as the sequences see them."""
    bridge = crm(required=False)
    if bridge is None:
        return {"people": [], "total": 0}
    people = bridge.people()
    if q:
        needle = q.strip().lower()
        people = [p for p in people
                  if needle in p["name"].lower() or needle in p["key"].lower()]
    return {"people": people[:limit], "total": len(people)}


@app.get("/api/crm/waiting")
def crm_waiting() -> dict:
    """Everything a sequence has written that you have not looked at yet."""
    bridge = crm(required=False)
    if bridge is None:
        return {"waiting": []}
    return {"waiting": bridge.awaiting_you()}


class ApproveBody(BaseModel):
    item_id: str
    to: str
    identifier: str
    body: str
    kind: str = "message"
    thread_urn: str | None = None    # which conversation it belongs to


class ReplyBody(BaseModel):
    conv_id: int                     # the conversation, as Conversations numbers them
    body: str


def _carry(bridge, *, to, identifier, body, thread_urn, kind="message",
           written_by, item_id=None):
    """The one road out. Every message that reaches a person comes through here.

    There is deliberately no second way. A reply typed on the Conversations screen
    and a message a sequence wrote both arrive at this function and face the same
    checks, because the moment there are two roads one of them stops being
    maintained, and it is the unmaintained one that reaches somebody.

    The checks, in the order they run:

      1. your CRM has the parts that do the checking      — no parts, no message
      2. the person is not on your hold list              — no list, everyone held
      3. nothing in the words was left unfilled           — no "Hi {first_name}"
      4. their own name field is not typed back at them   — the emoji trap
      5. it is not near enough to the last one to be a copy

    Then, only for a message a SEQUENCE wrote, a sixth: it cannot approve its own
    work, so your own review step has to release it and name who did. A reply you
    typed yourself does not face that one, and the reason matters. The rule was
    never "a person must retype what a machine wrote". It is that a machine must
    not decide somebody should hear from you. When you type the words, the
    deciding was yours, and there is nothing left to review.

    Whatever happens next, the words are written into your outbox FIRST, so a send
    that fails loses nothing.
    """
    ok, missing = bridge.can("draft")
    if not ok:
        raise HTTPException(409, "not installed yet: " + ", ".join(missing))
    if bridge.is_held(identifier, to):
        raise HTTPException(409, "that person is on your hold list")
    left = _unfilled(body)
    if left:
        raise HTTPException(
            409,
            "this still has %s in it, which is a gap nothing filled in. Write the "
            "words in yourself, or pick somebody the record is complete for."
            % ", ".join(left[:3]))
    leaked = _their_decoration(body, to)
    if leaked:
        raise HTTPException(
            409,
            "this opens with %r, which is their LinkedIn name field copied straight "
            "out. People put symbols in that field on purpose to catch messages "
            "nobody read. Greet them by name." % leaked)
    same = _too_similar_to_recent(bridge, body)
    if same:
        raise HTTPException(409, same)
    if item_id:
        try:
            bridge.approve(item_id)
        except crm_bridge.NotAllowed as exc:
            raise HTTPException(409, str(exc))

    message = {"to": to, "identifier": identifier, "kind": kind,
               "author": written_by, "body": body, "item_id": item_id or ""}
    try:
        # A sequence's message goes through your send gate whole: all five checks,
        # including the one saying somebody other than the author released it.
        # Words you typed yourself take the other door, which runs the same gate
        # and obeys every refusal it gives except the one that cannot apply to
        # your own writing. Neither door skips the hold list, and neither is
        # reachable without the five checks above.
        staged = (bridge.stage(message) if item_id
                  else bridge.stage_your_own(message))
    except crm_bridge.NotAllowed as exc:
        raise HTTPException(409, str(exc))

    # Written down. Now carry it.
    #
    # The copy in your outbox stays either way: if the carrying fails you still
    # have the words, and if it works you have a record of what went out that does
    # not depend on LinkedIn still showing it to you.
    sent = {"sent": False, "confirmed": False, "why": "not attempted"}
    if thread_urn:
        # THE BROWSER LOCK IS TAKEN ONCE, INSIDE THE KEEPER, AND NOT HERE.
        #
        # This used to take it here as well. Your lock does not queue - it refuses
        # if something already has it - so the keeper, one line later, asked for a
        # lock this very function was already holding and was told no. Every send
        # ended with "something else is using your LinkedIn browser", and the
        # something else was itself. The words survived, because they are written
        # down first, but nothing ever went out.
        try:
            from .inbox import keeper as K
            with K.drive(spawn=False, action="dm") as (page, why):
                if page is None:
                    sent = {"sent": False, "confirmed": False, "why": why}
                else:
                    r = K.send_message(page, thread_urn, body,
                                       do_send=True, name=to)
                    sent = {"sent": bool(r.get("sent")),
                            "confirmed": bool(r.get("confirmed")),
                            "why": r.get("msg", "")}
        except crm_bridge.NotAllowed as exc:
            sent = {"sent": False, "confirmed": False, "why": str(exc)}
        except Exception as exc:      # noqa: BLE001 - a failed send must not lose the words
            sent = {"sent": False, "confirmed": False, "why": "%s" % exc}
    else:
        sent = {"sent": False, "confirmed": False,
                "why": "LinkChat does not know which conversation this belongs to "
                       "yet - sync your inbox, then open the person in Conversations"}

    # WHAT IS COUNTED, AND WHAT IS NOT.
    #
    # A message that actually went out is a thing LinkedIn saw, so it is counted
    # against the one daily total you share with Gather. A message that only
    # reached your outbox is a file on your own computer and is counted as
    # nothing, because counting an intention spends an allowance that was never
    # used, and the next run behaves as though it was.
    bridge.log("message_sent" if sent.get("sent") else "message_staged",
               identifier, payload={"item_id": item_id or ""})
    if sent.get("sent"):
        bridge.did_act("message", to)

    return {"staged": str(staged),
            "sent": bool(sent.get("sent")),
            "confirmed": bool(sent.get("confirmed")),
            "why": sent.get("why", ""),
            "next": ("it has gone, and the reply comes back into Conversations"
                     if sent.get("sent")
                     else "it is in your outbox - " + str(sent.get("why", "")))}


@app.post("/api/crm/approve")
def crm_approve(body: ApproveBody) -> dict:
    """You, saying a message a sequence wrote may go - and then it going."""
    bridge = crm(required=True)
    return _carry(bridge, to=body.to, identifier=body.identifier, body=body.body,
                  thread_urn=body.thread_urn, kind=body.kind,
                  written_by=bridge.AUTHOR, item_id=body.item_id)


@app.post("/api/crm/reply")
def crm_reply(body: ReplyBody) -> dict:
    """You, replying to somebody in your own words, from the Conversations screen.

    This used to refuse, and the reason it gave was right at the time: a reply
    typed here would have gone out without facing the checks every other message
    faces. That has been fixed by routing it down the SAME road rather than by
    building a second one - so the hold list, the unfilled-words check and the
    copy check all stand in front of it, exactly as they do for a sequence.

    What it does not face is the review step, because you wrote it. That step
    exists so a sequence cannot mark its own homework. There is no sequence here.
    """
    bridge = crm(required=True)
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(422, "there is nothing written to send")

    from .inbox import db as cvdb
    cx = cvdb.connect()
    try:
        row = cx.execute("SELECT thread_urn, participant_name, participant_profile_url "
                         "FROM conversations WHERE id = ?", (body.conv_id,)).fetchone()
    except Exception:
        row = None
    finally:
        try:
            cx.close()
        except Exception:
            pass
    if row is None:
        raise HTTPException(404, "there is no conversation with that number")
    urn = row["thread_urn"] or ""
    if not urn:
        raise HTTPException(
            409, "this conversation has no address on LinkedIn yet - sync your "
                 "inbox and it will get one")
    name = row["participant_name"] or ""
    identifier = ""
    try:
        identifier = row["participant_profile_url"] or ""
    except Exception:
        identifier = ""
    return _carry(bridge, to=name, identifier=identifier or name, body=text,
                  thread_urn=urn, kind="reply", written_by=bridge.you() or "you")


# The sequence engine and the checks that read what happened and pick a branch.
from . import flows_engine as _fe
from . import flows_sensors as _fs


# ---------------------------------------------------------------------------
# Find people - your own Gather jobs, run from here.
#
# Not a second copy of them. LinkChat runs the ones in your CRM, so the records,
# the daily ceiling and the permission check are the same ones Gather uses when
# you run it from a terminal.
# ---------------------------------------------------------------------------

@app.get("/api/gather/state")
def gather_state() -> dict:
    """Whether your Gather jobs are here, and what they are."""
    bridge = crm(required=False)
    if bridge is None:
        return {"installed": False, "why": "no CRM chosen yet", "jobs": {}, "sources": {}}
    return gather.state(bridge.root)


class GatherRun(BaseModel):
    job: str
    mode: str = "probe"          # never commit unless asked, explicitly, every time
    source: str | None = None
    term: str | None = None
    limit: int | None = None


@app.post("/api/gather/run")
def gather_run(body: GatherRun) -> dict:
    """Run one job. Takes the browser lock first, so it cannot fight the inbox."""
    bridge = crm(required=True)
    if not gather.installed(bridge.root):
        raise HTTPException(409, "Gather is not installed in this CRM yet")
    if body.mode == "commit":
        # Asking strangers to connect IS the action LinkedIn rations, so this one
        # answers to the ceiling. Replying to somebody who already replied does not,
        # which is why approving a message does not come through here.
        allowed, reason = bridge.may_act(body.job)
        if not allowed:
            raise HTTPException(409, reason)
    try:
        with bridge.browser("linkchat-gather"):
            result = gather.run(bridge.root, body.job, mode=body.mode,
                                source=body.source, term=body.term, limit=body.limit)
    except crm_bridge.NotAllowed as exc:
        raise HTTPException(409, str(exc))
    if result.get("ok") and body.mode == "commit":
        bridge.did_act(body.job, body.source or body.job)
    return result


def _flows_gate() -> None:
    """Sequences are always on here.

    In the program these files came from, sequences were one feature among
    sixteen and started switched off. In LinkChat they are one of the two
    screens, so a switch that can hide them is a way for the program to look
    broken for no reason.
    """
    return


@app.get("/api/flows/versions")
def flows_versions() -> dict:
    _flows_gate()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, lineage_uuid, name, scope_campaign_id, status, source,"
            " created_at, activated_at, retired_at, updated_at FROM flow_versions"
            " ORDER BY id DESC")]
    return {"versions": rows}


class FlowVersionBody(BaseModel):
    name: str | None = None
    clone_from: int | None = None
    scope_campaign_id: int | None = None


@app.post("/api/flows/versions")
def flows_create_version(body: FlowVersionBody) -> dict:
    _flows_gate()
    try:
        vid = _fe.create_draft(name=body.name, clone_from=body.clone_from,
                               scope_campaign_id=body.scope_campaign_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"id": vid}


@app.post("/api/flows/versions/{vid}/activate")
def flows_activate(vid: int) -> dict:
    _flows_gate()
    try:
        return _fe.activate_version(vid)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/flows/versions/{vid}/graph")
def flows_graph(vid: int) -> dict:
    _flows_gate()
    try:
        return _fe.version_graph(vid)
    except KeyError as e:
        raise HTTPException(404, str(e))


class FlowGraphBody(BaseModel):
    updated_at: str                      # optimistic lock: must match the stored stamp
    nodes: list[dict]
    edges: list[dict]
    arms: list[dict]
    meta: dict | None = None
    name: str | None = None


@app.put("/api/flows/versions/{vid}/graph")
def flows_put_graph(vid: int, body: FlowGraphBody) -> dict:
    """Replace a DRAFT version's whole graph in one transaction. Single-writer per
    draft: an updated_at mismatch is a 409 refuse-and-reload, never a silent merge
    (§6b-24). Active/retired versions are immutable — clone to edit."""
    _flows_gate()
    import json as _json
    errs = []
    for n in body.nodes:
        if n.get("kind") == "branch":
            errs += [f"{n.get('node_key')}: {e}"
                     for e in _fe.validate_patterns(n.get("patterns") or [])]
    if errs:
        raise HTTPException(422, "pattern validation failed: " + "; ".join(errs))
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc).isoformat()
    with db.connect() as conn:
        v = conn.execute("SELECT * FROM flow_versions WHERE id=?", (vid,)).fetchone()
        if not v:
            raise HTTPException(404, f"flow version {vid} not found")
        if v["status"] != "draft":
            raise HTTPException(409, f"version {vid} is {v['status']} — clone it to edit")
        if (v["updated_at"] or "") != body.updated_at:
            raise HTTPException(409, "version changed since you loaded it — reload and re-apply")
        conn.execute("DELETE FROM flow_nodes WHERE version_id=?", (vid,))
        conn.execute("DELETE FROM flow_edges WHERE version_id=?", (vid,))
        conn.execute("DELETE FROM flow_arms WHERE version_id=?", (vid,))
        for n in body.nodes:
            conn.execute(
                "INSERT INTO flow_nodes (version_id, node_key, kind, label, read, color,"
                " patterns, priority, body, meta, canvas_x, canvas_y)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, n["node_key"], n.get("kind", "branch"), n.get("label"), n.get("read"),
                 n.get("color"), _json.dumps(n.get("patterns") or [], ensure_ascii=False),
                 int(n.get("priority", 100)), n.get("body"),
                 _json.dumps(n["meta"], ensure_ascii=False) if n.get("meta") else None,
                 n.get("canvas_x"), n.get("canvas_y")))
        for e in body.edges:
            conn.execute(
                "INSERT OR IGNORE INTO flow_edges (version_id, from_node, to_node,"
                " cond_type, cond_value) VALUES (?,?,?,?,?)",
                (vid, e["from_node"], e["to_node"], e.get("cond_type", "label"),
                 e.get("cond_value")))
        for a in body.arms:
            conn.execute(
                "INSERT INTO flow_arms (version_id, node_key, arm_key, body, content_hash,"
                " enabled, retired_at) VALUES (?,?,?,?,?,?,?)",
                (vid, a["node_key"], a["arm_key"], a.get("body") or "",
                 _fe.content_hash(a.get("body") or ""), int(a.get("enabled", 1)),
                 a.get("retired_at")))
        if body.meta is not None:
            conn.execute("UPDATE flow_versions SET meta=? WHERE id=?",
                         (_json.dumps(body.meta, ensure_ascii=False), vid))
        if body.name:
            conn.execute("UPDATE flow_versions SET name=? WHERE id=?", (body.name, vid))
        conn.execute("UPDATE flow_versions SET updated_at=? WHERE id=?", (now, vid))
    return {"ok": True, "updated_at": now}


@app.get("/api/flows/stats")
def flows_stats(version_id: int | None = None, lineage: str | None = None,
                since: str | None = None) -> dict:
    _flows_gate()
    try:
        out = _fe.stats(lineage_uuid=lineage, version_id=version_id, since=since)
    except KeyError as e:
        raise HTTPException(404, str(e))
    # mirror-staleness honesty (§6-10): stats move when the inbox syncs, not when
    # reality changes — the canvas header must show how fresh the ground truth is.
    try:
        from .inbox import db as _cvdb
        cx = _cvdb.connect()
        try:
            r = cx.execute("SELECT MAX(last_synced_at) m FROM conversations").fetchone()
            out["mirror_as_of"] = r["m"]
        finally:
            cx.close()
    except Exception:  # noqa: BLE001
        out["mirror_as_of"] = None
    return out


class ClassifyPreviewBody(BaseModel):
    patterns: list[str]
    limit: int = 50


@app.post("/api/flows/classify-preview")
def flows_classify_preview(body: ClassifyPreviewBody) -> dict:
    """Edit-time truth for the pattern editor: which RECENT inbound replies would this
    pattern set match. The preview ADVISES; validation BLOCKS (422) — finding 5."""
    _flows_gate()
    errs = _fe.validate_patterns(body.patterns)
    if errs:
        raise HTTPException(422, "; ".join(errs))
    from .inbox import db as _cvdb
    cx = _cvdb.connect()
    try:
        rows = cx.execute(
            "SELECT participant_name, last_preview FROM conversations WHERE "
            "last_msg_dir='in' AND last_preview IS NOT NULL AND last_preview != '' "
            "ORDER BY last_msg_at DESC LIMIT ?", (max(1, min(body.limit, 200)),)).fetchall()
    finally:
        cx.close()
    ordered = [("match", body.patterns)]
    out = [{"name": r["participant_name"], "reply": r["last_preview"],
            "matches": _fe.classify_ordered(r["last_preview"], ordered) == "match"}
           for r in rows]
    return {"sampled": len(out), "matched": sum(1 for r in out if r["matches"]),
            "replies": out}


class MarkBookedBody(BaseModel):
    at: str | None = None


@app.post("/api/flows/leads/{lead_id}/mark-booked")
def flows_mark_booked(lead_id: int, body: MarkBookedBody) -> dict:
    _flows_gate()
    try:
        inserted = _fs.mark_booked(lead_id, at=body.at,
                                   account_id=getattr(Config.load(), "flows_account_id",
                                                      "default") or "default")
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "inserted": inserted}


@app.get("/api/flows/reactivate-queue")
def flows_reactivate_queue(limit: int = 100) -> dict:
    """The re-activate tab's data: no-reply leads whose one nudge hasn't happened.
    STAGING ONLY — the UI lists candidates; drafting runs the human-gated DM chain."""
    _flows_gate()
    return _fs.build_reactivate_queue(limit=max(1, min(limit, 500)))


class FlowImportBody(BaseModel):
    flows: dict | None = None            # inline flows.json document...
    path: str | None = None              # ...or a file path (personal install)
    name: str = "imported flow"
    activate: bool = False


@app.post("/api/flows/import")
def flows_import(body: FlowImportBody) -> dict:
    _flows_gate()
    src = body.flows if body.flows is not None else body.path
    if not src:
        raise HTTPException(422, "provide 'flows' (inline json) or 'path'")
    try:
        vid = _fe.import_flows_json(src, name=body.name, activate=body.activate)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"id": vid, "activated": body.activate}


@app.post("/api/flows/start-from-shape")
def flows_start_from_shape() -> dict:
    """Load the starter sequence — the shape a member begins from.

    The Sequences screen otherwise opens on a blank canvas, and a blank canvas
    is where somebody who has never built a branch stops. This loads
    `sequences/starter-sequence.json`: one opening message and the four ways a
    person comes back from it, with every message left as a gap rather than as
    words.

    The gaps are the point. Check three refuses any message with a gap still in
    it, so the starter cannot send anything until the member has written their
    own words into all five. Nobody else's copy goes out under their name, and
    there is no way to approve past it.

    The file is found from the program's own folder rather than from wherever
    the window happened to be started, because those are not the same folder
    and the difference is invisible until it fails.
    """
    src = Path(__file__).resolve().parent.parent / "sequences" / "starter-sequence.json"
    if not src.exists():
        raise HTTPException(404, "the starter sequence is not in this copy of LinkChat")
    try:
        vid = _fe.import_flows_json(str(src), name="Starter sequence")
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": vid, "activated": False}


@app.get("/api/flows/versions/{vid}/export")
def flows_export(vid: int) -> dict:
    _flows_gate()
    try:
        return _fe.export_flows_json(vid)
    except KeyError as e:
        raise HTTPException(404, str(e))




# ---------------------------------------------------------------------------
# The inbox, brought over whole.
# Mounted before the catch-all below, or its routes would be swallowed by it:
# routes are matched in the order they are registered.
# ---------------------------------------------------------------------------
# WHY THIS FAILURE IS CARRIED RATHER THAN PRINTED. If the Conversations half of
# the program cannot load, the window still opens and the screen still draws — it
# just shows an inbox with nothing in it. That is the same picture as an inbox
# that is genuinely empty, and it is the worst way for a fault to behave, because
# on a call it reads as "your LinkedIn has no messages" rather than "this half
# did not start". Printing it does not help either: the window runs without a
# console, so nobody ever sees the line. So the reason is kept, and every screen
# that asks what LinkChat can do is told.
CONVERSATIONS_FAULT = ""
try:
    from .inbox.server import router as _inbox_router
    app.include_router(_inbox_router)
except Exception as _inbox_err:      # noqa: BLE001
    CONVERSATIONS_FAULT = "%s: %s" % (_inbox_err.__class__.__name__, _inbox_err)
    print("[linkchat] conversations did not load: %s" % CONVERSATIONS_FAULT,
          file=sys.stderr)


# ---------------------------------------------------------------------------
# The screens themselves, served from the same process so there is one program
# to start rather than two. Mounted LAST so it never shadows a route above.
# ---------------------------------------------------------------------------
from fastapi.staticfiles import StaticFiles      # noqa: E402


@app.api_route("/api/{rest:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def _api_not_found(rest: str):
    """An unknown /api path is a 404, never the page.

    Without this the mount below answers with the page's HTML and a 200, and the
    screen then tries to read HTML as an answer and reports a confusing fault
    instead of a missing route.
    """
    return JSONResponse({"detail": "no such route: /api/%s" % rest}, status_code=404)


_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="screens")
else:
    @app.get("/")
    def _no_ui():
        return JSONResponse(
            {"detail": "the screens have not been built yet — run: cd web && npm install && npm run build"},
            status_code=503)


# ---------------------------------------------------------------------------
# The window you double-click.
#
# Same shape as the one LinkForge opens: the engine runs on a background thread
# and a real native window owns the main one, so it has its own title bar and its
# own icon in the taskbar rather than looking like a browser someone left open.
# Started with pythonw, which is Python without a console, so no black box flashes
# up behind it.
# ---------------------------------------------------------------------------

def _serving(host, port):
    """Is OUR engine answering on that port, rather than merely something?

    Checking the port is open is not enough: a half-dead process from a previous
    run holds the port without answering, and the window would then open onto a
    refused connection with nothing to explain it.
    """
    import urllib.request
    try:
        with urllib.request.urlopen("http://%s:%d/api/health" % (host, port),
                                    timeout=1.5) as r:
            return b"LinkChat" in r.read()
    except Exception:
        return False


def run_desktop(host: str = "127.0.0.1", port: int = 8790) -> None:
    """Open LinkChat as a window."""
    import threading
    import time

    # Under pythonw there is no console, so anything printed has nowhere to go and
    # a crash would be silent. Send it to a file instead.
    if sys.stdout is None or sys.stderr is None:
        log = open(str(Path(DATA_DIR) / "window.log"), "a", buffering=1,
                   encoding="utf-8", errors="replace")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log

    if sys.platform == "win32":
        # Gives the taskbar our own icon rather than the plain Python one.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Outliers.LinkChat.App")
        except Exception:
            pass

    if not _serving(host, port):
        import uvicorn
        config = uvicorn.Config(app, host=host, port=port,
                                log_level="warning", access_log=False)
        threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()
        for _ in range(120):
            if _serving(host, port):
                break
            time.sleep(0.25)
        else:
            print("[linkchat] the engine did not start in time")

    try:
        import webview
    except Exception as exc:
        # No window library on this machine. Do NOT exit: started by double-click
        # there is no console, so exiting here means the icon does nothing at all
        # and the member has no way to find out why. Open their normal browser on
        # the same address - the whole program is there.
        print("[linkchat] no window library (%s); opening your browser instead" % exc)
        try:
            import webbrowser
            webbrowser.open("http://%s:%d/" % (host, port))
        except Exception:
            pass
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    # The address carries the time so the window never shows a stale page from a
    # previous version of the screens.
    webview.create_window("LinkChat",
                          "http://%s:%d/?v=%d" % (host, port, int(time.time())),
                          width=1400, height=960, min_size=(1100, 700))
    threading.Thread(target=_set_window_icon, daemon=True).start()
    webview.start()


def _set_window_icon():
    """Put the LinkChat icon on the title bar and the taskbar.

    The window library's own icon setting is ignored by the viewer Windows uses,
    so the icon is set on the window directly once it exists. The handle types
    matter: without them the window handle is cut to half its width and the icon
    comes out blank.
    """
    import time
    icon = Path(__file__).resolve().parent.parent / "web" / "public" / "favicon.ico"
    if sys.platform != "win32" or not icon.exists():
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                        wintypes.WPARAM, wintypes.LPARAM]
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        IMAGE_ICON, LOAD_FROM_FILE = 1, 0x0010
        hwnd = 0
        for _ in range(40):
            hwnd = user32.FindWindowW(None, "LinkChat")
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        for size, which in ((32, ICON_BIG), (16, ICON_SMALL)):
            handle = user32.LoadImageW(None, str(icon), IMAGE_ICON, size, size,
                                       LOAD_FROM_FILE)
            if handle:
                user32.SendMessageW(hwnd, WM_SETICON, which, handle)
    except Exception:
        pass
