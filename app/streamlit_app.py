"""Streamlit entry point — Phase 2 (Supabase auth + DB).

Run locally:

    cd job_spy
    streamlit run app/streamlit_app.py

Required secrets (.streamlit/secrets.toml or env vars):
    SUPABASE_URL          = "https://xxx.supabase.co"
    SUPABASE_ANON_KEY     = "eyJ..."
    SUPABASE_SERVICE_KEY  = "eyJ..."         # only needed for admin CLI / scraper
    GROQ_API_KEY          = "gsk_..."        # for the config extractor
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Make `app` importable both as a module and as a script entrypoint.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.auth import (  # noqa: E402
    get_current_user,
    logout,
    send_otp,
    verify_otp,
)
from app.lib.storage import (  # noqa: E402
    get_profile_by_id,
    load_user_config,
    update_full_name,
)

st.set_page_config(page_title="Job Spy", page_icon="🎯", layout="centered")


# ---------------------------------------------------------------------------
# UI bits
# ---------------------------------------------------------------------------
def _header() -> None:
    col1, col2 = st.columns([3, 1])
    col1.markdown("## 🎯 Job Spy")
    col1.caption("Ton radar d'offres, sans spam.")
    user = get_current_user()
    if user:
        col2.caption(f"👤 {user.email}")
        if col2.button("Déconnexion", use_container_width=True):
            logout()
            st.rerun()


# ---------------------------------------------------------------------------
# Login flow — 2 steps, OTP
# ---------------------------------------------------------------------------
def _login_screen() -> None:
    st.markdown("### Connexion")
    st.write(
        "Entre ton email — tu recevras un **code à 6 chiffres** par mail. "
        "À la première connexion, ta demande d'accès est envoyée à Hugo pour validation."
    )

    step = st.session_state.get("login_step", "email")

    if step == "email":
        with st.form("send_otp", clear_on_submit=False):
            email = st.text_input(
                "Email",
                value=st.session_state.get("login_email", ""),
                placeholder="toi@exemple.com",
            )
            submitted = st.form_submit_button("Envoyer le code", type="primary")
        if submitted:
            ok, msg = send_otp(email)
            if ok:
                st.session_state["login_email"] = email.strip().lower()
                st.session_state["login_step"] = "code"
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)
        return

    # Step "code"
    email = st.session_state.get("login_email", "")
    st.info(f"📧 Code envoyé à **{email}**. Vérifie ta boîte mail (et les spams).")
    with st.form("verify_otp", clear_on_submit=False):
        code = st.text_input(
            "Code reçu par email",
            placeholder="12345678",
            max_chars=10,
            help="Supabase envoie un code à 6 ou 8 chiffres selon la config du projet.",
        )
        submitted = st.form_submit_button("Valider", type="primary")
    col1, col2 = st.columns([1, 1])
    if col1.button("← Changer d'email", use_container_width=True):
        st.session_state["login_step"] = "email"
        st.rerun()
    if col2.button("Renvoyer le code", use_container_width=True):
        ok, msg = send_otp(email)
        st.toast(msg, icon="✅" if ok else "❌")

    if submitted:
        ok, msg = verify_otp(email, code)
        if ok:
            # Clear login wizard state.
            for k in ("login_step", "login_email"):
                st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error(msg)


# ---------------------------------------------------------------------------
# Status screens
# ---------------------------------------------------------------------------
def _pending_screen(profile: dict) -> None:
    st.info(
        f"⏳ **Demande en attente**\n\n"
        f"Ta demande pour **{profile['email']}** a bien été enregistrée "
        f"le {(profile.get('requested_at') or '?')[:10]}. "
        f"Tu recevras un email dès qu'Hugo aura validé ton accès."
    )
    st.caption(
        "Si tu ne le connais pas et que tu es arrivé ici par erreur, "
        "tu peux fermer la page — aucune donnée personnelle n'est conservée hors de ton email."
    )


def _revoked_screen(profile: dict) -> None:
    st.error(
        "🚫 **Accès révoqué**\n\n"
        f"L'accès à cet outil pour **{profile['email']}** a été retiré. "
        "Contacte Hugo si tu penses que c'est une erreur."
    )


def _approved_landing(profile: dict, user_id: str) -> None:
    # First-time setup of display name.
    if not (profile.get("full_name") or "").strip():
        with st.form("name_form"):
            st.markdown("### Bienvenue 👋")
            name = st.text_input("Comment tu t'appelles ?", placeholder="Alex")
            if st.form_submit_button("Enregistrer", type="primary"):
                if name.strip():
                    update_full_name(user_id, name.strip())
                    st.rerun()
        return

    config = load_user_config(user_id)

    st.success(f"✅ Bienvenue {profile.get('full_name')} !")
    if profile.get("is_admin"):
        st.caption("🛡 Compte admin")

    if config is None:
        st.markdown(
            "### Première étape : configurer ta recherche\n"
            "On va extraire ton profil depuis ton CV et un petit brief libre. "
            "Compte 5 minutes."
        )
        st.page_link(
            "pages/1_onboarding.py", label="🚀 Démarrer l'onboarding", icon="▶️"
        )
    else:
        st.markdown(
            "### Ta recherche est configurée\n"
            "Tu peux consulter tes offres ou reprendre l'onboarding pour ajuster ta config."
        )
        col1, col2 = st.columns(2)
        col1.page_link("pages/2_dashboard.py", label="📋 Mes offres", icon="📋")
        col2.page_link("pages/1_onboarding.py", label="⚙️ Modifier ma config", icon="⚙️")

    if profile.get("is_admin"):
        st.divider()
        st.page_link("pages/99_admin.py", label="🛡 Panneau admin", icon="🛡")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    _header()
    st.divider()

    user = get_current_user()
    if not user:
        _login_screen()
        return

    profile = get_profile_by_id(user.user_id)
    if profile is None:
        # Trigger should have created it; if not, surface the issue clearly.
        st.error(
            "Profil introuvable. Le trigger Supabase n'a pas créé ta ligne dans "
            "`profiles`. Vérifie que tu as bien exécuté `supabase/schema.sql`."
        )
        if st.button("Déconnexion"):
            logout()
            st.rerun()
        return

    status = profile.get("status", "pending")
    if status == "pending":
        _pending_screen(profile)
    elif status == "revoked":
        _revoked_screen(profile)
    elif status == "approved":
        _approved_landing(profile, user.user_id)
    else:
        st.error(f"Statut inconnu : {status!r}")


if __name__ == "__main__":
    main()
