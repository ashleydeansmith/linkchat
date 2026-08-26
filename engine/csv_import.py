"""csv_import.py — import leads from a CSV into a campaign (WP8).

The distribution unlock: a friend (or the operator) starting empty can drop in a CSV
exported from Sales Navigator, LinkedIn, or a spreadsheet and have a campaign
populated without any scraping. No LinkedIn access — pure file read.

Required column: a profile URL — any of `profile_url`, `url`, `linkedin`,
`linkedin_url`, `public_url`, `sales_nav_url`.
Optional: `first_name`/`full_name`/`name`, `company`, `title`/`headline`,
`location`. Header matching is case/space/underscore-insensitive.

Leads land at status 'collected' in the named campaign (created if absent),
de-duplicated on the canonical profile URL via db.upsert_lead. Dry-run by default.
"""
from __future__ import annotations

import csv
import sys

from . import canon, db, emit_result

URL_KEYS = ("profile_url", "url", "linkedin", "linkedin_url", "linkedinurl",
            "public_url", "publicurl", "sales_nav_url", "salesnavurl", "profile")
NAME_KEYS = ("full_name", "fullname", "name")
FIRST_KEYS = ("first_name", "firstname", "first")
LAST_KEYS = ("last_name", "lastname", "last", "surname")
COMPANY_KEYS = ("company", "company_name", "companyname", "organisation", "organization")
# `headline` deliberately NOT in TITLE_KEYS — it has its own column and its own meaning.
# The self-written headline ("Founder | I help coaches scale") is the field the the automation folder
# qualification agents actually read; the bare title ("Data Science Intern") is not.
TITLE_KEYS = ("title", "job_title", "jobtitle", "position", "role")
HEADLINE_KEYS = ("headline", "occupation", "tagline")
LOC_KEYS = ("location", "city", "region", "country")
# The archive export's "Connected On" — the day this person became a connection. It used
# to be read and dropped, which left every imported connection carrying only the date of
# the import, so nothing could be sorted newest-first. Event invitations are meant to walk
# your connections newest first, so the date is now kept.
CONNECTED_KEYS = ("connected_on", "connectedon", "connected", "connection_date")


def _norm(k: str) -> str:
    return (k or "").strip().lower().replace(" ", "_").replace("-", "_")


def _pick(row: dict, keys) -> str:
    for k in keys:
        if k in row and (row[k] or "").strip():
            return row[k].strip()
    return ""


def _norm_date(raw: str | None) -> str | None:
    """LinkedIn writes the connection date as '19 Mar 2026' in the export and
    '2026-03-19' in our own scrape. Both become 2026-03-19 so they sort together."""
    raw = (raw or "").strip()
    if not raw:
        return None
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return _dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw[:32]


def _read(path: str) -> list[dict]:
    out = []
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            row = {_norm(k): v for k, v in raw.items() if k}
            url = _pick(row, URL_KEYS)
            if not url:
                continue
            # Canonicalise /in/ profiles through the ONE canon function (strips the
            # /en locale suffix the scraper emits, forces https+www). A Sales-Nav
            # /sales/lead/ URL canonicalises to None — keep it as-is rather than
            # dropping the row; those leads are a legitimate cohort, just not /in/.
            url = canon.canon_in(url) or url.split("?")[0].strip().rstrip("/")
            name = _pick(row, NAME_KEYS)
            if not name:
                fn, ln = _pick(row, FIRST_KEYS), _pick(row, LAST_KEYS)
                name = (fn + " " + ln).strip()
            out.append({"profile_url": url, "full_name": name or None,
                        "company": _pick(row, COMPANY_KEYS) or None,
                        "title": _pick(row, TITLE_KEYS) or None,
                        # NEVER "" — db.upsert_lead uses COALESCE(?,col), so an empty
                        # string OVERWRITES a real stored value with emptiness.
                        "headline": _pick(row, HEADLINE_KEYS) or None,
                        "location": _pick(row, LOC_KEYS) or None,
                        "connected_on": _norm_date(_pick(row, CONNECTED_KEYS))})
    return out


