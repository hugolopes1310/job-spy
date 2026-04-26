"""Smoke tests for `app/pages/1_mon_profil.py`.

We don't have a Streamlit server in CI — these tests stub `streamlit` and
the project deps with just enough surface to load and exercise the page.

Run from the repo root:
    PYTHONPATH=. python app/pages/test_mon_profil.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Streamlit stub — same shape as test_suivi_kanban.py, extended with the
# extra widgets `1_mon_profil.py` uses (multiselect, slider, number_input,
# selectbox, checkbox, file_uploader, spinner, write).
# ---------------------------------------------------------------------------
class _CtxStub:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def button(self, *a, **k):
        return False

    def markdown(self, *a, **k):
        pass

    def caption(self, *a, **k):
        pass

    def write(self, *a, **k):
        pass

    def number_input(self, *a, **k):
        return k.get("value", 0)

    def slider(self, *a, **k):
        return k.get("value", 0)

    def multiselect(self, *a, **k):
        return list(k.get("default", []))

    def selectbox(self, *a, **k):
        opts = k.get("options") or (a[1] if len(a) > 1 else [None])
        idx = k.get("index", 0)
        return opts[idx] if opts else None

    def text_input(self, *a, **k):
        return k.get("value", "")

    def checkbox(self, *a, **k):
        return k.get("value", True)

    def file_uploader(self, *a, **k):
        return None

    def page_link(self, *a, **k):
        pass


def _fake_columns(spec, **k):
    n = len(spec) if isinstance(spec, (list, tuple)) else int(spec)
    return tuple(_CtxStub() for _ in range(n))


def _fake_container(*a, **k):
    return _CtxStub()


def _fake_expander(*a, **k):
    return _CtxStub()


def _fake_spinner(*a, **k):
    return _CtxStub()


def _build_streamlit_stub() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.session_state    = {}
    st.set_page_config  = lambda *a, **k: None
    st.markdown         = lambda *a, **k: None
    st.write            = lambda *a, **k: None
    st.caption          = lambda *a, **k: None
    st.success          = lambda *a, **k: None
    st.error            = lambda *a, **k: None
    st.warning          = lambda *a, **k: None
    st.info             = lambda *a, **k: None
    st.toast            = lambda *a, **k: None
    st.rerun            = lambda *a, **k: None
    st.stop             = lambda *a, **k: None
    st.page_link        = lambda *a, **k: None
    st.button           = lambda *a, **k: False
    st.text_input       = lambda *a, **k: k.get("value", "")
    st.selectbox        = lambda *a, **k: (k.get("options") or [None])[k.get("index", 0)]
    st.checkbox         = lambda *a, **k: k.get("value", True)
    st.number_input     = lambda *a, **k: k.get("value", 0)
    st.slider           = lambda *a, **k: k.get("value", 0)
    st.multiselect      = lambda *a, **k: list(k.get("default", []))
    st.file_uploader    = lambda *a, **k: None
    st.columns          = _fake_columns
    st.container        = _fake_container
    st.expander         = _fake_expander
    st.spinner          = _fake_spinner
    return st


# ---------------------------------------------------------------------------
# Project module stubs.
# ---------------------------------------------------------------------------
def _build_page_setup_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.page_setup")

    class _U:
        user_id = "test-user"
        email = "x@y.z"

    mod.setup_authed_page = lambda **k: (_U(), {"email": "x@y.z", "full_name": "Test"})
    return mod


def _build_theme_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.theme")
    mod.render_wordmark = lambda *a, **k: None
    return mod


def _build_storage_stub(cv_text: str = "", config: dict | None = None) -> types.ModuleType:
    mod = types.ModuleType("app.lib.storage")
    mod._cv_text = cv_text
    mod._config = config
    mod._save_cv_calls = []
    mod._save_config_calls = []
    mod.load_cv_text       = lambda uid, **k: mod._cv_text
    mod.load_user_config   = lambda uid, **k: mod._config
    def _save_cv(uid, text):
        mod._save_cv_calls.append((uid, len(text)))
    def _save_cfg(uid, cfg):
        mod._save_config_calls.append((uid, cfg))
    mod.save_cv_text       = _save_cv
    mod.save_user_config   = _save_cfg
    return mod


def _build_cv_parser_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.cv_parser")
    mod.parse_cv = lambda b, n: "stub cv text"
    class _Err(Exception): pass
    mod.CVParseError           = _Err
    mod.CVParseConfigError     = _Err
    mod.EncryptedPDFError      = type("EncryptedPDFError", (_Err,), {})
    mod.ImageOnlyPDFError      = type("ImageOnlyPDFError", (_Err,), {})
    mod.FileTooLargeError      = type("FileTooLargeError", (_Err,), {})
    mod.EmptyFileError         = type("EmptyFileError", (_Err,), {})
    mod.UnsupportedFormatError = type("UnsupportedFormatError", (_Err,), {})
    mod.CorruptedFileError     = type("CorruptedFileError", (_Err,), {})
    return mod


def _build_synth_store_stub(active=None, versions=None) -> types.ModuleType:
    mod = types.ModuleType("app.lib.profile_synthesis_store")
    mod._active = active
    mod._versions = versions or []
    mod._activate_calls = []
    mod._archive_calls = []
    mod._draft_calls = []
    mod.load_active_synthesis    = lambda uid, **k: mod._active
    mod.list_synthesis_versions  = lambda uid, **k: list(mod._versions)
    def _insert(uid, syn, **k):
        mod._draft_calls.append({"uid": uid, "syn": syn, "kwargs": k})
        return f"new-id-{len(mod._draft_calls)}"
    def _activate(sid, **k):
        mod._activate_calls.append(sid)
    def _archive(uid, **k):
        mod._archive_calls.append(uid)
    mod.insert_synthesis_draft   = _insert
    mod.activate_synthesis       = _activate
    mod.archive_active_synthesis = _archive
    return mod


def _build_synthesizer_stub(synthesis: dict | None = None, fail: bool = False) -> types.ModuleType:
    mod = types.ModuleType("app.lib.profile_synthesizer")
    mod.PROMPT_VERSION = "v1.0-test"

    class ProfileSynthesisError(RuntimeError):
        pass

    mod.ProfileSynthesisError = ProfileSynthesisError
    mod._calls = []

    def _synth(cv_text, user_config, **k):
        mod._calls.append({"cv_len": len(cv_text or ""), "kwargs": k})
        if fail:
            raise ProfileSynthesisError("stub failure")
        return synthesis or {
            "summary_fr": "stub summary",
            "role_families": [
                {
                    "label": "Test family",
                    "titles": ["Engineer"],
                    "weight": 0.7,
                    "active": True,
                    "source": {"type": "inferred", "evidence": ""},
                }
            ],
            "seniority_band": {"label": "mid", "yoe_min": 2, "yoe_max": 5},
            "geo": {"primary": ["Paris"], "acceptable": [], "exclude": []},
            "deal_breakers": [],
            "dream_companies": [],
            "languages": ["FR-native"],
            "confidence": 0.7,
            "open_questions": [],
        }

    mod.synthesize_profile = _synth
    return mod


# ---------------------------------------------------------------------------
# Loader.
# ---------------------------------------------------------------------------
def _load_page(*, active=None, cv_text="", config=None, synth_fail=False):
    """Load the page module with controlled stubs. Returns the namespace
    after exec — main() ran once at import (with our stubs) without raising.
    """
    sys.modules["streamlit"]                       = _build_streamlit_stub()
    sys.modules["app.lib.page_setup"]              = _build_page_setup_stub()
    sys.modules["app.lib.theme"]                   = _build_theme_stub()
    sys.modules["app.lib.cv_parser"]               = _build_cv_parser_stub()
    sys.modules["app.lib.storage"]                 = _build_storage_stub(
        cv_text=cv_text, config=config
    )
    sys.modules["app.lib.profile_synthesis_store"] = _build_synth_store_stub(active=active)
    sys.modules["app.lib.profile_synthesizer"]     = _build_synthesizer_stub(fail=synth_fail)

    src = (ROOT / "app" / "pages" / "1_mon_profil.py").read_text(encoding="utf-8")
    # Strip the auto-run at module bottom — we call main() ourselves later.
    auto_run = '\nif __name__ == "__main__":\n    main()\nelse:\n    main()\n'
    if auto_run in src:
        src = src.replace(auto_run, "\n")

    ns: dict = {
        "__name__": "mon_profil_test",
        "__file__": str(ROOT / "app" / "pages" / "1_mon_profil.py"),
    }
    exec(compile(src, "1_mon_profil.py", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_pure_helpers_load():
    """Module loads, helpers are callable and pure."""
    ns = _load_page()
    pill = ns["_confidence_pill"]
    assert "Confiance haute" in pill(0.9)
    assert "Confiance moyenne" in pill(0.6)
    assert "Confiance faible" in pill(0.2)
    assert "Confiance faible" in pill(None)
    print("[OK] _confidence_pill")


def test_build_edited_synthesis_pure():
    """`_build_edited_synthesis` only takes the previous synthesis + UI edits
    and returns the new shape (no LLM call)."""
    ns = _load_page()
    base = {
        "summary_fr": "old",
        "role_families": [{"label": "A", "titles": ["x"], "active": True, "weight": 0.7}],
        "geo": {"primary": ["Paris"], "acceptable": [], "exclude": []},
        "seniority_band": {"label": "mid", "yoe_min": 2, "yoe_max": 5},
        "deal_breakers": ["sales"],
        "dream_companies": ["Stripe"],
        "languages": ["FR-native"],
        "confidence": 0.7,
        "open_questions": [
            {"id": "q_contract", "text": "CDI ?", "answer": None},
            {"id": "q_relocation", "text": "Bouger ?", "answer": "Oui"},
        ],
    }
    new = ns["_build_edited_synthesis"](
        base=base,
        role_families=[{"label": "A", "titles": ["x", "y"], "weight": 0.9, "active": True}],
        geo={"primary": ["Lyon"], "acceptable": ["Paris"], "exclude": ["US"]},
        seniority_band={"label": "senior", "yoe_min": 5, "yoe_max": 10},
        deal_breakers=["consulting", "sales"],
        dream_companies=["Stripe", "Anthropic"],
        languages=["FR-native", "EN-C1"],
        open_q_answers={"q_contract": "CDI", "q_relocation": "Oui"},
    )

    assert new["role_families"][0]["weight"] == 0.9
    assert new["geo"] == {"primary": ["Lyon"], "acceptable": ["Paris"], "exclude": ["US"]}
    assert new["seniority_band"]["label"] == "senior"
    assert "consulting" in new["deal_breakers"]
    assert "Anthropic" in new["dream_companies"]
    # Open questions: IDs preserved, answers set.
    answers_by_id = {q["id"]: q["answer"] for q in new["open_questions"]}
    assert answers_by_id == {"q_contract": "CDI", "q_relocation": "Oui"}
    # Summary not in the edited list → preserved from base.
    assert new["summary_fr"] == "old"
    print("[OK] _build_edited_synthesis preserves IDs and overlays edits")


def test_main_empty_state_no_cv():
    """No active synth, no CV → empty state path runs without raising."""
    ns = _load_page(active=None, cv_text="", config=None)
    ns["main"]()
    print("[OK] main() — empty state (no CV)")


def test_main_lazy_migration_path():
    """Has CV but no active synth → lazy migration banner."""
    ns = _load_page(active=None, cv_text="cv content " * 50, config={"target": {}})
    ns["main"]()
    print("[OK] main() — lazy migration banner")


def test_main_with_active_synthesis():
    """Active synth exists → full edit view renders."""
    syn = {
        "id": "abc",
        "version": 3,
        "llm_model": "gemini-2.5-flash",
        "synthesis": {
            "summary_fr": "Tu es un dev senior pharma.",
            "role_families": [
                {
                    "label": "Pharma PM",
                    "titles": ["CRA", "Clinical Project Manager"],
                    "weight": 0.9,
                    "active": True,
                    "source": {"type": "cv", "evidence": "5 ans Roche"},
                }
            ],
            "seniority_band": {"label": "senior", "yoe_min": 7, "yoe_max": 12},
            "geo": {"primary": ["Geneva, Switzerland"], "acceptable": [], "exclude": []},
            "deal_breakers": ["sales"],
            "dream_companies": ["Roche"],
            "languages": ["FR-native", "EN-C1"],
            "confidence": 0.85,
            "open_questions": [
                {"id": "q_remote", "text": "Remote OK ?", "answer": None},
            ],
        },
    }
    ns = _load_page(active=syn)
    ns["main"]()
    print("[OK] main() — synthesis view")


def test_run_initial_synthesis_inserts_and_activates():
    """`_run_initial_synthesis` calls the LLM, inserts a draft, activates it."""
    ns = _load_page(active=None, cv_text="cv text", config={})
    ns["_run_initial_synthesis"]("user-x", cv_text="cv text", user_config={})
    store = sys.modules["app.lib.profile_synthesis_store"]
    assert len(store._draft_calls) == 1, store._draft_calls
    assert store._draft_calls[0]["uid"] == "user-x"
    assert store._activate_calls and store._activate_calls[0].startswith("new-id-")
    print("[OK] _run_initial_synthesis happy path")


def test_run_initial_synthesis_llm_failure_does_nothing():
    """If `synthesize_profile` raises, no draft is inserted / activated."""
    ns = _load_page(active=None, cv_text="cv", config={}, synth_fail=True)
    ns["_run_initial_synthesis"]("user-x", cv_text="cv", user_config={})
    store = sys.modules["app.lib.profile_synthesis_store"]
    assert store._draft_calls == [], store._draft_calls
    assert store._activate_calls == [], store._activate_calls
    print("[OK] _run_initial_synthesis surfaces error, doesn't write")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_pure_helpers_load()
    test_build_edited_synthesis_pure()
    test_main_empty_state_no_cv()
    test_main_lazy_migration_path()
    test_main_with_active_synthesis()
    test_run_initial_synthesis_inserts_and_activates()
    test_run_initial_synthesis_llm_failure_does_nothing()
    print("\nAll mon_profil smoke tests passed ✅")
