"""flows_chart.py — the DM conversation-flow chart (ruled 2026-07-14): "map what
replies we are getting and what's the next best thing to say — a flow chart I can
visually see and interface with… if we map ~80% of where conversations go it stops
needing agent input: pattern recognition + a semi-pre-arranged message."

Deterministic, zero-LLM. Reads the owner-defined flow model (flows.json — patterns,
reads, pre-arranged next moves, forward branches) + LIVE reply data (messages +
conversations mirror), classifies every reply by pattern (unmatched = 'unclassified',
honestly — that bucket IS the roadmap to 80% coverage), and renders one self-contained
interactive HTML: opener → branch (live counts + real reply texts) → pre-arranged next
move → forward branches. The COVERAGE number at the top is the automation threshold.

  python -m engine flows-chart [--since YYYY-MM-DD]
Config: dm_flows_path (flows.json), dm_flows_html (output; default next to flows.json).
"""
from __future__ import annotations

import html
import json
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import DATA_DIR, db
from . import flows_engine
from .config import Config


def _norm_name(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", s.lower())).strip()


def _norm_text(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def classify(reply: str, branches: list[dict]) -> str | None:
    """Deterministic branch classification — DELEGATES to flows_engine (the one module
    that owns flow logic, plan §5.3). Same order semantics as v0: R5 outranks the
    yes/no reads, R2 before R1. Unmatched -> None."""
    return flows_engine.classify_ordered(reply, flows_engine.branches_to_ordered(branches))


def collect(since: str, flows: dict) -> dict:
    """Join messaged leads to inbound replies; classify each reply; count coverage."""
    cv_path = DATA_DIR / "conversations.db"
    inbound: dict[str, dict] = {}
    if cv_path.exists():
        cv = sqlite3.connect(cv_path)
        cv.row_factory = sqlite3.Row
        for r in cv.execute("SELECT participant_name, last_preview, last_msg_dir, "
                            "last_msg_at FROM conversations"):
            n = _norm_name(r["participant_name"])
            if n and n not in inbound:
                inbound[n] = {"preview": (r["last_preview"] or "").strip(),
                              "dir": r["last_msg_dir"], "at": r["last_msg_at"]}
    rows = []
    with db.connect() as conn:
        for r in conn.execute(
                "SELECT m.lead_id, MIN(m.sent_at) first_sent, l.full_name, "
                "l.status lead_status, COALESCE(ss.status,'') seq_status, "
                "GROUP_CONCAT(m.body, ' || ') bodies "
                "FROM messages m JOIN leads l ON l.id=m.lead_id "
                "LEFT JOIN sequence_state ss ON ss.lead_id=m.lead_id "
                "WHERE m.status='sent' AND m.sent_at >= ? GROUP BY m.lead_id", (since,)):
            n = _norm_name(r["full_name"])
            th = inbound.get(n)
            if not th:
                continue
            first_ms = None
            try:
                first_ms = int(datetime.fromisoformat(r["first_sent"]).timestamp() * 1000)
            except Exception:  # noqa: BLE001
                pass
            try:
                at = int(th["at"] or 0)
            except Exception:  # noqa: BLE001
                at = 0
            # A thread counts as a REPLY only on real evidence: their message is the
            # thread's last (awaiting us), or the pipeline marked them replied (we've
            # since responded). A thread whose last message is our own unanswered
            # welcome is NOT a reply (the first render counted 149 of those as
            # "replies" — dishonest join, fixed same night).
            replied = th["dir"] == "in" and first_ms and at > first_ms
            answered = (th["dir"] == "out"
                        and (r["lead_status"] == "replied" or r["seq_status"] == "stopped_reply"))
            if not (replied or answered):
                continue
            bodies = _norm_text(r["bodies"])
            opener = ("C" if "second brain" in bodies else
                      "B" if ("digital employee" in bodies or "see more of you" in bodies)
                      else "A")
            rows.append({"name": r["full_name"], "reply": th["preview"],
                         "opener": opener, "awaiting_us": bool(replied),
                         "branch": classify(th["preview"], flows["branches"])})
    classified = [r for r in rows if r["branch"]]
    return {
        "since": since,
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "total_replies": len(rows),
        "classified": len(classified),
        "coverage_pct": round(100 * len(classified) / len(rows), 1) if rows else None,
        "awaiting_us": sum(1 for r in rows if r["awaiting_us"]),
        "rows": rows,
    }


def render(flows: dict, live: dict) -> str:
    """One self-contained interactive HTML — no external deps, opens as a local file."""
    per_branch: dict[str, list[dict]] = {}
    for r in live["rows"]:
        per_branch.setdefault(r["branch"] or "unclassified", []).append(r)
    opener_counts: dict[str, int] = {}
    for r in live["rows"]:
        opener_counts[r["opener"]] = opener_counts.get(r["opener"], 0) + 1
    data = {"flows": flows, "live": live,
            "per_branch": per_branch, "opener_counts": opener_counts}
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    cov = live["coverage_pct"]
    cov_txt = f"{cov}%" if cov is not None else "no data"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>DM Conversation Flows</title><style>
:root{{--bg:#0e1116;--card:#171c24;--ink:#e8ecf2;--dim:#8a93a6;--line:#2a3140}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 'Segoe UI',system-ui,sans-serif;padding:28px}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:var(--dim);font-size:12px;margin-bottom:18px}}
.kpis{{display:flex;gap:14px;margin-bottom:24px;flex-wrap:wrap}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 18px;min-width:150px}}
.kpi b{{display:block;font-size:26px}}.kpi span{{color:var(--dim);font-size:11px;
text-transform:uppercase;letter-spacing:.06em}}
.cols{{display:grid;grid-template-columns:220px 1fr 1fr;gap:18px;align-items:start}}
.colh{{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.08em;
margin-bottom:10px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;margin-bottom:10px;cursor:pointer;border-left:4px solid var(--line)}}
.card:hover{{border-color:#3a4256}}
.card h3{{margin:0;font-size:14px;display:flex;justify-content:space-between;align-items:center}}
.count{{background:#232a36;border-radius:12px;padding:1px 10px;font-size:12px}}
.await{{color:#e0b05a;font-size:11px}}
.detail{{display:none;margin-top:10px;border-top:1px solid var(--line);padding-top:10px}}
.card.open .detail{{display:block}}
.lbl{{color:var(--dim);font-size:10px;text-transform:uppercase;letter-spacing:.06em;margin:8px 0 3px}}
.move{{background:#10141b;border:1px solid var(--line);border-radius:8px;padding:9px 11px;
font-size:13px}}
.reply{{background:#10141b;border-radius:8px;padding:7px 10px;margin:5px 0;font-size:12px}}
.reply b{{color:var(--ink)}}.reply span{{color:var(--dim)}}
.never{{color:#c25b5b;font-size:12px}}.fwd{{font-size:12px;margin:3px 0}}
.fwd b{{color:#8fb0ff}}.foot{{color:var(--dim);font-size:11px;margin-top:26px}}
.opener{{border-left-color:#4a5468}}
</style></head><body>
<h1>DM Conversation Flows — pattern map &amp; live coverage</h1>
<div class="sub">since {html.escape(live['since'])} · generated {html.escape(live['generated'])}
 · source of truth: <code>Resources/System/dm-flows/flows.json</code> (edit there, re-render with
 <code>python -m engine flows-chart</code>)</div>
<div class="kpis">
 <div class="kpi"><b>{cov_txt}</b><span>pattern coverage — at ~80% the basic flows go mechanical, zero LLM</span></div>
 <div class="kpi"><b>{live['total_replies']}</b><span>replies mapped</span></div>
 <div class="kpi"><b>{live['awaiting_us']}</b><span>awaiting our move</span></div>
</div>
<div class="cols">
 <div><div class="colh">1 · Openers (A/B arms)</div><div id="openers"></div></div>
 <div><div class="colh">2 · Reply branches — click to open (real replies + the read)</div><div id="branches"></div></div>
 <div><div class="colh">3 · Pre-arranged next move → where it leads</div><div id="moves"></div></div>
</div>
<div class="foot" id="foot"></div>
<script>
const D={payload};
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
const openers=document.getElementById('openers');
for(const o of D.flows.openers){{
  const n=D.opener_counts[o.id]||0;
  openers.insertAdjacentHTML('beforeend',
   `<div class="card opener"><h3>${{esc(o.label)}}<span class="count">${{n}} replies</span></h3>
    <div class="detail"><div class="lbl">opener text</div><div class="reply">${{esc(o.text)}}</div></div></div>`);
}}
const branches=document.getElementById('branches');
const moves=document.getElementById('moves');
const all=[...D.flows.branches,{{id:'unclassified',label:'Unclassified — the road to 80%',
 color:'#666',read:'Patterns did not match. Every entry here is either a new pattern to add to flows.json or a genuine one-off for a human.',next_move:'Human (or agent) sorts it; recurring shapes get promoted to a named branch.',never:[],forward:[]}}];
for(const b of all){{
  const rows=D.per_branch[b.id]||[];
  const awaiting=rows.filter(r=>r.awaiting_us).length;
  branches.insertAdjacentHTML('beforeend',
   `<div class="card" style="border-left-color:${{b.color}}"><h3>${{esc(b.label)}}
     <span class="count">${{rows.length}}${{awaiting?` · <span class=await>${{awaiting}} awaiting</span>`:''}}</span></h3>
    <div class="detail"><div class="lbl">the read</div><div>${{esc(b.read)}}</div>
    <div class="lbl">live replies</div>
    ${{rows.map(r=>`<div class="reply"><b>${{esc(r.name)}}</b> <span>[${{r.opener}}${{r.awaiting_us?' · awaiting us':' · answered'}}]</span><br>${{esc(r.reply)}}</div>`).join('')||'<div class="reply"><span>none yet</span></div>'}}
    </div></div>`);
  moves.insertAdjacentHTML('beforeend',
   `<div class="card" style="border-left-color:${{b.color}}"><h3>${{esc(b.id)}} → next move</h3>
    <div class="detail"><div class="lbl">semi-pre-arranged move</div><div class="move">${{esc(b.next_move)}}</div>
    ${{(b.never&&b.never.length)?`<div class="lbl">never</div><div class="never">${{b.never.map(esc).join(' · ')}}</div>`:''}}
    ${{(b.forward&&b.forward.length)?`<div class="lbl">then, depending on their answer</div>`+
      b.forward.map(f=>`<div class="fwd"><b>${{esc(f.on)}}</b> → ${{esc(f.then)}}</div>`).join(''):''}}
    </div></div>`);
}}
document.getElementById('foot').innerHTML=
 `Escalation (always): ${{D.flows.escalation.map(esc).join(' · ')}}<br>Give bank: ${{D.flows.give_bank.map(esc).join(' | ')}}`;
document.querySelectorAll('.card').forEach(c=>c.addEventListener('click',()=>c.classList.toggle('open')));
</script></body></html>"""


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = Config.load()
    since = "2026-07-12"
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    fp = getattr(cfg, "dm_flows_path", "") or str(DATA_DIR / "flows.json")
    flows = json.loads(Path(fp).read_text(encoding="utf-8"))
    live = collect(since, flows)
    out = getattr(cfg, "dm_flows_html", "") or str(Path(fp).parent / "index.html")
    Path(out).write_text(render(flows, live), encoding="utf-8")
    # THE 20% FLOW (ruled 2026-07-14): unmatched replies awaiting us go to a review
    # queue the Overseer dispatches dm-conversation + James against (T2 drafts).
    review = [r for r in live["rows"] if r["awaiting_us"] and not r["branch"]]
    rq = DATA_DIR / "metrics" / "dm-review-queue.json"
    rq.parent.mkdir(parents=True, exist_ok=True)
    rq.write_text(json.dumps({"generated": live["generated"], "entries": review},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[flows-chart] {live['total_replies']} replies mapped, "
          f"coverage {live['coverage_pct']}%, awaiting us: {live['awaiting_us']}")
    print(f"RESULT {json.dumps({'lane': 'flows-chart', 'ok': True, 'coverage_pct': live['coverage_pct'], 'replies': live['total_replies'], 'html': out})}")


if __name__ == "__main__":
    main()
