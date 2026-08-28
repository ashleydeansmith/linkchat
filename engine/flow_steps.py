"""flow_steps.py — the flow file's STEPS: what the engine walks, derived from flows.json.

WHY THIS EXISTS (Build Plan V3 §5, 2026-08-27). `import_flows_json` carried Ashley's
branches, openers and templates into the database and dropped the rest of the ladder:
`followups` (the second follow-up, the final message) was not one of the ten keys it
read, so rungs 2 and 3 lived in flows.json and on the board and nowhere the sender could
reach. The sender read a campaign's one-item step list instead. 630 people finished
their "journey" after one message.

This module turns a flows.json document — v8.0 as Ashley writes it today, or v9 with an
explicit `steps` list per branch — into one PROGRAM: for every branch, the ordered steps
the engine walks (send / wait / transfer / park), which parent it inherits its silence
rule from, and the words each step sends, keyed so the pass can resolve them. Nothing in
Ashley's file is reworded; a v8 file is TRANSLATED into steps by the rulings in
`RULINGS.md` (three follow-ups then park, R-F/G/H/I), and a v9 file is read as written.

`flow_check` refuses a shape the engine could not walk, naming the line. The program
lives in `flow_nodes.meta` (per branch) and `flow_arms` (the words), so it rides the
same versions, lineage and activation as everything else on the canvas.

Zero LLM. Deterministic. Read by flow_run.py.
"""
from __future__ import annotations

import json
import re
from typing import Any

_ROUTE = re.compile(r"\broute(?:s|d)? to (R\d+[a-z]?)\b", re.I)

# Terminal branches: no silence rule, on purpose (flows.json `silence.never`: never nudge
# someone who said no, never nudge a seller; R7 escalates same day; R0 IS the ladder).
TERMINAL = {"R0", "R4", "R5", "R7", "R12", "R14"}
PARENT_KEY = "in_conversation"
REACT_KEY = "reactivation"

# Actions this product can run. LinkChat's registry is smaller; flow_check takes the
# product's set so an `invite_to_event` step in a member's file is refused by name.
ACTIONS_LINKFORGE = {"send_opener", "send", "wait", "park", "transfer", "red_list",
                     "withdraw_invite", "invite_to_event", "send_booking_link", "crm_write"}

BUBBLE_SEP = " · "


