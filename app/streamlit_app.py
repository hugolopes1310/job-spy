"""Kairo — Streamlit entry point.

Run locally:

    cd job_spy
    streamlit run app/streamlit_app.py

Required secrets (.streamlit/secrets.toml or env vars):
    SUPABASE_URL          = "https://xxx.supabase.co"
    SUPABASE_ANON_KEY     = "eyJ..."
    SUPABASE_SERVICE_KEY  = "eyJ..."         # admin CLI / scraper
    GROQ_API_KEY          = "gsk_..."        # config extractor + scorer
    GEMINI_API_KEY        = "AIza..."        # scorer fallback
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import streamlit as st

# Make `app` importable both as a module and as a script entrypoint.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.auth import (  # noqa: E402
    MIN_PASSWORD_LENGTH,
    consume_session_expired_message,
    email_has_password,
    get_current_user,
    logout,
    reset_password_flag_by_email,
    send_otp,
    set_password,
    signin_with_password,
    verify_otp,
)
from app.lib.storage import (  # noqa: E402
    get_profile_by_id,
    load_user_config,
    update_full_name,
)
from app.lib.theme import (  # noqa: E402
    BRAND,
    inject_theme,
    render_badge,
    render_hero,
    render_score_badge,
    render_sidebar,
    render_wordmark,
)

st.set_page_config(
    page_title=f"{BRAND['name']} — {BRAND['tagline']}",
    page_icon="🎯",
    layout="centered",
    initial_sidebar_state="expanded",
)
inject_theme()


# ---------------------------------------------------------------------------
# Header (logged-in) — lightweight : juste un petit padding, la nav est en sidebar
# ---------------------------------------------------------------------------
def _authed_header(email: str, *, is_admin: bool = False) -> None:
    """Plus de topbar : la sidebar contient logo + user + navigation + logout.
    On laisse juste un peu d'espace pour que le contenu respire."""
    st.markdown('<div style="height:0.4rem;"></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Login flow — classique : login, signup, forgot, code.
# ---------------------------------------------------------------------------
def _valid_email(email: str) -> bool:
    email = (email or "").strip().lower()
    return "@" in email and "." in email.split("@")[-1]


def _goto(step: str, **extra: Any) -> None:
    """Switch login step + any extra session_state keys, then rerun."""
    st.session_state["login_step"] = step
    for k, v in extra.items():
        st.session_state[k] = v
    st.rerun()


def _login_step_login() -> None:
    """Classic sign-in : email + password on the same screen."""
    expired_msg = consume_session_expired_message()
    if expired_msg:
        st.info(expired_msg, icon=":material/schedule:")
    st.markdown(
        "<div class='kairo-login-head'>"
        "<h3>Connexion</h3>"
        "<p>Bon retour sur Kairo.</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    with st.form("form_login", clear_on_submit=False):
        email = st.text_input(
            "Email",
            value=st.session_state.get("login_email", ""),
            placeholder="toi@exemple.com",
        )
        password = st.text_input(
            "Mot de passe",
            type="password",
            placeholder="••••••••",
        )
        submitted = st.form_submit_button(
            "Se connecter →", type="primary", use_container_width=True
        )
        forgot = st.form_submit_button(
            "Mot de passe oublié ?", use_container_width=True
        )

    if submitted:
        if not _valid_email(email):
            st.error("Email invalide.")
            return
        if not password:
            st.error("Mot de passe requis.")
            return
        email_norm = email.strip().lower()
        st.session_state["login_email"] = email_norm
        # If the account has no pwd yet, route them to "signup-like" OTP flow
        # so they can create one rather than showing a misleading error.
        if not email_has_password(email_norm):
            ok, msg = send_otp(email_norm)
            if ok:
                st.info(
                    "Ce compte n'a pas encore de mot de passe. "
                    "On t'a envoyé un code par email pour le créer."
                )
                _goto("code", otp_context="signup")
            else:
                st.error(msg)
            return
        ok, msg = signin_with_password(email_norm, password)
        if ok:
            for k in ("login_step", "login_email", "otp_context"):
                st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error(msg)

    if forgot:
        email_norm = (email or "").strip().lower()
        if not _valid_email(email_norm):
            st.error("Renseigne d'abord ton email, puis clique à nouveau sur 'Mot de passe oublié ?'.")
            return
        st.session_state["login_email"] = email_norm
        ok, msg = send_otp(email_norm)
        if ok:
            reset_password_flag_by_email(email_norm)
            _goto("code", otp_context="forgot")
        else:
            st.error(msg)

    # --- Signup link ---
    st.markdown(
        '<hr style="margin:1.2rem 0 1rem;border:none;border-top:1px solid #F1F5F9;">'
        '<div style="text-align:center;color:#64748B;font-size:0.9rem;margin-bottom:0.6rem;">'
        'Pas encore de compte ?'
        '</div>',
        unsafe_allow_html=True,
    )
    if st.button("Créer un compte", use_container_width=True, key="go_signup"):
        _goto("signup")


def _login_step_signup() -> None:
    """Create account: email → send OTP → code → set password."""
    st.markdown(
        "<div class='kairo-login-head'>"
        "<h3>Créer un compte</h3>"
        "<p>On t'envoie un code par email pour vérifier ton adresse. "
        "Tu choisiras ton mot de passe juste après.</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    with st.form("form_signup", clear_on_submit=False):
        email = st.text_input(
            "Email",
            value=st.session_state.get("login_email", ""),
            placeholder="toi@exemple.com",
        )
        submitted = st.form_submit_button(
            "Recevoir le code →", type="primary", use_container_width=True
        )
    if st.button("← Retour", use_container_width=True, key="signup_back"):
        _goto("login")
    if submitted:
        if not _valid_email(email):
            st.error("Email invalide.")
            return
        email_norm = email.strip().lower()
        st.session_state["login_email"] = email_norm
        ok, msg = send_otp(email_norm)
        if ok:
            _goto("code", otp_context="signup")
        else:
            st.error(msg)


def _login_step_code() -> None:
    """OTP verification (après signup OU forgot)."""
    email = st.session_state.get("login_email", "")
    ctx = st.session_state.get("otp_context", "signup")
    title = "Vérifie ton email" if ctx == "signup" else "Réinitialiser le mot de passe"
    st.markdown(
        f"<div class='kairo-login-head'>"
        f"<h3>{title}</h3>"
        f"<p>On a envoyé un code à <b>{email}</b>. Saisis-le ci-dessous.</p>"
        f"</div>"
        f'<div style="background:rgba(102,126,234,0.08);border:1px solid rgba(102,126,234,0.2);'
        f'border-radius:10px;padding:0.7rem 1rem;margin:0 0 1rem 0;font-size:0.88rem;color:#475569;'
        f'text-align:center;">'
        f'Regarde ta boîte mail (et les indésirables).'
        f'</div>',
        unsafe_allow_html=True,
    )
    # Scoped CSS to turn the OTP input into a big, spaced, monospace field
    # — reads like 6 separated "cases" without needing a custom component.
    st.markdown(
        """
<style>
div[data-testid="stTextInput"] input[maxlength="6"] {
  font-family: 'JetBrains Mono', 'SF Mono', Menlo, Monaco, Consolas, monospace;
  font-size: 2.1rem;
  font-weight: 600;
  color: #667eea;
  letter-spacing: 1.1rem;
  text-align: center;
  padding: 1.1rem 0.6rem 1.1rem 1.7rem;  /* extra left so spacing looks centered */
  border-radius: 14px;
  border: 1.5px solid #E2E8F0;
  background:
    linear-gradient(90deg, rgba(102,126,234,0.05), rgba(118,75,162,0.05));
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
div[data-testid="stTextInput"] input[maxlength="6"]:focus {
  border-color: #667eea;
  box-shadow: 0 0 0 3px rgba(102,126,234,0.18);
  outline: none;
}
div[data-testid="stTextInput"] input[maxlength="6"]::placeholder {
  color: #CBD5E1;
  font-weight: 500;
}
</style>
        """,
        unsafe_allow_html=True,
    )
    with st.form("form_verify_otp", clear_on_submit=False):
        code = st.text_input(
            "Code",
            placeholder="······",
            max_chars=6,
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button(
            "Valider →", type="primary", use_container_width=True
        )
    col1, col2 = st.columns(2)
    if col1.button("← Retour", use_container_width=True, key="otp_back"):
        _goto("login")
    if col2.button("Renvoyer le code", use_container_width=True, key="otp_resend"):
        ok, msg = send_otp(email)
        st.toast(msg, icon="✅" if ok else "❌")
    if submitted:
        ok, msg = verify_otp(email, code)
        if ok:
            for k in ("login_step", "login_email", "otp_context"):
                st.session_state.pop(k, None)
            st.rerun()
        else:
            st.error(msg)


_LOGIN_CSS = """
<style>
/* Hide Streamlit's sidebar + its collapse toggle on the login screen —
   it's empty, has no navigation to offer, and looks out of place. */
[data-testid="stSidebar"],
[data-testid="collapsedControl"],
[data-testid="stSidebarCollapseButton"] {
  display: none !important;
}

/* Let the centered layout breathe on wide screens but stay readable. */
.block-container {
  max-width: 460px;
  padding-top: 3.5rem;
}

/* Centered title + subtitle above the form — aligns with the Kairo hero. */
.kairo-login-head {
  text-align: center;
  margin: 0.4rem 0 1.6rem 0;
}
.kairo-login-head h3 {
  margin: 0;
  font-size: 1.35rem;
  font-weight: 700;
  color: #0F172A;
  letter-spacing: -0.01em;
}
.kairo-login-head p {
  margin: 0.4rem 0 0 0;
  color: #64748B;
  font-size: 0.94rem;
  line-height: 1.5;
}

/* Inputs — softer, no gradient, consistent focus state. */
div[data-testid="stTextInput"] > div > div > input {
  border: 1px solid #E2E8F0 !important;
  border-radius: 10px !important;
  padding: 0.7rem 0.9rem !important;
  background: #FFFFFF !important;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
div[data-testid="stTextInput"] > div > div > input:focus {
  border-color: #667eea !important;
  box-shadow: 0 0 0 3px rgba(102,126,234,0.15) !important;
  outline: none !important;
}
div[data-testid="stTextInput"] label {
  color: #475569 !important;
  font-size: 0.86rem !important;
  font-weight: 500 !important;
}

/* Tighter form spacing. */
form[data-testid="stForm"] {
  border: none !important;
  padding: 0 !important;
  background: transparent !important;
}

/* Discreet separator between the login and the "Pas encore de compte ?" link. */
.kairo-login-sep {
  border: none;
  border-top: 1px solid #F1F5F9;
  margin: 1.4rem 0 1rem 0;
}
</style>
"""


def _login_screen() -> None:
    # Scoped login-only CSS — hides sidebar + tightens card visuals.
    st.markdown(_LOGIN_CSS, unsafe_allow_html=True)

    render_hero()  # Kairo logo + title + tagline

    step = st.session_state.get("login_step", "login")
    if step == "signup":
        _login_step_signup()
    elif step == "code":
        _login_step_code()
    else:
        _login_step_login()

    st.markdown(
        '<div style="text-align:center;margin-top:2.4rem;color:#94A3B8;font-size:0.82rem;">'
        'Pas de pub · Ton CV reste privé · Résiliation en 1 clic'
        '</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Set-password screen — forcé juste après la 1re OTP (ou après "pwd oublié")
# ---------------------------------------------------------------------------
def _set_password_screen(email: str) -> None:
    with st.container(border=True):
        st.markdown(
            "#### Choisis un mot de passe"
            "<p style='color:#64748B;margin:0.25rem 0 1.2rem 0;font-size:0.93rem;'>"
            f"Tu es connecté comme <b>{email}</b>. "
            f"Définis un mot de passe pour les prochaines connexions — "
            f"au moins <b>{MIN_PASSWORD_LENGTH} caractères</b>."
            "</p>",
            unsafe_allow_html=True,
        )
        with st.form("set_password_form", clear_on_submit=False):
            pwd = st.text_input(
                "Mot de passe",
                type="password",
                placeholder="••••••••",
                key="set_pwd",
            )
            confirm = st.text_input(
                "Confirmer",
                type="password",
                placeholder="••••••••",
                key="set_pwd_confirm",
            )
            submitted = st.form_submit_button(
                "Valider →", type="primary", use_container_width=True
            )
        if submitted:
            ok, msg = set_password(pwd, confirm=confirm)
            if ok:
                st.toast("Mot de passe enregistré ✅")
                st.rerun()
            else:
                st.error(msg)


# ---------------------------------------------------------------------------
# Status screens
# ---------------------------------------------------------------------------
def _pending_screen(profile: dict) -> None:
    with st.container(border=True):
        st.markdown(
            f"""
<div style="text-align:center;padding:1rem 0.5rem 0.5rem;">
  <div style="font-size:2.4rem;margin-bottom:0.5rem;">⏳</div>
  <h3 style="margin:0;">Demande en attente</h3>
  <p style="color:#64748B;margin:0.6rem 0 0;font-size:0.93rem;">
    Ta demande pour <b>{profile['email']}</b> est enregistrée
    {('le ' + (profile.get('requested_at') or '?')[:10]) if profile.get('requested_at') else ''}.
    Tu recevras un email dès que ton accès est validé.
  </p>
</div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown(
        '<div style="text-align:center;margin-top:1.5rem;color:#94A3B8;font-size:0.82rem;max-width:480px;margin-left:auto;margin-right:auto;">'
        'Si tu es arrivé ici par erreur, tu peux fermer la page — aucune donnée personnelle n\'est conservée hors de ton email.'
        '</div>',
        unsafe_allow_html=True,
    )


def _revoked_screen(profile: dict) -> None:
    with st.container(border=True):
        st.markdown(
            f"""
<div style="text-align:center;padding:1rem 0.5rem 0.5rem;">
  <div style="font-size:2.4rem;margin-bottom:0.5rem;">🚫</div>
  <h3 style="margin:0;color:#DC2626;">Accès révoqué</h3>
  <p style="color:#64748B;margin:0.6rem 0 0;font-size:0.93rem;">
    L'accès pour <b>{profile['email']}</b> a été retiré.
    Contacte l'équipe Kairo si tu penses que c'est une erreur.
  </p>
</div>
            """,
            unsafe_allow_html=True,
        )


def _approved_landing(profile: dict, user_id: str) -> None:
    # ---- 1) First-time setup of display name --------------------------------
    if not (profile.get("full_name") or "").strip():
        st.markdown(
            "<h2 style='margin:0 0 0.4rem 0;'>Bienvenue sur Kairo</h2>"
            "<p style='color:#64748B;margin:0 0 1.2rem 0;font-size:0.96rem;'>"
            "Avant de commencer, comment doit-on t'appeler&nbsp;?"
            "</p>",
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            with st.form("name_form"):
                name = st.text_input(
                    "Prénom",
                    placeholder="Ton prénom",
                    label_visibility="collapsed",
                )
                if st.form_submit_button(
                    "Continuer →", type="primary", use_container_width=True
                ):
                    if name.strip():
                        update_full_name(user_id, name.strip())
                        st.rerun()
        return

    first_name = (profile.get("full_name") or profile["email"]).split()[0]
    is_admin   = bool(profile.get("is_admin"))
    config     = load_user_config(user_id)

    # ---- 2) Pas de config → redirection directe vers l'onboarding -----------
    #   Plus de landing CTA intermédiaire : dès qu'un user vient de se créer
    #   un compte + mot de passe, on l'envoie direct dans le wizard de
    #   configuration. Streamlit's st.switch_page fait un rerun propre vers
    #   la page demandée.
    if config is None:
        st.switch_page("pages/1_onboarding.py")
        return

    # ---- 3) Config présente → vrai dashboard avec KPIs -----------------------
    try:
        from app.lib.jobs_store import get_user_kpis, list_top_matches_preview
        kpis = get_user_kpis(user_id)
        top_matches = list_top_matches_preview(user_id, limit=3)
    except Exception:  # noqa: BLE001
        kpis = {"total": 0, "top_matches": 0, "new_24h": 0, "new_7d": 0,
                "unseen": 0, "applied": 0, "last_scored_at": None, "avg_top5": None}
        top_matches = []

    # Header de section : prénom + dernière MAJ
    last_run_str = _format_relative(kpis.get("last_scored_at"))
    st.markdown(
        f"""
<div style="display:flex;align-items:flex-end;justify-content:space-between;
            gap:1rem;flex-wrap:wrap;margin-bottom:1.4rem;">
  <div>
    <h2 style="margin:0 0 0.25rem 0;">Bonjour {first_name}.</h2>
    <p style="color:#64748B;margin:0;font-size:0.95rem;">
      Voici l'état de ta recherche aujourd'hui.
    </p>
  </div>
  <div style="color:#94A3B8;font-size:0.82rem;font-weight:500;
              padding-bottom:0.4rem;white-space:nowrap;">
    {('Dernière mise à jour ' + last_run_str) if last_run_str else 'En attente du premier passage'}
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )

    _render_kpi_row(kpis)

    st.markdown('<div style="height:1.6rem;"></div>', unsafe_allow_html=True)

    # ---- Top matches preview + actions latérales ----------------------------
    left, right = st.columns([3, 2], gap="large")

    with left:
        st.markdown(
            """
<div style="display:flex;align-items:baseline;justify-content:space-between;
            margin-bottom:0.8rem;">
  <h3 style="margin:0;font-size:1.05rem;">Tes meilleurs matches</h3>
</div>
            """,
            unsafe_allow_html=True,
        )
        if not top_matches:
            with st.container(border=True):
                st.markdown(
                    """
<div style="padding:1.4rem 0.4rem;text-align:center;color:#64748B;">
  <div style="font-weight:600;color:#0F172A;margin-bottom:0.3rem;">
    Pas encore de matches scorés
  </div>
  <div style="font-size:0.9rem;line-height:1.5;">
    Le scraper tourne automatiquement plusieurs fois par jour.<br>
    Reviens d'ici quelques heures.
  </div>
</div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            for m in top_matches:
                _render_match_preview_card(m)
        st.markdown('<div style="height:0.6rem;"></div>', unsafe_allow_html=True)
        st.page_link(
            "pages/2_dashboard.py",
            label="Voir toutes les offres",
            icon=":material/arrow_forward:",
        )

    with right:
        st.markdown(
            "<h3 style='margin:0 0 0.8rem 0;font-size:1.05rem;'>Raccourcis</h3>",
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            st.markdown(
                """
<div style="padding:0.2rem 0;">
  <div style="font-weight:600;color:#0F172A;margin-bottom:0.2rem;">
    Ma configuration
  </div>
  <div style="color:#64748B;font-size:0.86rem;line-height:1.5;margin-bottom:0.6rem;">
    CV, secteurs, localisation, signaux d'alerte.
  </div>
</div>
                """,
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/1_onboarding.py",
                label="Modifier ma recherche",
                icon=":material/tune:",
            )
        with st.container(border=True):
            st.markdown(
                """
<div style="padding:0.2rem 0;">
  <div style="font-weight:600;color:#0F172A;margin-bottom:0.2rem;">
    Toutes mes offres
  </div>
  <div style="color:#64748B;font-size:0.86rem;line-height:1.5;margin-bottom:0.6rem;">
    Liste complète, filtres et feedback.
  </div>
</div>
                """,
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/2_dashboard.py",
                label="Ouvrir le dashboard",
                icon=":material/dashboard:",
            )

    if is_admin:
        st.markdown(
            '<hr style="margin:2.4rem 0 1rem 0;border:none;border-top:1px solid #E2E8F0;">',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div style="display:flex;align-items:center;gap:0.5rem;color:#94A3B8;'
            'font-size:0.78rem;font-weight:600;letter-spacing:0.08em;'
            'text-transform:uppercase;margin-bottom:0.5rem;">'
            'Administration</div>',
            unsafe_allow_html=True,
        )
        st.page_link(
            "pages/99_admin.py",
            label="Panneau admin",
            icon=":material/shield:",
        )


# ---------------------------------------------------------------------------
# Landing helpers
# ---------------------------------------------------------------------------
def _format_relative(iso_ts: str | None) -> str:
    """'2026-04-19T08:30:00+00:00' → 'il y a 2 h' / 'il y a 3 j'."""
    if not iso_ts:
        return ""
    from datetime import datetime, timezone
    try:
        # Supabase returns timestamps with timezone; fromisoformat handles it on 3.11+
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return ""
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return "à l'instant"
    if secs < 3600:
        return f"il y a {secs // 60} min"
    if secs < 86400:
        return f"il y a {secs // 3600} h"
    return f"il y a {secs // 86400} j"


def _render_kpi_row(kpis: dict) -> None:
    """4 tuiles métiers — sans st.metric (trop large) pour un rendu serré."""
    total       = int(kpis.get("total") or 0)
    top         = int(kpis.get("top_matches") or 0)
    new_1h      = int(kpis.get("new_1h") or 0)
    new_24h     = int(kpis.get("new_24h") or 0)

    cards = [
        ("Offres scorées",  f"{total}",    "Total dans ta base"),
        ("Top matches",     f"{top}",      "Score ≥ 8 / 10"),
        ("Nouvelles 24h",   f"{new_24h}",  "Sur les dernières 24 h"),
        ("Nouvelles 1h",    f"{new_1h}",   "Sur la dernière heure"),
    ]

    st.markdown(
        """
<style>
.kairo-kpi-grid {
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:0.85rem;
}
.kairo-kpi {
  background:#FFFFFF;
  border:1px solid #E2E8F0;
  border-radius:14px;
  padding:1.1rem 1.2rem;
  box-shadow:0 1px 2px rgba(15,23,42,0.04);
  transition:box-shadow 0.2s ease, transform 0.2s ease, border-color 0.2s ease;
}
.kairo-kpi:hover {
  box-shadow:0 4px 14px rgba(15,23,42,0.06);
  border-color:#CBD5E1;
}
.kairo-kpi-label {
  font-size:0.75rem; font-weight:600; letter-spacing:0.06em;
  text-transform:uppercase; color:#94A3B8;
  margin-bottom:0.45rem;
}
.kairo-kpi-value {
  font-size:1.85rem; font-weight:700; color:#0F172A;
  letter-spacing:-0.02em; line-height:1.1;
}
.kairo-kpi-hint {
  color:#64748B; font-size:0.78rem; font-weight:500;
  margin-top:0.3rem;
}
@media (max-width: 720px) {
  .kairo-kpi-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
</style>
        """,
        unsafe_allow_html=True,
    )
    cells = "".join(
        f"""
<div class="kairo-kpi">
  <div class="kairo-kpi-label">{label}</div>
  <div class="kairo-kpi-value">{value}</div>
  <div class="kairo-kpi-hint">{hint}</div>
</div>
        """
        for label, value, hint in cards
    )
    st.markdown(f'<div class="kairo-kpi-grid">{cells}</div>', unsafe_allow_html=True)


def _render_match_preview_card(m: dict) -> None:
    """Compact card for a single top-match in the landing preview."""
    score = m.get("score")
    score_html = render_score_badge(score)
    title = (m.get("title") or "Sans titre").strip()
    company = (m.get("company") or "—").strip()
    location = (m.get("location") or "").strip()
    site = (m.get("site") or "").strip()
    url = m.get("url") or "#"

    meta_parts = []
    if company:
        meta_parts.append(company)
    if location:
        meta_parts.append(location)
    if site:
        meta_parts.append(site.capitalize())
    meta_html = '<span style="color:#CBD5E1;margin:0 0.5rem;">·</span>'.join(
        f'<span>{p}</span>' for p in meta_parts
    )

    st.markdown(
        f"""
<a href="{url}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">
  <div style="
    background:#FFFFFF;
    border:1px solid #E2E8F0;
    border-radius:14px;
    padding:1rem 1.2rem;
    margin-bottom:0.6rem;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:1rem;
    box-shadow:0 1px 2px rgba(15,23,42,0.04);
    transition:border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
  "
  onmouseover="this.style.borderColor='#667eea';this.style.boxShadow='0 4px 14px rgba(102,126,234,0.10)';"
  onmouseout="this.style.borderColor='#E2E8F0';this.style.boxShadow='0 1px 2px rgba(15,23,42,0.04)';"
  >
    <div style="min-width:0;flex:1;">
      <div style="font-weight:600;color:#0F172A;font-size:0.98rem;
                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
                  margin-bottom:0.2rem;">
        {title}
      </div>
      <div style="color:#64748B;font-size:0.84rem;
                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
        {meta_html}
      </div>
    </div>
    <div style="flex-shrink:0;">{score_html}</div>
  </div>
</a>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    user = get_current_user()

    if not user:
        _login_screen()
        return

    profile = get_profile_by_id(user.user_id)
    is_admin = bool(profile and profile.get("is_admin"))
    render_sidebar(
        email=user.email,
        full_name=(profile or {}).get("full_name"),
        is_admin=is_admin,
        on_logout=lambda: (logout(), st.rerun()),
    )
    _authed_header(user.email, is_admin=is_admin)
    if profile is None:
        st.error(
            "Profil introuvable. Le trigger Supabase n'a pas créé ta ligne dans "
            "`profiles`. Vérifie que tu as bien exécuté `supabase/schema.sql`."
        )
        if st.button("Déconnexion"):
            logout()
            st.rerun()
        return

    # Force password setup on 1st login (or after "forgot pwd" flow).
    if not profile.get("has_password"):
        _set_password_screen(user.email)
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
