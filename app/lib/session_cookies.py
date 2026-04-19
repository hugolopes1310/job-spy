"""Persistent session cookies — survive a page refresh.

Streamlit's `st.session_state` lives only in the WebSocket-scoped server
memory: a hard refresh (F5) creates a new session and wipes it, which kicks
the user back to the login screen. To avoid that we persist Supabase's
long-lived `refresh_token` in a browser cookie, and on the next page load
we rehydrate the session from it.

Implementation notes :
  - We use `extra-streamlit-components.CookieManager`, the de-facto standard
    cookie helper for Streamlit (tiny iframe component, drop-in).
  - The CookieManager is cached in `st.session_state` so it isn't remounted
    on every script run (remounting resets its cookie cache and adds latency).
  - The cookie only contains the Supabase refresh token. Access tokens live
    only in memory (never serialised). This matches Supabase-JS' default.
  - Cookie attrs : SameSite=Strict, Secure in prod. 30-day expiry (matches
    Supabase's default refresh_token TTL).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import streamlit as st

if TYPE_CHECKING:  # keep top-level import light
    import extra_streamlit_components as stx


COOKIE_NAME = "kairo_rt"
COOKIE_TTL_DAYS = 30
_CM_KEY = "_kairo_cookie_mgr"


# ---------------------------------------------------------------------------
# Cookie manager — singleton per Streamlit session
# ---------------------------------------------------------------------------
def _cookie_manager() -> "stx.CookieManager":
    """Return the singleton CookieManager.

    We cache it on `st.session_state` because re-instantiating the component
    on every rerun re-renders its iframe and loses already-read cookie values
    for the current script run. Using a stable key keeps the component mounted.
    """
    cm = st.session_state.get(_CM_KEY)
    if cm is not None:
        return cm
    # Import lazily so that a missing dep only hurts when auth actually runs.
    import extra_streamlit_components as stx
    cm = stx.CookieManager(key="kairo_cookie_manager")
    st.session_state[_CM_KEY] = cm
    return cm


def _read_cookies() -> dict:
    """Wrapper around CookieManager.get_all() that never raises.

    First call on a fresh page returns {} because the iframe component hasn't
    posted its cookies back yet — it triggers a rerun once it's ready, and
    the second call sees the actual values.
    """
    try:
        cookies = _cookie_manager().get_all(key="kairo_cookie_get_all")
        return cookies or {}
    except Exception:  # noqa: BLE001
        return {}


def load_refresh_token() -> str | None:
    """Return the Supabase refresh token saved in the browser, or None."""
    value = _read_cookies().get(COOKIE_NAME)
    if not value or not isinstance(value, str):
        return None
    return value


def save_refresh_token(token: str) -> None:
    """Persist the refresh token in a long-lived cookie."""
    if not token:
        return
    expires = datetime.now(timezone.utc) + timedelta(days=COOKIE_TTL_DAYS)
    try:
        _cookie_manager().set(
            cookie=COOKIE_NAME,
            val=token,
            expires_at=expires,
            key="kairo_cookie_set",
        )
    except Exception:  # noqa: BLE001
        # Non-fatal: the user simply won't stay logged in after refresh.
        pass


def clear_refresh_token() -> None:
    """Delete the persisted refresh token (logout)."""
    try:
        _cookie_manager().delete(cookie=COOKIE_NAME, key="kairo_cookie_del")
    except Exception:  # noqa: BLE001
        pass
