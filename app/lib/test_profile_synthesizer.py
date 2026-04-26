"""Tests for profile_synthesizer.

Run from the repo root:
    PYTHONPATH=. python app/lib/test_profile_synthesizer.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Stubs (no streamlit / supabase / network).
# ---------------------------------------------------------------------------
def _build_supabase_client_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.supabase_client")
    mod._secret = lambda *a, **k: None
    return mod


def _build_klog_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.klog")
    mod.log = lambda *a, **k: None
    return mod


def _import_synthesizer():
    sys.modules["app.lib.supabase_client"] = _build_supabase_client_stub()
    sys.modules["app.lib.klog"] = _build_klog_stub()
    sys.modules.pop("app.lib.scorer", None)
    sys.modules.pop("app.lib.profile_synthesizer", None)
    from app.lib import profile_synthesizer  # noqa: E402
    return profile_synthesizer


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _valid_synthesis() -> dict:
    return {
        "summary_fr": "Mid-senior pharma, 7 ans en clinical research.",
        "role_families": [
            {
                "label": "Clinical Research",
                "titles": ["CRA", "Senior CRA", "Clinical PM", "Clinical Trial Manager"],
                "weight": 0.9,
                "active": True,
                "source": {"type": "cv", "evidence": "5 ans en CRO"},
            }
        ],
        "seniority_band": {"label": "mid-senior", "yoe_min": 5, "yoe_max": 12},
        "geo": {
            "primary": ["Geneva, Switzerland"],
            "acceptable": ["Basel", "Lausanne"],
            "exclude": [],
        },
        "deal_breakers": ["sales", "intern"],
        "dream_companies": ["Roche", "Novartis"],
        "languages": ["FR-native", "EN-C1"],
        "confidence": 0.75,
        "open_questions": [
            {"id": "q_contract_type", "text": "CDD ok ?", "answer": None}
        ],
    }


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------
def test_validate_accepts_full_synthesis(ps):
    out = ps._validate_synthesis(_valid_synthesis())
    assert out["confidence"] == 0.75
    assert len(out["role_families"]) == 1
    print("[OK] _validate_synthesis accepts a fully-formed object")


def test_validate_rejects_missing_top_key(ps):
    bad = _valid_synthesis()
    del bad["geo"]
    try:
        ps._validate_synthesis(bad)
    except ValueError as e:
        assert "geo" in str(e)
        print("[OK] _validate_synthesis rejects missing top-level key")
        return
    raise AssertionError("expected ValueError for missing geo")


def test_validate_rejects_empty_role_families(ps):
    bad = _valid_synthesis()
    bad["role_families"] = []
    try:
        ps._validate_synthesis(bad)
    except ValueError as e:
        assert "role_families" in str(e)
        print("[OK] _validate_synthesis rejects empty role_families")
        return
    raise AssertionError("expected ValueError for empty role_families")


def test_validate_coerces_deal_breakers_to_lower(ps):
    obj = _valid_synthesis()
    obj["deal_breakers"] = ["SALES", " Intern ", "consulting"]
    out = ps._validate_synthesis(obj)
    assert out["deal_breakers"] == ["sales", "intern", "consulting"]
    print("[OK] deal_breakers normalized to lowercase + stripped")


def test_validate_clamps_confidence(ps):
    obj = _valid_synthesis()
    obj["confidence"] = 1.5
    assert ps._validate_synthesis(obj)["confidence"] == 1.0
    obj["confidence"] = -0.2
    assert ps._validate_synthesis(obj)["confidence"] == 0.0
    obj["confidence"] = "junk"
    assert ps._validate_synthesis(obj)["confidence"] == 0.5
    print("[OK] confidence clamped to [0, 1] (and 0.5 default for junk)")


def test_validate_coerces_missing_source_to_inferred(ps):
    obj = _valid_synthesis()
    obj["role_families"][0].pop("source", None)
    out = ps._validate_synthesis(obj)
    assert out["role_families"][0]["source"]["type"] == "inferred"
    print("[OK] missing role_family.source coerced to 'inferred'")


# ---------------------------------------------------------------------------
# synthesize_profile — happy path & failures.
# ---------------------------------------------------------------------------
def test_synthesize_returns_validated_object(ps):
    ps._call_llm = lambda *a, **k: _valid_synthesis()  # type: ignore[attr-defined]
    out = ps.synthesize_profile("CV text", {"target": {"roles": ["CRA"]}})
    assert out["confidence"] == 0.75
    assert out["role_families"][0]["label"] == "Clinical Research"
    print("[OK] synthesize_profile happy path returns validated object")


def test_synthesize_raises_when_both_llms_fail(ps):
    ps._call_llm = lambda *a, **k: None  # type: ignore[attr-defined]
    try:
        ps.synthesize_profile("CV", {})
    except ps.ProfileSynthesisError as e:
        assert "providers LLM" in str(e) or "synthèse" in str(e).lower()
        print("[OK] synthesize_profile raises ProfileSynthesisError when LLMs return None")
        return
    raise AssertionError("expected ProfileSynthesisError")


def test_synthesize_raises_on_invalid_schema(ps):
    """LLM returns valid JSON but schema is wrong → wrap in ProfileSynthesisError."""
    ps._call_llm = lambda *a, **k: {"summary_fr": "foo"}  # type: ignore[attr-defined]
    try:
        ps.synthesize_profile("CV", {})
    except ps.ProfileSynthesisError as e:
        assert "Schema invalide" in str(e) or "missing" in str(e).lower()
        print("[OK] synthesize_profile wraps validation errors in ProfileSynthesisError")
        return
    raise AssertionError("expected ProfileSynthesisError on invalid schema")


def test_synthesize_passes_previous_synthesis_to_user_msg(ps):
    """Caller can chain syntheses by passing previous_synthesis. Verify the
    user message actually carries it (so the LLM can preserve open_question
    IDs etc.)."""
    captured: list[str] = []

    def fake_call(system, user_msg, *, max_tokens=2200):
        captured.append(user_msg)
        return _valid_synthesis()

    ps._call_llm = fake_call  # type: ignore[attr-defined]
    prev = _valid_synthesis()
    prev["summary_fr"] = "PREVIOUS_MARKER"
    ps.synthesize_profile("CV", {}, previous_synthesis=prev)
    assert "PREVIOUS_MARKER" in captured[0]
    print("[OK] previous_synthesis flows into the user message")


def test_synthesize_passes_feedback_signals_to_user_msg(ps):
    captured: list[str] = []

    def fake_call(system, user_msg, *, max_tokens=2200):
        captured.append(user_msg)
        return _valid_synthesis()

    ps._call_llm = fake_call  # type: ignore[attr-defined]
    signals = [{"job_id": "j1", "status_changed_to": "rejected", "job_title": "Audit"}]
    ps.synthesize_profile("CV", {}, feedback_signals=signals)
    assert "Audit" in captured[0]
    assert "rejected" in captured[0]
    print("[OK] feedback_signals flow into the user message")


# ---------------------------------------------------------------------------
# propose_diff.
# ---------------------------------------------------------------------------
def test_propose_diff_returns_none_below_threshold(ps):
    """No LLM call should fire if signals are too few."""
    fired = {"n": 0}
    ps._call_llm = lambda *a, **k: (fired.update(n=fired["n"] + 1) or {"diff": {}})  # type: ignore[attr-defined]
    out = ps.propose_diff(_valid_synthesis(), [{"status_changed_to": "rejected"}])
    assert out is None
    assert fired["n"] == 0
    print("[OK] propose_diff returns None and skips LLM below threshold")


def test_propose_diff_calls_llm_above_threshold(ps):
    fired = {"n": 0}

    def fake_call(*a, **k):
        fired["n"] += 1
        return {
            "diff": {"add_deal_breakers": ["consulting"]},
            "rationale_fr": "5 rejects sur consulting cette semaine",
        }

    ps._call_llm = fake_call  # type: ignore[attr-defined]
    signals = [{"status_changed_to": "rejected"} for _ in range(5)]
    out = ps.propose_diff(_valid_synthesis(), signals)
    assert fired["n"] == 1
    assert out is not None
    assert "consulting" in out["diff"]["add_deal_breakers"]
    assert "5 rejects" in out["rationale_fr"]
    print("[OK] propose_diff calls LLM above threshold and returns diff+rationale")


def test_propose_diff_returns_none_when_llm_says_nothing_actionable(ps):
    ps._call_llm = lambda *a, **k: {"diff": {}, "rationale_fr": ""}  # type: ignore[attr-defined]
    signals = [{"status_changed_to": "rejected"} for _ in range(5)]
    out = ps.propose_diff(_valid_synthesis(), signals)
    assert out is None
    print("[OK] propose_diff returns None when LLM diff is empty")


# ---------------------------------------------------------------------------
# apply_diff (pure).
# ---------------------------------------------------------------------------
def test_apply_diff_adds_deal_breakers(ps):
    s = _valid_synthesis()
    out = ps.apply_diff(s, {"add_deal_breakers": ["CONSULTING", " Audit "]})
    assert "consulting" in out["deal_breakers"]
    assert "audit" in out["deal_breakers"]
    # original untouched
    assert "consulting" not in s["deal_breakers"]
    print("[OK] apply_diff adds deal_breakers (lowercased, stripped) without mutating input")


def test_apply_diff_idempotent(ps):
    s = _valid_synthesis()
    diff = {"add_deal_breakers": ["consulting"], "add_dream_companies": ["Lonza"]}
    once = ps.apply_diff(s, diff)
    twice = ps.apply_diff(once, diff)
    assert once == twice, "applying the same diff twice must be a no-op"
    assert once["deal_breakers"].count("consulting") == 1
    assert once["dream_companies"].count("Lonza") == 1
    print("[OK] apply_diff is idempotent (no duplicates on re-apply)")


def test_apply_diff_deactivate_role_family(ps):
    s = _valid_synthesis()
    out = ps.apply_diff(s, {"deactivate_role_families": ["Clinical Research"]})
    fam = next(f for f in out["role_families"] if f["label"] == "Clinical Research")
    assert fam["active"] is False
    # family is preserved (history), just deactivated
    assert len(out["role_families"]) == len(s["role_families"])
    print("[OK] apply_diff deactivates a role_family without removing it")


def test_apply_diff_adds_new_role_family(ps):
    s = _valid_synthesis()
    new_fam = {
        "label": "Pharmacovigilance",
        "titles": ["PV Officer", "Drug Safety Associate"],
    }
    out = ps.apply_diff(s, {"add_role_families": [new_fam]})
    labels = [f["label"] for f in out["role_families"]]
    assert "Pharmacovigilance" in labels
    pv = next(f for f in out["role_families"] if f["label"] == "Pharmacovigilance")
    # Defaults were filled in by apply_diff
    assert pv["active"] is True
    assert pv["weight"] == 0.7
    assert pv["source"]["type"] == "feedback"
    print("[OK] apply_diff adds a new role_family with sane defaults")


def test_apply_diff_remove_dream_company_case_insensitive(ps):
    s = _valid_synthesis()
    out = ps.apply_diff(s, {"remove_dream_companies": ["roche"]})  # lowercase input
    assert "Roche" not in out["dream_companies"]
    assert "Novartis" in out["dream_companies"]
    print("[OK] apply_diff removes dream_company case-insensitively")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ps = _import_synthesizer()
    test_validate_accepts_full_synthesis(ps)
    test_validate_rejects_missing_top_key(ps)
    test_validate_rejects_empty_role_families(ps)
    test_validate_coerces_deal_breakers_to_lower(ps)
    test_validate_clamps_confidence(ps)
    test_validate_coerces_missing_source_to_inferred(ps)

    test_synthesize_returns_validated_object(ps)
    test_synthesize_raises_when_both_llms_fail(ps)
    test_synthesize_raises_on_invalid_schema(ps)
    test_synthesize_passes_previous_synthesis_to_user_msg(ps)
    test_synthesize_passes_feedback_signals_to_user_msg(ps)

    test_propose_diff_returns_none_below_threshold(ps)
    test_propose_diff_calls_llm_above_threshold(ps)
    test_propose_diff_returns_none_when_llm_says_nothing_actionable(ps)

    test_apply_diff_adds_deal_breakers(ps)
    test_apply_diff_idempotent(ps)
    test_apply_diff_deactivate_role_family(ps)
    test_apply_diff_adds_new_role_family(ps)
    test_apply_diff_remove_dream_company_case_insensitive(ps)

    print("\nAll profile_synthesizer tests passed.")
