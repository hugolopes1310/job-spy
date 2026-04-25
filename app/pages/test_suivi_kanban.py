"""Smoke tests for the kanban view added to 3_suivi.py.

Run from the repo root:
    PYTHONPATH=. python app/pages/test_suivi_kanban.py
"""
from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Build a stub `streamlit` module rich enough to execute 3_suivi.py top-level
# without Streamlit installed (we only care about the helpers, not the UI).
# ---------------------------------------------------------------------------
class _ColumnStub:
    """Mimics st.columns() entries — supports `with col:` + `col.button(...)`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def button(self, *a, **k):
        return False

    def link_button(self, *a, **k):
        return None

    def markdown(self, *a, **k):
        pass

    def caption(self, *a, **k):
        pass


def _fake_columns(spec, **kwargs):
    n = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
    return tuple(_ColumnStub() for _ in range(n))


def _fake_container(border=False):
    c = _ColumnStub()
    return c


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
    st.error           = lambda *a, **k: None
    st.set_page_config = lambda *a, **k: None
    st.page_link       = lambda *a, **k: None
    st.segmented_control = lambda *a, **k: k.get("default", a[1][0] if len(a) > 1 else "")
    st.column_config   = _ColumnConfig
    return st


# ---------------------------------------------------------------------------
# Stubs for project modules we don't need to exercise.
# ---------------------------------------------------------------------------
def _build_page_setup_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.page_setup")

    class _U:
        user_id = "test-user"

    def setup_authed_page(**k):
        return _U(), {"email": "x@y.z", "full_name": "Test User"}

    mod.setup_authed_page = setup_authed_page
    return mod


def _build_jobs_store_stub() -> types.ModuleType:
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
    mod.STATUS_TONES = {
        "new":       "muted",
        "seen":      "muted",
        "applied":   "info",
        "interview": "info",
        "offer":     "ok",
        "rejected":  "warn",
        "archived":  "muted",
    }
    mod.list_tracked_matches = lambda *a, **k: []
    mod.set_status_calls = []
    def set_status(uid, jid, ns):
        mod.set_status_calls.append((uid, jid, ns))
    mod.set_status = set_status
    mod.toggle_favorite = lambda *a, **k: None
    mod.update_match    = lambda *a, **k: None
    return mod


def _build_theme_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.theme")
    mod.render_badge       = lambda label, tone="muted": f'<span class="badge {tone}">{label}</span>'
    mod.render_score_badge = lambda score: f'<span class="score">{score}</span>' if score is not None else ""
    return mod


# ---------------------------------------------------------------------------
# Load the suivi module by exec'ing the source with our stubs in place.
# ---------------------------------------------------------------------------
def _load_suivi_module():
    sys.modules["streamlit"]           = _build_streamlit_stub()
    sys.modules["app.lib.page_setup"]  = _build_page_setup_stub()
    sys.modules["app.lib.jobs_store"]  = _build_jobs_store_stub()
    sys.modules["app.lib.theme"]       = _build_theme_stub()

    src = (ROOT / "app" / "pages" / "3_suivi.py").read_text(encoding="utf-8")
    # Drop the implicit `main()` call at module bottom — we only want to import.
    src = src.replace("if __name__ == \"__main__\":\n    main()\nelse:\n    main()", "")

    ns: dict = {"__name__": "suivi_test", "__file__": str(ROOT / "app" / "pages" / "3_suivi.py")}
    exec(compile(src, "3_suivi.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Test cases.
# ---------------------------------------------------------------------------
def test_constants(ns):
    """_KANBAN_STATUSES has exactly 4 entries, _NEXT_STATUS covers all of them."""
    statuses = ns["_KANBAN_STATUSES"]
    assert len(statuses) == 4, f"expected 4 kanban columns, got {len(statuses)}"

    keys = [k for k, _ in statuses]
    assert keys == ["applied", "interview", "offer", "rejected"], keys

    next_map = ns["_NEXT_STATUS"]
    for k in keys:
        assert k in next_map, f"missing _NEXT_STATUS for {k}"

    advance = ns["_ADVANCE_LABEL"]
    for k in keys:
        target = next_map[k]
        assert target in advance, f"missing _ADVANCE_LABEL for {target}"

    # "interview" → "offer", not "rejected".
    assert next_map["applied"]   == "interview"
    assert next_map["interview"] == "offer"
    assert next_map["offer"]     == "archived"
    assert next_map["rejected"]  == "archived"
    print("[OK] kanban constants")


def test_kanban_buckets(ns):
    """_kanban_buckets groups by status and drops new/seen/archived."""
    rows = [
        {"job_id": "a", "status": "applied"},
        {"job_id": "b", "status": "interview"},
        {"job_id": "c", "status": "interview"},
        {"job_id": "d", "status": "new"},        # dropped
        {"job_id": "e", "status": "seen"},       # dropped
        {"job_id": "f", "status": "archived"},   # dropped
        {"job_id": "g", "status": "offer"},
        {"job_id": "h", "status": "rejected"},
        {"job_id": "i", "status": None},         # treated as "new" → dropped
    ]
    buckets = ns["_kanban_buckets"](rows)
    assert set(buckets.keys()) == {"applied", "interview", "offer", "rejected"}
    assert [r["job_id"] for r in buckets["applied"]]   == ["a"]
    assert [r["job_id"] for r in buckets["interview"]] == ["b", "c"]
    assert [r["job_id"] for r in buckets["offer"]]     == ["g"]
    assert [r["job_id"] for r in buckets["rejected"]]  == ["h"]
    # The 4 dropped rows must not leak into any kanban bucket.
    flat = [r["job_id"] for v in buckets.values() for r in v]
    for jid in ("d", "e", "f", "i"):
        assert jid not in flat, f"{jid} should not appear in kanban buckets"
    print("[OK] _kanban_buckets")


def test_to_date_with_relance_logic(ns):
    """_to_date + overdue logic should classify a kanban relance pill correctly."""
    today = date.today()
    yesterday = today.replace(day=max(1, today.day - 1)) if today.day > 1 else today
    # Rows with various next_action_at shapes.
    cases = [
        ({"next_action_at": today.isoformat()},     True),   # due
        ({"next_action_at": "2099-01-01"},          False),  # upcoming
        ({"next_action_at": None},                   None),  # no pill
        ({"next_action_at": ""},                     None),
    ]
    for row, expected_due in cases:
        d = ns["_to_date"](row.get("next_action_at"))
        if expected_due is None:
            assert d is None, f"expected None for {row}, got {d}"
        else:
            assert d is not None, f"expected a date for {row}"
            actually_due = d <= today
            assert actually_due == expected_due, f"due mismatch for {row}: {d}"
    print("[OK] kanban relance pill classification")


def test_render_kanban_card_smoke(ns):
    """Smoke: rendering a card with all decorations doesn't raise."""
    card = {
        "job_id": "j-1",
        "title": "Senior Backend Engineer",
        "company": "Acme Corp",
        "location": "Paris (Hybrid)",
        "score": 8,
        "status": "applied",
        "is_favorite": True,
        "next_action_at": "2099-01-15",
    }
    ns["_render_kanban_card"]("test-user", card)
    print("[OK] _render_kanban_card with full card")

    minimal = {"job_id": "j-2", "status": "rejected"}
    ns["_render_kanban_card"]("test-user", minimal)
    print("[OK] _render_kanban_card with minimal card")


def test_render_kanban_smoke(ns):
    """Empty list → friendly empty state; non-empty → no exception."""
    ns["_render_kanban"]("test-user", [])
    ns["_render_kanban"]("test-user", [
        {"job_id": "k1", "status": "applied",   "title": "T1", "company": "C1"},
        {"job_id": "k2", "status": "interview", "title": "T2", "company": "C2"},
        {"job_id": "k3", "status": "new",       "title": "T3", "company": "C3"},
    ])
    print("[OK] _render_kanban with mixed bucket")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ns = _load_suivi_module()
    test_constants(ns)
    test_kanban_buckets(ns)
    test_to_date_with_relance_logic(ns)
    test_render_kanban_card_smoke(ns)
    test_render_kanban_smoke(ns)
    print("\nAll kanban tests passed.")
