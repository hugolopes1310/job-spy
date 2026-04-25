"""Tests for the _UNSET sentinel in jobs_store.update_match.

The function distinguishes three semantics for date fields :
  - field omitted              → not in the patch (don't touch DB)
  - field=None  (explicit)     → in patch with NULL (clear DB column)
  - field=<date or ISO string> → in patch with the value (write DB column)

For other fields (status, notes, is_favorite, feedback) the convention is
simpler: None == omitted (don't touch). These cases are covered too as a
regression guard.

Run from the repo root:
    PYTHONPATH=. python app/lib/test_jobs_store_update.py
"""
from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Capture the patch payload sent to Supabase by stubbing _client → table chain.
# ---------------------------------------------------------------------------
_LAST_PATCH: dict | None = None
_CALL_COUNT = 0


class _FakeQuery:
    def __init__(self):
        global _LAST_PATCH
        _LAST_PATCH = None

    def update(self, patch):
        global _LAST_PATCH, _CALL_COUNT
        _LAST_PATCH = dict(patch)
        _CALL_COUNT += 1
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        class _R:
            data = []
        return _R()


class _FakeTable:
    def __init__(self):
        self._q = _FakeQuery()

    def update(self, patch):
        return self._q.update(patch)


class _FakeClient:
    def table(self, name):
        return _FakeTable()


def _reset():
    global _LAST_PATCH, _CALL_COUNT
    _LAST_PATCH = None
    _CALL_COUNT = 0


# ---------------------------------------------------------------------------
# Build a streamlit stub (jobs_store does not import streamlit but the
# supabase_client module it imports might — keep it cheap).
# ---------------------------------------------------------------------------
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
    """Stub the project's `app.lib.supabase_client` module so jobs_store loads
    without trying to read Streamlit secrets / network."""
    mod = types.ModuleType("app.lib.supabase_client")
    mod.get_client          = lambda *a, **k: _FakeClient()
    mod.get_service_client  = lambda *a, **k: _FakeClient()
    mod.get_anon_client     = lambda *a, **k: _FakeClient()
    mod.SUPABASE_AVAILABLE  = True
    return mod


def _build_auth_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.auth")
    mod.get_user_scoped_client = lambda *a, **k: _FakeClient()
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
    # Wipe any previously-imported jobs_store so our stubs apply.
    sys.modules.pop("app.lib.jobs_store", None)
    from app.lib import jobs_store  # noqa: E402
    return jobs_store


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------
def test_omitted_dates_not_in_patch(js):
    """update_match(uid, jid, status='applied') → patch has only 'status'."""
    _reset()
    js.update_match("u", "j", status="applied")
    assert _LAST_PATCH == {"status": "applied"}, _LAST_PATCH
    assert "applied_at" not in _LAST_PATCH
    assert "next_action_at" not in _LAST_PATCH
    print("[OK] omitted date fields stay out of the patch (sentinel honored)")


def test_explicit_none_clears_dates(js):
    """update_match(uid, jid, applied_at=None) → patch has 'applied_at': None.

    Same for next_action_at — explicit None means "clear this column", which is
    a meaningful state distinct from "don't touch".
    """
    _reset()
    js.update_match("u", "j", applied_at=None)
    assert _LAST_PATCH == {"applied_at": None}, _LAST_PATCH

    _reset()
    js.update_match("u", "j", next_action_at=None)
    assert _LAST_PATCH == {"next_action_at": None}, _LAST_PATCH
    print("[OK] explicit None clears date columns (NULL in DB)")


def test_date_value_coerced_and_written(js):
    """Passing a date / datetime / ISO string all coerce to ISO and land in patch."""
    _reset()
    js.update_match("u", "j", applied_at=date(2026, 4, 20))
    assert _LAST_PATCH == {"applied_at": "2026-04-20"}, _LAST_PATCH

    _reset()
    js.update_match("u", "j", next_action_at="2026-05-02")
    assert _LAST_PATCH == {"next_action_at": "2026-05-02"}, _LAST_PATCH
    print("[OK] date / ISO string values reach the patch as ISO-8601")


def test_empty_call_short_circuits(js):
    """update_match(uid, jid) (nothing to update) → no DB call at all."""
    _reset()
    js.update_match("u", "j")
    assert _LAST_PATCH is None, _LAST_PATCH
    assert _CALL_COUNT == 0, f"expected 0 update calls, got {_CALL_COUNT}"
    print("[OK] empty patch short-circuits the network call")


def test_string_field_none_is_omitted(js):
    """For non-sentinel fields (notes/status/feedback), None means 'don't touch'."""
    _reset()
    js.update_match("u", "j", notes=None, status=None, feedback=None)
    assert _LAST_PATCH is None, _LAST_PATCH
    assert _CALL_COUNT == 0
    print("[OK] None on string fields is treated as 'omit'")


def test_mixed_call_only_changed_fields(js):
    """Mixing a string change with explicit None on a date — both must land."""
    _reset()
    js.update_match("u", "j", notes="hello", applied_at=None)
    assert _LAST_PATCH == {"notes": "hello", "applied_at": None}, _LAST_PATCH
    print("[OK] mixed string + cleared-date patch shapes correctly")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    js = _import_jobs_store()
    test_omitted_dates_not_in_patch(js)
    test_explicit_none_clears_dates(js)
    test_date_value_coerced_and_written(js)
    test_empty_call_short_circuits(js)
    test_string_field_none_is_omitted(js)
    test_mixed_call_only_changed_fields(js)
    print("\nAll update_match sentinel tests passed.")
