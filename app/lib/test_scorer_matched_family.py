"""Tests for matched_role_family emission + validation in scorer.py (PR4.a).

Two angles :

1. `_coerce_matched_family(value, synthesis)` :
   - drops empty / non-string values
   - drops invented labels (LLM hallucinated near-misses)
   - drops labels of inactive families
   - keeps exact-match active labels

2. `_heuristic_score_from_synthesis(...)` :
   - emits the label of the matching family in `matched_role_family`
   - emits None when no family matches the title
   - on multi-family hit, picks the highest-weight match

Run from the repo root :
    PYTHONPATH=. python app/lib/test_scorer_matched_family.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _build_supabase_client_stub() -> types.ModuleType:
    """scorer only needs `_secret` from supabase_client. Return None so the
    HTTP path is skipped — we drive the heuristic fallback directly."""
    mod = types.ModuleType("app.lib.supabase_client")
    mod._secret = lambda *a, **k: None
    return mod


def _import_scorer():
    sys.modules["app.lib.supabase_client"] = _build_supabase_client_stub()
    sys.modules.pop("app.lib.scorer", None)
    from app.lib import scorer  # noqa: E402
    return scorer


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
def _synthesis() -> dict:
    return {
        "role_families": [
            {
                "label": "Equity Structurer",
                "titles": ["Equity Structurer", "Investment Solutions Specialist"],
                "weight": 1.0,
                "active": True,
            },
            {
                "label": "Cross-Asset Sales",
                "titles": ["Cross-Asset Sales", "Solutions Sales"],
                "weight": 0.6,
                "active": True,
            },
            {
                "label": "Inactive Family",
                "titles": ["Skip Me"],
                "weight": 0.5,
                "active": False,
            },
        ],
        "seniority_band": {"label": "mid", "yoe_min": 3, "yoe_max": 7},
        "geo": {
            "primary": ["Geneva, Switzerland"],
            "acceptable": ["Zurich, Switzerland"],
            "exclude": ["United States"],
        },
        "deal_breakers": ["audit"],
        "dream_companies": ["Pictet"],
    }


# ---------------------------------------------------------------------------
# _coerce_matched_family — accept exact active labels, drop everything else.
# ---------------------------------------------------------------------------
def test_coerce_keeps_exact_active_label(s):
    assert s._coerce_matched_family("Equity Structurer", _synthesis()) == "Equity Structurer"
    assert s._coerce_matched_family("Cross-Asset Sales", _synthesis()) == "Cross-Asset Sales"
    print("[OK] _coerce_matched_family keeps exact active labels")


def test_coerce_drops_invented_label(s):
    """LLM hallucinates a near-miss → return None (matches go into 'Sans famille')."""
    assert s._coerce_matched_family("Equity Structurer Senior", _synthesis()) is None
    assert s._coerce_matched_family("Quant Risk Manager", _synthesis()) is None
    assert s._coerce_matched_family("Sales", _synthesis()) is None
    print("[OK] _coerce_matched_family drops invented near-miss labels")


def test_coerce_drops_inactive_family_label(s):
    """The LLM shouldn't have access to inactive families anyway, but if it
    pulls one out somehow, we don't propagate it (the user deactivated it)."""
    assert s._coerce_matched_family("Inactive Family", _synthesis()) is None
    print("[OK] _coerce_matched_family drops labels of inactive families")


def test_coerce_handles_empty_and_garbage(s):
    """None / '' / wrong-type / dict → None, no crash."""
    assert s._coerce_matched_family(None, _synthesis()) is None
    assert s._coerce_matched_family("", _synthesis()) is None
    assert s._coerce_matched_family("   ", _synthesis()) is None
    assert s._coerce_matched_family(42, _synthesis()) is None  # type: ignore[arg-type]
    assert s._coerce_matched_family({"label": "x"}, _synthesis()) is None  # type: ignore[arg-type]
    print("[OK] _coerce_matched_family robust to missing / wrong-type")


def test_coerce_strips_whitespace(s):
    """LLM occasionally returns ' Equity Structurer ' — exact match should
    still succeed once trimmed."""
    assert s._coerce_matched_family(" Equity Structurer ", _synthesis()) == "Equity Structurer"
    print("[OK] _coerce_matched_family trims whitespace before comparing")


def test_coerce_with_no_synthesis_families(s):
    """Synthesis with no families at all → nothing can match."""
    empty = {"role_families": []}
    assert s._coerce_matched_family("Equity Structurer", empty) is None
    assert s._coerce_matched_family("anything", {}) is None
    print("[OK] _coerce_matched_family safely handles empty synthesis")


# ---------------------------------------------------------------------------
# _heuristic_score_from_synthesis — make sure the new key is emitted.
# ---------------------------------------------------------------------------
def test_heuristic_emits_matching_family_label(s):
    out = s._heuristic_score_from_synthesis(
        _synthesis(),
        "irrelevant cv",
        {
            "title": "Equity Structurer",
            "company": "BNP Paribas",
            "location": "Geneva, Switzerland",
        },
    )
    assert out["matched_role_family"] == "Equity Structurer"
    print("[OK] heuristic synthesis path emits the matching family label")


def test_heuristic_emits_none_when_no_family_matches(s):
    """Title doesn't contain any family's titles → None bucket."""
    out = s._heuristic_score_from_synthesis(
        _synthesis(),
        "irrelevant cv",
        {
            "title": "Plumber",  # totally off
            "company": "Random Co",
            "location": "Geneva, Switzerland",
        },
    )
    assert out["matched_role_family"] is None
    print("[OK] heuristic emits None when no family matches the title")


def test_heuristic_picks_highest_weight_on_multi_match(s):
    """Title contains tokens of two families → the higher-weight one wins."""
    # Title that hits BOTH 'Equity Structurer' (weight 1.0) and
    # 'Cross-Asset Sales' (weight 0.6). The label of the heavier one should win.
    syn = _synthesis()
    out = s._heuristic_score_from_synthesis(
        syn,
        "irrelevant cv",
        {
            "title": "Equity Structurer / Cross-Asset Sales hybrid",
            "company": "Random",
            "location": "Geneva",
        },
    )
    assert out["matched_role_family"] == "Equity Structurer", out["matched_role_family"]
    print("[OK] heuristic picks highest-weight family on multi-match")


def test_heuristic_skips_inactive_families(s):
    """Title hits only an INACTIVE family's titles → None (the inactive
    family is treated as non-existent for both match_role and label)."""
    out = s._heuristic_score_from_synthesis(
        _synthesis(),
        "irrelevant cv",
        {
            "title": "Skip Me Senior Lead",  # only matches inactive family
            "company": "Random",
            "location": "Geneva",
        },
    )
    assert out["matched_role_family"] is None
    print("[OK] heuristic ignores inactive families for the family label")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    s = _import_scorer()
    test_coerce_keeps_exact_active_label(s)
    test_coerce_drops_invented_label(s)
    test_coerce_drops_inactive_family_label(s)
    test_coerce_handles_empty_and_garbage(s)
    test_coerce_strips_whitespace(s)
    test_coerce_with_no_synthesis_families(s)
    test_heuristic_emits_matching_family_label(s)
    test_heuristic_emits_none_when_no_family_matches(s)
    test_heuristic_picks_highest_weight_on_multi_match(s)
    test_heuristic_skips_inactive_families(s)
    print("\nAll matched_role_family tests passed.")
