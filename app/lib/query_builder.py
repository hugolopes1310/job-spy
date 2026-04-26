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

# Phase 4 caps — synthesis can hold many role_families × titles, so we hard-
# limit the cartesian product to keep one scraper run bounded. Tuned for a
# 40 jobs/run scoring cap (~30 queries × 25 results = 750 raw jobs max).
_MAX_QUERIES_PER_SCRAPE = 30
_MAX_TITLES_PER_FAMILY = 4


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


# ---------------------------------------------------------------------------
# Phase 4 — synthesis-driven queries
# ---------------------------------------------------------------------------
def _country_for_indeed_from_geo(geo_strings: list[str]) -> str:
    """Pick an Indeed country code from synthesis geo strings.

    Synthesis stores geo as "City, Country" or "Country" plain strings, so we
    sniff the country tail of the first non-empty entry. Defaults to france.
    """
    for s in geo_strings:
        if not s:
            continue
        # Last comma-separated token is usually the country.
        tail = s.split(",")[-1].strip().lower()
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
        if tail in mapping:
            return mapping[tail]
    return "france"


def build_queries_from_synthesis(
    synthesis: dict[str, Any],
    *,
    hours_old: int = _DEFAULT_HOURS_OLD,
    results_per_query: int = _DEFAULT_RESULTS,
    sites: list[str] | None = None,
    max_queries: int = _MAX_QUERIES_PER_SCRAPE,
    max_titles_per_family: int = _MAX_TITLES_PER_FAMILY,
) -> list[dict[str, Any]]:
    """Build scraper queries from a profile synthesis (Phase 4 path).

    Iterates `synthesis["role_families"]`, filtered by `active=True`, ordered
    by `weight` DESC. For each family, takes the first `max_titles_per_family`
    titles and cross-products with `synthesis["geo"]["primary"]`. If we still
    have headroom under `max_queries`, we extend with `geo.acceptable`.

    Returned dicts have the same shape as `build_queries()` so the scraper can
    swap in this function without further changes.
    """
    role_families = synthesis.get("role_families") or []
    geo = synthesis.get("geo") or {}
    primary = [s for s in (geo.get("primary") or []) if (s or "").strip()]
    acceptable = [s for s in (geo.get("acceptable") or []) if (s or "").strip()]

    # Active families, weight DESC. Stable secondary sort: original order.
    active = [f for f in role_families if isinstance(f, dict) and f.get("active", True)]
    active.sort(key=lambda f: float(f.get("weight") or 0.0), reverse=True)

    if not active:
        return []

    sites_norm = _normalize_sources(sites) if sites else list(_DEFAULT_SITES)
    country_indeed = _country_for_indeed_from_geo(primary or acceptable)

    def _make_query(title: str, location: str | None) -> dict[str, Any]:
        return {
            "search_term": title,
            "location": location,
            "sites": sites_norm,
            "results_wanted": results_per_query,
            "hours_old": hours_old,
            "distance": _DEFAULT_RADIUS_KM if location else None,
            "country_indeed": country_indeed,
        }

    queries: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()

    def _push(title: str, location: str | None) -> bool:
        """Add the query if unique. Return False once cap is reached."""
        if len(queries) >= max_queries:
            return False
        key = (title.strip().lower(), (location or "").strip().lower() or None)
        if key in seen:
            return True  # don't break the loop, just skip dups
        seen.add(key)
        queries.append(_make_query(title.strip(), location))
        return True

    # First pass — primary geo. If no geo at all, fall back to remote-friendly
    # (location=None) one query per top-2 titles per family.
    locations_pass1 = primary or [None]
    for family in active:
        titles = [t for t in (family.get("titles") or []) if (t or "").strip()]
        titles = titles[:max_titles_per_family]
        for title in titles:
            for loc in locations_pass1:
                if not _push(title, loc):
                    return queries

    # Second pass — extend with geo.acceptable while we still have budget.
    if acceptable and len(queries) < max_queries:
        for family in active:
            titles = [t for t in (family.get("titles") or []) if (t or "").strip()]
            titles = titles[:max_titles_per_family]
            for title in titles:
                for loc in acceptable:
                    if not _push(title, loc):
                        return queries

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
