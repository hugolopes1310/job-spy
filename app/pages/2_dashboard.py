"""Dashboard — Phase 3: real scraped offers scored per user.

The scraper (app/scraper/run.py) populates `user_job_matches`. This page reads
the `user_matches_enriched` view (jobs × matches) under RLS, so a user only
ever sees their own matches.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.auth import get_current_user  # noqa: E402
from app.lib.jobs_store import list_matches_for_user, update_match  # noqa: E402
from app.lib.storage import get_profile_by_id, load_user_config  # noqa: E402

st.set_page_config(page_title="Dashboard — Job Spy", page_icon="📋", layout="wide")


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _score_badge(score: int | None) -> str:
    if score is None:
        return "⚫ N/A"
    if score >= 8:
        return f"🟢 {score}/10"
    if score >= 5:
        return f"🟡 {score}/10"
    return f"🔴 {score}/10"


def _render_match(user_id: str, m: dict) -> None:
    """Render one match card with feedback buttons."""
    analysis = m.get("analysis") or {}
    score = m.get("score")
    job_id = m["job_id"]
    title = m.get("title") or "(sans titre)"
    company = m.get("company") or "?"
    location = m.get("location") or "?"
    url = m.get("url") or "#"
    reason = analysis.get("reason") or ""
    red_flags = analysis.get("red_flags") or []
    strengths = analysis.get("strengths") or []
    status = m.get("status") or "new"
    feedback = m.get("feedback")

    with st.container(border=True):
        head_cols = st.columns([5, 1])
        header = f"**[{title}]({url})**  \n{company} · {location}"
        if m.get("is_repost"):
            header += "  \n♻️ _Republiée_"
        head_cols[0].markdown(header)
        head_cols[1].markdown(f"### {_score_badge(score)}")

        if reason:
            st.caption(reason)

        if strengths or red_flags:
            c1, c2 = st.columns(2)
            if strengths:
                c1.markdown("**Atouts**")
                for s in strengths[:3]:
                    c1.markdown(f"- ✅ {s}")
            if red_flags:
                c2.markdown("**Red flags**")
                for r in red_flags[:3]:
                    c2.markdown(f"- ⚠️ {r}")

        salary = analysis.get("salary")
        deadline = analysis.get("deadline")
        apply_hint = analysis.get("apply_hint")
        meta_bits = [b for b in [
            f"💰 {salary}" if salary else None,
            f"📅 {deadline}" if deadline else None,
            f"📨 {apply_hint}" if apply_hint else None,
        ] if b]
        if meta_bits:
            st.caption(" · ".join(meta_bits))

        # --- Actions ---
        btn_cols = st.columns([1, 1, 1, 1, 2])
        if btn_cols[0].button(
            "👍 Intéressant" + (" ✓" if feedback == "good" else ""),
            key=f"good_{job_id}",
            use_container_width=True,
        ):
            update_match(user_id, job_id, feedback="good")
            st.rerun()
        if btn_cols[1].button(
            "👎 Non merci" + (" ✓" if feedback == "bad" else ""),
            key=f"bad_{job_id}",
            use_container_width=True,
        ):
            update_match(user_id, job_id, feedback="bad", status="rejected")
            st.rerun()
        if btn_cols[2].button(
            "✉️ Postulé" + (" ✓" if feedback == "applied" else ""),
            key=f"applied_{job_id}",
            use_container_width=True,
        ):
            update_match(user_id, job_id, feedback="applied", status="applied")
            st.rerun()
        if status == "new":
            if btn_cols[3].button("👁 Vu", key=f"seen_{job_id}", use_container_width=True):
                update_match(user_id, job_id, seen=True)
                st.rerun()
        else:
            btn_cols[3].caption(f"_{status}_")
        btn_cols[4].markdown(f"[🔗 Ouvrir l'offre]({url})")


def main() -> None:
    user, profile = _guard()
    user_id = user.user_id

    st.markdown("## 📋 Tes offres")
    st.caption(f"👤 {profile.get('full_name') or profile['email']}")

    cfg = load_user_config(user_id)
    if cfg is None:
        st.info("Tu n'as pas encore configuré ta recherche.")
        st.page_link("pages/1_onboarding.py", label="🚀 Démarrer l'onboarding")
        return

    # --- Filters ---
    with st.expander("🎛 Filtres", expanded=False):
        c1, c2, c3 = st.columns(3)
        min_score = c1.slider("Score minimum", 0, 10, 5, 1)
        status_choice = c2.selectbox(
            "Statut",
            ["tous", "nouveaux", "vus", "postulés", "rejetés"],
            index=1,
        )
        limit = c3.number_input("Afficher max", min_value=10, max_value=500, value=100, step=10)

    status_map = {
        "tous": None,
        "nouveaux": "new",
        "vus": "seen",
        "postulés": "applied",
        "rejetés": "rejected",
    }

    matches = list_matches_for_user(
        user_id,
        min_score=min_score if min_score > 0 else None,
        status=status_map[status_choice],
        limit=int(limit),
    )

    if not matches:
        st.info(
            "Aucune offre qui matche ces critères. "
            "Si tu viens d'arriver, le scraper tourne toutes les heures — reviens dans un moment. "
            "Tu peux aussi ajuster ta config pour élargir ta recherche."
        )
        c1, c2 = st.columns(2)
        c1.page_link("pages/1_onboarding.py", label="⚙️ Modifier ma config")
        return

    # --- Summary metrics ---
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Matches affichés", len(matches))
    m2.metric(
        "Score moyen",
        f"{sum(m.get('score') or 0 for m in matches) / max(len(matches), 1):.1f}/10",
    )
    m3.metric("Top score", f"{max((m.get('score') or 0) for m in matches)}/10")
    m4.metric("Nouveaux", sum(1 for m in matches if m.get("status") == "new"))
    st.divider()

    # --- Matches list ---
    for m in matches:
        _render_match(user_id, m)

    st.divider()
    st.page_link("pages/1_onboarding.py", label="⚙️ Modifier ma config")


if __name__ == "__main__":
    main()
