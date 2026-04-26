"""Smoke tests for the score-decomposition helpers in 2_dashboard.py.

We don't load the full dashboard module (its top-level calls Supabase /
auth), so we extract the two helpers via `ast` + `exec` into an isolated
namespace with a minimal `streamlit` stub.

Run from the repo root:
    PYTHONPATH=. python app/pages/test_dashboard_score_decomp.py
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Minimal streamlit stub — captures markdown calls so we can assert content.
# ---------------------------------------------------------------------------
def _build_streamlit_stub() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st._markdown_calls: list[tuple] = []

    def _md(*a, **k):
        st._markdown_calls.append((a, k))

    st.markdown = _md
    return st


# ---------------------------------------------------------------------------
# Loader — slices `_score_chip` + `_render_score_decomposition` out of the
# real source file, exec's them into an isolated ns. Avoids the page's
# top-level auth call.
# ---------------------------------------------------------------------------
def _extract_helpers() -> dict:
    src = (ROOT / "app" / "pages" / "2_dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"_score_chip", "_render_score_decomposition"}
    fns = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert len(fns) == 2, f"expected to find both helpers, got {[n.name for n in fns]}"
    module = ast.Module(body=fns, type_ignores=[])
    ns: dict = {"st": _build_streamlit_stub()}
    exec(compile(module, "2_dashboard.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_score_chip_color_buckets():
    ns = _extract_helpers()
    chip = ns["_score_chip"]
    # green
    assert "#15803D" in chip("Rôle", 9)
    assert "9/10" in chip("Rôle", 9)
    # amber
    assert "#B45309" in chip("Géo", 6)
    # red
    assert "#B91C1C" in chip("Séniorité", 3)
    print("[OK] _score_chip color buckets (9/6/3)")


def test_score_chip_handles_missing_or_negative():
    ns = _extract_helpers()
    chip = ns["_score_chip"]
    assert chip("Rôle", None) == ""
    assert chip("Géo", -1) == ""        # heuristic "couldn't compute"
    assert chip("Géo", "garbage") == ""  # type: ignore[arg-type]
    print("[OK] _score_chip returns '' for None / negative / non-numeric")


def test_score_chip_rounds_to_int():
    ns = _extract_helpers()
    chip = ns["_score_chip"]
    assert "8/10" in chip("Rôle", 7.6)  # rounds up
    assert "7/10" in chip("Rôle", 7.4)  # rounds down
    print("[OK] _score_chip rounds floats to nearest int")


def test_render_score_decomposition_full():
    """Synthesis-scored analysis with all 3 sub-scores → 1 markdown call,
    and the rendered HTML mentions every label."""
    st_stub = _build_streamlit_stub()
    ns: dict = {"st": st_stub}
    src = (ROOT / "app" / "pages" / "2_dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name in {"_score_chip", "_render_score_decomposition"}]
    exec(compile(ast.Module(body=fns, type_ignores=[]), "2_dashboard.py", "exec"), ns)

    ns["_render_score_decomposition"]({
        "match_role": 8,
        "match_geo": 10,
        "match_seniority": 6,
    })
    assert len(st_stub._markdown_calls) == 1, st_stub._markdown_calls
    html = st_stub._markdown_calls[0][0][0]
    assert "Rôle" in html
    assert "Géo" in html
    assert "Séniorité" in html
    assert "8/10" in html and "10/10" in html and "6/10" in html
    assert "Décomposition" in html
    print("[OK] _render_score_decomposition renders 3 chips + label")


def test_render_score_decomposition_partial_legacy():
    """Legacy analysis without sub-scores → no markdown call. Card stays clean."""
    st_stub = _build_streamlit_stub()
    ns: dict = {"st": st_stub}
    src = (ROOT / "app" / "pages" / "2_dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name in {"_score_chip", "_render_score_decomposition"}]
    exec(compile(ast.Module(body=fns, type_ignores=[]), "2_dashboard.py", "exec"), ns)

    ns["_render_score_decomposition"]({"reason": "legacy"})
    assert st_stub._markdown_calls == []
    print("[OK] _render_score_decomposition silent on legacy analysis")


def test_render_score_decomposition_partial_one_field():
    """Only one of the three present → that single chip still renders."""
    st_stub = _build_streamlit_stub()
    ns: dict = {"st": st_stub}
    src = (ROOT / "app" / "pages" / "2_dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef)
           and n.name in {"_score_chip", "_render_score_decomposition"}]
    exec(compile(ast.Module(body=fns, type_ignores=[]), "2_dashboard.py", "exec"), ns)

    ns["_render_score_decomposition"]({"match_role": 7})
    assert len(st_stub._markdown_calls) == 1
    html = st_stub._markdown_calls[0][0][0]
    assert "Rôle" in html and "7/10" in html
    assert "Géo" not in html and "Séniorité" not in html
    print("[OK] _render_score_decomposition renders single available chip")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_score_chip_color_buckets()
    test_score_chip_handles_missing_or_negative()
    test_score_chip_rounds_to_int()
    test_render_score_decomposition_full()
    test_render_score_decomposition_partial_legacy()
    test_render_score_decomposition_partial_one_field()
    print("\nAll dashboard score-decomp tests passed.")
