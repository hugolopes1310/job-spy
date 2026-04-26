"""Tests for the session-expired banner mechanism in auth.py.

We don't exercise the supabase-py client directly (that needs network +
secrets). Instead we stub out the dependencies and drive `get_current_user`
through its decision branches.

Run from the repo root:
    PYTHONPATH=. python app/lib/test_auth_session_expired.py
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Streamlit stub — only need session_state.
# ---------------------------------------------------------------------------
def _build_streamlit_stub() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.session_state = {}
    st.error = lambda *a, **k: None
    st.warning = lambda *a, **k: None
    st.info = lambda *a, **k: None
    st.stop = lambda: None
    # `get_current_user` may call `st.rerun()` during the cookie warm-up
    # branch; in tests we make it a no-op so the function can fall through
    # to `return None` instead of raising RerunException.
    st.rerun = lambda *a, **k: None
    st.secrets = {}
    return st


# Cookie module stub: no persistent state. load_refresh_token returns None
# in these tests so we never try to "rehydrate" from cookie.
def _build_cookies_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.session_cookies")
    mod.load_refresh_token = lambda: None
    mod.save_refresh_token = lambda *a, **k: None
    mod.clear_refresh_token = lambda *a, **k: None
    return mod


def _build_supabase_client_stub() -> types.ModuleType:
    mod = types.ModuleType("app.lib.supabase_client")
    class _StubClient:
        class _Auth:
            @staticmethod
            def refresh_session(*a, **k):
                # Force a refresh failure to drive the expiry branch.
                raise RuntimeError("refresh failed")
            @staticmethod
            def sign_out(*a, **k):
                return None
        auth = _Auth()
        class _Postgrest:
            @staticmethod
            def auth(*a, **k):
                return None
        postgrest = _Postgrest()
    mod.get_anon_client = lambda *a, **k: _StubClient()
    mod.get_service_client = lambda *a, **k: _StubClient()
    return mod


def _import_auth():
    import importlib
    sys.modules["streamlit"]                  = _build_streamlit_stub()
    sys.modules["app.lib.session_cookies"]    = _build_cookies_stub()
    sys.modules["app.lib.supabase_client"]    = _build_supabase_client_stub()
    sys.modules.pop("app.lib.auth", None)
    # `import_module` forces re-execution of auth's top level; the regular
    # `from app.lib import auth` syntax returns the package's cached attr
    # and would silently keep references to a previous test's stubs.
    auth = importlib.import_module("app.lib.auth")
    # Don't actually sleep between cookie warm-up reruns in tests.
    auth._COOKIE_WARMUP_SLEEP_S = 0
    return auth


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------
def test_no_session_returns_none(auth):
    """Empty session_state + no cookie → get_current_user returns None
    AND does NOT set the expired flag (this is just an unauth user)."""
    import streamlit as st
    st.session_state.clear()
    assert auth.get_current_user() is None
    assert auth.SESSION_EXPIRED_KEY not in st.session_state
    assert auth.consume_session_expired_message() is None
    print("[OK] unauthed visitor → no expiry banner")


def test_expired_token_sets_flag(auth):
    """Token whose expiry is in the past + refresh fails → flag set."""
    import streamlit as st
    st.session_state.clear()
    st.session_state["sb_access_token"]  = "access-stub"
    st.session_state["sb_refresh_token"] = "refresh-stub"
    st.session_state["sb_user_id"]       = "u-1"
    st.session_state["sb_user_email"]    = "x@y.z"
    st.session_state["sb_expires_at"]    = int(time.time()) - 10  # already expired
    user = auth.get_current_user()
    assert user is None, "get_current_user must return None when refresh fails"
    msg = auth.consume_session_expired_message()
    assert msg is not None and "expir" in msg.lower(), msg
    # Calling consume again should return None — the message is one-shot.
    assert auth.consume_session_expired_message() is None
    # logout() must have wiped the access/refresh tokens.
    assert "sb_access_token" not in st.session_state
    print("[OK] expired token + refresh fail → expiry message + logout")


def test_valid_token_passes_through(auth):
    """A token that hasn't expired yet → returns CurrentUser, no flag set."""
    import streamlit as st
    st.session_state.clear()
    st.session_state["sb_access_token"]  = "access-stub"
    st.session_state["sb_refresh_token"] = "refresh-stub"
    st.session_state["sb_user_id"]       = "u-1"
    st.session_state["sb_user_email"]    = "x@y.z"
    # Plenty of time remaining (1h ahead).
    st.session_state["sb_expires_at"]    = int(time.time()) + 3600
    user = auth.get_current_user()
    assert user is not None
    assert user.user_id == "u-1"
    assert user.email == "x@y.z"
    assert auth.SESSION_EXPIRED_KEY not in st.session_state
    print("[OK] fresh token → CurrentUser, no banner")


def test_consume_session_expired_message_clears(auth):
    """Manually planted message survives one read, then is gone."""
    import streamlit as st
    st.session_state.clear()
    st.session_state[auth.SESSION_EXPIRED_KEY] = "Bonjour, ta session a expiré."
    out1 = auth.consume_session_expired_message()
    out2 = auth.consume_session_expired_message()
    assert out1 == "Bonjour, ta session a expiré."
    assert out2 is None
    print("[OK] consume_session_expired_message is one-shot")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    auth = _import_auth()
    test_no_session_returns_none(auth)
    test_expired_token_sets_flag(auth)
    test_valid_token_passes_through(auth)
    test_consume_session_expired_message_clears(auth)
    print("\nAll auth session-expired tests passed.")
