"""capability.py — what THIS account can source, detected at runtime.

The "knows what it can do WITH Sales Nav, and performs anyway WITHOUT it" layer.
Most the parent program users (especially bootstrapped founders) have NO Sales Navigator
seat — so the no-SN sources are the PRIMARY path and SN is a power-up that
**auto-activates the moment a seat is detected** (no toggle, no "unlock"). Users
without a seat simply see the SN-only sources marked unavailable.

Single source of truth, consumed everywhere (the "Collect leads" picker, the
collect dispatcher, the InMail lane):

    capability.detect(force=False) -> Capability   # probes if cache stale (opens browser, READ-ONLY)
    capability.get() -> Capability                  # cached read, NO browser
    cap.sources()        -> ordered, best-available-first Source list per tier
    cap.can_inmail()     -> does the tier support InMail at all
    cap.search_available -> basic search usable (not CUL-throttled)

Detection is READ-ONLY recon (the same governed read-context the salesnav probe
uses) — it sends nothing and is safe to run regardless of the engine's arm state.
Tier resolves to "salesnav" | "basic". Premium-Business (which removes the
commercial-use search limit but is NOT Sales Nav) is recorded via a manual
override and/or runtime CUL detection — auto-detecting it from the DOM is flagged
needs-validation (research H §7), so v1 does not guess it.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from . import DATA_DIR, emit_result
from . import safe_close as _safe_close

CAP_PATH = DATA_DIR / "capability.json"
CACHE_TTL_HOURS = 24
SALES_HOME = "https://www.linkedin.com/sales/home"

# Tiers, lowest -> highest sourcing capability.
TIER_BASIC = "basic"          # Free or Premium Career: basic search grammar, CUL-throttled
TIER_PREMIUM_BIZ = "premium_business"  # CUL removed, but still basic search grammar (no SN lists/filters)
TIER_SALESNAV = "salesnav"    # saved lists/searches, 30+ filters, 2500 results, 50 InMails


# Per-tier SAFE thresholds — the "warn above this" lines shown in the limits editor.
# NOT hard caps: the user can exceed them (a warning shows). Grounded in LinkedIn's real
# per-plan limits — basic/Free is CUL-throttled on search+profile-views and has NO InMail
# credits; Premium clears the CUL and has a few InMail/mo; Sales Navigator clears the CUL
# and has ~50 InMail/mo. Connections are a LinkedIn-core weekly limit (~100-200/wk), roughly
# tier-agnostic and gated more by account warmth than by plan. `weekly: None` = no weekly line.
SAFE_CEILINGS = {
    TIER_BASIC: {   # Free / Premium Career — the tightest tier
        "connect":       {"daily": 20, "weekly": 100},
        "withdraw":      {"daily": 25, "weekly": None},
        "message":       {"daily": 40, "weekly": None},
        "profile_view":  {"daily": 25, "weekly": None},   # CUL hard-throttle — keep low
        "inmail":        {"daily": 0,  "weekly": 0},      # no InMail credits on Free
        "event_invite":  {"daily": 50, "weekly": 350},
        "inmail_monthly": 0,
    },
    TIER_PREMIUM_BIZ: {   # Premium Business — between Free and Sales Nav, much higher than Free
        "connect":       {"daily": 30, "weekly": 150},
        "withdraw":      {"daily": 35, "weekly": None},
        "message":       {"daily": 80, "weekly": None},
        "profile_view":  {"daily": 80, "weekly": None},   # CUL removed
        "inmail":        {"daily": 5,  "weekly": 15},     # ~15 InMail/mo
        "event_invite":  {"daily": 50, "weekly": 350},
        "inmail_monthly": 15,
    },
    TIER_SALESNAV: {   # Sales Navigator — calibrated to the operator's real usage (2026-06-22)
        "connect":       {"daily": 40, "weekly": 200},    # ~40/day (LinkedIn's core invite limit, not raised by SN)
        "withdraw":      {"daily": 40, "weekly": None},   # 20-40/day
        "message":       {"daily": 100, "weekly": None},  # plan = unlimited; this is the behavioural-safety line
        "profile_view":  {"daily": 120, "weekly": None},  # no CUL
        "inmail":        {"daily": 10, "weekly": 30},     # ~10 new/day, 50/mo
        "event_invite":  {"daily": 50, "weekly": 350},
        "inmail_monthly": 50,
    },
}


def safe_ceilings(tier: str = TIER_BASIC) -> dict:
    """The per-action safe daily/weekly thresholds for a plan tier (warn-above lines)."""
    return SAFE_CEILINGS.get(tier, SAFE_CEILINGS[TIER_BASIC])


# Read-only detection probe: are we inside the Sales Navigator app (a seat), or
# bounced to an upsell/marketing page (no seat)? Mirrors salesnav.LISTS_JS signals
# plus the nav-shell test, evaluated on /sales/home.
DETECT_JS = r"""() => {
  const body = (document.body.innerText || '');
  const head = (document.querySelector('header, nav') || {}).innerText || '';
  const shell = /home/i.test(head) && /(accounts|leads|messaging)/i.test(head);
  return {
    url: location.href,
    // a real seat lands you in the app at /sales/ and is NOT redirected to a checkout/upsell path
    inApp: /\/sales\//.test(location.href)
           && !/upsell|checkout|gtm|product\/sales-navigator|\/sales\/?$/i.test(location.href),
    navShell: shell,
    // negative signal: marketing/upsell copy means no active seat (same regex family as salesnav.LISTS_JS)
    upsell: /try sales navigator|free trial|reactivate|start your free trial|buy now|choose your plan/i
            .test(body.slice(0, 800)),
    listLinks: document.querySelectorAll('a[href*="/sales/lists/people/"]').length,
  };
}"""


# ---------------------------------------------------------------------------
# Source catalogue — the no-SN-FIRST ordering. `built` marks which modules are
# wired today vs planned, so the UI/dispatcher can show "coming soon" honestly
# rather than dead-ending. (research H §9 ranked order.)
# ---------------------------------------------------------------------------

@dataclass
class Source:
    key: str
    label: str
    blurb: str
    cli: str            # the `python -m engine <cli>` command that runs it
    built: bool         # is the module wired today?
    sn_only: bool = False   # requires a Sales Nav seat
    search_based: bool = False  # consumes the commercial-use search limit on basic tiers
    # filled by sources():
    available: bool = True
    reason: str = ""


# Canonical catalogue (order = no-SN-first preference for a near-empty Basic account).
_CATALOGUE: list[Source] = [
    Source("csv_connections", "Import your connections (1-click export)",
           "Request your LinkedIn connections file and we import it. Zero risk — a native export, never scraped.",
           cli="import-csv", built=True),
    Source("connections_scrape", "Scrape my connections (live, from My Network)",
           "Reads your 1st-degree connections straight off the My Network page and imports them. "
           "Higher risk than the 1-click export (it's live in-session automation) — paced and phased to stay safe.",
           cli="scrape-connections", built=True),   # not sn_only, not search_based -> available on every tier
    Source("csv_external", "Import any CSV / list of profiles",
           "Drop in any list with a profile-URL column (conference, CRM, community). Sidesteps LinkedIn entirely.",
           cli="import-csv", built=True),
    Source("post_engagers", "People who engaged with a post",
           "Likers & commenters on your or a target's post — warm, in-context strangers. Your net-new growth engine.",
           cli="engagers", built=True),
    Source("event_attendees", "Attendees of an event you've joined",
           "A clean on-topic pool. You join the event; we read who else is attending.",
           cli="events", built=False),
    Source("alumni", "Alumni from your school",
           "A warm seam — filter by where they work, what they do, where they live.",
           cli="alumni", built=False),
    Source("viewers", "People who viewed your profile",
           "A passive bonus. Basic shows the last 5 (90 days); Premium/Sales Nav show far more.",
           cli="viewers", built=True),
    Source("basic_search", "Regular LinkedIn search",
           "A top-up source. Free/Career throttle after ~300 searches/month (we warn first); "
           "Premium Business removes that limit; Sales Navigator adds filters & depth.",
           cli="search", built=True, search_based=True),
    Source("salesnav_list", "Saved Sales Navigator lists & searches",
           "Switches on automatically if you have Sales Navigator — saved-list sourcing, 30+ filters, "
           "2,500-result searches, 50 InMails/mo.",
           cli="salesnav", built=True, sn_only=True),
]


@dataclass
class Capability:
    tier: str = TIER_BASIC
    has_salesnav: bool = False
    premium: bool | None = None        # Premium Career/Business (non-SN); None = unknown
    cul_blocked_until: str | None = None   # ISO date the basic-search throttle resets, or None
    override_manual: bool = False      # user asserted "I have Sales Navigator" (trusted over the probe)
    sim_tier: str | None = None        # EXPERIMENT pin: force a tier (basic/premium_business/salesnav),
                                       # honoured over BOTH the probe and the SN override. None = off.
    checked_at: str | None = None
    detected: bool = False             # has a live probe ever run?

    # ---- derived helpers -------------------------------------------------
    @property
    def search_available(self) -> bool:
        """Basic search usable right now? Premium Business / Sales Nav have no commercial-use
        limit; Free/Career do — and once throttled, search is unavailable until the reset date."""
        if self.tier in (TIER_PREMIUM_BIZ, TIER_SALESNAV):
            return True
        if not self.cul_blocked_until:
            return True
        try:
            return datetime.now(timezone.utc) >= datetime.fromisoformat(self.cul_blocked_until)
        except Exception:  # noqa: BLE001
            return True

    def can_inmail(self) -> bool:
        """Does the tier support InMail at all? (Sales Nav = 50/mo; Premium = 5–15/mo.)
        The per-lead credit guard lives in inmail.py; Open-Profile free messages work on
        ANY tier and are handled per-lead, not here."""
        return self.has_salesnav or bool(self.premium)

    def sources(self) -> list[Source]:
        """The catalogue with per-tier availability + an honest reason filled in,
        in no-SN-first preference order. SN sources sort to the TOP when a seat is
        present (auto-on), and to the bottom marked unavailable when it isn't."""
        out: list[Source] = []
        for s in _CATALOGUE:
            src = Source(**{k: getattr(s, k) for k in
                            ("key", "label", "blurb", "cli", "built", "sn_only", "search_based")})
            if src.sn_only and not self.has_salesnav:
                src.available = False
                src.reason = "Switches on automatically if you have Sales Navigator."
            elif src.search_based and not self.search_available:
                src.available = False
                src.reason = (f"Search limit reached — resets {self.cul_blocked_until}. "
                              "Use your connections, post-engagers or a CSV meanwhile.")
            elif not src.built:
                src.available = False
                src.reason = "Coming soon."
            else:
                src.available = True
                src.reason = ""
            out.append(src)
        # SN sources first when we actually have a seat (the auto-on power-up);
        # otherwise keep the no-SN-first catalogue order.
        if self.has_salesnav:
            out.sort(key=lambda s: (not (s.sn_only and s.available), not s.available))
        else:
            out.sort(key=lambda s: not s.available)   # available first, planned/locked after
        return out

    def to_dict(self) -> dict:
        return {"tier": self.tier, "has_salesnav": self.has_salesnav, "premium": self.premium,
                "cul_blocked_until": self.cul_blocked_until, "override_manual": self.override_manual,
                "sim_tier": self.sim_tier,
                "checked_at": self.checked_at, "detected": self.detected}


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------

