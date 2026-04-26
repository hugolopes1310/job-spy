"""Tests for scraper_runs_store.py — the Phase 5 telemetry CRUD.

Two angles :

1. Happy-path shape — start_run / finish_run / get_last_run / list_recent_runs
   each call the right Supabase chain with the expected payload.

2. Defensive behavior — the docstring says "telemetry MUST NEVER raise". We
   simulate Supabase blowing up (raise inside .execute()) and assert the
   helpers swallow + return their sentinels (None / False / []).

The Supabase client is stubbed by injecting `app.lib.supabase_client` into
`sys.modules` BEFORE importing scraper_runs_store. The stub returns a fake
client whose `.table(...)` chain records every call into a module-level list.

Run from the repo root :
    PYTHONPATH=. python app/lib/test_scraper_runs_store.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Capture state.
# ---------------------------------------------------------------------------
# Each test resets these via _reset(). We track :
#   _CALLS : flat list of (verb, args) tuples in the order they happen, so we
#            can assert e.g. insert(...) → execute() with no extra ops.
#   _NEXT_DATA / _NEXT_RAISE : drive what the fake's .execute() returns or
#            whether it raises — set per-test.
# ---------------------------------------------------------------------------
_CALLS: list[tuple] = []
_NEXT_DATA: list[dict] = []
_NEXT_RAISE: Exception | None = None


def _reset() -> None:
    global _CALLS, _NEXT_DATA, _NEXT_RAISE
    _CALLS = []
    _NEXT_DATA = []
    _NEXT_RAISE = None


class _FakeQuery:
    """Fluent stub matching the slice of supabase-py's PostgREST builder we use."""
    def insert(self, payload):
        _CALLS.append(("insert", payload))
        return self

    def update(self, patch):
        _CALLS.append(("update", patch))
        return self

    def select(self, cols):
        _CALLS.append(("select", cols))
        return self

    def eq(self, col, val):
        _CALLS.append(("eq", col, val))
        return self

    def order(self, col, desc=False):
        _CALLS.append(("order", col, desc))
        return self

    def limit(self, n):
        _CALLS.append(("limit", n))
        return self

    def execute(self):
        _CALLS.append(("execute",))
        if _NEXT_RAISE is not None:
            raise _NEXT_RAISE

        class _Res:
            data = list(_NEXT_DATA)

        return _Res()


class _FakeClient:
    def table(self, name):
        _CALLS.append(("table", name))
        return _FakeQuery()


# ---------------------------------------------------------------------------
# Stub installer — must run BEFORE the first `from app.lib import …` import.
# ---------------------------------------------------------------------------
def _build_supabase_client_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.supabase_client")
    mod.get_service_client = lambda: _FakeClient()
    mod.get_client         = lambda *a, **k: _FakeClient()
    mod.get_anon_client    = lambda *a, **k: _FakeClient()
    mod.SUPABASE_AVAILABLE = True
    return mod


def _build_klog_stub() -> types.ModuleType:
    """klog.log is called on the error path — make it a no-op so the test
    output stays clean."""
    mod = types.ModuleType("app.lib.klog")
    mod.log = lambda *a, **k: None
    mod.bind = lambda *a, **k: types.SimpleNamespace(
        info=lambda *a, **k: None,
        warn=lambda *a, **k: None,
        error=lambda *a, **k: None,
        bind=lambda *a, **k: None,
    )
    return mod


def _import_store():
    sys.modules["app.lib.supabase_client"] = _build_supabase_client_stub()
    sys.modules["app.lib.klog"] = _build_klog_stub()
    sys.modules.pop("app.lib.scraper_runs_store", None)
    from app.lib import scraper_runs_store  # noqa: E402
    return scraper_runs_store


# ---------------------------------------------------------------------------
# start_run
# ---------------------------------------------------------------------------
def test_start_run_inserts_row_and_returns_id(s):
    """Happy path: row inserted with status='running' + returned id."""
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = [{"id": "abc-123"}]
    rid = s.start_run(runner="cron")
    assert rid == "abc-123", rid

    # First three calls must be: table(scraper_runs), insert(payload), execute().
    assert _CALLS[0] == ("table", "scraper_runs"), _CALLS
    assert _CALLS[1][0] == "insert"
    payload = _CALLS[1][1]
    assert payload["runner"] == "cron"
    assert payload["status"] == "running"
    assert "started_at" in payload
    assert payload["totals"] == {}
    assert payload["errors"] == []
    # No optional cols leaked when the caller didn't set them.
    assert "triggered_by_user_id" not in payload
    assert "notes" not in payload
    assert _CALLS[2] == ("execute",)
    print("[OK] start_run: inserts {runner='running', status='running'} and returns id")