def _bubbles(v) -> list[str]:
    """A words value as a list of bubbles: a list stays a list; a string splits on the
    canvas separator; anything else is empty."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [x.strip() for x in v.split(BUBBLE_SEP) if x.strip()]
    return []


# ---------------------------------------------------------------------------
# v8 -> steps
# ---------------------------------------------------------------------------

def ladder_steps_v8(r0: dict) -> list[dict]:
    """R0's ladder from a v8.0 branch (`templates` + `followups`), per the rulings:
    opener -> f1 (4 days, crossed on the cold ladder, matched on re-entry: R-I) ->
    f2 (8 days) -> final (18 days) -> re-activation 30 silent days after the final
    (R-E, R-H). No disconnect. Every wait is measured from the previous send."""
    steps = [
        {"key": "opener", "do": "send_opener",
         "words": {"by_arm": {"B": "openers.B.text", "C": "openers.C.text"}},
         "wait": {"days": 0, "from": "accepted"}, "on_reply": "interrupt"},
        {"key": "f1", "do": "send",
         "words": {"by_arm": {"cold": {"B": "R0.t1", "C": "R0.t2"},
                              "matched": {"B": "R0.t2", "C": "R0.t1"}},
                   "table": "cold"},
         "wait": {"days": 4, "from": "previous_send"}, "on_reply": "interrupt"},
    ]
    fu = r0.get("followups") or {}
    if _bubbles((fu.get("followup_2") or {}).get("bubbles")):
        steps.append({"key": "f2", "do": "send", "words": "R0.followup_2",
                      "wait": {"days": 8, "from": "previous_send"}, "on_reply": "interrupt"})
    if _bubbles((fu.get("final") or {}).get("bubbles")):
        steps.append({"key": "final", "do": "send", "words": "R0.final",
                      "wait": {"days": 18, "from": "previous_send"}, "on_reply": "interrupt"})
    steps.append({"key": "to_reactivation", "do": "transfer", "to": f"{REACT_KEY}.entry",
                  "wait": {"days": 30, "from": "previous_send"}})
    return steps


def silence_steps_v8(doc: dict) -> list[dict]:
    """The parent's silence rule from v8's top-level `silence` block, per R-F/R-G:
    👀 three days after our last send (max three days late), then seven days later a
    transfer INTO the ladder at f1 with the matched table and the cycle counter up."""
    seq = ((doc.get("silence") or {}).get("sequence") or {})
    eyes_days = int(((seq.get("step_1") or {}).get("after_days")) or (doc.get("silence") or {}).get("fires_after_days") or 3)
    ladder_days = int(((seq.get("step_2") or {}).get("after_days")) or 7)
    return [
        {"key": "eyes", "do": "send", "words": "silence.step_1",
         "wait": {"days": eyes_days, "from": "our_last_send"}, "max_late_days": 3,
         "on_reply": "interrupt"},
        {"key": "ladder", "do": "transfer", "to": "R0.f1", "table": "matched",
         "ladder_cycle": "+1", "wait": {"days": ladder_days, "from": "previous_send"}},
    ]


def build_program(doc: dict) -> dict:
    """The program for a flows.json document (v8 translated, v9 read as written).

    Returns {
      "branches": {bid: {"steps": [...], "parent": str|None, "silence": dict|None|"absent"}},
      "sections": {"in_conversation": {"silence": {"steps": [...]}}, "reactivation": {"steps": [...]}},
      "words":    {ref: [bubbles]}   # every words reference the steps use, resolved
    }"""
    branches: dict[str, dict] = {}
    words: dict[str, list[str]] = {}
    for o in doc.get("openers", []):
        words[f"openers.{o['id']}.text"] = _bubbles(o.get("text"))
    for b in doc.get("branches", []):
        bid = b["id"]
        for ti, tmpl in enumerate(b.get("templates", []) or []):
            words[f"{bid}.t{ti + 1}"] = _bubbles(tmpl)
        fu = b.get("followups") or {}
        for fk, fv in fu.items():
            bb = _bubbles((fv or {}).get("bubbles")) if isinstance(fv, dict) else []
            if bb:
                words[f"{bid}.{fk}"] = bb
        # steps: v9 explicit, else v8 translation — the ladder for R0, and for a reply
        # branch that carries templates, ONE `move` step: its locked reply, answered
        # 4–18 minutes after the reply arrived (R-K), interrupted by a further reply.
        # Two templates split by opener arm (B -> t1, C -> t2); one serves both.
        if isinstance(b.get("steps"), list):
            steps = b["steps"]
        elif bid == "R0" and (b.get("templates") or fu):
            steps = ladder_steps_v8(b)
        elif b.get("templates"):
            n_t = len([t for t in b["templates"] if _bubbles(t)])
            w = {"by_arm": {"B": f"{bid}.t1", "C": f"{bid}.t2"}} if n_t >= 2 else f"{bid}.t1"
            steps = [{"key": "move", "do": "send", "words": w,
                      "wait": {"minutes": "answer", "from": "reply"}, "on_reply": "interrupt"}]
        else:
            steps = []
        # where a reply to THIS branch's move may go next ("route to Rn" in forward) —
        # the stage-2 candidates. Empty means: a reply here goes to Ashley.
        next_branches = sorted({m.group(1).upper() for f in (b.get("forward") or [])
                                for m in [_ROUTE.search(f.get("then") or "")] if m})
        # silence: explicit dict, explicit null, or absent -> the default table
        if "silence" in b:
            silence = b["silence"]          # dict or None
            parent = b.get("parent") if silence is None and b.get("parent") else None
            if silence is None and not b.get("parent"):
                silence = None              # declared terminal
        elif b.get("parent"):
            silence, parent = "absent", b["parent"]
        elif bid in TERMINAL:
            silence, parent = None, None
        else:
            silence, parent = "absent", PARENT_KEY
        branches[bid] = {"steps": steps, "parent": parent, "silence": silence,
                         "next_branches": next_branches,
                         "label": b.get("label"), "read": b.get("read"),
                         "patterns": b.get("patterns") or []}
    # words an export carries explicitly (ref -> bubbles) — import -> export -> import
    # must be a fixed point, and an exported branch has arms, not `templates`/`followups`
    for ref, v in (doc.get("words") or {}).items():
        bb = _bubbles(v)
        if bb and ref not in words:
            words[ref] = bb
    sil = (doc.get("silence") or {})
    # the stall nudge: v8's silence.sequence.step_1, else an explicit words entry (a v9 file,
    # a member's starter where it is a gap they fill), else the 👀 the rulings chose
    step1 = (_bubbles(((sil.get("sequence") or {}).get("step_1") or {}).get("bubbles"))
             or _bubbles((doc.get("words") or {}).get("silence.step_1")) or ["👀"])
    words["silence.step_1"] = step1
    has_ladder_f1 = any(s.get("key") == "f1" for s in (branches.get("R0") or {}).get("steps", []))
    sil_steps = (sil.get("steps") if isinstance(sil.get("steps"), list) else silence_steps_v8(doc))
    if not has_ladder_f1:
        # a flow with no ladder (a minimal file, a member's starter) stalls to 👀 then stops
        sil_steps = [s for s in sil_steps if not (s.get("do") == "transfer" and str(s.get("to", "")).startswith("R0."))]
    sections = {
        PARENT_KEY: {"silence": {"steps": sil_steps}},
        REACT_KEY: {"steps": (doc.get(REACT_KEY, {}).get("steps")
                              if isinstance((doc.get(REACT_KEY) or {}).get("steps"), list)
                              else [{"key": "entry", "do": "park",
                                     "reason": "30 silent days after our last message",
                                     "reactivate": "sequence_unwritten"}])},
    }
    return {"branches": branches, "sections": sections, "words": words}


# ---------------------------------------------------------------------------
# flow_check — refuse a shape the engine could not walk
# ---------------------------------------------------------------------------

def _word_refs(words_field) -> list[str]:
    """Every reference a step's `words` field names (string, or by_arm tables)."""
    if isinstance(words_field, str):
        return [words_field]
    if isinstance(words_field, dict):
        out = []
        ba = words_field.get("by_arm") or {}
        for v in ba.values():
            if isinstance(v, dict):
                out += [x for x in v.values() if isinstance(x, str)]
            elif isinstance(v, str):
                out.append(v)
        return out
    return []


