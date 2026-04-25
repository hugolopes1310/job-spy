"""Custom career sites scraping — Phase 3.

Lets a user add their own corporate careers URLs (e.g. `https://boards.greenhouse.io/stripe`,
`https://jobs.lever.co/loft`, `https://apply.workable.com/acme/`, or a raw careers page
hosted by the company).

Strategy:
  1. `detect_ats(url)` inspects the URL and returns one of:
        ("greenhouse", "stripe")
        ("lever",      "loft")
        ("workable",   "acme")
        ("ashby",      "openai")
        ("generic",    None)        ← fallback: fetch HTML, pipe to LLM
  2. `scrape_career_site(source)` dispatches to the right fetcher and returns a
     list of normalized dicts — same shape as `scrape_via_jobspy()`:

        {url, title, company, location, description, date_posted, site}

Fail-soft: every fetcher raises on network/parsing issues so the scraper runner
can catch, log, and flag the source as "not_scrapable" in the user's config.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,fr;q=0.9",
}
TIMEOUT = 20

# ---------------------------------------------------------------------------
# ATS detection
# ---------------------------------------------------------------------------
# Regex patterns that pull the "company slug" out of known ATS URL shapes.
_ATS_RULES: list[tuple[str, re.Pattern[str]]] = [
    # Greenhouse:
    #   https://boards.greenhouse.io/stripe
    #   https://boards.greenhouse.io/embed/job_board?for=stripe
    #   https://stripe.greenhouse.io/...
    ("greenhouse", re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-zA-Z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"https?://([a-zA-Z0-9._-]+)\.greenhouse\.io", re.I)),
    # Lever:
    #   https://jobs.lever.co/loft
    #   https://jobs.lever.co/loft/<posting-id>
    ("lever", re.compile(r"jobs\.lever\.co/([a-zA-Z0-9._-]+)", re.I)),
    # Workable:
    #   https://apply.workable.com/acme/
    #   https://acme.workable.com/
    ("workable", re.compile(r"apply\.workable\.com/([a-zA-Z0-9._-]+)", re.I)),
    ("workable", re.compile(r"https?://([a-zA-Z0-9._-]+)\.workable\.com", re.I)),
    # Ashby:
    #   https://jobs.ashbyhq.com/openai
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-zA-Z0-9._-]+)", re.I)),
    ("ashby", re.compile(r"app\.ashbyhq\.com/jobs/([a-zA-Z0-9._-]+)", re.I)),
]


def detect_ats(url: str) -> tuple[str, str | None]:
    """Returns (ats_type, identifier). ats_type is 'generic' if unknown."""
    if not url:
        return ("generic", None)
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    for ats, pat in _ATS_RULES:
        m = pat.search(u)
        if m:
            slug = m.group(1).strip().strip("/")
            if slug:
                return (ats, slug)
    return ("generic", None)


def canonical_label(url: str, ats: str, slug: str | None) -> str:
    """Human-friendly label for the source. Used in the dashboard badge."""
    if ats != "generic" and slug:
        return f"{ats}/{slug}"
    host = urlparse(url if url.startswith("http") else "https://" + url).netloc
    return host or url


# ---------------------------------------------------------------------------
# HTML → text helper
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_WS_RE = re.compile(r"\s+")


def _html_to_text(raw: str | None, *, limit: int = 20_000) -> str:
    if not raw:
        return ""
    s = _SCRIPT_RE.sub(" ", raw)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = _WS_RE.sub(" ", s).strip()
    return s[:limit]


def _iso(ts_ms: int | None) -> str | None:
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# ATS fetchers — each returns list[dict] in the normalized shape.
# Each raises on HTTP / parsing errors so the caller can flag the source.
# ---------------------------------------------------------------------------
def fetch_greenhouse(slug: str) -> list[dict]:
    """https://developers.greenhouse.io/job-board.html"""
    api = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    r = requests.get(api, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    site = f"greenhouse/{slug}"
    out: list[dict] = []
    for job in data.get("jobs", []):
        url = job.get("absolute_url") or ""
        title = (job.get("title") or "").strip()
        if not (url and title):
            continue
        loc = (job.get("location") or {}).get("name") or ""
        out.append({
            "url": url,
            "title": title,
            "company": slug,
            "location": loc,
            "description": _html_to_text(job.get("content")),
            "date_posted": (job.get("updated_at") or "")[:10],
            "site": site,
        })
    return out


def fetch_lever(slug: str) -> list[dict]:
    """https://github.com/lever/postings-api"""
    api = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    r = requests.get(api, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    site = f"lever/{slug}"
    out: list[dict] = []
    for job in data:
        url = job.get("hostedUrl") or job.get("applyUrl")
        title = (job.get("text") or "").strip()
        if not (url and title):
            continue
        cats = job.get("categories") or {}
        loc = cats.get("location") or cats.get("team") or ""
        desc = job.get("descriptionPlain") or _html_to_text(job.get("description"))
        out.append({
            "url": url,
            "title": title,
            "company": slug,
            "location": loc,
            "description": (desc or "")[:20_000],
            "date_posted": _iso(job.get("createdAt")) or "",
            "site": site,
        })
    return out


def fetch_workable(slug: str) -> list[dict]:
    """Public account widget: apply.workable.com/api/v3/accounts/{slug}/jobs"""
    api = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    r = requests.get(api, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    site = f"workable/{slug}"
    out: list[dict] = []
    for job in data.get("results", []) or []:
        short = job.get("shortcode") or job.get("id")
        url = (
            job.get("url")
            or job.get("application_url")
            or (short and f"https://apply.workable.com/{slug}/j/{short}/")
            or ""
        )
        title = (job.get("title") or "").strip()
        if not (url and title):
            continue
        locd = job.get("location") or {}
        loc = ", ".join(
            [x for x in (locd.get("city"), locd.get("region"), locd.get("country")) if x]
        )
        out.append({
            "url": url,
            "title": title,
            "company": slug,
            "location": loc,
            "description": _html_to_text(job.get("description") or job.get("full_description")),
            "date_posted": (job.get("published") or job.get("created_at") or "")[:10],
            "site": site,
        })
    return out


def fetch_ashby(slug: str) -> list[dict]:
    """https://developers.ashbyhq.com/reference/publicjobpostings"""
    api = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false"
    r = requests.get(api, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    site = f"ashby/{slug}"
    out: list[dict] = []
    for job in data.get("jobs", []) or []:
        url = job.get("jobUrl") or job.get("applyUrl") or ""
        title = (job.get("title") or "").strip()
        if not (url and title):
            continue
        loc = job.get("locationName") or job.get("location") or ""
        desc = _html_to_text(job.get("descriptionHtml")) or job.get("descriptionPlain") or ""
        out.append({
            "url": url,
            "title": title,
            "company": slug,
            "location": loc,
            "description": desc[:20_000],
            "date_posted": (job.get("publishedDate") or job.get("updatedAt") or "")[:10],
            "site": site,
        })
    return out


# ---------------------------------------------------------------------------
# Generic fallback — fetch HTML and ask an LLM to pull job cards out of it.
# Uses Groq first (cheap), Gemini as backup — same pattern as cv_extractor.
# ---------------------------------------------------------------------------
_GENERIC_SYSTEM_PROMPT = """You are a structured web-page extractor.
You are given the visible text of a company's careers page.
Return a JSON object with a single key "jobs" which is an array.
Each element must have these fields (strings, empty string if unknown):
  - title    : the job title
  - location : city / country / "remote"
  - url      : absolute URL to the offer detail page (if the page mentioned one)
  - description : 1-3 sentence summary if available, otherwise ""
  - date_posted : ISO date (YYYY-MM-DD) if mentioned, otherwise ""

Rules:
  - Only include CURRENT open positions. Skip alumni lists, blog posts, team bios.
  - If the page shows "0 open roles" or similar, return {"jobs": []}.
  - Never invent roles. If unsure, skip.
  - Maximum 40 jobs. If more, pick the first 40 as listed on the page.
"""


def _secret(key: str) -> str | None:
    """Mirror of supabase_client._secret (avoid circular import on Streamlit)."""
    try:
        import streamlit as st  # type: ignore

        if key in st.secrets:
            return str(st.secrets[key])
    except (ImportError, FileNotFoundError, RuntimeError, KeyError):
        pass
    return os.environ.get(key)


def _call_groq_for_jobs(page_text: str) -> list[dict] | None:
    api_key = _secret("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        body = {
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": _GENERIC_SYSTEM_PROMPT},
                {"role": "user", "content": page_text[:40_000]},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        jobs = parsed.get("jobs") or []
        return jobs if isinstance(jobs, list) else None
    except Exception as e:  # noqa: BLE001
        print(f"[career_sites] groq extraction failed: {e}")
        return None


def _call_gemini_for_jobs(page_text: str) -> list[dict] | None:
    api_key = _secret("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        body = {
            "systemInstruction": {"parts": [{"text": _GENERIC_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": page_text[:40_000]}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.0,
            },
        }
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json=body,
            timeout=60,
        )
        r.raise_for_status()
        content = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(content)
        jobs = parsed.get("jobs") or []
        return jobs if isinstance(jobs, list) else None
    except Exception as e:  # noqa: BLE001
        print(f"[career_sites] gemini extraction failed: {e}")
        return None


def fetch_generic(url: str) -> list[dict]:
    """Download the page HTML, strip it, ask an LLM to extract jobs."""
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    text = _html_to_text(r.text, limit=60_000)
    if len(text) < 80:
        raise RuntimeError("page text too short to parse")

    jobs = _call_groq_for_jobs(text)
    if jobs is None:
        jobs = _call_gemini_for_jobs(text)
    if jobs is None:
        raise RuntimeError("LLM extraction unavailable (no Groq / Gemini key)")

    host = urlparse(url).netloc
    site = f"custom/{host}" if host else "custom"
    out: list[dict] = []
    for j in jobs[:40]:
        if not isinstance(j, dict):
            continue
        title = (j.get("title") or "").strip()
        if not title:
            continue
        # URL: if LLM didn't give one, fall back to the source page itself.
        job_url = (j.get("url") or "").strip() or url
        if not job_url.startswith(("http://", "https://")):
            # Try to resolve relative paths against the source page.
            try:
                from urllib.parse import urljoin
                job_url = urljoin(url, job_url)
            except Exception:  # noqa: BLE001
                job_url = url
        out.append({
            "url": job_url,
            "title": title,
            "company": host.split(".")[-2] if "." in host else host,
            "location": (j.get("location") or "").strip(),
            "description": (j.get("description") or "").strip(),
            "date_posted": (j.get("date_posted") or "")[:10],
            "site": site,
        })
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def scrape_career_site(source: dict[str, Any]) -> list[dict]:
    """Scrape one custom source. Returns normalized job dicts.

    `source` is a dict from `config.custom_career_sources`, shape:
        {url: str, label?: str, ats_type?: str, status?: str, ...}
    Raises on fetch/parse failure so the caller can flag the source.
    """
    url = (source or {}).get("url") or ""
    if not url:
        raise ValueError("empty url")

    # Trust cached ats_type if present (users may override detection), else detect.
    ats = (source.get("ats_type") or "").strip().lower()
    slug: str | None = source.get("slug") or None
    if ats not in {"greenhouse", "lever", "workable", "ashby", "generic"} or not slug:
        ats, slug = detect_ats(url)

    if ats == "greenhouse" and slug:
        return fetch_greenhouse(slug)
    if ats == "lever" and slug:
        return fetch_lever(slug)
    if ats == "workable" and slug:
        return fetch_workable(slug)
    if ats == "ashby" and slug:
        return fetch_ashby(slug)
    # Everything else: LLM fallback.
    return fetch_generic(url)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://boards.greenhouse.io/stripe"
    ats, slug = detect_ats(test_url)
    print(f"detected: ats={ats!r} slug={slug!r}")
    try:
        jobs = scrape_career_site({"url": test_url})
    except Exception as e:  # noqa: BLE001
        print(f"scrape failed: {e}")
        sys.exit(1)
    print(f"got {len(jobs)} jobs")
    for j in jobs[:3]:
        print(f"  - {j['title']!r} @ {j['location']!r} — {j['url']}")
