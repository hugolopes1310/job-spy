"""Auth via Supabase — email+password + OTP fallback, designed for Streamlit.

Flow:
    - 1ère connexion (ou "mot de passe oublié") :
        email → `send_otp(email)` → code 6 chiffres → `verify_otp(email, code)`
        → session créée → `set_password(pwd)` force le user à choisir un pwd.
    - Connexions suivantes :
        email → `signin_with_password(email, pwd)` → session créée. Une étape.
    - `email_has_password(email)` côté UI pour router vers password vs OTP.
    - `logout()` wipe la session.

Session keys used:
    sb_access_token, sb_refresh_token, sb_user_id, sb_user_email, sb_expires_at

FIX-14 — cookie persistence removed
-----------------------------------
We previously persisted Supabase's `refresh_token` in a browser cookie via
`extra-streamlit-components.CookieManager` so a hard refresh (F5) wouldn't
log the user out. The iframe-backed component caused three different bugs
in two weeks (FIX-11 / FIX-12 / FIX-13) — most notably, the warmup loop in
`get_current_user()` was firing `st.rerun()` on every rerender, which
swallowed every widget event on the login screen (forms AND plain buttons),
leaving users with a "spinner forever, no error" experience that we could
not reproduce locally.

Pragmatic call: rip out the cookies. F5 now logs the user out — annoying
but predictable. Auth state lives entirely in `st.session_state`.

NOTE: this module deliberately does NOT use `from __future__ import annotations`.
Python 3.11's @dataclass decorator has a bug (fixed in 3.12) where it crashes
with `'NoneType' object has no attribute '__dict__'` if the dataclass is
defined while the surrounding module is still being registered in sys.modules
under Streamlit's scriptrunner. Eager evaluation of annotations sidesteps it.
"""
import re
import time
from dataclasses import dataclass
from typing import Any

import streamlit as st

from app.lib.klog import log
from app.lib.supabase_client import get_anon_client, get_service_client


# Session-state key used to carry an auth-expiry banner from the moment the
# refresh fails to the next page render (i.e. the login screen). Read once
# by the login page, then cleared.
SESSION_EXPIRED_KEY = "auth_session_expired_msg"

# Minimum enforced length for user-chosen passwords (matches Supabase default).
MIN_PASSWORD_LENGTH = 8


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
        msg = str(e)
        # FIX-11: log every send_otp failure so a paused/unreachable backend
        # surfaces in the cloud logs instead of just a generic "Erreur envoi
        # code" message disappearing in a rerun.
        log("auth.send_otp.error", level="warn",
            err_type=type(e).__name__, err=msg[:200])
        # Supabase rate-limits OTP sends (default: 60s between requests per email).
        # Parse the remaining cooldown to show a friendly message.
        m = re.search(r"after\s+(\d+)\s+seconds?", msg, flags=re.IGNORECASE)
        if m:
            secs = int(m.group(1))
            return False, f"Un code a déjà été envoyé récemment. Réessaie dans {secs} s."
        # Network / backend-down heuristic → clearer banner.
        low = msg.lower()
        if any(k in low for k in (
            "connection", "timeout", "timed out", "name resolution",
            "max retries", "unreachable", "503", "502", "504",
        )):
            return False, (
                "Le service d'authentification ne répond pas. "
                "Réessaie dans quelques minutes — si le problème persiste, "
                "préviens l'équipe."
            )
        return False, f"Erreur envoi code : {msg}"


def verify_otp(email: str, code: str) -> tuple[bool, str]:
    """Verify the 6-digit code and persist the session.

    Returns:
        (ok, message_for_ui)
    """
    email = (email or "").strip().lower()
    code = (code or "").strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) < 4:
        return False, "Saisis le code reçu par email."

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
    """Return the authenticated user, refreshing the token if needed.

    Auth state lives entirely in `st.session_state`. A hard refresh (F5)
    therefore creates a new session and logs the user out — that is the
    deliberate FIX-14 trade-off (see module docstring).

    If the token has expired AND the refresh fails, we set a one-shot
    session flag so the login screen can show "ta session a expiré" instead
    of just silently kicking the user.
    """
    token = st.session_state.get("sb_access_token")
    if not token:
        return None

    expires_at = _coerce_epoch(st.session_state.get("sb_expires_at"))
    # Refresh if the token expires in <60s.
    if expires_at and expires_at - 60 < time.time():
        if not _try_refresh():
            log("auth.session_expired", level="info",
                user_id=st.session_state.get("sb_user_id"))
            st.session_state[SESSION_EXPIRED_KEY] = (
                "Ta session a expiré. Reconnecte-toi pour continuer."
            )
            logout()
            return None
        token = st.session_state["sb_access_token"]

    user_id = st.session_state.get("sb_user_id")
    email   = st.session_state.get("sb_user_email")
    if not (user_id and email and token):
        return None

    return CurrentUser(user_id=user_id, email=email, access_token=token)