def test_start_run_with_manual_runner_and_user(s):
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = [{"id": "row-7"}]
    rid = s.start_run(
        runner="manual",
        triggered_by_user_id="uuid-user",
        notes="dashboard click",
    )
    assert rid == "row-7"
    payload = _CALLS[1][1]
    assert payload["runner"] == "manual"
    assert payload["triggered_by_user_id"] == "uuid-user"
    assert payload["notes"] == "dashboard click"
    print("[OK] start_run: manual runner carries triggered_by_user_id + notes")


def test_start_run_bad_runner_falls_back_to_cli(s):
    """Invalid runner → coerced to 'cli', NOT raised."""
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = [{"id": "row-8"}]
    rid = s.start_run(runner="bogus")  # type: ignore[arg-type]
    assert rid == "row-8"
    payload = _CALLS[1][1]
    assert payload["runner"] == "cli", payload["runner"]
    print("[OK] start_run: bad runner falls back to 'cli'")


def test_start_run_bounds_notes_to_500(s):
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = [{"id": "row-9"}]
    s.start_run(notes="x" * 1000)
    payload = _CALLS[1][1]
    assert "notes" in payload
    assert len(payload["notes"]) == 500, len(payload["notes"])
    print("[OK] start_run: notes truncated to 500 chars")


def test_start_run_no_data_returned_returns_none(s):
    """Supabase returned an empty list (insert succeeded but RETURNING got
    swallowed somewhere) — start_run logs + returns None, never raises."""
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = []
    rid = s.start_run()
    assert rid is None
    print("[OK] start_run: empty .data → None (logged, not raised)")


def test_start_run_swallows_exceptions(s):
    """Supabase blows up → return None, swallow."""
    _reset()
    global _NEXT_RAISE
    _NEXT_RAISE = RuntimeError("boom")
    rid = s.start_run()
    assert rid is None
    print("[OK] start_run: exception swallowed → None")


# ---------------------------------------------------------------------------
# finish_run
# ---------------------------------------------------------------------------
def test_finish_run_happy_path(s):
    _reset()
    ok = s.finish_run(
        "row-1",
        status="ok",
        totals={"scored": 5},
        errors=[{"stage": "x"}],
        llm_quota={"all_exhausted": False},
        notes="ran fine",
    )
    assert ok is True
    # table → update(patch) → eq(id, row-1) → execute
    assert _CALLS[0] == ("table", "scraper_runs")
    assert _CALLS[1][0] == "update"
    patch = _CALLS[1][1]
    assert patch["status"] == "ok"
    assert patch["totals"] == {"scored": 5}
    assert patch["errors"] == [{"stage": "x"}]
    assert patch["llm_quota"] == {"all_exhausted": False}
    assert "finished_at" in patch
    assert _CALLS[2] == ("eq", "id", "row-1"), _CALLS[2]
    assert _CALLS[3] == ("execute",)
    print("[OK] finish_run: writes patch with status/totals/errors/quota/notes")


def test_finish_run_no_op_when_id_is_none(s):
    """run_id=None → caller's start_run failed ; finish_run must do nothing."""
    _reset()
    ok = s.finish_run(None, status="ok")
    assert ok is False
    assert _CALLS == [], _CALLS
    print("[OK] finish_run: None run_id → no DB call (start failed earlier)")


def test_finish_run_bad_status_coerces_to_failed(s):
    _reset()
    ok = s.finish_run("row-1", status="weird")  # type: ignore[arg-type]
    assert ok is True
    patch = _CALLS[1][1]
    assert patch["status"] == "failed", patch["status"]
    print("[OK] finish_run: invalid status coerced to 'failed' (be conservative)")


def test_finish_run_truncates_errors(s):
    """Errors list bounded to _MAX_ERRORS_PER_RUN client-side."""
    _reset()
    huge = [{"i": i} for i in range(200)]
    ok = s.finish_run("row-1", status="partial", errors=huge)
    assert ok is True
    patch = _CALLS[1][1]
    assert len(patch["errors"]) == s._MAX_ERRORS_PER_RUN
    assert patch["errors"][0] == {"i": 0}  # head kept
    print(
        f"[OK] finish_run: errors truncated to first {s._MAX_ERRORS_PER_RUN} "
        f"(was 200)"
    )


def test_finish_run_swallows_exceptions(s):
    """Supabase blows up → return False, no raise."""
    _reset()
    global _NEXT_RAISE
    _NEXT_RAISE = RuntimeError("network down")
    ok = s.finish_run("row-1", status="ok")
    assert ok is False
    print("[OK] finish_run: exception swallowed → False")