def flow_check(program: dict, actions: set[str] = ACTIONS_LINKFORGE,
               opener_arms: tuple[str, ...] = ("B", "C")) -> list[str]:
    """Faults, each naming the branch and step. Empty list = the engine can walk it.
    Refuses: a words reference with no words; a by_arm table missing a live opener arm
    (A is parked by rule and never required); a transfer to a node that does not exist;
    a branch declaring none of parent / silence / silence:null; a transfer into the
    ladder with no ladder_cycle bump; an unknown action for this product; a wait with
    no `from`."""
    faults: list[str] = []
    words = program["words"]
    all_nodes: set[str] = set()
    for bid, b in program["branches"].items():
        for st in b["steps"]:
            all_nodes.add(f"{bid}.{st.get('key')}")
    for sk, sec in program["sections"].items():
        for st in (sec.get("steps") or (sec.get("silence") or {}).get("steps") or []):
            all_nodes.add(f"{sk}.{st.get('key')}")

    def check_steps(owner: str, steps: list[dict], into_ladder_ok: bool):
        for st in steps:
            key = f"{owner}.{st.get('key', '?')}"
            do = st.get("do")
            if do not in actions:
                faults.append(f"{key}: unknown action '{do}' for this product")
                continue
            if do in ("send", "send_opener", "send_booking_link"):
                refs = _word_refs(st.get("words"))
                if not refs:
                    faults.append(f"{key}: a send with no words")
                for r in refs:
                    if not words.get(r):
                        faults.append(f"{key}: words '{r}' have no template")
                ba = (st.get("words") or {}).get("by_arm") if isinstance(st.get("words"), dict) else None
                if ba:
                    tables = ba.values() if all(isinstance(v, dict) for v in ba.values()) else [ba]
                    for t in tables:
                        for arm in opener_arms:
                            if arm not in t:
                                faults.append(f"{key}: by_arm table missing arm {arm}")
            if do == "transfer":
                to = st.get("to") or ""
                if to not in all_nodes:
                    faults.append(f"{key}: transfer to '{to}', which is not a step in this flow")
                if to.startswith("R0.") and st.get("ladder_cycle") != "+1" and not into_ladder_ok:
                    faults.append(f"{key}: a transfer into the ladder must carry ladder_cycle '+1'")
            w = st.get("wait")
            if w is not None and not (isinstance(w, dict) and "from" in w and ("days" in w or "minutes" in w)):
                faults.append(f"{key}: a wait must say days (or minutes) and what it is measured from")

    for bid, b in program["branches"].items():
        if b["silence"] == "absent" and not b["parent"]:
            faults.append(f"{bid}: declares none of parent / silence / silence: null")
        check_steps(bid, b["steps"], into_ladder_ok=(bid == "R0"))
    for sk, sec in program["sections"].items():
        steps = sec.get("steps") or (sec.get("silence") or {}).get("steps") or []
        check_steps(sk, steps, into_ladder_ok=False)
    return faults


