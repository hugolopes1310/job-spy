"""Admin panel — approve / revoke users.

Phase 2: Supabase-backed. The admin gate uses the JWT-scoped profile; list/update
calls go through service_role so RLS doesn't hide rows the admin should see.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.auth import get_current_user  # noqa: E402
from app.lib.storage import (  # noqa: E402
    get_profile_by_id,
    list_profiles,
    update_status,
)

st.set_page_config(page_title="Admin — Job Spy", page_icon="🛡", layout="wide")


def _guard() -> dict:
    user = get_current_user()
    if not user:
        st.error("Connecte-toi d'abord.")
        st.page_link("streamlit_app.py", label="← Accueil")
        st.stop()
    profile = get_profile_by_id(user.user_id)
    if not profile or not profile.get("is_admin"):
        st.error("🚫 Accès refusé — réservé aux admins.")
        st.page_link("streamlit_app.py", label="← Accueil")
        st.stop()
    return profile


def _render_section(title: str, profiles: list[dict], actions: list[tuple[str, str, str]]) -> None:
    """actions: list of (button_label, target_status, button_type)."""
    st.markdown(f"### {title}  ({len(profiles)})")
    if not profiles:
        st.caption("_(vide)_")
        return
    for prof in profiles:
        with st.container(border=True):
            c1, c2 = st.columns([3, 2])
            badge = "★ admin" if prof.get("is_admin") else ""
            c1.markdown(
                f"**{prof['email']}** {badge}  \n"
                f"{prof.get('full_name') or '_(prénom non renseigné)_'}  \n"
                f"<sub>demandé le {(prof.get('requested_at') or '?')[:10]}</sub>",
                unsafe_allow_html=True,
            )
            for label, target_status, btn_type in actions:
                if c2.button(
                    label,
                    key=f"{target_status}_{prof['email']}",
                    type=btn_type,  # type: ignore[arg-type]
                    use_container_width=True,
                ):
                    # Admin ops → service_role (bypass RLS, see all rows).
                    update_status(prof["email"], target_status, use_service=True)
                    st.rerun()


def main() -> None:
    _guard()
    st.markdown("## 🛡 Panneau admin")
    st.caption("Approuve ou retire l'accès aux utilisateurs.")

    pending = list_profiles(status="pending", use_service=True)
    approved = list_profiles(status="approved", use_service=True)
    revoked = list_profiles(status="revoked", use_service=True)

    tab1, tab2, tab3 = st.tabs(
        [
            f"⏳ En attente ({len(pending)})",
            f"✅ Approuvés ({len(approved)})",
            f"🚫 Révoqués ({len(revoked)})",
        ]
    )

    with tab1:
        _render_section(
            "Demandes d'accès en attente",
            pending,
            actions=[
                ("✅ Approuver", "approved", "primary"),
                ("🚫 Rejeter", "revoked", "secondary"),
            ],
        )

    with tab2:
        _render_section(
            "Utilisateurs actifs",
            approved,
            actions=[("🚫 Révoquer l'accès", "revoked", "secondary")],
        )

    with tab3:
        _render_section(
            "Utilisateurs révoqués",
            revoked,
            actions=[("♻️ Re-approuver", "approved", "primary")],
        )


if __name__ == "__main__":
    main()