def _load() -> Capability:
    if not CAP_PATH.exists():
        return Capability()
    try:
        d = json.loads(CAP_PATH.read_text(encoding="utf-8"))
        return Capability(**{k: d.get(k) for k in
                             ("tier", "has_salesnav", "premium", "cul_blocked_until",
                              "override_manual", "sim_tier", "checked_at", "detected")})
    except Exception:  # noqa: BLE001
        return Capability()


def _save(cap: Capability) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CAP_PATH.write_text(json.dumps(cap.to_dict(), indent=2), encoding="utf-8")


def _stale(cap: Capability) -> bool:
    if not cap.checked_at:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cap.checked_at)
        return age > timedelta(hours=CACHE_TTL_HOURS)
    except Exception:  # noqa: BLE001
        return True


def _as_tier(cap: Capability, tier: str) -> Capability:
    """Overlay a tier's derived flags onto a Capability (used by the experiment pin).
    Sets has_salesnav / premium so can_inmail() and search_available behave like that
    real tier — e.g. simulated Basic has no InMail and CAN hit the commercial-use limit."""
    cap.tier = tier
    cap.has_salesnav = (tier == TIER_SALESNAV)
    if tier == TIER_PREMIUM_BIZ:
        cap.premium = True       # CUL removed, but no SN lists/filters
    elif tier == TIER_BASIC:
        cap.premium = False      # Free/Career — CUL bites, no InMail credits
    return cap


