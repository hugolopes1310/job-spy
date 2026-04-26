"""Tests for the synthesis-driven scoring path (Phase 4).

Run from the repo root:
    PYTHONPATH=. python app/lib/test_scorer_synthesis.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _build_supabase_client_stub() -> types.ModuleType:
    """`scorer` only imports `_secret` from supabase_client. Return None for
    every key so the LLM HTTP path is skipped (we don't exercise the network)."""
    mod = types.ModuleType("app.lib.supabase_client")
    mod._secret = lambda *a, **k: None
    return mod


def _import_scorer():
    sys.modules["app.lib.supabase_client"] = _build_supabase_client_stub()
    sys.modules.pop("app.lib.scorer", None)
    from app.lib import scorer  # noqa: E402
    return scorer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _synthesis() -> dict:
    return {
        "summary_fr": "Profil structureur equity, 4 ans XP, Genève.",
        "role_families": [
            {
                "label": "Equity Structurer",
                "titles": ["Equity Structurer", "Investment Solutions Specialist"],
                "weight": 1.0,
                "active": True,
                "source": {"type": "cv", "evidence": "exp directe"},
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
        "deal_breakers": ["audit", "compliance", "intern"],
        "dream_companies": ["Pictet", "Lombard Odier"],
        "languages": ["FR-native", "EN-C1"],
        "confidence": 0.8,
        "open_questions": [
            {"id": "q_remote", "text": "Acceptes-tu 100% remote ?", "answer": "oui"},
            {"id": "q_other", "text": "Mobilité Asie ?", "answer": None},
        ],
    }


# ---------------------------------------------------------------------------
# _summarize_synthesis — content checks.
# ---------------------------------------------------------------------------
def test_summarize_synthesis_includes_active_families(s):
    out = s._summarize_synthesis(_synthesis(), "Some CV text.")
    assert "Equity Structurer" in out
    assert "Cross-Asset Sales" in out
    assert "Skip Me" not in out, "inactive family titles must NOT leak"
    assert "Inactive Family" not in out
    print("[OK] _summarize_synthesis filters inactive families")


def test_summarize_synthesis_includes_geo_and_breakers(s):
    out = s._summarize_synthesis(_synthesis(), "")
    assert "Geneva, Switzerland" in out
    assert "Zurich, Switzerland" in out
    assert "United States" in out  # exclude
    assert "audit" in out and "intern" in out
    assert "Pictet" in out
    print("[OK] geo + deal_breakers + dream_companies present in prompt block")


def test_summarize_synthesis_folds_answered_open_questions(s):
    out = s._summarize_synthesis(_synthesis(), "")
    assert "remote" in out.lower()
    assert "oui" in out, "answered open_question must be folded into block"
    assert "Mobilité Asie" not in out, "unanswered open_question must NOT leak"
    print("[OK] answered open_questions folded, unanswered ignored")


def test_summarize_synthesis_renders_seniority_band(s):
    out = s._summarize_synthesis(_synthesis(), "")
    assert "mid" in out
    assert "3-7" in out  # yoe range
    print("[OK] seniority_band rendered with yoe range")


def test_summarize_synthesis_handles_empty(s):
    """Robust to missing keys."""
    out = s._summarize_synthesis({}, "")
    assert isinstance(out, str) and len(out) > 0
    assert "aucune famille active" in out.lower()
    print("[OK] _summarize_synthesis tolerates empty synthesis")


# ---------------------------------------------------------------------------
# build_system_prompt_from_synthesis — schema + formula presence.
# ---------------------------------------------------------------------------
def test_prompt_contains_json_schema(s):
    p = s.build_system_prompt_from_synthesis(_synthesis(), "")
    assert '"score":' in p
    assert '"_reasoning":' in p
    assert '"match_role":' in p
    assert "0.40 × match_role" in p
    print("[OK] system prompt carries JSON schema + formula")


def test_prompt_carries_synthesis_fields(s):
    p = s.build_system_prompt_from_synthesis(_synthesis(), "")
    assert "Equity Structurer" in p
    assert "Pictet" in p
    assert "audit" in p
    assert "geo.exclude" in p or "exclude" in p.lower()
    print("[OK] prompt carries synthesis fields verbatim")


# ---------------------------------------------------------------------------
# analyze_offer_with_synthesis — happy path + retry + fallback.
# ---------------------------------------------------------------------------
def test_analyze_with_synthesis_happy_path(s):
    s._call_llm = lambda *a, **k: {  # type: ignore[attr-defined]
        "_reasoning": "step 1: titre = Structurer ...",
        "score": 8,
        "reason": "match",
        "match_role": 9,
        "match_geo": 10,
        "match_seniority": 8,
        "red_flags": [],
        "strengths": ["match"],
    }
    out = s.analyze_offer_with_synthesis(
        _synthesis(),
        "CV",
        {"title": "Equity Structurer", "company": "Pictet", "location": "Geneva"},
    )
    assert out is not None
    assert out["score"] == 8
    assert "_reasoning" not in out, "CoT must be stripped"
    assert out["match_role"] == 9
    print("[OK] happy path returns normalized analysis")


def test_analyze_with_synthesis_score_clamping(s):
    s._call_llm = lambda *a, **k: {"score": 15, "match_role": -3}  # type: ignore[attr-defined]
    out = s.analyze_offer_with_synthesis(_synthesis(), "", {"title": "x"})
    assert out["score"] == 10
    assert out["match_role"] == 0
    print("[OK] score + sub-scores clamped to [0, 10]")


def test_analyze_with_synthesis_self_correction(s):
    """Parse fail → retry once with the strong correction suffix."""
    calls: list[str] = []

    def fake_call(system, user_msg, *, max_tokens=700):
        calls.append(user_msg)
        if len(calls) == 1:
            return None
        return {"score": 6, "reason": "ok-after-retry"}

    s._call_llm = fake_call  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]

    out = s.analyze_offer_with_synthesis(
        _synthesis(), "", {"title": "Equity Structurer"}
    )
    assert out is not None
    assert out["score"] == 6
    assert len(calls) == 2
    assert "RAPPEL CRITIQUE" in calls[1]
    print("[OK] synthesis path retries with self-correction suffix")


def test_analyze_with_synthesis_heuristic_fallback(s):
    """All LLMs dead → heuristic fires using synthesis fields.

    Synthesis says target = Equity Structurer in Geneva, dream co = Pictet.
    A perfect match offer should score >= 7 via the dream-co + role floor.
    """
    s._call_llm = lambda *a, **k: None  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]

    out = s.analyze_offer_with_synthesis(
        _synthesis(),
        "",
        {
            "title": "Equity Structurer",
            "company": "Pictet Group",
            "location": "Geneva, Switzerland",
            "description": "great role",
        },
    )
    assert out is not None
    assert out.get("_method") == "heuristic"
    assert out["score"] >= 7, f"dream-co + role match should floor at 7, got {out['score']}"
    assert out["match_geo"] == 10  # Geneva in geo.primary

    # reset
    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    print("[OK] heuristic fallback uses synthesis dream_companies + geo")


def test_heuristic_caps_on_deal_breaker(s):
    """Heuristic synthesis path must cap at 2 when title contains a
    deal_breaker token from synthesis."""
    s._call_llm = lambda *a, **k: None  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]

    out = s.analyze_offer_with_synthesis(
        _synthesis(),
        "",
        {
            "title": "Equity Structurer Internship",  # 'intern' is a breaker
            "company": "Pictet",
            "location": "Geneva",
            "description": "x",
        },
    )
    assert out["score"] <= 2, f"deal-breaker title must cap, got {out['score']}"

    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    print("[OK] heuristic caps to <=2 when title hits a deal_breaker token")


