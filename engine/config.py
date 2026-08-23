"""config.py — LinkForge configuration with conservative, LinkedIn-safe defaults.

Defaults are deliberately CAUTIOUS (safety-first). Override any field by writing
linkforge/config.json (only the keys you want to change). Two master switches:

    enabled : False  -> the engine performs NO actions at all (hard off)
    dry_run : True   -> the engine plans + logs what it WOULD do, but never
                        types/clicks/sends on LinkedIn

Both must be flipped before LinkForge can act. Caps below are per the SHARED
linkedin_ops budget (LinkForge logs through it), plus LinkForge's own daily/weekly
ceilings enforced in safety.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

from . import CONFIG_PATH


@dataclass
class Config:
    # --- master switches (both OFF by default) -----------------------------
    enabled: bool = False        # hard on/off for all LinkedIn activity
    dry_run: bool = True         # plan + log only; never act on LinkedIn

    # --- LinkedIn plan tier (drives the safe-limit guidance in the editor) --
    # "basic" (Free/Premium-Career, CUL-throttled, no InMail) | "premium_business" | "salesnav".
    # The user declares this (or "Detect my plan" sets it); it differentiates what's safe.
    plan: str = "basic"

    # --- working window (human realism + safety) ---------------------------
    timezone: str = "Europe/London"
    working_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon-Fri (0=Mon)
    work_start: str = "08:30"    # HH:MM local
    work_end: str = "18:00"

    # --- pacing (seconds between actions; randomised) ----------------------
    min_gap_sec: int = 45
    max_gap_sec: int = 180
    long_pause_every: int = 7         # after N actions, take a longer breather
    long_pause_min_sec: int = 300
    long_pause_max_sec: int = 900

    # --- LinkForge daily caps (per action, local day) ----------------------
    # action names: connect, withdraw, message, profile_view, inmail
    # Defaults sit AT OR UNDER the tightest plan's (Basic/Free) safe ceilings, so a fresh
    # install never boots into its own red zone (LF-006). Basic has NO InMail credits, so
    # InMail defaults to 0 (the user opts up on a plan that has credits); profile-views are
    # CUL-throttled on Basic, so the default matches the 25/day safe line. All raise-only.
    daily_caps: dict[str, int] = field(default_factory=lambda: {
        "connect": 15,
        "withdraw": 20,
        "message": 15,
        "profile_view": 25,
        "inmail": 0,
        "event_invite": 50,    # event invites are a SEPARATE ~1000/wk LinkedIn budget; stay well under
    })
    # --- LinkForge trailing-7-day caps (LinkedIn watches weekly invite volume)
    weekly_caps: dict[str, int] = field(default_factory=lambda: {
        "connect": 80,
        "message": 90,
        "inmail": 35,
        "event_invite": 350,   # hard-stop FAR below LinkedIn's ~1000/week event-invite ceiling
    })

    # --- InMail credit guard — 0 by default (Basic/Free has no InMail credits, LF-006);
    #     raise it to your plan's real monthly allowance (Premium ~15, Sales Nav ~50). ---
    inmail_monthly_cap: int = 0

    # --- invite hygiene ----------------------------------------------------
    withdraw_after_days: int = 21     # withdraw pending invites older than this
    pending_invite_ceiling: int = 180 # keep total outstanding invites below this

    # --- targeting (filled per-campaign; these are fallback defaults) -------
    sales_nav_list_urls: list[str] = field(default_factory=list)
    search_urls: list[str] = field(default_factory=list)
    max_leads_per_harvest: int = 50

    # --- V1 vault import (optional, per-user data source; V2 replaces with harvest) --
    # MUST default empty: these are an individual user's own vault paths. Hardcoding Ashley's
    # personal Windows paths here shipped them into every tester's config.json (incl. Mac, where
    # they don't even resolve). Each user points these at their own vault, or leaves them empty.
    vault_people_dir: str = ""
    vault_prospect_dirs: list[str] = field(default_factory=list)

    # --- connect lane -----------------------------------------------------
    connect_note_template: str = ""   # optional templated note; empty = send WITHOUT a note

    # --- connect-lane rebuild (2026-07-13) ----------------------------------
    # The sn-invite sweep walks the campaign's LIVE search; the DB is the ledger of who
    # we've touched, never a gatekeeper for the search (rebuild plan V3 §2).
    connect_max_sweep_pages: int = 25    # per-search page-walk depth ceiling
    connect_page_load_budget: int = 30   # page loads per RUN across all campaigns (throttle guard)
    connect_retry_transient: bool = True # scheduler may re-run connect ONCE (>=2h later) after a
                                         # transient shortfall (pagination/render failure only)
    invite_unknown_rows: bool = True     # a search row not in the DB is a FRESH prospect of this
                                         # campaign: insert it (collect's parser) and invite it.
                                         # false = old behaviour (queue-gated, drift-starved)

    # --- watchdog (SHOULD-vs-DID monitor, 2026-07-14) ------------------------
    # Personal-install only: absolute path of a markdown inbox that gets one line per
    # discrepancy batch (Ashley: AI/Signals/Recommendations-Inbox.md -> Jeeves 07:30
    # brief). Empty (the default) = queue file only, no vault dependency in core.
    watchdog_inbox_path: str = ""

    # Funnel-metrics report dir (2026-07-14): where metrics-funnel drops its daily
    # markdown for the sales layer. Empty = data/metrics only (no vault dependency).
    metrics_report_dir: str = ""

    # DM conversation-flow chart (2026-07-14): the Ashley-owned flow model + the
    # rendered interactive chart. Empty = data dir defaults (no vault dependency).
    dm_flows_path: str = ""
    dm_flows_html: str = ""

    # --- ConversationForge (F1 data spine, 2026-07-15) -----------------------
    # Master flag for the flows feature: gates /api/flows/* and the F2 editor tab.
    # OFF by default per the phase discipline — flag flips only at a phase gate.
    flows_enabled: bool = False
    # Stamps carry an account id from day one (§6b-19): one cheap column now vs a
    # migration nightmare when the distributable meets a second LinkedIn identity.
    flows_account_id: str = "default"
    # Booked-call FILE CONTRACT (plan §5.4): meeting-triage (the fleet agent that
    # already reads Fathom) appends {name, at, source, ref?} lines to this JSONL;
    # LinkForge only READS it — it must never touch Fathom itself (single-browser
    # doctrine + domain isolation). Empty = data/meetings-feed.jsonl.
    meetings_feed_path: str = ""

    # --- ICP scorer (the no-SN qualification layer; see score.py) ----------
    # Per-user ICP definition. Scored against each lead's headline/title/company with
    # word-boundary matching. positive = role/intent words you WANT; industry = soft
    # bonus; negative = the exclusions regular search can't filter (recruiters, agencies,
    # job-seekers). Tune these to YOUR target — defaults target B2B founders/owners.
    icp: dict = field(default_factory=lambda: {
        "positive": ["founder", "co-founder", "cofounder", "ceo", "owner",
                     "managing director", "director", "entrepreneur", "building",
                     "i build", "we build", "launching", "bootstrapping", "indie hacker"],
        "industry": ["saas", "software", "b2b", "platform", "app", "tech", "ai",
                     "fintech", "startup", "product", "automation"],
        "negative": ["recruiter", "recruitment", "talent", "headhunter",
                     "executive search", "hiring", "we help", "helping", "bdr", "sdr",
                     "coach", "mentor", "agency", "freelance", "freelancer",
                     "open to work", "looking for work", "consultant",
                     "virtual assistant", "lead gen", "lead generation"],
        "thresholds": {"prime": 75, "strong": 55, "fit": 40},
    })

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                overrides = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                raise SystemExit(f"config.json is not valid JSON: {e}")
            for k, v in overrides.items():
                if k.startswith("_"):
                    continue  # allow _comment keys
                if hasattr(cfg, k):
                    cur = getattr(cfg, k)
                    # MERGE dict fields (e.g. daily_caps/weekly_caps) so NEW safe-default
                    # keys survive even when config.json persists an older dict; on-disk
                    # values still win per-key. Non-dict fields are replaced as before.
                    if isinstance(cur, dict) and isinstance(v, dict):
                        merged = dict(cur)
                        merged.update(v)
                        setattr(cfg, k, merged)
                    else:
                        setattr(cfg, k, v)
                # unknown keys are ignored (forward-compatible)
        return cfg

    def write_template(self) -> None:
        """Write the current defaults to config.json as an editable starting point."""
        data = asdict(self)
        data["_note"] = ("LinkForge config. Flip enabled+dry_run to arm. "
                         "Caps are safety-first; raise slowly only after clean runs.")
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get() -> Config:
    return Config.load()
