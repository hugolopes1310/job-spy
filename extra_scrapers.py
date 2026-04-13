"""Additional job scrapers beyond python-jobspy.

Each scraper returns a list of normalized dicts compatible with the tracker
pipeline:
    {title, company, location, job_url, description, date_posted, site}

Currently supported:
  - Welcome to the Jungle (WTTJ) — via their public Algolia search index
  - JobUp.ch                    — via their public JSON search API
  - eFinancialCareers           — via HTML scraping (best effort, may break)

All functions are fail-soft: they log and return [] on any error.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 20) -> Any:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_json(url: str, headers: dict | None = None, timeout: int = 20) -> Any:
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Welcome to the Jungle (WTTJ)
# ---------------------------------------------------------------------------
# WTTJ's public search UI is backed by Algolia.
# App id: CSEKAHHMEL, API key: 42574f76994b2a4ed0c1fc31e16dafe0 (public, rotates
# periodically). If it breaks, grab a fresh one by opening the site's
# network tab and searching for an Algolia request.

WTTJ_ALGOLIA_APP_ID = "CSEKAHHMEL"
WTTJ_ALGOLIA_API_KEY = "42574f76994b2a4ed0c1fc31e16dafe0"
WTTJ_INDEX = "wk_poland_jobs_production"  # default multilingual index


def scrape_wttj(search: str, location: str | None = None, limit: int = 25) -> list[dict]:
    """Scrape WTTJ via Algolia. Returns list of normalized job dicts."""
    url = (
        f"https://{WTTJ_ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
        f"?x-algolia-application-id={WTTJ_ALGOLIA_APP_ID}"
        f"&x-algolia-api-key={WTTJ_ALGOLIA_API_KEY}"
    )
    filters = []
    if location:
        # WTTJ uses city names in offices.city or language filters — simpler to
        # send as part of the query string and let Algolia rank.
        pass
    params_list = [
        ("query", search),
        ("hitsPerPage", str(limit)),
        ("page", "0"),
        ("attributesToRetrieve",
         "name,slug,organization,offices,published_at,language,description,contract_type"),
    ]
    params = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params_list)
    payload = {
        "requests": [
            {"indexName": "wk_fr_jobs_production", "params": params},
            {"indexName": "wk_en_jobs_production", "params": params},
        ]
    }
    try:
        data = _http_post_json(url, payload)
    except Exception as e:  # noqa: BLE001
        print(f"[wttj] error: {e}")
        return []

    results: list[dict] = []
    for result in data.get("results", []):
        for hit in result.get("hits", []):
            org = hit.get("organization") or {}
            offices = hit.get("offices") or []
            city = offices[0].get("city") if offices else None
            country = offices[0].get("country") if offices else None
            loc_str = ", ".join(filter(None, [city, country]))
            if location and location.lower() not in (loc_str or "").lower() and location.lower() not in (city or "").lower():
                # best-effort location filter
                continue
            slug = hit.get("slug")
            org_slug = org.get("slug")
            if not slug or not org_slug:
                continue
            job_url = f"https://www.welcometothejungle.com/fr/companies/{org_slug}/jobs/{slug}"
            results.append({
                "title": hit.get("name") or "",
                "company": org.get("name") or "",
                "location": loc_str,
                "job_url": job_url,
                "description": hit.get("description") or "",
                "date_posted": hit.get("published_at") or "",
                "site": "wttj",
            })
    return results[:limit]


# ---------------------------------------------------------------------------
# JobUp.ch
# ---------------------------------------------------------------------------
# JobUp.ch exposes a public GraphQL/JSON endpoint. Simple search URL works too.

def scrape_jobup(search: str, location: str | None = None, limit: int = 25) -> list[dict]:
    """Scrape JobUp.ch search results."""
    qs = {"term": search, "page": "1"}
    if location:
        qs["location"] = location
    url = "https://www.jobup.ch/api/v1/public/search?" + urllib.parse.urlencode(qs)
    try:
        data = _http_get_json(url)
    except Exception as e:  # noqa: BLE001
        print(f"[jobup] error: {e}")
        return []

    results: list[dict] = []
    for hit in data.get("documents", [])[:limit]:
        company_obj = hit.get("company", {}) or {}
        location_obj = hit.get("location", {}) or {}
        loc_str = location_obj.get("city") or location_obj.get("region") or ""
        slug = hit.get("slug") or hit.get("id")
        if not slug:
            continue
        results.append({
            "title": hit.get("title") or "",
            "company": company_obj.get("name") or "",
            "location": loc_str,
            "job_url": f"https://www.jobup.ch/fr/emplois/detail/{slug}/",
            "description": hit.get("description") or "",
            "date_posted": hit.get("publicationDate") or "",
            "site": "jobup",
        })
    return results


# ---------------------------------------------------------------------------
# eFinancialCareers (best-effort HTML scrape)
# ---------------------------------------------------------------------------
# Their public search returns HTML. We rely on a predictable URL pattern and
# regex-level extraction to avoid a BeautifulSoup dependency. Fail-soft.

import re

_EFC_CARD_RE = re.compile(
    r'<a[^>]+data-automation="job-title-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<span[^>]+data-automation="job-company"[^>]*>(.*?)</span>.*?'
    r'<span[^>]+data-automation="job-location"[^>]*>(.*?)</span>',
    re.DOTALL,
)

def scrape_efinancialcareers(search: str, location: str | None = None, limit: int = 25) -> list[dict]:
    """Scrape eFinancialCareers search HTML. Tolerant to layout changes."""
    qs = {"searchKeyword": search}
    if location:
        qs["searchLocation"] = location
    url = "https://www.efinancialcareers.fr/search?" + urllib.parse.urlencode(qs)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        print(f"[efc] error: {e}")
        return []

    def _strip(html_s: str) -> str:
        return re.sub(r"<[^>]+>", "", html_s).strip()

    out: list[dict] = []
    for m in _EFC_CARD_RE.finditer(html):
        href, title_h, company_h, loc_h = m.groups()
        full_url = href if href.startswith("http") else f"https://www.efinancialcareers.fr{href}"
        out.append({
            "title": _strip(title_h),
            "company": _strip(company_h),
            "location": _strip(loc_h),
            "job_url": full_url,
            "description": "",
            "date_posted": datetime.utcnow().date().isoformat(),
            "site": "efc",
        })
        if len(out) >= limit:
            break
    return out


SCRAPERS = {
    "wttj": scrape_wttj,
    "jobup": scrape_jobup,
    "efc": scrape_efinancialcareers,
}


if __name__ == "__main__":
    # Smoke test
    import sys
    source = sys.argv[1] if len(sys.argv) > 1 else "wttj"
    q = sys.argv[2] if len(sys.argv) > 2 else "structurer"
    loc = sys.argv[3] if len(sys.argv) > 3 else None
    res = SCRAPERS[source](q, loc, limit=5)
    print(f"{source}: {len(res)} results")
    for r in res[:5]:
        print(f"  - {r['title']} @ {r['company']} ({r['location']}) → {r['job_url']}")