def get() -> Capability:
    """Cached capability — never opens a browser. Returns the last probe (or a
    Basic default if none). Precedence: experiment sim_tier > manual SN override > probe."""
    cap = _load()
    if cap.sim_tier:
        return _as_tier(cap, cap.sim_tier)
    if cap.override_manual:
        cap.has_salesnav = True
        cap.tier = TIER_SALESNAV
    return cap


def needs_detect() -> bool:
    """Should a live probe run? True when the account was NEVER probed or the cache is
    past its TTL — and neither an experiment pin nor a manual override is in force
    (both are user assertions the probe must not fight). The server uses this to
    AUTO-detect, so a fresh install never silently sits on the Basic default."""
    cap = _load()
    if cap.sim_tier or cap.override_manual:
        return False
    return (not cap.detected) or _stale(cap)


def note_seat_observed(has_sn: bool) -> Capability:
    """Self-healing write-back from a lane that just OBSERVED the seat live (e.g. the
    salesnav lane landing in the app vs being bounced to the upsell page). A lane run IS
    a detection — fresher than any cache. No-op while a simulation pin or manual override
    is in force. Preserves a known `premium` flag when the seat is absent."""
    cap = _load()
    if cap.sim_tier or cap.override_manual:
        return cap
    cap.has_salesnav = has_sn
    cap.tier = TIER_SALESNAV if has_sn else (TIER_PREMIUM_BIZ if cap.premium else TIER_BASIC)
    cap.detected = True
    cap.checked_at = datetime.now(timezone.utc).isoformat()
    _save(cap)
    return cap