# ---------------------------------------------------------------------------
# storing and reading the program on a version
# ---------------------------------------------------------------------------

def _arm_key_for(ref: str) -> tuple[str, str]:
    """A words reference -> (node_key, arm_key) on the canvas.
    'openers.B.text' -> ('opener-B', 'a') · 'R0.t1' -> ('R0-move', 't1') ·
    'R0.followup_2' -> ('R0-move', 'followup_2') · 'silence.step_1' -> ('in_conversation-move', 'step_1')."""
    parts = ref.split(".")
    if parts[0] == "openers" and len(parts) == 3:
        return f"opener-{parts[1]}", "a"
    if parts[0] == "silence":
        return f"{PARENT_KEY}-move", parts[1]
    return f"{parts[0]}-move", ".".join(parts[1:])


def attach_program(conn, version_id: int, doc: dict) -> dict:
    """Write the program onto a freshly imported version: steps/parent/silence into each
    branch node's meta; the two section nodes; every words reference as an arm that does
    not already exist. Returns the program. Raises ValueError on flow_check faults."""
    program = build_program(doc)
    faults = flow_check(program)
    if faults:
        raise ValueError("flow_check refused the flow: " + "; ".join(faults))
    for bid, b in program["branches"].items():
        row = conn.execute("SELECT meta FROM flow_nodes WHERE version_id=? AND node_key=?",
                           (version_id, bid)).fetchone()
        meta = json.loads(row["meta"]) if row and row["meta"] else {}
        meta["steps"] = b["steps"]
        meta["parent"] = b["parent"]
        meta["silence"] = b["silence"]
        meta["next_branches"] = b.get("next_branches") or []
        conn.execute("UPDATE flow_nodes SET meta=? WHERE version_id=? AND node_key=?",
                     (json.dumps(meta, ensure_ascii=False), version_id, bid))
    for sk, sec in program["sections"].items():
        conn.execute(
            "INSERT OR IGNORE INTO flow_nodes (version_id, node_key, kind, label, meta) "
            "VALUES (?,?,?,?,?)",
            (version_id, sk, "section", sk.replace("_", " "), json.dumps(sec, ensure_ascii=False)))
        conn.execute(
            "INSERT OR IGNORE INTO flow_nodes (version_id, node_key, kind, label) VALUES (?,?,?,?)",
            (version_id, f"{sk}-move", "move", f"{sk} → words"))
    from .flows_engine import content_hash
    for ref, bubbles in program["words"].items():
        if not bubbles:
            continue
        nk, ak = _arm_key_for(ref)
        body = BUBBLE_SEP.join(bubbles)
        have = conn.execute("SELECT 1 FROM flow_arms WHERE version_id=? AND node_key=? AND arm_key=?",
                            (version_id, nk, ak)).fetchone()
        if not have:
            conn.execute("INSERT INTO flow_arms (version_id, node_key, arm_key, body, content_hash) "
                         "VALUES (?,?,?,?,?)", (version_id, nk, ak, body, content_hash(body)))
    return program


