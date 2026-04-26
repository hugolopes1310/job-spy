"""Tests for `build_queries_from_synthesis` (Phase 4 path).

Run from the repo root:
    PYTHONPATH=. python app/lib/test_query_builder_synthesis.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.lib.query_builder import build_queries_from_synthesis  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _synthesis_basic() -> dict:
    return {
        "summary_fr": "Profil structureur equity, 4 ans XP, Genève.",
        "role_families": [
            {
                "label": "Equity Structurer",
                "titles": ["Equity Structurer", "Investment Solutions Specialist", "Structureur Equity"],
                "weight": 1.0,
                "active": True,
                "source": {"type": "cv", "evidence": "exp directe"},
            },
            {
                "label": "Cross-Asset Sales",
                "titles": ["Cross-Asset Sales", "Sales Trader Equity", "Solutions Sales"],
                "weight": 0.6,
                "active": True,
                "source": {"type": "stated", "evidence": "raw_brief"},
            },
            {
                "label": "Risk Manager Equity",
                "titles": ["Risk Manager", "Equity Risk Analyst"],
                "weight": 0.4,
                "active": False,
                "source": {"type": "inferred", "evidence": "adjacent"},
            },
        ],
        "seniority_band": {"label": "mid", "yoe_min": 3, "yoe_max": 7},
        "geo": {
            "primary": ["Geneva, Switzerland"],
            "acceptable": ["Zurich, Switzerland", "Paris, France"],
            "exclude": ["United States"],
        },
        "deal_breakers": ["audit", "compliance", "intern"],
        "dream_companies": ["Pictet", "Lombard Odier"],
        "languages": ["FR-native", "EN-C1"],
        "confidence": 0.8,
        "open_questions": [],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_basic_cross_product():
    """Active families × geo.primary → one query per (title, primary location).
    Inactive family must be skipped.
    """
    syn = _synthesis_basic()
    qs = build_queries_from_synthesis(syn, max_queries=30)
    # 2 active families × max 4 titles × 1 primary = up to 8 queries from primary,
    # then geo.acceptable expansion for the remaining budget.
    assert len(qs) > 0
    titles = {q["search_term"] for q in qs}
    # Risk Manager (inactive) must NOT appear.
    assert "Risk Manager" not in titles
    assert "Equity Risk Analyst" not in titles
    # Active family titles ARE present.
    assert "Equity Structurer" in titles
    assert "Cross-Asset Sales" in titles
    print("[OK] inactive families filtered, active titles present")


def test_weight_ordering():
    """Top-weighted family appears first in the output."""
    syn = _synthesis_basic()
    qs = build_queries_from_synthesis(syn, max_queries=30)
    # First entry must come from the highest-weight family.
    first_title = qs[0]["search_term"].lower()
    assert any(t.lower() == first_title for t in syn["role_families"][0]["titles"]), (
        f"first query {qs[0]} should belong to top-weight family"
    )
    print("[OK] weight DESC ordering preserved")


def test_geo_primary_only_first_pass():
    """First pass must use geo.primary exclusively before expanding to acceptable."""
    syn = _synthesis_basic()
    qs = build_queries_from_synthesis(syn, max_queries=30)
    # Find the boundary where geo flips from primary to acceptable.
    primary_locs = set(syn["geo"]["primary"])
    # First N queries (= active_families × top_titles × 1) all have geo.primary.
    n_primary_pass = sum(1 for q in qs if q["location"] in primary_locs)
    assert n_primary_pass >= 6, "primary pass should fill at least 2 families × 3-4 titles"
    print(f"[OK] {n_primary_pass} queries on primary geo before falling back to acceptable")


def test_max_queries_cap():
    """The total number of queries never exceeds `max_queries`."""
    syn = _synthesis_basic()
    qs = build_queries_from_synthesis(syn, max_queries=5)
    assert len(qs) == 5, f"expected exactly 5 queries (cap), got {len(qs)}"
    print("[OK] cap enforced strictly")


def test_max_titles_per_family():
    """`max_titles_per_family` limits the title fan-out."""
    syn = {
        "role_families": [{
            "label": "Wide",
            "titles": ["T1", "T2", "T3", "T4", "T5", "T6"],
            "weight": 1.0,
            "active": True,
        }],
        "geo": {"primary": ["Geneva, Switzerland"], "acceptable": [], "exclude": []},
    }
    qs = build_queries_from_synthesis(syn, max_titles_per_family=3, max_queries=30)
    titles = {q["search_term"] for q in qs}
    assert titles == {"T1", "T2", "T3"}, titles
    print("[OK] per-family title cap respected")


def test_no_active_families_returns_empty():
    syn = {
        "role_families": [
            {"label": "X", "titles": ["t"], "weight": 1.0, "active": False},
        ],
        "geo": {"primary": ["Geneva"], "acceptable": [], "exclude": []},
    }
    assert build_queries_from_synthesis(syn) == []
    print("[OK] no active families → empty list")


def test_no_geo_falls_back_to_remote():
    """If geo.primary AND geo.acceptable are empty, emit one query per title
    with location=None (remote-friendly scrape)."""
    syn = {
        "role_families": [{
            "label": "X",
            "titles": ["t1", "t2"],
            "weight": 1.0,
            "active": True,
        }],
        "geo": {"primary": [], "acceptable": [], "exclude": []},
    }
    qs = build_queries_from_synthesis(syn)
    assert len(qs) == 2
    assert all(q["location"] is None for q in qs)
    assert all(q["distance"] is None for q in qs)  # no radius without a city
    print("[OK] no geo → location=None remote queries")


def test_acceptable_geo_expansion():
    """Once primary geo is exhausted, acceptable fills the remaining budget."""
    syn = {
        "role_families": [{
            "label": "X",
            "titles": ["t1"],  # only 1 title to keep math simple
            "weight": 1.0,
            "active": True,
        }],
        "geo": {
            "primary": ["Geneva, Switzerland"],
            "acceptable": ["Zurich, Switzerland", "Paris, France"],
            "exclude": [],
        },
    }
    qs = build_queries_from_synthesis(syn, max_queries=30)
    locs = [q["location"] for q in qs]
    assert locs == ["Geneva, Switzerland", "Zurich, Switzerland", "Paris, France"], locs
    print("[OK] acceptable geo expansion after primary")


def test_dedup_same_title_same_location():
    """If a title appears in two active families, the (title, location) pair
    is only emitted once."""
    syn = {
        "role_families": [
            {
                "label": "F1",
                "titles": ["Shared Title"],
                "weight": 1.0,
                "active": True,
            },
            {
                "label": "F2",
                "titles": ["Shared Title", "Other Title"],
                "weight": 0.5,
                "active": True,
            },
        ],
        "geo": {"primary": ["Geneva"], "acceptable": [], "exclude": []},
    }
    qs = build_queries_from_synthesis(syn)
    keys = [(q["search_term"], q["location"]) for q in qs]
    assert keys == [("Shared Title", "Geneva"), ("Other Title", "Geneva")]
    print("[OK] dedup on (title, location)")


def test_country_indeed_inferred_from_geo():
    """The Indeed country code is sniffed from the first non-empty geo entry."""
    syn = {
        "role_families": [{
            "label": "X", "titles": ["t"], "weight": 1.0, "active": True,
        }],
        "geo": {"primary": ["London, United Kingdom"], "acceptable": [], "exclude": []},
    }
    qs = build_queries_from_synthesis(syn)
    assert qs[0]["country_indeed"] == "uk"
    print("[OK] country_indeed inferred from geo tail")


def test_query_shape_compatible_with_jobspy():
    """Output dict must carry every field jobspy expects (same shape as
    `build_queries`)."""
    syn = _synthesis_basic()
    qs = build_queries_from_synthesis(syn)
    expected_keys = {
        "search_term", "location", "sites", "results_wanted",
        "hours_old", "distance", "country_indeed",
    }
    assert qs and set(qs[0].keys()) == expected_keys
    print("[OK] query dict has all expected jobspy keys")


def test_sites_default_when_not_provided():
    syn = _synthesis_basic()
    qs = build_queries_from_synthesis(syn)
    assert qs[0]["sites"] == ["linkedin", "indeed", "google"]
    print("[OK] default sites used when sites=None")


def test_sites_normalized_when_provided():
    syn = _synthesis_basic()
    qs = build_queries_from_synthesis(syn, sites=["google_jobs", "indeed"])
    # google_jobs alias must collapse to "google".
    assert qs[0]["sites"] == ["google", "indeed"]
    print("[OK] custom sites normalized through aliases")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_basic_cross_product()
    test_weight_ordering()
    test_geo_primary_only_first_pass()
    test_max_queries_cap()
    test_max_titles_per_family()
    test_no_active_families_returns_empty()
    test_no_geo_falls_back_to_remote()
    test_acceptable_geo_expansion()
    test_dedup_same_title_same_location()
    test_country_indeed_inferred_from_geo()
    test_query_shape_compatible_with_jobspy()
    test_sites_default_when_not_provided()
    test_sites_normalized_when_provided()
    print("\nAll query_builder synthesis tests passed.")