def set_simulation(tier: str | None) -> Capability:
    """EXPERIMENT pin: force the app to behave as `tier` (basic / premium_business /
    salesnav), honoured over the live probe so a stray --detect can't undo it. Pass None
    (or an unknown value) to turn the simulation OFF and revert to the real detection.
    Starting a fresh basic/premium sim also clears any stale CUL block so the experiment
    begins with search available, like a brand-new account that hasn't searched yet."""
    cap = _load()
    cap.sim_tier = tier if tier in (TIER_BASIC, TIER_PREMIUM_BIZ, TIER_SALESNAV) else None
    if cap.sim_tier:
        cap.cul_blocked_until = None
    _save(cap)
    return get()


def set_override(has_sn: bool) -> Capability:
    """User asserts whether they have Sales Navigator — trusted over the probe (for
    the rare case auto-detection misfires). Persists immediately."""
    cap = _load()
    cap.override_manual = has_sn
    if has_sn:
        cap.has_salesnav = True
        cap.tier = TIER_SALESNAV
    else:
        # clearing the assertion -> revert to a clean unknown; re-run --detect to refresh
        cap.has_salesnav = False
        cap.tier = TIER_BASIC
        cap.detected = False
    _save(cap)
    return cap


def note_search_throttled(reset_iso: str) -> Capability:
    """Record that basic search hit the commercial-use limit (called by the search
    lane when it detects the throttle). `reset_iso` = first-of-next-month."""
    cap = _load()
    cap.cul_blocked_until = reset_iso
    _save(cap)
    return cap


# ---------------------------------------------------------------------------
# Live detection (READ-ONLY recon — opens the governed read context, sends nothing)
# ---------------------------------------------------------------------------

