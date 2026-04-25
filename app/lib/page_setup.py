"""Page bootstrap helper for every Kairo Streamlit page.

Every page in `app/pages/*.py` should call `setup_authed_page()` at import time
to get:
    - st.set_page_config  (consistent title, layout, sidebar)
    - inject_theme()      (Kairo CSS + background + sidebar nav hide)
    - auth guard          (user connected + profile approved)
    - render_sidebar      (logo, user card, navigation, logout)

Usage:

    from app.lib.page_setup import setup_authed_page
    user, profile = setup_authed_page(
        page_title="Mes offres",
        page_icon=":material/dashboard:",
        require_approved=True,
    )
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from app.lib.auth import (
    CurrentUser,
    consume_session_expired_message,
    get_current_user,
    logout,
)
from app.lib.storage import get_profile_by_id
from app.lib.theme import BRAND, inject_theme, render_sidebar


def setup_authed_page(
    *,
    page_title: str,
    page_icon: str = "🎯",
    layout: str = "centered",
    require_approved: bool = True,
    require_admin: bool = False,
) -> tuple[CurrentUser, dict[str, Any]]:
    """One-liner to boot an authenticated page with the Kairo chrome.

    Raises st.stop() if the user isn't authenticated or isn't authorized.
    Returns (user, profile) on success.
    """
    st.set_page_config(
        page_title=f"{page_title} — {BRAND['name']}",
        page_icon=page_icon,
        layout=layout,
        initial_sidebar_state="expanded",
    )
    inject_theme()

    user = get_current_user()
    if user is None:
        _render_unauthed_redirect()
        st.stop()

    profile = get_profile_by_id(user.user_id)
    if not profile:
        st.error(
            "Profil introuvable. Retourne à l'accueil et reconnecte-toi."
        )
        st.page_link("streamlit_app.py", label="Retour à l'accueil",
                     icon=":material/home:")
        st.stop()

    status = profile.get("status") or "pending"
    if require_approved and status != "approved":
        _render_forbidden(status)
        st.stop()

    if require_admin and not profile.get("is_admin"):
        st.error("Accès réservé aux administrateurs.")
        st.page_link("streamlit_app.py", label="Retour à l'accueil",
                     icon=":material/home:")
        st.stop()

    # Sidebar (navigation + logout). Consistent across every authed page.
    render_sidebar(
        email=user.email,
        full_name=profile.get("full_name"),
        is_admin=bool(profile.get("is_admin")),
        on_logout=_logout_and_go_home,
    )

    return user, profile


def _render_unauthed_redirect() -> None:
    expired_msg = consume_session_expired_message()
    if expired_msg:
        title = "Session expirée"
        subtitle = expired_msg
    else:
        title = "Connexion requise"
        subtitle = "Tu dois être connecté pour accéder à cette page."
    st.markdown(
        f"""
<div style="max-width:420px;margin:4rem auto;text-align:center;">
  <h2 style="margin:0 0 0.5rem 0;">{title}</h2>
  <p style="color:#64748B;margin:0 0 1.5rem 0;">
    {subtitle}
  </p>
</div>
        """,
        unsafe_allow_html=True,
    )
    st.page_link("streamlit_app.py", label="Aller à la connexion",
                 icon=":material/login:")


def _render_forbidden(status: str) -> None:
    if status == "pending":
        title = "Accès en attente"
        msg = "Ton accès doit être validé par un administrateur avant que tu puisses utiliser cette page."
    elif status == "revoked":
        title = "Accès révoqué"
        msg = "Ton accès a été retiré. Contacte l'équipe Kairo si tu penses que c'est une erreur."
    else:
        title = "Accès refusé"
        msg = f"Statut du compte : {status!r}."
    st.markdown(
        f"""
<div style="max-width:480px;margin:3rem auto;text-align:center;">
  <h2 style="margin:0 0 0.5rem 0;">{title}</h2>
  <p style="color:#64748B;margin:0 0 1.5rem 0;font-size:0.95rem;line-height:1.55;">{msg}</p>
</div>
        """,
        unsafe_allow_html=True,
    )
    st.page_link("streamlit_app.py", label="Retour à l'accueil",
                 icon=":material/home:")


def _logout_and_go_home() -> None:
    logout()
    st.switch_page("streamlit_app.py")