def test_finish_run_omits_optional_keys_when_unset(s):
    """`llm_quota` / `notes` not passed → not in patch (don't overwrite cols)."""
    _reset()
    s.finish_run("row-1", status="ok", totals={"x": 1})
    patch = _CALLS[1][1]
    assert "llm_quota" not in patch
    assert "notes" not in patch
    print("[OK] finish_run: omitted llm_quota / notes stay out of the patch")


# ---------------------------------------------------------------------------
# get_last_run
# ---------------------------------------------------------------------------
def test_get_last_run_returns_first_row(s):
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = [{"id": "r1", "status": "ok"}, {"id": "r2"}]
    row = s.get_last_run()
    assert row == {"id": "r1", "status": "ok"}
    # table → select(*) → order(started_at, desc=True) → limit(1) → execute
    assert _CALLS[0] == ("table", "scraper_runs")
    assert _CALLS[1] == ("select", "*")
    assert _CALLS[2] == ("order", "started_at", True)
    assert _CALLS[3] == ("limit", 1)
    assert _CALLS[4] == ("execute",)
    print("[OK] get_last_run: returns first row of newest-first query")


def test_get_last_run_with_status_filter(s):
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = [{"id": "r9", "status": "ok"}]
    row = s.get_last_run(only_status="ok")
    assert row["id"] == "r9"
    # An eq() must appear in the chain.
    eq_calls = [c for c in _CALLS if c[0] == "eq"]
    assert eq_calls == [("eq", "status", "ok")], eq_calls
    print("[OK] get_last_run: only_status='ok' adds .eq('status', 'ok')")


def test_get_last_run_empty_table(s):
    _reset()
    row = s.get_last_run()
    assert row is None
    print("[OK] get_last_run: empty table → None")


def test_get_last_run_swallows_exceptions(s):
    _reset()
    global _NEXT_RAISE
    _NEXT_RAISE = RuntimeError("supabase 500")
    row = s.get_last_run()
    assert row is None
    print("[OK] get_last_run: exception swallowed → None")


# ---------------------------------------------------------------------------
# list_recent_runs
# ---------------------------------------------------------------------------
def test_list_recent_runs_default(s):
    _reset()
    global _NEXT_DATA
    _NEXT_DATA = [{"id": f"r{i}"} for i in range(3)]
    rows = s.list_recent_runs()
    assert len(rows) == 3
    # default limit is 20.
    limit_calls = [c for c in _CALLS if c[0] == "limit"]
    assert limit_calls == [("limit", 20)], limit_calls
    print("[OK] list_recent_runs: default limit=20")


def test_list_recent_runs_bounds_limit(s):
    """limit is clamped to [1, 200] regardless of caller input."""
    _reset()
    s.list_recent_runs(limit=9999)
    assert ("limit", 200) in _CALLS

    _reset()
    s.list_recent_runs(limit=0)
    assert ("limit", 1) in _CALLS

    _reset()
    s.list_recent_runs(limit=-50)
    assert ("limit", 1) in _CALLS
    print("[OK] list_recent_runs: limit clamped to [1, 200]")


def test_list_recent_runs_swallows_exceptions(s):
    _reset()
    global _NEXT_RAISE
    _NEXT_RAISE = RuntimeError("offline")
    rows = s.list_recent_runs()
    assert rows == []
    print("[OK] list_recent_runs: exception swallowed → []")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    s = _import_store()

    # start_run
    test_start_run_inserts_row_and_returns_id(s)
    test_start_run_with_manual_runner_and_user(s)
    test_start_run_bad_runner_falls_back_to_cli(s)
    test_start_run_bounds_notes_to_500(s)
    test_start_run_no_data_returned_returns_none(s)
    test_start_run_swallows_exceptions(s)

    # finish_run
    test_finish_run_happy_path(s)
    test_finish_run_no_op_when_id_is_none(s)
    test_finish_run_bad_status_coerces_to_failed(s)
    test_finish_run_truncates_errors(s)
    test_finish_run_swallows_exceptions(s)
    test_finish_run_omits_optional_keys_when_unset(s)

    # get_last_run
    test_get_last_run_returns_first_row(s)
    test_get_last_run_with_status_filter(s)
    test_get_last_run_empty_table(s)
    test_get_last_run_swallows_exceptions(s)

    # list_recent_runs
    test_list_recent_runs_default(s)
    test_list_recent_runs_bounds_limit(s)
    test_list_recent_runs_swallows_exceptions(s)

    print("\nAll scraper_runs_store tests passed.")