def read_program(conn, version_id: int) -> dict:
    """The program back out of a version — what flow_run walks."""
    branches, sections = {}, {}
    for r in conn.execute("SELECT node_key, kind, meta, label, read, patterns FROM flow_nodes WHERE version_id=? "
                          "AND kind IN ('branch','section')", (version_id,)):
        meta = json.loads(r["meta"]) if r["meta"] else {}
        if r["kind"] == "branch":
            branches[r["node_key"]] = {"steps": meta.get("steps") or [],
                                       "parent": meta.get("parent"),
                                       "silence": meta.get("silence", "absent"),
                                       "next_branches": meta.get("next_branches") or [],
                                       "label": r["label"], "read": r["read"],
                                       "patterns": json.loads(r["patterns"]) if r["patterns"] else []}
        else:
            sections[r["node_key"]] = meta
    return {"branches": branches, "sections": sections}


def words_for(conn, version_id: int, ref: str) -> list[str]:
    """The bubbles a words reference resolves to on this version. Empty if none."""
    nk, ak = _arm_key_for(ref)
    r = conn.execute("SELECT body FROM flow_arms WHERE version_id=? AND node_key=? AND arm_key=? "
                     "AND enabled=1", (version_id, nk, ak)).fetchone()
    return _bubbles(r["body"]) if r else []


def steps_for(program: dict, node_key: str) -> tuple[list[dict], int]:
    """(the step list that contains node_key, its index). node_key is '<owner>.<step key>'.
    Raises KeyError when the node is not in the program — the caller parks node_missing."""
    owner, _, key = node_key.partition(".")
    if owner in program["branches"]:
        steps = program["branches"][owner]["steps"]
    elif owner in program["sections"]:
        sec = program["sections"][owner]
        steps = sec.get("steps") or (sec.get("silence") or {}).get("steps") or []
    else:
        raise KeyError(node_key)
    for i, st in enumerate(steps):
        if st.get("key") == key:
            return steps, i
    raise KeyError(node_key)


def candidates_for(program: dict, node_key: str, branch_key: str | None = None) -> list[str]:
    """The closed list of branches a reply at THIS stage may be judged against — never
    the whole flow (Ashley 2026-08-27: "it should never be shown stage 3 replies if it's
    in stage 1"). On the ladder (a reply to an opener or a follow-up): every reply branch
    with patterns. On a branch, or stalled after one: that branch's `next_branches` — and
    in a v8 file none are written, so a reply there goes to Ashley."""
    owner = node_key.split(".")[0]
    if owner == "R0":
        return [k for k, v in program["branches"].items() if k != "R0" and (v.get("patterns") or v.get("steps"))]
    src = branch_key if owner in program["sections"] else owner
    b = program["branches"].get(src or "")
    return list(b.get("next_branches") or []) if b else []


def branch_dicts(program: dict, keys: list[str]) -> list[dict]:
    """The candidate branches in the shape reply_read.decide reads: id, label, read,
    patterns, and `templates` = whether the branch has words of its own to send."""
    out = []
    for k in keys:
        b = program["branches"].get(k) or {}
        out.append({"id": k, "label": b.get("label"), "read": b.get("read"),
                    "patterns": b.get("patterns") or [],
                    "templates": ["x"] if any(s.get("do") == "send" for s in b.get("steps") or []) else []})
    return out


def silence_steps_for(program: dict, branch: str) -> list[dict]:
    """The silence steps that apply to a branch: its own, else the nearest ancestor's,
    else none (a declared terminal). One rule, written once (Build Plan V3 §5.3)."""
    seen = set()
    cur = branch
    while cur and cur not in seen:
        seen.add(cur)
        b = program["branches"].get(cur)
        if b is None:
            sec = program["sections"].get(cur) or {}
            return (sec.get("silence") or {}).get("steps") or []
        if isinstance(b.get("silence"), dict):
            return b["silence"].get("steps") or []
        if b.get("silence") is None and not b.get("parent"):
            return []
        cur = b.get("parent")
    return []


def program_summary(program: dict) -> dict[str, Any]:
    return {"branches": {k: [s.get("key") for s in v["steps"]] for k, v in program["branches"].items() if v["steps"]},
            "parents": {k: v["parent"] for k, v in program["branches"].items()},
            "terminal": sorted(k for k, v in program["branches"].items() if v["silence"] is None and not v["parent"]),
            "sections": {k: [s.get("key") for s in (v.get("steps") or (v.get("silence") or {}).get("steps") or [])]
                         for k, v in program["sections"].items()},
            "words": sorted(program.get("words", {}).keys())}
