"""Tests for _persist_changes — the diff/save core of the Suivi page.

Run from the repo root:
    PYTHONPATH=. python app/pages/test_suivi_persist.py

Reuses the same Streamlit/page_setup/jobs_store/theme stubs as
test_suivi_kanban.py, but exposes spies on toggle_favorite/update_match so we
can assert what was sent to the DB.
"""
from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Streamlit / project stubs (mirrors test_suivi_kanban.py — kept inline so each
# test file can run standalone without an import cycle).
# ---------------------------------------------------------------------------
class _ColumnStub:
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def button(self, *a, **k): return False
    def link_button(self, *a, **k): return None
    def markdown(self, *a, **k): pass
    def caption(self, *a, **k): pass


def _fake_columns(spec, **kwargs):
    n = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
    return tuple(_ColumnStub() for _ in range(n))


def _fake_container(border=False):
    return _ColumnStub()


def _fake_dialog(title, **kwargs):
    def _decorator(func):
        return func
    return _decorator


class _ColumnConfig:
    @staticmethod
    def _passthrough(*a, **k):
        return ("col", a, k)
    CheckboxColumn = staticmethod(_passthrough.__func__)
    TextColumn     = staticmethod(_passthrough.__func__)
    NumberColumn   = staticmethod(_passthrough.__func__)
    SelectboxColumn = staticmethod(_passthrough.__func__)
    DateColumn     = staticmethod(_passthrough.__func__)
    LinkColumn     = staticmethod(_passthrough.__func__)


def _build_streamlit_stub() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.session_state = {}
    st.markdown        = lambda *a, **k: None
    st.caption         = lambda *a, **k: None
    st.text_input      = lambda *a, **k: ""
    st.selectbox       = lambda *a, **k: ("", "")
    st.button          = lambda *a, **k: False
    st.link_button     = lambda *a, **k: None
    st.text_area       = lambda *a, **k: ""
    st.data_editor     = lambda df, **k: df
    st.dataframe       = lambda *a, **k: None
    st.empty           = lambda: _ColumnStub()
    st.columns         = _fake_columns
    st.container       = _fake_container
    st.dialog          = _fake_dialog
    st.expander        = lambda *a, **k: _ColumnStub()
    st.toast           = lambda *a, **k: None
    st.rerun           = lambda *a, **k: None
    st.info            = lambda *a, **k: None
    st.success         = lambda *a, **k: None
    st.warning         = lambda *a, **k: None
    st.error           = lambda *a, **k: None
    st.set_page_config = lambda *a, **k: None
    st.page_link       = lambda *a, **k: None
    st.segmented_control = lambda *a, **k: k.get("default", "")
    st.column_config   = _ColumnConfig
    return st


def _build_page_setup_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.page_setup")
    class _U:
        user_id = "test-user"
    mod.setup_authed_page = lambda **k: (_U(), {"email": "x@y.z", "full_name": "Test"})
    return mod


def _build_jobs_store_stub() -> types.ModuleType:
    """Spy version — record every call so tests can assert on payload shape."""
    mod = types.ModuleType("app.lib.jobs_store")
    mod.PIPELINE_STATUSES = (
        "new", "seen", "applied", "interview", "offer", "rejected", "archived",
    )
    mod.STATUS_LABELS = {
        "new":       "Nouveau",
        "seen":      "Vue",
        "applied":   "Postulé",
        "interview": "Entretien",
        "offer":     "Offre",
        "rejected":  "Refusé",
        "archived":  "Archivée",
    }
    mod.STATUS_TONES = {k: "muted" for k in mod.STATUS_LABELS}
    mod.list_tracked_matches = lambda *a, **k: []

    mod.toggle_favorite_calls: list[tuple] = []
    mod.update_match_calls:    list[tuple[str, str, dict]] = []
    mod.set_status_calls:      list[tuple] = []
    mod.update_match_raise_for: dict[str, Exception] = {}

    def toggle_favorite(uid, jid, value):
        mod.toggle_favorite_calls.append((uid, jid, value))

    def update_match(uid, jid, **kwargs):
        if jid in mod.update_match_raise_for:
            raise mod.update_match_raise_for[jid]
        mod.update_match_calls.append((uid, jid, dict(kwargs)))

    def set_status(uid, jid, ns):
        mod.set_status_calls.append((uid, jid, ns))

    mod.toggle_favorite = toggle_favorite
    mod.update_match    = update_match
    mod.set_status      = set_status
    return mod


def _build_theme_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.theme")
    mod.render_badge       = lambda label, tone="muted": ""
    mod.render_score_badge = lambda score: ""
    return mod


def _load_suivi_module():
    sys.modules["streamlit"]           = _build_streamlit_stub()
    sys.modules["app.lib.page_setup"]  = _build_page_setup_stub()
    sys.modules["app.lib.jobs_store"]  = _build_jobs_store_stub()
    sys.modules["app.lib.theme"]       = _build_theme_stub()

    src = (ROOT / "app" / "pages" / "3_suivi.py").read_text(encoding="utf-8")
    src = src.replace('if __name__ == "__main__":\n    main()\nelse:\n    main()', "")

    ns: dict = {"__name__": "suivi_test", "__file__": str(ROOT / "app" / "pages" / "3_suivi.py")}
    exec(compile(src, "3_suivi.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Helpers — build Suivi-shaped DataFrames quickly.
# ---------------------------------------------------------------------------
def _row(**overrides) -> dict:
    base = {
        "job_id":     "j1",
        "Favori":     False,
        "Poste":      "Senior Engineer",
        "Entreprise": "Acme",
        "Lieu":       "Paris",
        "Score":      8,
        "Statut":     "Postulé",   # human label, mapped via LABEL_TO_STATUS
        "Postulé le": None,
        "Relance":    None,
        "MAJ":        "2026-04-25",
        "Notes":      "",
        "Lien":       "https://example.com",
    }
    base.update(overrides)
    return base


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _reset_spies(jobs_store):
    jobs_store.toggle_favorite_calls.clear()
    jobs_store.update_match_calls.clear()
    jobs_store.update_match_raise_for.clear()


# ---------------------------------------------------------------------------
# Test cases.
# ---------------------------------------------------------------------------
def test_no_change_returns_zero(ns, jobs_store):
    """Identical original and edited → 0 OK, no errors, no calls."""
    _reset_spies(jobs_store)
    df = _df([_row()])
    n_ok, errors = ns["_persist_changes"]("test-user", df, df.copy())
    assert n_ok == 0, f"expected 0 ok, got {n_ok}"
    assert errors == [], f"expected no errors, got {errors}"
    assert jobs_store.update_match_calls    == []
    assert jobs_store.toggle_favorite_calls == []
    print("[OK] no-change short-circuit")


def test_each_field_change_routes_to_correct_call(ns, jobs_store):
    """One row, all 5 fields changed → 5 OK writes, dispatched correctly."""
    _reset_spies(jobs_store)
    original = _df([_row()])
    edited = _df([_row(
        Favori=True,
        Statut="Entretien",
        Notes="Relance prévue mardi",
        **{"Postulé le": date(2026, 4, 20), "Relance": date(2026, 5, 2)},
    )])
    n_ok, errors = ns["_persist_changes"]("test-user", original, edited)

    assert n_ok == 5, f"expected 5 writes, got {n_ok}"
    assert errors == [], f"expected no errors, got {errors}"
    assert jobs_store.toggle_favorite_calls == [("test-user", "j1", True)]

    # Each field-update should be its own update_match call, with only that
    # field in kwargs (minimal patch principle).
    payloads = {
        tuple(sorted(kw.keys())): kw
        for _, _, kw in jobs_store.update_match_calls
    }
    assert ("status",)         in payloads, payloads
    assert ("notes",)          in payloads, payloads
    assert ("applied_at",)     in payloads, payloads
    assert ("next_action_at",) in payloads, payloads

    assert payloads[("status",)]["status"] == "interview"
    assert payloads[("notes",)]["notes"] == "Relance prévue mardi"
    assert payloads[("applied_at",)]["applied_at"]         == date(2026, 4, 20)
    assert payloads[("next_action_at",)]["next_action_at"] == date(2026, 5, 2)
    print("[OK] all 5 fields routed")


def test_match_by_job_id_not_position(ns, jobs_store):
    """Reordering rows between original and edited must NOT corrupt the diff."""
    _reset_spies(jobs_store)
    original = _df([_row(job_id="A"), _row(job_id="B", Notes="b1")])
    # Swap order in edited, change notes on B only.
    edited = _df([
        _row(job_id="B", Notes="b1-EDIT"),
        _row(job_id="A"),
    ])
    n_ok, errors = ns["_persist_changes"]("test-user", original, edited)

    assert errors == []
    assert n_ok == 1
    # Only B should have been updated, not A.
    assert len(jobs_store.update_match_calls) == 1
    uid, jid, payload = jobs_store.update_match_calls[0]
    assert jid == "B" and payload == {"notes": "b1-EDIT"}, (jid, payload)
    print("[OK] diff matches by job_id (resilient to reordering)")


def test_date_out_of_range_yields_error_no_db_write(ns, jobs_store):
    """A 2099 typo on Postulé le should be caught, no DB write, error reported."""
    _reset_spies(jobs_store)
    original = _df([_row(job_id="X")])
    edited = _df([_row(job_id="X", **{"Postulé le": date(2099, 1, 1)})])
    n_ok, errors = ns["_persist_changes"]("test-user", original, edited)

    assert n_ok == 0, f"date typo should not be persisted, got n_ok={n_ok}"
    assert len(errors) == 1, errors
    label, msg = errors[0]
    assert "Acme" in label or "Senior" in label, label
    assert "Postulé" in msg, msg
    assert "2099" in msg, msg
    # No update_match call should have hit the DB for this row.
    assert jobs_store.update_match_calls == [], jobs_store.update_match_calls
    print("[OK] out-of-range date caught and reported")


def test_partial_failure_does_not_abort_batch(ns, jobs_store):
    """Row Y crashes mid-batch — row Z's update should still go through."""
    _reset_spies(jobs_store)
    # Configure the spy to raise on Y's update.
    jobs_store.update_match_raise_for["Y"] = RuntimeError("PostgREST timeout")

    original = _df([
        _row(job_id="Y", Notes="y0"),
        _row(job_id="Z", Notes="z0"),
    ])
    edited = _df([
        _row(job_id="Y", Notes="y1"),
        _row(job_id="Z", Notes="z1"),
    ])
    n_ok, errors = ns["_persist_changes"]("test-user", original, edited)

    # Z's notes update should have succeeded.
    assert n_ok == 1, f"expected 1 OK (Z), got {n_ok}"
    assert len(errors) == 1, errors
    assert "PostgREST timeout" in errors[0][1], errors

    # Z's call must be in the spy log; Y's was raised so didn't append.
    z_calls = [c for c in jobs_store.update_match_calls if c[1] == "Z"]
    assert len(z_calls) == 1 and z_calls[0][2] == {"notes": "z1"}, z_calls
    print("[OK] one row crashing doesn't abort the batch")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ns = _load_suivi_module()
    jobs_store = sys.modules["app.lib.jobs_store"]

    test_no_change_returns_zero(ns, jobs_store)
    test_each_field_change_routes_to_correct_call(ns, jobs_store)
    test_match_by_job_id_not_position(ns, jobs_store)
    test_date_out_of_range_yields_error_no_db_write(ns, jobs_store)
    test_partial_failure_does_not_abort_batch(ns, jobs_store)
    print("\nAll _persist_changes tests passed.")
