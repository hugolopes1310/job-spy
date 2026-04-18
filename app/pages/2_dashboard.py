"""Dashboard — shows the user's config recap (offers come in Phase 3).

Phase 2: Supabase auth + DB. Reads by `user_id` (UUID), not email.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.auth import get_current_user  # noqa: E402
from app.lib.storage import get_profile_by_id, load_user_config  # noqa: E402

st.set_page_config(page_title="Dashboard — Job Spy", page_icon="📋", layout="wide")


def _guard() -> tuple[object, dict]:
    user = get_current_user()
    if not user:
        st.warning("Connecte-toi d'abord.")
        st.page_link("streamlit_app.py", label="← Accueil")
        st.stop()
    profile = get_profile_by_id(user.user_id)
    if not profile or profile.get("status") != "approved":
        st.error("🚫 Accès refusé — ton compte n'est pas (ou plus) approuvé.")
        st.page_link("streamlit_app.py", label="← Accueil")
        st.stop()
    return user, profile


def main() -> None:
    user, profile = _guard()

    st.markdown("## 📋 Tes offres")
    st.caption(f"👤 {profile.get('full_name') or profile['email']}")

    cfg = load_user_config(user.user_id)
    if cfg is None:
        st.info("Tu n'as pas encore configuré ta recherche.")
        st.page_link("pages/1_onboarding.py", label="🚀 Démarrer l'onboarding")
        return

    st.success(
        "Ta config est enregistrée. Le scraper (Phase 3) peuplera ce dashboard "
        "avec les offres matchées. Pour l'instant, voici un récap de ta recherche :"
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Rôles visés", len(cfg.get("target", {}).get("roles", [])))
    c2.metric("Localisations", len(cfg.get("constraints", {}).get("locations", [])))
    c3.metric("Deal-breakers", len(cfg.get("scoring_hints", {}).get("deal_breakers", [])))

    with st.expander("🔎 Ta config en détail", expanded=False):
        st.json(cfg)

    st.page_link("pages/1_onboarding.py", label="⚙️ Modifier ma config")


if __name__ == "__main__":
    main()
