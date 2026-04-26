"""Smoke tests for the action-bar / role-family helpers in 2_dashboard.py
(PR4.b + PR4.c).

We don't load the full dashboard module (its top-level calls Supabase / auth),
so we extract the pure helpers via `ast` + `exec` into an isolated namespace
with a minimal `streamlit` stub.

Covered helpers :
    _parse_iso(value)             — ISO string / datetime → tz-aware datetime
    _format_relative(dt, *, now)  — "il y a Xh / Xj" labels
    _last_run_summary(matches)    — (label, fresh_24h_count) from match rows
    _group_by_family(matches)     — [(label_or_None, count)] sorted

Run from the repo root :
    PYTHONPATH=. python app/pages/test_dashboard_action_bar.py
"""
from __future__ import annotations

import ast
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Streamlit stub — the helpers don't render anything, but the AST module we
# exec captures `st` as a free variable, so we feed in a no-op stub.
# ---------------------------------------------------------------------------
def _build_streamlit_stub() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.markdown = lambda *a, **k: None
    st.session_state = {}
    return st


# ---------------------------------------------------------------------------
# Loader — slice the helpers out of the real source. Avoids the page's
# top-level setup_authed_page() call.
# ---------------------------------------------------------------------------
_HELPERS = {
    "_parse_iso",
    "_format_relative",
    "_last_run_summary",
    "_group_by_family",
}


def _load_ns() -> dict:
    src = (ROOT / "app" / "pages" / "2_dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _HELPERS
    ]
    assert {f.name for f in fns} == _HELPERS, (
        f"missing helpers: {_HELPERS - {f.name for f in fns}}"
    )
    # The helpers reference _NO_LAST_RUN and _NO_FAMILY_LABEL as module
    # constants — replicate them in the namespace so exec() doesn't NameError.
    module = ast.Module(body=fns, type_ignores=[])
    ns: dict = {
        "st": _build_streamlit_stub(),
        "datetime": datetime,
        "timezone": timezone,
        "_NO_LAST_RUN": "Jamais lancé",
        "_NO_FAMILY_LABEL": "Sans famille",
    }
    exec(compile(module, "2_dashboard.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------
def test_parse_iso_handles_supabase_z_suffix():
    """Supabase serializes timestamptz with a 'Z' suffix on some clients;
    Python <3.11 fromisoformat doesn't accept it. _parse_iso must coerce."""
    ns = _load_ns()
    out = ns["_parse_iso"]("2026-04-26T12:00:00Z")
    assert out is not None
    assert out.tzinfo is not None
    assert out.year == 2026 and out.month == 4 and out.day == 26
    print("[OK] _parse_iso accepts trailing 'Z' (Supabase timestamptz)")


def test_parse_iso_handles_offset_form():
    ns = _load_ns()
    out = ns["_parse_iso"]("2026-04-26T12:00:00+00:00")
    assert out is not None
    assert out.year == 2026
    print("[OK] _parse_iso accepts '+00:00' offset form")


def test_parse_iso_handles_naive_datetime():
    """A naive datetime gets coerced to UTC so the relative-format math doesn't
    blow up (subtraction between naive + aware datetimes raises in Python)."""
    ns = _load_ns()
    naive = datetime(2026, 4, 26, 10, 0, 0)
    out = ns["_parse_iso"](naive)
    assert out is not None and out.tzinfo is not None
    print("[OK] _parse_iso coerces naive datetime to UTC")


def test_parse_iso_robust_to_garbage():
    """None / non-ISO / empty / wrong-type → None, never raise."""
    ns = _load_ns()
    parse = ns["_parse_iso"]
    assert parse(None) is None
    assert parse("") is None
    assert parse("not-a-date") is None
    assert parse({"x": 1}) is None
    assert parse(42) is None
    print("[OK] _parse_iso returns None on bad input (no exceptions)")


# ---------------------------------------------------------------------------
# _format_relative
# ---------------------------------------------------------------------------
def test_format_relative_buckets():
    """All five buckets from the docstring."""
    ns = _load_ns()
    fmt = ns["_format_relative"]
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)

    assert fmt(None) == "—"
    # < 60s : "à l'instant"
    assert fmt(now - timedelta(seconds=10), now=now) == "à l'instant"
    # 23 min ago
    assert fmt(now - timedelta(minutes=23), now=now) == "il y a 23 min"
    # 5h ago
    assert fmt(now - timedelta(hours=5), now=now) == "il y a 5 h"
    # 3 days ago
    assert fmt(now - timedelta(days=3), now=now) == "il y a 3 j"
    # >30j → ISO date
    far = now - timedelta(days=45)
    assert fmt(far, now=now) == f"le {far.strftime('%Y-%m-%d')}"
    print("[OK] _format_relative buckets (instant / min / h / j / date)")


def test_format_relative_handles_clock_skew():
    """If dt is in the future (server clock ahead of client clock for example),
    don't render 'in N min' — just 'à l'instant'."""
    ns = _load_ns()
    fmt = ns["_format_relative"]
    now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=timezone.utc)
    future = now + timedelta(minutes=5)
    assert fmt(future, now=now) == "à l'instant"
    print("[OK] _format_relative tolerates clock skew (future dt)")


