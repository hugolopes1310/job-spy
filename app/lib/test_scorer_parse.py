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


class _FakeSupabaseClient:
    """Inert client : every operation chains and returns empty data, no errors.
    Some downstream imports (auth.py) try a real call at module-load time —
    returning a usable object beats raising AttributeError."""
    def __getattr__(self, _):
        return self
    def __call__(self, *a, **k):
        return self
    @property
    def data(self):
        return []


def _build_supabase_client_stub() -> types.ModuleType:
    """`scorer` only imports `_secret`, but later tests in this file import
    `app.scraper.rescore` which transitively pulls `app.lib.auth` → it expects
    `get_anon_client` and `get_service_client` to exist. We expose the full
    surface so the import chain doesn't blow up with ImportError. The actual
    network calls are never executed (the dummy client returns empty data)."""
    mod = types.ModuleType("app.lib.supabase_client")
    mod._secret = lambda *a, **k: None
    mod.get_client         = lambda *a, **k: _FakeSupabaseClient()
    mod.get_service_client = lambda *a, **k: _FakeSupabaseClient()
    mod.get_anon_client    = lambda *a, **k: _FakeSupabaseClient()
    mod.SUPABASE_AVAILABLE = True
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
# v2 — Reliability: self-correction, _reasoning strip, heuristic fallback.
# ---------------------------------------------------------------------------
def test_reasoning_field_stripped(s):
    """The LLM emits `_reasoning` for chain-of-thought; we drop it post-parse."""
    s._call_llm = lambda *a, **k: {  # type: ignore[attr-defined]
        "_reasoning": "etape 1: ... etape 2: ...",
        "score": 8,
        "reason": "match",
    }
    out = s.analyze_offer_for_user({}, "", {"title": "x"})
    assert out is not None
    assert "_reasoning" not in out, "internal CoT field must not leak to UI"
    assert out["score"] == 8
    print("[OK] _reasoning stripped from final analysis")


def test_self_correction_retry_on_parse_fail(s):
    """When the first call returns None and providers aren't exhausted, we
    retry once with the correction suffix appended."""
    calls: list[str] = []

    def fake_call(system, user_msg, *, max_tokens=700):
        calls.append(user_msg)
        # First call returns None (parse failure). Second returns valid.
        if len(calls) == 1:
            return None
        return {"score": 6, "reason": "ok-after-retry"}

    s._call_llm = fake_call  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]

    out = s.analyze_offer_for_user({}, "", {"title": "x", "description": "d"})
    assert out is not None
    assert out["score"] == 6
    assert len(calls) == 2, f"expected 2 calls (original + correction), got {len(calls)}"
    assert "RAPPEL CRITIQUE" in calls[1], "correction suffix must include the strong hint"
    assert "RAPPEL CRITIQUE" not in calls[0], "first call must NOT carry the suffix"
    print("[OK] self-correction loop retries once with strong suffix")


def test_self_correction_skipped_when_all_quotas_dead(s):
    """No point retrying when both providers are quota-exhausted — skip the
    self-correction call and go straight to the heuristic."""
    calls: list[str] = []

    def fake_call(system, user_msg, *, max_tokens=700):
        calls.append(user_msg)
        return None  # everything fails

    s._call_llm = fake_call  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]

    out = s.analyze_offer_for_user(
        {"target": {"roles": ["structurer"]}},
        "",
        {"title": "Equity Structurer", "company": "X", "location": "Geneva"},
    )
    assert out is not None, "heuristic must give us *something*"
    assert out.get("_method") == "heuristic"
    assert len(calls) == 1, f"only the original call (no retry), got {len(calls)}"

    # reset
    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    print("[OK] self-correction skipped when all quotas dead")