def run(path: str, campaign: str, dry_run: bool, source: str = "csv",
        is_connection: bool = False) -> None:
    """Import a CSV of leads into `campaign`.

    `is_connection=True` marks every imported row as an existing 1st-degree connection,
    which permanently bars it from the connect lane (you cannot invite someone you are
    already connected to). Pass it for ANY connection-roster import — the native LinkedIn
    archive export, or the connections scraper. Getting this wrong is what put 6,396
    existing connections into the connect queue on 2026-07-09.
    """
    try:
        rows = _read(path)
    except FileNotFoundError:
        print(f"[error] file not found: {path}")
        emit_result("csv-import", False, f"File not found: {path}")
        return
    except Exception as e:  # noqa: BLE001
        print(f"[error] could not read CSV: {e}")
        emit_result("csv-import", False, f"Could not read CSV: {e}")
        return

    if not rows:
        print("[warn] no rows with a recognisable profile URL — check the column headers.")
        emit_result("csv-import", False, "No rows with a profile URL column found")
        return

    print(f"CSV '{path}': {len(rows)} lead(s) with a profile URL  ->  campaign '{campaign}'")
    for r in rows[:8]:
        print(f"  {r['full_name'] or '(no name)':<26} {r['company'] or '':<22} {r['profile_url']}")
    if len(rows) > 8:
        print(f"  … and {len(rows) - 8} more")

    if dry_run:
        print("\n[dry-run] nothing imported. Re-run with --commit to add them.")
        emit_result("csv-import", True, f"Rehearsal — {len(rows)} lead(s) ready to import into '{campaign}'")
        return

    # A connection roster is not a connect pipeline: no connect step, and the rows land
    # 'connection' — NOT 'accepted'. 'accepted' means "we invited them and they said yes";
    # it feeds accept-rate and the safety posture. These people were already connected, so
    # counting them as accepts inflates the rate to ~100% and hides a dead connect lane.
    steps = ([{"type": "collect"}] if is_connection
             else [{"type": "collect"}, {"type": "connect", "max_per_run": 10}])
    res = db.create_composite_campaign(campaign, "csv", path, steps)
    cid = res["id"]
    added = 0
    # Rank runs newest first, matching the order LinkedIn shows your connections page in,
    # so the event lane can walk "most recent at the top, all the way down". Rows with no
    # date keep whatever rank they already had rather than being pushed to the front.
    dated = sorted([r for r in rows if r.get("connected_on")],
                   key=lambda r: r["connected_on"], reverse=True)
    rank_of = {r["profile_url"]: i + 1 for i, r in enumerate(dated)}
    with db.connect() as conn:
        for r in rows:
            db.upsert_lead(conn, profile_url=r["profile_url"], full_name=r["full_name"],
                           company=r["company"], title=r["title"], headline=r.get("headline"),
                           location=r["location"], source=source,
                           status="connection" if is_connection else "collected")
            # is_connection RATCHETS: a later non-connection import must never clear it.
            # You cannot stop being connected to someone.
            # An existing row keeps its status (upsert_lead never rewrites pipeline state),
            # so promote it here — but ONLY out of the two inert statuses. Never demote a
            # lead that is mid-flight ('invited', 'messaged', 'replied', …).
            flag = 1 if is_connection else 0
            conn.execute(
                "UPDATE leads SET campaign_id=?, "
                "is_connection=MAX(COALESCE(is_connection,0), ?), "
                "status=CASE WHEN ?=1 AND status IN ('new','collected') THEN 'connection' "
                "            ELSE status END "
                "WHERE profile_url=?",
                (cid, flag, flag, r["profile_url"]))
            if r.get("connected_on"):
                conn.execute("UPDATE leads SET connected_on=?, connected_rank=? WHERE profile_url=?",
                             (r["connected_on"], rank_of.get(r["profile_url"]), r["profile_url"]))
            added += 1
    print(f"\n[done] imported {added} lead(s) into '{campaign}'.")
    emit_result("csv-import", True, f"Imported {added} lead(s) into '{campaign}'",
                count=added, campaign=campaign)


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from .salesnav import _arg_str
    args = [a for a in sys.argv[2:] if not a.startswith("-")]
    path = args[0] if args else None
    campaign = _arg_str("--name") or "CSV Import"
    if not path:
        print("usage: import-csv <file.csv> --name <campaign> [--commit] [--as-connections]")
        print("       --as-connections  the CSV is your 1st-degree connection roster (the")
        print("                         LinkedIn archive export). Rows land 'accepted' and are")
        print("                         permanently barred from the connect lane.")
        return
    run(path, campaign, dry_run="--commit" not in sys.argv,
        is_connection="--as-connections" in sys.argv)


if __name__ == "__main__":
    main()
