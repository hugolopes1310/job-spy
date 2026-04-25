"""Tests for the _with_retry helper in jobs_store.

Run from the repo root:
    PYTHONPATH=. python app/lib/test_jobs_store_retry.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


def _build_streamlit_stub() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.cache_data       = lambda *a, **k: (lambda f: f)
    st.cache_resource   = lambda *a, **k: (lambda f: f)
    st.session_state    = {}
    st.error            = lambda *a, **k: None
    st.warning          = lambda *a, **k: None
    st.info             = lambda *a, **k: None
    st.stop             = lambda: None
    st.secrets          = {}
    return st


def _build_supabase_client_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.supabase_client")
    mod.get_client          = lambda *a, **k: None
    mod.get_service_client  = lambda *a, **k: None
    mod.get_anon_client     = lambda *a, **k: None
    mod.SUPABASE_AVAILABLE  = True
    return mod


def _build_auth_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.auth")
    mod.get_user_scoped_client = lambda *a, **k: None
    return mod


def _build_scrapers_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.scrapers")
    mod.job_id_for = lambda *a, **k: "stub-id"
    return mod


def _import_jobs_store():
    sys.modules["streamlit"]                = _build_streamlit_stub()
    sys.modules["app.lib.supabase_client"]  = _build_supabase_client_stub()
    sys.modules["app.lib.auth"]             = _build_auth_stub()
    sys.modules["app.lib.scrapers"]         = _build_scrapers_stub()
    sys.modules.pop("app.lib.jobs_store", None)
    from app.lib import jobs_store  # noqa: E402
    return jobs_store


# ---------------------------------------------------------------------------
# Fake errors for variety.
# ---------------------------------------------------------------------------
class _PostgrestErr(Exception):
    """Mimics supabase-py / postgrest APIError shape: has .code attribute."""

    def __init__(self, message: str, code: int | str = ""):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------
def test_succeeds_first_try(js):
    calls = {"n": 0}
    def _fn():
        calls["n"] += 1
        return "ok"
    assert js._with_retry(_fn, op="t.success") == "ok"
    assert calls["n"] == 1
    print("[OK] no error → fn called once")


def test_retries_then_succeeds(js):
    """Two transient failures, then success on the third call."""
    calls = {"n": 0}
    def _fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _PostgrestErr("upstream timeout", code=504)
        return "ok"
    # base_delay=0 to keep tests fast.
    assert js._with_retry(_fn, op="t.transient", base_delay=0, max_delay=0) == "ok"
    assert calls["n"] == 3
    print("[OK] retries 504s up to limit and returns success")


def test_gives_up_after_attempts(js):
    calls = {"n": 0}
    def _fn():
        calls["n"] += 1
        raise _PostgrestErr("upstream timeout", code=504)
    raised = False
    try:
        js._with_retry(_fn, op="t.exhausted", attempts=3, base_delay=0, max_delay=0)
    except _PostgrestErr:
        raised = True
    assert raised, "expected the original error to bubble up after all retries"
    assert calls["n"] == 3
    print("[OK] re-raises after exhausting attempts")


def test_does_not_retry_permanent_error(js):
    """4xx-ish errors with no transient signal must surface immediately."""
    calls = {"n": 0}
    def _fn():
        calls["n"] += 1
        raise _PostgrestErr("permission denied", code=403)
    raised = False
    try:
        js._with_retry(_fn, op="t.permanent", attempts=3, base_delay=0, max_delay=0)
    except _PostgrestErr:
        raised = True
    assert raised
    assert calls["n"] == 1, f"expected 1 call, got {calls['n']}"
    print("[OK] permanent errors (403) bubble up without retry")


def test_string_pattern_detection(js):
    """Detect transient errors purely from str(e) when no code attribute."""
    calls = {"n": 0}
    def _fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Connection reset by peer")
        return "ok"
    assert js._with_retry(_fn, op="t.string_match", base_delay=0, max_delay=0) == "ok"
    assert calls["n"] == 2
    print("[OK] string-match retries 'connection reset'")


def test_is_transient_error(js):
    """Direct check on the predicate."""
    assert js._is_transient_error(_PostgrestErr("x", code=503))
    assert js._is_transient_error(RuntimeError("Read timed out"))
    assert js._is_transient_error(RuntimeError("HTTP 502 bad gateway"))
    assert not js._is_transient_error(_PostgrestErr("not found", code=404))
    assert not js._is_transient_error(ValueError("bad payload"))
    print("[OK] _is_transient_error classifies correctly")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    js = _import_jobs_store()
    test_succeeds_first_try(js)
    test_retries_then_succeeds(js)
    test_gives_up_after_attempts(js)
    test_does_not_retry_permanent_error(js)
    test_string_pattern_detection(js)
    test_is_transient_error(js)
    print("\nAll retry tests passed.")
