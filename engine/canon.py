"""One canonical LinkedIn-URL function, used by every ingest path.

Before this module there were FOUR divergent implementations with different edge-case
coverage — `salesnav._canon_in`, `vault_import.canon_url`, `csv_import`'s inline
`url.split("?")[0].rstrip("/")`, and a hand-copied variant in a throwaway script — and
the busiest ingest path (csv_import, hence `connections --commit`) used the weakest one.

Two bugs that cost real data:

  * The connections scraper emits locale-suffixed hrefs (`/in/<slug>/en`). None of the
    old canonicalisers stripped the suffix, so committing a scrape at scale would have
    inserted a duplicate row for every person already present under `/in/<slug>`.
    Observed live in `connections_20260709-233028.csv` (18/18 rows carried `/en`).
  * `_canon_in` left a scheme-less `www.linkedin.com/in/foo` untouched — no `https://`.

Deliberately does NOT lowercase the vanity slug. Audited 2026-07-10 across all 9,689
live leads: zero rows where `profile_url != canon_in(profile_url)`, zero same-slug
duplicate pairs, and 42 slugs containing uppercase. Lowercasing would re-key those 42
for no benefit. If LinkedIn ever proves slugs are case-insensitive in a way that splits
records, revisit — with evidence, not on principle.

The slug itself is a user-editable vanity handle (changeable ~5x per 6 months; the old
URL 404s after ~180 days), so it is a *stable-enough* key, not a permanent identity.
An alias table is deliberately NOT built here: 0 URL-variance pairs have been observed.
Build it when variance is observed.
"""

from __future__ import annotations

import re

__all__ = ["canon_in"]

# Trailing locale segment LinkedIn appends to profile links, e.g. /in/jane-doe/en
_LOCALE = re.compile(
    r"/(?:en|de|fr|es|it|pt|nl|sv|da|no|fi|pl|cs|ro|tr|ru|uk|ar|he|hi|id|ms|th|vi"
    r"|ja|ko|zh|zh-cn|zh-tw|en-us|en-gb|pt-br|es-es)$",
    re.I,
)


def canon_in(url: str | None) -> str | None:
    """Canonical LinkedIn `/in/` identity, or None if this is not a personal profile.

    Returns None for `/sales/lead/...`, company pages, and anything without `/in/`.
    Callers MUST handle None rather than silently skipping — 3,289 of the 9,689 live
    leads are Sales-Navigator `/sales/lead/` URLs and would otherwise vanish.

    >>> canon_in("https://www.linkedin.com/in/jane-doe/en")
    'https://www.linkedin.com/in/jane-doe'
    >>> canon_in("http://linkedin.com/in/jane-doe/?trk=x")
    'https://www.linkedin.com/in/jane-doe'
    >>> canon_in("www.linkedin.com/in/jane-doe")
    'https://www.linkedin.com/in/jane-doe'
    >>> canon_in("https://www.linkedin.com/sales/lead/ACwAAB") is None
    True
    """
    if not url:
        return None
    u = url.strip().split("?")[0].split("#")[0].rstrip("/")
    if "/in/" not in u:
        return None

    u = re.sub(r"^http://", "https://", u, flags=re.I)
    if u.startswith("//"):
        u = "https:" + u
    elif not u.lower().startswith("https://"):
        # bare host (`www.linkedin.com/in/x`) or bare path (`/in/x`)
        u = "https://www.linkedin.com" + u if u.startswith("/") else "https://" + u
    u = re.sub(r"^https://(?:[a-z]{2}\.)?linkedin\.com", "https://www.linkedin.com", u, flags=re.I)

    # /in/<slug>/en  ->  /in/<slug>   (only ONE trailing locale segment, never the slug)
    head, _, tail = u.partition("/in/")
    if tail:
        stripped = _LOCALE.sub("", tail.rstrip("/"))
        u = f"{head}/in/{stripped.rstrip('/')}"

    return u.rstrip("/")