# ---------------------------------------------------------------------------
# _last_run_summary
# ---------------------------------------------------------------------------
def test_last_run_summary_empty():
    ns = _load_ns()
    label, fresh = ns["_last_run_summary"]([])
    assert label == "Jamais lancé"
    assert fresh == 0
    print("[OK] _last_run_summary on empty list → 'Jamais lancé'")


def test_last_run_summary_counts_24h_only():
    """Only matches in the last 24h count toward `fresh`."""
    ns = _load_ns()
    now = datetime.now(timezone.utc)
    matches = [
        {"scored_at": (now - timedelta(hours=2)).isoformat()},   # fresh
        {"scored_at": (now - timedelta(hours=10)).isoformat()},  # fresh
        {"scored_at": (now - timedelta(hours=30)).isoformat()},  # too old
        {"scored_at": (now - timedelta(days=5)).isoformat()},    # too old
    ]
    label, fresh = ns["_last_run_summary"](matches)
    assert fresh == 2, fresh
    # The label is computed off the most-recent timestamp (2h ago).
    assert "il y a 2 h" in label or "il y a 1 h" in label, label
    print(f"[OK] _last_run_summary: 2 fresh in 24h, label={label!r}")


def test_last_run_summary_skips_unparseable_rows():
    """Rows with missing / bad scored_at are silently dropped — they don't
    count toward fresh, and they don't break the max() lookup."""
    ns = _load_ns()
    now = datetime.now(timezone.utc)
    matches = [
        {"scored_at": None},
        {"scored_at": "garbage"},
        {"scored_at": (now - timedelta(hours=3)).isoformat()},
    ]
    label, fresh = ns["_last_run_summary"](matches)
    assert fresh == 1
    assert "h" in label  # roughly "il y a 3 h"
    print("[OK] _last_run_summary skips unparseable rows without crashing")


def test_last_run_summary_all_unparseable_falls_back_to_no_run():
    """If every row has a bad scored_at, treat as 'never ran'."""
    ns = _load_ns()
    matches = [{"scored_at": None}, {"scored_at": ""}, {"foo": "bar"}]
    label, fresh = ns["_last_run_summary"](matches)
    assert label == "Jamais lancé"
    assert fresh == 0
    print("[OK] _last_run_summary falls back to 'Jamais lancé' when all rows unparseable")


def test_last_run_summary_uses_scraper_runs_when_user_id_given():
    """FIX-5 — when user_id is provided AND scraper_runs has a relevant row,
    that row's started_at drives the 'last run' label, not max(scored_at)."""
    ns = _load_ns()
    now = datetime.now(timezone.utc)
    run_started = (now - timedelta(hours=2)).isoformat()
    fake = types.ModuleType("app.lib.scraper_runs_store")
    fake.get_last_run_for_user = lambda uid: {
        "started_at": run_started,
        "status": "ok",
        "runner": "manual",
    }
    sys.modules["app.lib.scraper_runs_store"] = fake
    try:
        # No matches at all → without telemetry, label would be 'Jamais lancé'.
        # With telemetry, label should reflect the scraper_runs row.
        label, fresh = ns["_last_run_summary"]([], user_id="uid-abc")
        assert label != "Jamais lancé", label
        assert "h" in label, label  # "il y a 2 h" or similar
        assert fresh == 0
    finally:
        sys.modules.pop("app.lib.scraper_runs_store", None)
    print("[OK] _last_run_summary: telemetry row drives label (FIX-5)")


def test_last_run_summary_falls_back_when_telemetry_raises():
    """FIX-5 — if scraper_runs_store raises or returns None, the function
    must fall back to max(scored_at)."""
    ns = _load_ns()
    fake = types.ModuleType("app.lib.scraper_runs_store")

    def _boom(uid):
        raise RuntimeError("postgrest 500")

    fake.get_last_run_for_user = _boom
    sys.modules["app.lib.scraper_runs_store"] = fake
    try:
        now = datetime.now(timezone.utc)
        matches = [{"scored_at": (now - timedelta(hours=3)).isoformat()}]
        label, fresh = ns["_last_run_summary"](matches, user_id="uid-x")
        assert label != "Jamais lancé", label
        assert fresh == 1
    finally:
        sys.modules.pop("app.lib.scraper_runs_store", None)
    print("[OK] _last_run_summary: telemetry exception → falls back to scored_at")