def detect(force: bool = False) -> Capability:
    """Probe whether this account has a Sales Navigator seat and cache the result.
    Read-only navigation to /sales/home via the shared governed read context — the
    same safe pattern as salesnav.probe(). Returns the (possibly cached) Capability."""
    cap = _load()
    if cap.sim_tier:
        # an experiment pin is active — never probe over it (even on --detect/force)
        print(f"[capability] simulation pinned to '{cap.sim_tier}' - skipping live probe.",
              file=sys.stderr)
        return _as_tier(cap, cap.sim_tier)
    if cap.override_manual:
        # user has spoken; don't fight the probe against an explicit assertion
        cap.has_salesnav = True
        cap.tier = TIER_SALESNAV
        return cap
    if not force and not _stale(cap):
        return cap

    from . import ops
    import linkedin_browser as lb
    from playwright.sync_api import sync_playwright
    from . import nav

    with ops.lock(lb.READ_LOCK, agent="engine-capability", wait_sec=120) as got:
        if not got:
            print("chromium session busy — using cached capability", file=sys.stderr)
            return cap
        with sync_playwright() as pw:
            ctx = lb.open_read_context(pw, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page = nav.human_open(page, SALES_HOME)   # human path, not a cold deep-link
                time.sleep(4)
                r = page.evaluate(DETECT_JS)
                has_sn = bool((r.get("inApp") and r.get("navShell")) or r.get("listLinks", 0) > 0) \
                    and not (r.get("upsell") and not r.get("listLinks"))
                cap.has_salesnav = has_sn
                cap.tier = TIER_SALESNAV if has_sn else TIER_BASIC
                cap.detected = True
                cap.checked_at = datetime.now(timezone.utc).isoformat()
                _save(cap)
                ops.log_action("engine-capability", "scrape",
                               target="sales-home", result="ok")
            except Exception as e:  # noqa: BLE001
                print(f"[capability] probe failed, keeping cache: {str(e)[:140]}", file=sys.stderr)
            finally:
                _safe_close(ctx)
    return cap


def _print(cap: Capability) -> None:
    label = {TIER_SALESNAV: "Sales Navigator", TIER_PREMIUM_BIZ: "Premium Business",
             TIER_BASIC: "Free / Basic"}.get(cap.tier, cap.tier)
    print(f"plan        : {label}"
          + (f"   [SIMULATED — real plan untouched]" if cap.sim_tier else ""))
    print(f"sales nav   : {'yes (sourcing auto-on)' if cap.has_salesnav else 'no'}"
          f"{'  [manual override]' if cap.override_manual else ''}")
    print(f"inmail      : {'available' if cap.can_inmail() else 'not on this plan (connect-then-message instead)'}")
    print(f"search      : {'available' if cap.search_available else 'throttled until ' + str(cap.cul_blocked_until)}")
    print(f"checked     : {cap.checked_at or '(never probed — showing default)'}")
    print("\nlead sources (best available first):")
    for s in cap.sources():
        mark = "ON " if s.available else "—  "
        print(f"  [{mark}] {s.label}" + (f"   ({s.reason})" if s.reason else ""))


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--simulate" in sys.argv:
        i = sys.argv.index("--simulate")
        arg = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        tier = {"basic": TIER_BASIC, "free": TIER_BASIC,
                "premium": TIER_PREMIUM_BIZ, "premium_business": TIER_PREMIUM_BIZ,
                "salesnav": TIER_SALESNAV, "sn": TIER_SALESNAV}.get(arg.lower())
        if arg.lower() in ("off", "none", "stop", "real", ""):
            cap = set_simulation(None)
            print("[ok] simulation OFF — reverted to real detection "
                  "(run --detect to refresh the live probe).")
        elif tier:
            cap = set_simulation(tier)
            print(f"[ok] simulating '{tier}' — the app now behaves as this plan. "
                  f"Your real Sales Navigator seat is untouched.")
        else:
            print("usage: capability --simulate <basic|premium_business|salesnav|off>")
            return
        _print(cap)
        emit_result("capability", True,
                    (f"Simulating {cap.sim_tier}" if cap.sim_tier else "Simulation off"),
                    has_salesnav=cap.has_salesnav, tier=cap.tier, simulated=bool(cap.sim_tier))
    elif "--detect" in sys.argv:
        cap = detect(force=True)
        _print(cap)
        emit_result("capability", True,
                    f"Detected plan: {'Sales Navigator' if cap.has_salesnav else 'Free/Basic'}",
                    has_salesnav=cap.has_salesnav, tier=cap.tier)
    elif "--have-salesnav" in sys.argv:
        cap = set_override(True)
        print("[ok] marked: this account HAS Sales Navigator (sourcing auto-on).")
        _print(cap)
    elif "--no-salesnav" in sys.argv:
        cap = set_override(False)
        print("[ok] cleared the Sales Navigator override.")
        _print(cap)
    else:
        _print(get())
        print("\n(use --detect to probe live, --have-salesnav / --no-salesnav to override,")
        print(" --simulate <basic|premium_business|salesnav|off> to run an experiment)")


if __name__ == "__main__":
    main()
