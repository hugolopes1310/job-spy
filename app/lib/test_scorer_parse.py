"""Tests for scorer JSON parsing + analysis normalization.

Run from the repo root:
    PYTHONPATH=. python app/lib/test_scorer_parse.py
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
# JSON-parsing tests.
# ---------------------------------------------------------------------------
def test_pure_json(s):
    parsed = s._parse_llm_json('{"score": 8, "reason": "ok"}', provider="t")
    assert parsed == {"score": 8, "reason": "ok"}, parsed
    print("[OK] pure JSON parses")


def test_markdown_fences(s):
    raw = '```json\n{"score": 7}\n```'
    assert s._parse_llm_json(raw, provider="t") == {"score": 7}
    raw2 = '```\n{"score": 7}\n```'
    assert s._parse_llm_json(raw2, provider="t") == {"score": 7}
    raw3 = '```javascript\n{"score": 7}\n```'
    assert s._parse_llm_json(raw3, provider="t") == {"score": 7}
    print("[OK] strips ```json / ``` / ```javascript fences")


def test_trailing_prose(s):
    """LLMs sometimes append 'Hope this helps!' after the JSON."""
    raw = '{"score": 6, "reason": "x"}\n\nVoilà mon analyse.'
    assert s._parse_llm_json(raw, provider="t") == {"score": 6, "reason": "x"}
    print("[OK] trailing prose stripped via brace extraction")


def test_leading_prose(s):
    raw = "Voici l'analyse demandée :\n\n{\"score\": 5}"
    assert s._parse_llm_json(raw, provider="t") == {"score": 5}
    print("[OK] leading prose stripped via brace extraction")


def test_nested_braces(s):
    """Brace-counting must respect nested objects."""
    raw = '{"score": 9, "analysis": {"role": "match", "geo": "ok"}, "list": [1, 2]}'
    parsed = s._parse_llm_json(raw, provider="t")
    assert parsed and parsed["analysis"] == {"role": "match", "geo": "ok"}
    print("[OK] nested object preserved")


def test_braces_inside_strings(s):
    """A '}' inside a string literal must not close the outer object."""
    raw = '{"reason": "we use {curly} braces here", "score": 3}'
    parsed = s._parse_llm_json(raw, provider="t")
    assert parsed == {"reason": "we use {curly} braces here", "score": 3}
    print("[OK] braces inside strings handled")


def test_unrecoverable_returns_none(s):
    """Garbled output → None, no exception."""
    assert s._parse_llm_json("not json at all", provider="t") is None
    assert s._parse_llm_json("", provider="t") is None
    assert s._parse_llm_json("{ unbalanced", provider="t") is None
    print("[OK] unrecoverable input → None")


def test_non_dict_returns_none(s):
    """JSON that's a list or scalar isn't a usable analysis."""
    assert s._parse_llm_json("[1, 2, 3]", provider="t") is None
    assert s._parse_llm_json("42", provider="t") is None
    print("[OK] non-dict JSON → None")


def test_make_parse_failed_analysis(s):
    a = s.make_parse_failed_analysis()
    assert a["score"] is None
    assert a["_error"] == "parse_failed"
    assert isinstance(a["red_flags"], list)
    assert isinstance(a["strengths"], list)
    print("[OK] parse_failed analysis shape")


def test_quota_state_initial(s):
    state = s.llm_quota_state()
    assert state == {"groq_tpd": False, "gemini_quota": False, "all_exhausted": False}, state
    print("[OK] llm_quota_state initial value")


def test_quota_state_groq_exhausted(s):
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._LLM_QUOTA["groq_tpd"] = True
    state = s.llm_quota_state()
    assert state["groq_tpd"] is True
    assert state["all_exhausted"] is False  # gemini still available
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]
    s._LLM_QUOTA["gemini_quota"] = True
    assert s.llm_quota_state()["all_exhausted"] is True
    # reset so other tests start clean
    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    s._LLM_QUOTA["groq_tpd"] = False
    s._LLM_QUOTA["gemini_quota"] = False
    print("[OK] llm_quota_state flips correctly")


# ---------------------------------------------------------------------------
# analyze_offer_for_user — score clamping.
# ---------------------------------------------------------------------------
def test_score_clamping(s):
    """Score above 10 / below 0 / non-int must clamp to [0, 10]."""
    # Patch _call_llm to return a doctored result.
    cases = [
        ({"score": 15}, 10),
        ({"score": -3}, 0),
        ({"score": "8"}, 8),
        ({"score": None}, 0),
        ({"score": "junk"}, 0),
        ({"score": 7.9}, 7),  # int() truncates floats.
    ]
    for fake, expected in cases:
        s._call_llm = lambda *a, **k: dict(fake)  # type: ignore[attr-defined]
        out = s.analyze_offer_for_user({}, "", {"title": "x"})
        assert out is not None
        assert out["score"] == expected, f"expected {expected} from {fake}, got {out['score']}"
    print("[OK] score clamped to [0, 10] for all wonky inputs")


def test_sub_scores_normalized(s):
    s._call_llm = lambda *a, **k: {  # type: ignore[attr-defined]
        "score": 7, "match_role": 12, "match_geo": -5, "match_seniority": "ok",
    }
    out = s.analyze_offer_for_user({}, "", {"title": "x"})
    assert out["match_role"]      == 10
    assert out["match_geo"]       == 0
    assert out["match_seniority"] == -1   # non-int falls back to -1 sentinel
    print("[OK] sub-scores clamped, non-int → -1")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    s = _import_scorer()
    test_pure_json(s)
    test_markdown_fences(s)
    test_trailing_prose(s)
    test_leading_prose(s)
    test_nested_braces(s)
    test_braces_inside_strings(s)
    test_unrecoverable_returns_none(s)
    test_non_dict_returns_none(s)
    test_make_parse_failed_analysis(s)
    test_quota_state_initial(s)
    test_quota_state_groq_exhausted(s)
    test_score_clamping(s)
    test_sub_scores_normalized(s)
    print("\nAll scorer parsing tests passed.")