def _coerce_epoch(value: Any) -> int:
    """Best-effort cast of a session expiry to a Unix timestamp (int).

    Supabase mostly returns `expires_at` as an int, but some client versions
    surface it as a `datetime`. We accept both — and shrug off anything else
    so a malformed value doesn't blow up the auth path."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    # datetime → epoch
    try:
        from datetime import datetime
        if isinstance(value, datetime):
            return int(value.timestamp())
    except Exception:  # noqa: BLE001
        pass
    # Last resort: try to parse a numeric string.
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def consume_session_expired_message() -> str | None:
    """Pop the one-shot "session expired" message, if any.

    Login pages call this and render the returned string as an info banner.
    Subsequent calls return None (banner is not sticky).
    """
    return st.session_state.pop(SESSION_EXPIRED_KEY, None)


def _apply_session(sess: Any, user: Any | None = None) -> None:
    """Copy a Supabase Session (+ optional User) into st.session_state."""
    st.session_state["sb_access_token"]  = sess.access_token
    st.session_state["sb_refresh_token"] = sess.refresh_token
    st.session_state["sb_expires_at"]    = sess.expires_at
    if user is not None:
        st.session_state["sb_user_id"]    = user.id
        st.session_state["sb_user_email"] = user.email


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
        _apply_session(sess)
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
# Password auth (returning users)
# ---------------------------------------------------------------------------
def email_has_password(email: str) -> bool:
    """Check (via service_role, since user isn't authed yet) whether the
    profile linked to this email has a password set. Safe to call before login.

    Returns False for unknown emails and on any error — the UI will then
    fall back to the OTP flow which also creates the user if absent.

    FIX-11: previously this swallowed exceptions silently, so when Supabase
    was unreachable (paused free-tier project, network blip, expired keys)
    the login screen would just spin forever as send_otp ALSO timed out,
    with no signal in the server logs. We now log the exception so the next
    backend outage is visible in `[WARN] event=auth.email_has_password.error`
    and the caller can surface a clearer "service unavailable" banner.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    # FIX-13: log entry so we can confirm the call is reached even when
    # everything works (FIX-11 only logged the error path).
    log("auth.email_has_password.start", level="info", email=email)
    try:
        svc = get_service_client()
        res = (
            svc.table("profiles")
            .select("has_password")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        data = res.data or []
        has_pwd = bool(data and data[0].get("has_password"))
        log("auth.email_has_password.done", level="info",
            email=email, has_password=has_pwd, found=bool(data))
        return has_pwd
    except Exception as e:  # noqa: BLE001
        log("auth.email_has_password.error", level="warn",
            err_type=type(e).__name__, err=str(e)[:200])
        return False


def reset_password_flag_by_email(email: str) -> bool:
    """Clear `profiles.has_password` for this email (service_role, no auth needed).

    Used by the "mot de passe oublié" flow: after the user clicks the
    forgot-password link, we flip the flag so that after the OTP the login
    router forces them through `_set_password_screen` again.
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    try:
        get_service_client().table("profiles").update(
            {"has_password": False}
        ).eq("email", email).execute()
        return True
    except Exception:  # noqa: BLE001
        return False


def signin_with_password(email: str, password: str) -> tuple[bool, str]:
    """Email + password login. Creates a Supabase session on success.

    Returns:
        (ok, message_for_ui)
    """
    email = (email or "").strip().lower()
    if "@" not in email:
        return False, "Email invalide."
    if not password:
        return False, "Mot de passe requis."

    # FIX-13: log entry BEFORE the Supabase call so we can confirm the form
    # submission actually reached the auth code. If this event never appears
    # in the cloud logs after a click, the hang is upstream (cookie warmup
    # loop, form-submit event lost, or websocket dropped).
    log("auth.signin.start", level="info", email=email)
    try:
        client = get_anon_client()
        result = client.auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except Exception as e:  # noqa: BLE001
        # Supabase returns 400 for wrong creds — don't leak which part failed
        # to the UI, but DO log it (FIX-11) so a paused/unreachable backend
        # is visible in server logs instead of looking like silent rejection.
        msg = str(e)
        log("auth.signin.error", level="warn",
            err_type=type(e).__name__, err=msg[:200])
        # Heuristic: connection / network / DNS errors → backend down, not creds.
        low = msg.lower()
        if any(k in low for k in (
            "connection", "timeout", "timed out", "name resolution",
            "max retries", "unreachable", "503", "502", "504",
        )):
            return False, (
                "Le service d'authentification ne répond pas. "
                "Réessaie dans quelques minutes — si le problème persiste, "
                "préviens l'équipe."
            )
        return False, "Email ou mot de passe incorrect."

    session = getattr(result, "session", None)
    user = getattr(result, "user", None)
    if not session or not user:
        log("auth.signin.empty_response", level="warn", email=email)
        return False, "Email ou mot de passe incorrect."

    st.session_state["sb_access_token"]  = session.access_token
    st.session_state["sb_refresh_token"] = session.refresh_token
    st.session_state["sb_user_id"]       = user.id
    st.session_state["sb_user_email"]    = user.email
    st.session_state["sb_expires_at"]    = session.expires_at
    log("auth.signin.success", level="info", user_id=user.id, email=email)
    return True, "Connecté."


def set_password(password: str, confirm: str | None = None) -> tuple[bool, str]:
    """Set/replace the current user's password. Requires an active session.

    Also flips `profiles.has_password` to TRUE so future logins skip the OTP
    step.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, f"Le mot de passe doit faire au moins {MIN_PASSWORD_LENGTH} caractères."
    if confirm is not None and password != confirm:
        return False, "Les deux mots de passe ne correspondent pas."

    user = get_current_user()
    if not user:
        return False, "Session expirée. Reconnecte-toi."

    try:
        client = get_anon_client()
        # Attach the current session so updateUser knows who we are.
        client.auth.set_session(
            st.session_state["sb_access_token"],
            st.session_state["sb_refresh_token"],
        )
        client.auth.update_user({"password": password})
    except Exception as e:  # noqa: BLE001
        return False, f"Erreur Supabase : {e}"

    # Flip the flag in profiles (service-role: bypasses RLS).
    try:
        get_service_client().table("profiles").update(
            {"has_password": True}
        ).eq("user_id", user.user_id).execute()
    except Exception:  # noqa: BLE001
        pass  # best effort — the user can still log in, just might see the OTP flow again

    return True, "Mot de passe enregistré."


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
