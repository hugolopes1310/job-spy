"""Email-OTP auth via Supabase, designed for Streamlit.

Flow:
    1. User types their email → `send_otp(email)` → Supabase emails them a
       6-digit code (uses Supabase's built-in mailer, free tier).
    2. User types the code → `verify_otp(email, code)` → returns a session
       (access_token + refresh_token + user) which we store in st.session_state.
    3. On every rerun, `get_current_session()` returns the cached session,
       refreshing the access_token automatically when needed.
    4. `logout()` wipes the session.

Session keys used:
    sb_access_token, sb_refresh_token, sb_user_id, sb_user_email, sb_expires_at
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import streamlit as st

from app.lib.supabase_client import get_anon_client


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
@dataclass
class CurrentUser:
    user_id: str
    email: str
    access_token: str


# ---------------------------------------------------------------------------
# Send + verify OTP
# ---------------------------------------------------------------------------
def send_otp(email: str) -> tuple[bool, str]:
    """Send a 6-digit code to `email`. Creates the auth.users row if absent.

    Returns:
        (ok, message_for_ui)
    """
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return False, "Email invalide."

    try:
        client = get_anon_client()
        # `should_create_user=True` is the default; we keep it explicit so signup
        # works seamlessly the first time. The DB trigger creates a `pending`
        # profile row automatically.
        client.auth.sign_in_with_otp(
            {
                "email": email,
                "options": {"should_create_user": True},
            }
        )
        return True, f"Code envoyé à {email}. Vérifie ta boîte mail (et les spams)."
    except Exception as e:  # noqa: BLE001
        return False, f"Erreur envoi code : {e}"


def verify_otp(email: str, code: str) -> tuple[bool, str]:
    """Verify the 6-digit code and persist the session.

    Returns:
        (ok, message_for_ui)
    """
    email = (email or "").strip().lower()
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) < 6:
        return False, "Le code doit faire 6 chiffres."

    try:
        client = get_anon_client()
        result = client.auth.verify_otp(
            {"email": email, "token": code, "type": "email"}
        )
    except Exception as e:  # noqa: BLE001
        return False, f"Code invalide ou expiré : {e}"

    session = result.session
    user = result.user
    if not session or not user:
        return False, "Réponse Supabase inattendue (pas de session)."

    st.session_state["sb_access_token"]  = session.access_token
    st.session_state["sb_refresh_token"] = session.refresh_token
    st.session_state["sb_user_id"]       = user.id
    st.session_state["sb_user_email"]    = user.email
    st.session_state["sb_expires_at"]    = session.expires_at
    return True, "Connecté."


# ---------------------------------------------------------------------------
# Session retrieval (called on every page load)
# ---------------------------------------------------------------------------
def get_current_user() -> CurrentUser | None:
    """Return the authenticated user, refreshing the token if needed."""
    token = st.session_state.get("sb_access_token")
    if not token:
        return None

    expires_at = st.session_state.get("sb_expires_at") or 0
    # Refresh if the token expires in <60s.
    if expires_at and expires_at - 60 < time.time():
        if not _try_refresh():
            logout()
            return None
        token = st.session_state["sb_access_token"]

    user_id = st.session_state.get("sb_user_id")
    email   = st.session_state.get("sb_user_email")
    if not (user_id and email and token):
        return None
    return CurrentUser(user_id=user_id, email=email, access_token=token)


def _try_refresh() -> bool:
    refresh = st.session_state.get("sb_refresh_token")
    if not refresh:
        return False
    try:
        client = get_anon_client()
        result = client.auth.refresh_session(refresh)
        sess = result.session
        if not sess:
            return False
        st.session_state["sb_access_token"]  = sess.access_token
        st.session_state["sb_refresh_token"] = sess.refresh_token
        st.session_state["sb_expires_at"]    = sess.expires_at
        return True
    except Exception:  # noqa: BLE001
        return False


def logout() -> None:
    for k in (
        "sb_access_token",
        "sb_refresh_token",
        "sb_user_id",
        "sb_user_email",
        "sb_expires_at",
    ):
        st.session_state.pop(k, None)
    # Also clear onboarding state so re-login starts fresh.
    for k in list(st.session_state.keys()):
        if k.startswith("ob_"):
            st.session_state.pop(k, None)
    try:
        get_anon_client().auth.sign_out()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Authenticated client helper (RLS-enforced under the user's JWT)
# ---------------------------------------------------------------------------
def get_user_scoped_client() -> Any | None:
    """Return a Supabase client whose every request is signed by the current
    user's JWT, so RLS policies match `auth.uid() = user_id` correctly."""
    user = get_current_user()
    if not user:
        return None
    client = get_anon_client()
    # Supabase-py >=2 lets us set the auth header per-request via .postgrest.auth()
    client.postgrest.auth(user.access_token)
    return client