def test_last_run_summary_falls_back_when_telemetry_returns_none():
    """FIX-5 — telemetry returns None (no row for this user) → fall back to
    max(scored_at). Regression : early code-paths that prematurely return
    'Jamais lancé' even though scored_at would resolve."""
    ns = _load_ns()
    fake = types.ModuleType("app.lib.scraper_runs_store")
    fake.get_last_run_for_user = lambda uid: None
    sys.modules["app.lib.scraper_runs_store"] = fake
    try:
        now = datetime.now(timezone.utc)
        matches = [{"scored_at": (now - timedelta(hours=4)).isoformat()}]
        label, fresh = ns["_last_run_summary"](matches, user_id="uid-y")
        assert label != "Jamais lancé", label
        assert fresh == 1
    finally:
        sys.modules.pop("app.lib.scraper_runs_store", None)
    print("[OK] _last_run_summary: telemetry None → falls back to scored_at")


# ---------------------------------------------------------------------------
# _group_by_family
# ---------------------------------------------------------------------------
def test_group_by_family_counts_and_sorts():
    """Real labels first by count desc, then alpha; "Sans famille" (None) last."""
    ns = _load_ns()
    matches = [
        {"analysis": {"matched_role_family": "Data Engineer"}},
        {"analysis": {"matched_role_family": "Data Engineer"}},
        {"analysis": {"matched_role_family": "Data Engineer"}},
        {"analysis": {"matched_role_family": "ML Engineer"}},
        {"analysis": {"matched_role_family": "ML Engineer"}},
        {"analysis": {"matched_role_family": "Cloud Architect"}},
        {"analysis": {}},  # Sans famille
        {"analysis": {"matched_role_family": None}},  # Sans famille
        {"analysis": {"matched_role_family": ""}},  # empty → Sans famille
    ]
    out = ns["_group_by_family"](matches)
    assert out == [
        ("Data Engineer", 3),
        ("ML Engineer", 2),
        ("Cloud Architect", 1),
        (None, 3),
    ], out
    print("[OK] _group_by_family: 4 buckets, sorted, None bucket last")


def test_group_by_family_empty():
    ns = _load_ns()
    assert ns["_group_by_family"]([]) == []
    print("[OK] _group_by_family on empty list → []")


def test_group_by_family_no_analysis_key():
    """Match rows without an `analysis` field at all → all in Sans famille."""
    ns = _load_ns()
    matches = [{}, {"score": 7}, {"job_id": "x"}]
    out = ns["_group_by_family"](matches)
    assert out == [(None, 3)], out
    print("[OK] _group_by_family: rows without analysis → 'Sans famille' bucket")


def test_group_by_family_alphabetical_tiebreaker():
    """Same count → labels sort alphabetically (deterministic UI)."""
    ns = _load_ns()
    matches = [
        {"analysis": {"matched_role_family": "Zeta"}},
        {"analysis": {"matched_role_family": "Alpha"}},
        {"analysis": {"matched_role_family": "Mu"}},
    ]
    out = ns["_group_by_family"](matches)
    # All count=1 → alpha order.
    assert [k for k, _ in out] == ["Alpha", "Mu", "Zeta"], out
    print("[OK] _group_by_family ties broken alphabetically")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_parse_iso_handles_supabase_z_suffix()
    test_parse_iso_handles_offset_form()
    test_parse_iso_handles_naive_datetime()
    test_parse_iso_robust_to_garbage()
    test_format_relative_buckets()
    test_format_relative_handles_clock_skew()
    test_last_run_summary_empty()
    test_last_run_summary_counts_24h_only()
    test_last_run_summary_skips_unparseable_rows()
    test_last_run_summary_all_unparseable_falls_back_to_no_run()
    test_last_run_summary_uses_scraper_runs_when_user_id_given()
    test_last_run_summary_falls_back_when_telemetry_raises()
    test_last_run_summary_falls_back_when_telemetry_returns_none()
    test_group_by_family_counts_and_sorts()
    test_group_by_family_empty()
    test_group_by_family_no_analysis_key()
    test_group_by_family_alphabetical_tiebreaker()
    print("\nAll dashboard action-bar tests passed.")
