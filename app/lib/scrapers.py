"""Job scrapers — Phase 3.

Thin wrapper over `python-jobspy` for LinkedIn / Indeed / Google Jobs. Returns
normalized dicts compatible with the `public.jobs` table schema.

Design note: we intentionally keep this module dependency-light. Extra sources
(WTTJ, JobUp, company feeds) live in `extra_scrapers.py` and `company_feeds.py`
at the repo root — they can be plugged back in later if needed.

Output dict shape (per job):
    {
      "url": str,
      "title": str,
      "company": str,
      "location": str,
      "description": str,
      "date_posted": str,   # YYYY-MM-DD if parseable, else raw
      "site": str,          # "linkedin" | "indeed" | "google"
    }
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable


# The three source names jobspy accepts for our MVP.
JOBSPY_SITES = {"linkedin", "indeed", "google"}


def job_id_for(url: str) -> str:
    """SHA-1 of the canonical URL — matches the V1 scheme so repos stay compatible."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _normalize_date(raw: Any) -> str:
    """Best-effort ISO-ish date. jobspy returns strings, None, or NaT."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s.lower() in {"nan", "nat", "none"}:
        return ""
    # jobspy often gives YYYY-MM-DD HH:MM:SS — keep just the date
    return s.split("T")[0].split(" ")[0]


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none"} else s


def scrape_via_jobspy(
    search_term: str,
    location: str | None,
    sites: Iterable[str] = ("linkedin", "indeed", "google"),
    results_wanted: int = 25,
    hours_old: int = 24,
    distance: int | None = 50,
    country_indeed: str = "france",
) -> list[dict]:
    """Run one jobspy query and return normalized job dicts.

    Fail-soft: logs and returns [] on any error.
    """
    try:
        from jobspy import scrape_jobs  # type: ignore
    except ImportError:
        print("[scrapers] python-jobspy not installed — skipping")
        return []

    sites_list = [s for s in sites if s in JOBSPY_SITES]
    if not sites_list:
        print(f"[scrapers] no supported sites in {sites!r}")
        return []

    kwargs: dict[str, Any] = {
        "site_name": sites_list,
        "search_term": search_term,
        "results_wanted": results_wanted,
        "hours_old": hours_old,
        "linkedin_fetch_description": True,
    }
    if location:
        kwargs["location"] = location
    if distance is not None:
        kwargs["distance"] = distance
    if "indeed" in sites_list:
        kwargs["country_indeed"] = country_indeed

    try:
        df = scrape_jobs(**kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"[scrapers] jobspy error on {search_term!r} @ {location!r}: {e}")
        return []

    if df is None or df.empty:
        return []

    out: list[dict] = []
    for _, row in df.iterrows():
        url = _clean_str(row.get("job_url") or row.get("url"))
        if not url:
            continue
        title = _clean_str(row.get("title"))
        if not title:
            continue
        out.append({
            "url": url,
            "title": title,
            "company": _clean_str(row.get("company")),
            "location": _clean_str(row.get("location")),
            "description": _clean_str(row.get("description")),
            "date_posted": _normalize_date(row.get("date_posted")),
            "site": _clean_str(row.get("site")) or sites_list[0],
        })
    return out


if __name__ == "__main__":
    # Smoke test:
    #   python -m app.lib.scrapers "structurer" "Geneva"
    import json
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "structured products"
    loc = sys.argv[2] if len(sys.argv) > 2 else "Geneva, Switzerland"
    results = scrape_via_jobspy(q, loc, results_wanted=5, hours_old=48)
    print(f"Got {len(results)} jobs for {q!r} @ {loc!r}")
    for r in results[:3]:
        print(json.dumps({k: v for k, v in r.items() if k != "description"}, ensure_ascii=False, indent=2))