def test_heuristic_fallback_dream_co_match(s):
    """All LLMs fail → heuristic must still produce a score, with dream-co
    boost when the company is in target.companies."""
    s._call_llm = lambda *a, **k: None  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]

    cfg = {
        "target": {
            "roles": ["structurer", "structuring"],
            "companies": ["pictet"],
            "seniority": ["analyst"],
        },
        "constraints": {"locations": [{"city": "Geneva"}]},
    }
    out = s.analyze_offer_for_user(cfg, "", {
        "title": "Equity Structurer Analyst",
        "company": "Pictet Group",
        "location": "Geneva, Switzerland",
        "description": "great role",
    })
    assert out is not None
    assert out.get("_method") == "heuristic"
    assert out["score"] >= 7, f"dream-co + role match should floor at 7, got {out['score']}"
    assert out["match_role"] >= 7
    assert out["match_geo"] == 10

    # reset
    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    print("[OK] heuristic floors score at 7 for dream-co + role match")


def test_heuristic_fallback_deal_breaker_caps(s):
    """Heuristic must cap at 2 when title contains a structural deal-breaker."""
    s._call_llm = lambda *a, **k: None  # type: ignore[attr-defined]
    s._GROQ_TPD_EXHAUSTED = True  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = True  # type: ignore[attr-defined]

    cfg = {
        "target": {"roles": ["structurer"], "companies": ["pictet"]},
        "constraints": {"locations": [{"city": "Geneva"}]},
    }
    # Even at Pictet in Geneva, an "Internship" title must cap the score.
    out = s.analyze_offer_for_user(cfg, "", {
        "title": "Equity Structuring Internship",
        "company": "Pictet",
        "location": "Geneva",
        "description": "x",
    })
    assert out is not None
    assert out["score"] <= 2, f"deal-breaker title must cap, got {out['score']}"

    s._GROQ_TPD_EXHAUSTED = False  # type: ignore[attr-defined]
    s._GEMINI_QUOTA_EXHAUSTED = False  # type: ignore[attr-defined]
    print("[OK] heuristic caps at 2 on title-level deal-breaker")


def test_is_failed_analysis_helper():
    """Helper used by rescore --only-failed to decide which rows to re-queue.

    The helper lives in `app.scraper.rescore`, but importing that module
    cascades into `app.lib.storage` → `app.lib.auth` → `streamlit`, which
    is not always present in dev sandboxes. Skip cleanly if the import
    chain isn't available — CI (GitHub Actions) installs streamlit so it
    always runs there.
    """
    try:
        from app.scraper.rescore import _is_failed_analysis  # noqa: E402
    except (ModuleNotFoundError, ImportError) as e:
        # ModuleNotFoundError : streamlit (or another transitive dep) is not
        # installed in the dev sandbox.
        # ImportError : a stub module is in sys.modules and doesn't expose the
        # symbol auth.py is importing (e.g. get_anon_client). With the beefed-
        # up _build_supabase_client_stub above this shouldn't trigger anymore,
        # but keep the broader except as a defense-in-depth.
        name = getattr(e, "name", None) or str(e)
        print(f"[SKIP] _is_failed_analysis import unavailable ({name}) — CI will exercise it")
        return

    assert _is_failed_analysis(None) is True, "NULL analysis must be re-queued"
    assert _is_failed_analysis({"score": 8, "reason": "ok"}) is False
    assert _is_failed_analysis({"_error": "parse_failed"}) is True
    assert _is_failed_analysis({"_error": "llm_unavailable"}) is True
    assert _is_failed_analysis({"_method": "heuristic", "score": 7}) is True
    assert _is_failed_analysis({"score": 5}) is False  # clean LLM result
    print("[OK] _is_failed_analysis correctly identifies re-queue targets")


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
    # v2 reliability
    test_reasoning_field_stripped(s)
    test_self_correction_retry_on_parse_fail(s)
    test_self_correction_skipped_when_all_quotas_dead(s)
    test_heuristic_fallback_dream_co_match(s)
    test_heuristic_fallback_deal_breaker_caps(s)
    test_is_failed_analysis_helper()
    print("\nAll scorer parsing tests passed.")
