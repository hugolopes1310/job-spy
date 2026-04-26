"""Tests for the cookie warm-up retry in `get_current_user()`.

Background: `extra-streamlit-components.CookieManager` runs in an iframe.
On the first script run after a hard refresh, the iframe hasn't posted its
cookies back yet, and `_read_cookies()` returns `{}` indistinguishably
from "no cookie set at all". Without retries, a logged-in user gets
bounced to the login screen on every refresh.

The fix: bounded number of `st.rerun()`s with a brief sleep, before
declaring the user truly logged out. These tests drive that path with
stubs only — no real Streamlit, no real Supabase.

Run from the repo root:
    PYTHONPATH=. python app/lib/test_auth_cookie_warmup.py
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Streamlit stub — captures `rerun()` calls so we can assert on them.
# ---------------------------------------------------------------------------
def _build_streamlit_stub() -> types.ModuleType:
    st = types.ModuleType("streamlit")
    st.session_state = {}
    st.error = lambda *a, **k: None
    st.warning = lambda *a, **k: None
    st.info = lambda *a, **k: None
    st.stop = lambda: None
    st.secrets = {}
    st._rerun_calls = 0  # incremented every time st.rerun() is invoked

    def _rerun(*a, **k):
        st._rerun_calls += 1
        # Don't raise — let the function under test fall through to
        # `return None` so we can assert on its observable result.

    st.rerun = _rerun
    return st


# ---------------------------------------------------------------------------
# Cookie module stub — programmable: tracks call count and serves either
# None (iframe not ready) or a real refresh token (iframe loaded).
# ---------------------------------------------------------------------------
class _CookieStubState:
    """Drives `load_refresh_token()` deterministically across calls.

    `tokens_per_call` is a list — index 0 is what the first call gets,
    index 1 is what the second call gets, etc. After the list is exhausted
    we keep serving the last value.
    """
    def __init__(self, tokens_per_call: list[str | None]):
        self.tokens_per_call = list(tokens_per_call)
        self.calls = 0

    def next(self) -> str | None:
        idx = min(self.calls, len(self.tokens_per_call) - 1)
        self.calls += 1
        return self.tokens_per_call[idx]


def _build_cookies_stub(state: _CookieStubState) -> types.ModuleType:
    mod = types.ModuleType("app.lib.session_cookies")
    mod.load_refresh_token = lambda: state.next()
    mod.save_refresh_token = lambda *a, **k: None
    mod.clear_refresh_token = lambda *a, **k: None
    mod._state = state
    return mod


def _build_supabase_client_stub(refresh_works: bool = True) -> types.ModuleType:
    """`refresh_session(rt)` returns a populated session iff `refresh_works`."""
    mod = types.ModuleType("app.lib.supabase_client")

    class _Session:
        access_token = "fresh-access"
        refresh_token = "fresh-refresh"
        expires_at = int(time.time()) + 3600

    class _User:
        id = "user-42"
        email = "user@example.com"

    class _Result:
        session = _Session()
        user = _User()

    class _StubClient:
        class _Auth:
            @staticmethod
            def refresh_session(*a, **k):
                if refresh_works:
                    return _Result()
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


def _import_auth(*, cookie_state: _CookieStubState, refresh_works: bool = True):
    import importlib
    sys.modules["streamlit"]               = _build_streamlit_stub()
    sys.modules["app.lib.session_cookies"] = _build_cookies_stub(cookie_state)
    sys.modules["app.lib.supabase_client"] = _build_supabase_client_stub(
        refresh_works=refresh_works
    )
    sys.modules.pop("app.lib.auth", None)
    # `import_module` re-executes the top-level code; using
    # `from app.lib import auth` would return the package's cached attr
    # and silently keep stale references to last test's stubs.
    auth = importlib.import_module("app.lib.auth")
    # Don't actually sleep between cookie warm-up reruns in tests.
    auth._COOKIE_WARMUP_SLEEP_S = 0
    return auth


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_iframe_not_ready_first_call_triggers_rerun():
    """First call: cookie iframe returns None (not ready) → we bump the
    counter and call `st.rerun()`. The function returns None during this
    transient state — caller will retry on the next script run."""
    state = _CookieStubState([None, None, "refresh-token"])
    auth = _import_auth(cookie_state=state)
    import streamlit as st

    user = auth.get_current_user()
    assert user is None  # transient — caller will rerun and retry
    assert st._rerun_calls == 1, f"expected one rerun, got {st._rerun_calls}"
    assert st.session_state.get(auth._COOKIE_WARMUP_KEY) == 1
    print("[OK] cookie iframe not ready → 1 rerun queued")


def test_iframe_loads_on_second_attempt_recovers_session():
    """Simulate the realistic failure mode: 1st call → iframe empty (rerun
    queued); 2nd call (= the rerun) → iframe loaded, refresh succeeds."""
    state = _CookieStubState([None, "refresh-token"])
    auth = _import_auth(cookie_state=state, refresh_works=True)
    import streamlit as st

    # 1st call — iframe not ready yet.
    u1 = auth.get_current_user()
    assert u1 is None
    assert st._rerun_calls == 1

    # 2nd call simulates the rerun: iframe is now loaded → cookie populated
    # → _try_refresh_from_cookie() succeeds → CurrentUser returned.
    u2 = auth.get_current_user()
    assert u2 is not None, "expected CurrentUser once cookie is readable"
    assert u2.user_id == "user-42"
    assert u2.email == "user@example.com"
    # Counter is reset on successful auth so a future hard refresh gets the
    # full retry budget back.
    assert auth._COOKIE_WARMUP_KEY not in st.session_state
    print("[OK] iframe loads on 2nd attempt → CurrentUser returned, counter reset")


def test_budget_exhausted_returns_none_genuinely_unauthed():
    """User who really has no cookie: after `_COOKIE_WARMUP_MAX` retries we
    stop spinning and return None so the login screen renders."""
    state = _CookieStubState([None])  # always None — never loads
    auth = _import_auth(cookie_state=state)
    import streamlit as st

    # Burn through the retry budget.
    for i in range(auth._COOKIE_WARMUP_MAX):
        u = auth.get_current_user()
        assert u is None
        assert st._rerun_calls == i + 1, st._rerun_calls

    # Final call: budget exhausted → no more rerun, counter reset, None.
    u = auth.get_current_user()
    assert u is None
    assert st._rerun_calls == auth._COOKIE_WARMUP_MAX  # unchanged
    assert auth._COOKIE_WARMUP_KEY not in st.session_state, (
        "counter must reset once we give up"
    )
    print(f"[OK] {auth._COOKIE_WARMUP_MAX} retries exhausted → None + counter reset")


def test_successful_auth_resets_counter():
    """After a logged-in user finally gets through, future refreshes start
    from a fresh budget — not the leftover count from the previous attempts."""
    state = _CookieStubState([None, "refresh-token", None])
    auth = _import_auth(cookie_state=state, refresh_works=True)
    import streamlit as st

    auth.get_current_user()  # 1: iframe empty → rerun
    auth.get_current_user()  # 2: cookie populated → success, counter reset
    assert auth._COOKIE_WARMUP_KEY not in st.session_state

    # Now wipe the in-memory session (simulating another hard refresh /
    # cold start) and confirm we get the full 2-rerun budget back.
    st.session_state.clear()
    state.calls = 2  # the next load_refresh_token() call returns None again
    state.tokens_per_call = [None]

    u = auth.get_current_user()
    assert u is None
    assert st.session_state.get(auth._COOKIE_WARMUP_KEY) == 1, (
        "counter should start fresh after a successful auth"
    )
    print("[OK] successful auth resets counter for future refreshes")


def test_existing_session_skips_warmup_entirely():
    """If `sb_access_token` is already in session_state, we don't touch
    the cookie warm-up path at all (no rerun, no counter, no sleep)."""
    state = _CookieStubState([None])  # would block warm-up if hit
    auth = _import_auth(cookie_state=state)
    import streamlit as st

    st.session_state["sb_access_token"]  = "valid-access"
    st.session_state["sb_refresh_token"] = "valid-refresh"
    st.session_state["sb_user_id"]       = "user-9"
    st.session_state["sb_user_email"]    = "x@y.z"
    st.session_state["sb_expires_at"]    = int(time.time()) + 1200

    user = auth.get_current_user()
    assert user is not None
    assert user.user_id == "user-9"
    assert st._rerun_calls == 0, "warm-up should not engage when token is fresh"
    assert auth._COOKIE_WARMUP_KEY not in st.session_state
    print("[OK] valid token → warm-up path bypassed entirely")


def test_expires_at_datetime_is_coerced():
    """Some Supabase client versions return `expires_at` as a datetime
    instead of int. _coerce_epoch handles both shapes — without that the
    `expires_at - 60 < time.time()` math blows up at runtime."""
    state = _CookieStubState([None])
    auth = _import_auth(cookie_state=state)
    import streamlit as st
    from datetime import datetime, timedelta, timezone

    st.session_state["sb_access_token"]  = "valid"
    st.session_state["sb_refresh_token"] = "valid"
    st.session_state["sb_user_id"]       = "u"
    st.session_state["sb_user_email"]    = "e@x.com"
    # datetime instead of int — used to crash, now coerced.
    st.session_state["sb_expires_at"]    = datetime.now(timezone.utc) + timedelta(hours=1)

    user = auth.get_current_user()
    assert user is not None  # didn't crash, didn't try to refresh
    print("[OK] _coerce_epoch handles datetime expires_at")


def test_coerce_epoch_handles_garbage():
    """Malformed values return 0 (= "no expiry tracked") rather than raising."""
    state = _CookieStubState([None])
    auth = _import_auth(cookie_state=state)
    assert auth._coerce_epoch(None)        == 0
    assert auth._coerce_epoch("garbage")   == 0
    assert auth._coerce_epoch({"x": 1})    == 0
    assert auth._coerce_epoch(1700000000)  == 1700000000
    assert auth._coerce_epoch("1700000000") == 1700000000
    assert auth._coerce_epoch(1700000000.7) == 1700000000
    print("[OK] _coerce_epoch robust to any input")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_iframe_not_ready_first_call_triggers_rerun()
    test_iframe_loads_on_second_attempt_recovers_session()
    test_budget_exhausted_returns_none_genuinely_unauthed()
    test_successful_auth_resets_counter()
    test_existing_session_skips_warmup_entirely()
    test_expires_at_datetime_is_coerced()
    test_coerce_epoch_handles_garbage()
    print("\nAll auth cookie warm-up tests passed.")
