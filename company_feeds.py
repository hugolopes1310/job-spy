"""Direct ATS feeds for target companies — zero-miss on strategic employers.

Many private banks / consultancies / fintechs use one of three ATS platforms
that expose public JSON endpoints. We poll them directly rather than relying
on LinkedIn indexing:

  - Greenhouse      : https://boards-api.greenhouse.io/v1/boards/<slug>/jobs
  - Lever           : https://api.lever.co/v0/postings/<slug>?mode=json
  - SmartRecruiters : https://api.smartrecruiters.com/v1/companies/<slug>/postings

Workday / SuccessFactors / Taleo require bespoke HTML scraping — not covered
here (fall back to python-jobspy / LinkedIn).

Returns normalized dicts:
    {title, company, location, job_url, description, date_posted, site}
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

USER_AGENT = "job-tracker/1.0 (+https://github.com/hugolopes1310/job-tracker)"


def _get_json(url: str, timeout: int = 20) -> Any | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[feeds] HTTP {e.code} on {url}")
    except Exception as e:  # noqa: BLE001
        print(f"[feeds] error on {url}: {e}")
    return None


def _clean(html: str) -> str:
    if not html:
        return ""
    # Strip HTML tags without pulling in BeautifulSoup
    return re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ").strip()


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------
def fetch_greenhouse(slug: str, display_name: str | None = None) -> list[dict]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not data:
        return []
    out: list[dict] = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name") or ""
        out.append({
            "title": j.get("title") or "",
            "company": display_name or slug,
            "location": location,
            "job_url": j.get("absolute_url") or "",
            "description": _clean(j.get("content") or "")[:4000],
            "date_posted": (j.get("updated_at") or "").split("T")[0],
            "site": f"greenhouse:{slug}",
        })
    return out


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------
def fetch_lever(slug: str, display_name: str | None = None) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not data:
        return []
    out: list[dict] = []
    for j in data:
        categories = j.get("categories") or {}
        loc = categories.get("location") or ""
        out.append({
            "title": j.get("text") or "",
            "company": display_name or slug,
            "location": loc,
            "job_url": j.get("hostedUrl") or j.get("applyUrl") or "",
            "description": _clean(j.get("descriptionPlain") or j.get("description") or "")[:4000],
            "date_posted": datetime.utcfromtimestamp(
                (j.get("createdAt") or 0) / 1000
            ).date().isoformat() if j.get("createdAt") else "",
            "site": f"lever:{slug}",
        })
    return out


# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------
def fetch_smartrecruiters(slug: str, display_name: str | None = None) -> list[dict]:
    data = _get_json(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
    )
    if not data:
        return []
    out: list[dict] = []
    for j in data.get("content", []):
        loc_obj = j.get("location") or {}
        loc = ", ".join(filter(None, [loc_obj.get("city"), loc_obj.get("country")]))
        posting_id = j.get("id")
        out.append({
            "title": j.get("name") or "",
            "company": display_name or slug,
            "location": loc,
            "job_url": f"https://jobs.smartrecruiters.com/{slug}/{posting_id}" if posting_id else "",
            "description": "",  # SmartRecruiters requires a 2nd call per posting
            "date_posted": (j.get("releasedDate") or j.get("createdOn") or "").split("T")[0],
            "site": f"sr:{slug}",
        })
    return out


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
}


def fetch_company(ats: str, slug: str, display_name: str | None = None) -> list[dict]:
    fn = ADAPTERS.get(ats)
    if not fn:
        print(f"[feeds] Unknown ATS: {ats}")
        return []
    return fn(slug, display_name)


def fetch_all(companies: list[dict], max_age_days: int | None = 14) -> list[tuple[str, list[dict]]]:
    """companies: [{name, ats, slug, axe, [display_name]}]. Returns [(axe, jobs), ...].

    Filters jobs older than max_age_days (based on date_posted when available).
    """
    cutoff = None
    if max_age_days:
        cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).date()

    out: list[tuple[str, list[dict]]] = []
    for c in companies:
        jobs = fetch_company(c["ats"], c["slug"], c.get("display_name"))
        if cutoff:
            def _recent(j):
                dp = j.get("date_posted")
                if not dp:
                    return True  # keep if unknown
                try:
                    return datetime.fromisoformat(dp).date() >= cutoff
                except ValueError:
                    return True
            jobs = [j for j in jobs if _recent(j)]
        out.append((c["axe"], jobs))
        print(f"[feeds] {c['ats']}:{c['slug']} → {len(jobs)} recent posting(s)")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python company_feeds.py <ats> <slug>")
        sys.exit(1)
    ats, slug = sys.argv[1], sys.argv[2]
    jobs = fetch_company(ats, slug)
    print(f"{ats}:{slug} → {len(jobs)} postings")
    for j in jobs[:10]:
        print(f"  - {j['title']} @ {j['company']} ({j['location']})")