def test_heuristic_caps_when_geo_excluded(s):
    """Geo in synthesis.geo.exclude → score <= 2 even with role match."""
    s._call_llm = lambda *a, **k: None  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]

    out = s.analyze_offer_with_synthesis(
        _synthesis(),
        "",
        {
            "title": "Equity Structurer",
            "company": "Random Bank",
            "location": "New York, United States",  # in geo.exclude
            "description": "x",
        },
    )
    assert out["score"] <= 2, f"geo.exclude must cap, got {out['score']}"
    assert out["match_geo"] == 0

    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    print("[OK] heuristic caps to <=2 when location matches geo.exclude")


def test_heuristic_acceptable_geo_partial(s):
    """Title in active family + location in geo.acceptable → mid-tier score."""
    s._call_llm = lambda *a, **k: None  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]

    out = s.analyze_offer_with_synthesis(
        _synthesis(),
        "",
        {
            "title": "Equity Structurer",
            "company": "Random Bank",  # not dream
            "location": "Zurich, Switzerland",  # acceptable, not primary
            "description": "x",
        },
    )
    assert out["match_geo"] == 7  # acceptable bucket
    assert 5 <= out["score"] <= 9

    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    print("[OK] heuristic gives match_geo=7 for geo.acceptable")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    s = _import_scorer()
    test_summarize_synthesis_includes_active_families(s)
    test_summarize_synthesis_includes_geo_and_breakers(s)
    test_summarize_synthesis_folds_answered_open_questions(s)
    test_summarize_synthesis_renders_seniority_band(s)
    test_summarize_synthesis_handles_empty(s)
    test_prompt_contains_json_schema(s)
    test_prompt_carries_synthesis_fields(s)
    test_analyze_with_synthesis_happy_path(s)
    test_analyze_with_synthesis_score_clamping(s)
    test_analyze_with_synthesis_self_correction(s)
    test_analyze_with_synthesis_heuristic_fallback(s)
    test_heuristic_caps_on_deal_breaker(s)
    test_heuristic_caps_when_geo_excluded(s)
    test_heuristic_acceptable_geo_partial(s)
    print("\nAll scorer synthesis tests passed.")
