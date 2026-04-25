"""Turn a user's structured config into a concrete list of scraper queries.

The user config is produced by `config_extractor.py` and stored in
`user_configs.config`. Its shape (abridged):

    {
      "target": {"roles": ["Structureur", ...], ...},
      "constraints": {
        "locations": [{"city": "Geneva", "country": "Switzerland", "radius_km": 30}, ...],
        ...
      },
      "active_sources": ["linkedin", "indeed", "google_jobs"]
    }

For each (role × location) we emit one scraper query. An absence of locations
produces one global remote-friendly query per role.
"""
from __future__ import annotations

from typing import Any


_DEFAULT_SITES = ["linkedin", "indeed", "google"]
_DEFAULT_RESULTS = 25
_DEFAULT_HOURS_OLD = 24
_DEFAULT_RADIUS_KM = 50


def _normalize_sources(sources: list[str] | None) -> list[str]:
    """Map config's `active_sources` names to jobspy's site names."""
    if not sources:
        return list(_DEFAULT_SITES)
    aliases = {
        "linkedin": "linkedin",
        "indeed": "indeed",
        "google": "google",
        "google_jobs": "google",
        "googlejobs": "google",
    }
    out: list[str] = []
    seen: set[str] = set()
    for s in sources:
        key = (s or "").strip().lower()
        mapped = aliases.get(key)
        if mapped and mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out or list(_DEFAULT_SITES)


def _format_location(loc: dict[str, Any]) -> str:
    """Turn a location dict into the free-form string jobspy expects."""
    city = (loc.get("city") or "").strip()
    country = (loc.get("country") or "").strip()
    parts = [p for p in (city, country) if p]
    return ", ".join(parts)


def _country_for_indeed(locations: list[dict]) -> str:
    """Pick the Indeed country code based on the first location.

    Indeed needs a country: france, switzerland, uk, usa...
    Defaults to france for MVP (target audience = FR/CH).
    """
    if not locations:
        return "france"
    country = (locations[0].get("country") or "").strip().lower()
    mapping = {
        "france": "france",
        "switzerland": "switzerland",
        "suisse": "switzerland",
        "united kingdom": "uk",
        "uk": "uk",
        "united states": "usa",
        "usa": "usa",
        "germany": "germany",
        "belgium": "belgium",
        "luxembourg": "luxembourg",
    }
    return mapping.get(country, "france")


def build_queries(
    config: dict[str, Any],
    *,
    hours_old: int = _DEFAULT_HOURS_OLD,
    results_per_query: int = _DEFAULT_RESULTS,
) -> list[dict[str, Any]]:
    """Build a list of scraper queries from a user's config.

    Returns a list of dicts suitable to pass as kwargs to
    `scrapers.scrape_via_jobspy(**query)`.
    """
    target = config.get("target", {}) or {}
    constraints = config.get("constraints", {}) or {}
    roles: list[str] = [r.strip() for r in (target.get("roles") or []) if (r or "").strip()]
    locations: list[dict] = constraints.get("locations") or []
    active_sources = config.get("active_sources") or _DEFAULT_SITES
    sites = _normalize_sources(active_sources)
    country_indeed = _country_for_indeed(locations)

    # No roles → no scraping. Surface this clearly so the caller can skip.
    if not roles:
        return []

    queries: list[dict[str, Any]] = []

    # Cartesian product: one (role × location) per query.
    if locations:
        for role in roles:
            for loc in locations:
                loc_str = _format_location(loc)
                if not loc_str:
                    continue
                radius = int(loc.get("radius_km") or _DEFAULT_RADIUS_KM)
                queries.append({
                    "search_term": role,
                    "location": loc_str,
                    "sites": sites,
                    "results_wanted": results_per_query,
                    "hours_old": hours_old,
                    "distance": radius,
                    "country_indeed": country_indeed,
                })
    else:
        # No locations specified — fire one query per role without filter.
        # Remote-friendly users may land here.
        for role in roles:
            queries.append({
                "search_term": role,
                "location": None,
                "sites": sites,
                "results_wanted": results_per_query,
                "hours_old": hours_old,
                "distance": None,
                "country_indeed": country_indeed,
            })

    return queries


if __name__ == "__main__":
    # Smoke test with a sample config
    import json

    sample = {
        "target": {"roles": ["Structureur", "Cross-Asset Sales"]},
        "constraints": {
            "locations": [
                {"city": "Geneva", "country": "Switzerland", "radius_km": 30},
                {"city": "Zurich", "country": "Switzerland", "radius_km": 30},
            ],
        },
        "active_sources": ["linkedin", "indeed", "google_jobs"],
    }
    qs = build_queries(sample, hours_old=24, results_per_query=15)
    print(f"Generated {len(qs)} queries:")
    for q in qs:
        print(json.dumps({k: v for k, v in q.items() if k != "sites"}, ensure_ascii=False))
