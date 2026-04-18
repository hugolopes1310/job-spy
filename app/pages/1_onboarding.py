"""Onboarding wizard — 4 screens:

    Step 1. Upload CV (+ optional cover letter)
    Step 2. Free-form brief
    Step 3. Five structured questions
    Step 4. Review & edit the extracted config, then save

Session keys used:
    ob_step    : int, current step (1..4)
    ob_cv_text / ob_cv_name / ob_cl_text / ob_brief / ob_answers / ob_config
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.auth import get_current_user  # noqa: E402
from app.lib.config_extractor import empty_config, extract_user_config  # noqa: E402
from app.lib.cv_parser import UnsupportedFormatError, parse_cv  # noqa: E402
from app.lib.storage import (  # noqa: E402
    get_profile_by_id,
    load_user_config,
    save_cover_letter_text,
    save_cv_text,
    save_user_config,
)

st.set_page_config(page_title="Onboarding — Job Spy", page_icon="🚀", layout="centered")


# ---------------------------------------------------------------------------
# Access guard
# ---------------------------------------------------------------------------
def _guard():
    user = get_current_user()
    if not user:
        st.warning("Connecte-toi d'abord depuis la page d'accueil.")
        st.page_link("streamlit_app.py", label="← Accueil")
        st.stop()
    profile = get_profile_by_id(user.user_id)
    if not profile or profile.get("status") != "approved":
        st.error("Tu n'as pas (ou plus) accès à l'onboarding.")
        st.page_link("streamlit_app.py", label="← Accueil")
        st.stop()
    return user, profile


# ---------------------------------------------------------------------------
# Progress bar
# ---------------------------------------------------------------------------
STEPS = [("1", "Upload"), ("2", "Brief"), ("3", "Questions"), ("4", "Review")]


def _progress_bar(current: int) -> None:
    cols = st.columns(len(STEPS))
    for i, (num, label) in enumerate(STEPS, start=1):
        box = cols[i - 1]
        if i < current:
            box.markdown(f"✅ **{num}. {label}**")
        elif i == current:
            box.markdown(f"🔵 **{num}. {label}**")
        else:
            box.markdown(f"◻️ {num}. {label}")
    st.divider()


def _init_state(user_id: str) -> None:
    if "ob_step" in st.session_state:
        return
    existing = load_user_config(user_id)
    st.session_state["ob_step"] = 1
    st.session_state["ob_cv_text"] = ""
    st.session_state["ob_cv_name"] = ""
    st.session_state["ob_cl_text"] = ""
    st.session_state["ob_brief"] = (existing or {}).get("raw_brief", "")
    st.session_state["ob_answers"] = {}
    st.session_state["ob_config"] = existing


# ---------------------------------------------------------------------------
# STEP 1
# ---------------------------------------------------------------------------
def _step_1_upload() -> None:
    st.markdown("### 1. Upload ton CV")
    st.caption("Formats : PDF, DOCX, TXT. Tes fichiers ne sont jamais partagés.")

    cv_file = st.file_uploader(
        "CV (obligatoire)", type=["pdf", "docx", "txt"], key="ob_cv_uploader"
    )
    cl_file = st.file_uploader(
        "Cover letter générique (optionnel)",
        type=["pdf", "docx", "txt"],
        key="ob_cl_uploader",
    )

    if cv_file is not None:
        try:
            text = parse_cv(cv_file.getvalue(), cv_file.name)
        except (UnsupportedFormatError, RuntimeError) as e:
            st.error(str(e))
            return
        if len(text) < 200:
            st.warning(
                f"Texte extrait très court ({len(text)} caractères). "
                "Si ton CV est un PDF image, fournis une version texte ou DOCX."
            )
        st.session_state["ob_cv_text"] = text
        st.session_state["ob_cv_name"] = cv_file.name
        st.success(f"✓ CV parsé : {cv_file.name} ({len(text)} caractères)")

    if cl_file is not None:
        try:
            cl_text = parse_cv(cl_file.getvalue(), cl_file.name)
        except (UnsupportedFormatError, RuntimeError) as e:
            st.error(str(e))
            return
        st.session_state["ob_cl_text"] = cl_text
        st.success(f"✓ Cover letter parsée ({len(cl_text)} caractères)")

    with st.expander("Voir / éditer le texte extrait du CV", expanded=False):
        preview = st.session_state.get("ob_cv_text", "")
        st.text_area(
            "Aperçu (éditable si besoin)",
            value=preview,
            height=300,
            key="ob_cv_text_preview",
        )
        st.session_state["ob_cv_text"] = st.session_state["ob_cv_text_preview"]

    _, col_next = st.columns([3, 1])
    can_next = bool(st.session_state.get("ob_cv_text", "").strip())
    if col_next.button("Suivant →", disabled=not can_next, type="primary"):
        st.session_state["ob_step"] = 2
        st.rerun()


# ---------------------------------------------------------------------------
# STEP 2
# ---------------------------------------------------------------------------
BRIEF_EXAMPLES = {
    "finance": (
        "Je cherche un poste de structurer cross-asset ou d'equity derivatives, "
        "idéalement chez un broker (Leonteq, Vontobel) ou une banque privée à Genève "
        "ou Zurich. J'ai 2 ans d'XP à Paris, j'aimerais passer en Suisse. "
        "Pas intéressé par le back-office ni les rôles purement tech. "
        "Junior/mid seniority."
    ),
    "tech": (
        "Je cherche un poste de développeur backend Python ou lead tech dans une "
        "scale-up lyonnaise, idéalement en fintech ou healthtech. "
        "5 ans d'XP. Télétravail hybride OK, 100% remote aussi mais pas génial. "
        "Je ne veux pas de gros groupe ni de mission de conseil."
    ),
    "produit": (
        "Product Manager senior sur produits B2B SaaS. Paris ou full remote. "
        "J'ai 6 ans dans le produit dont 3 sur du data/analytics. "
        "Je veux une boîte entre 50 et 500 personnes, pas une licorne. "
        "Deal-breaker : agences / ESN."
    ),
}


def _step_2_brief() -> None:
    st.markdown("### 2. Décris le job que tu cherches")
    st.caption(
        "Quelques phrases naturelles, comme à un pote. Plus c'est concret, mieux c'est."
    )

    c1, c2, c3 = st.columns(3)
    if c1.button("💡 Exemple finance", use_container_width=True):
        st.session_state["ob_brief"] = BRIEF_EXAMPLES["finance"]
    if c2.button("💡 Exemple tech", use_container_width=True):
        st.session_state["ob_brief"] = BRIEF_EXAMPLES["tech"]
    if c3.button("💡 Exemple produit", use_container_width=True):
        st.session_state["ob_brief"] = BRIEF_EXAMPLES["produit"]

    brief = st.text_area(
        "Ton brief",
        value=st.session_state.get("ob_brief", ""),
        height=220,
        key="ob_brief_input",
        placeholder=(
            "Ex: Je cherche un poste de X dans une boîte de taille Y à Z. "
            "J'ai N ans d'XP en A et B. Ce qui me branche c'est M1 et M2, "
            "ce qui me casse c'est D1 et D2. Pas de Z1 ni Z2."
        ),
    )
    st.session_state["ob_brief"] = brief

    col_prev, _, col_next = st.columns([1, 2, 1])
    if col_prev.button("← Retour", use_container_width=True):
        st.session_state["ob_step"] = 1
        st.rerun()
    if col_next.button(
        "Suivant →",
        type="primary",
        disabled=len(brief.strip()) < 20,
        use_container_width=True,
    ):
        st.session_state["ob_step"] = 3
        st.rerun()


# ---------------------------------------------------------------------------
# STEP 3
# ---------------------------------------------------------------------------
CONTRACT_OPTIONS = ["CDI", "CDD", "Freelance", "Apprentissage", "Stage"]
CURRENCIES = ["EUR", "CHF", "GBP", "USD"]
REMOTE_LABELS = {
    "full": "100% remote",
    "hybrid_ok": "Hybride OK",
    "onsite_only": "Présentiel uniquement",
    "any": "Peu importe",
}
AVAILABILITY_LABELS = {
    "immediate": "Immédiat",
    "1-3_months": "1-3 mois",
    "3-6_months": "3-6 mois",
    "flexible": "Flexible",
}


def _step_3_questions() -> None:
    st.markdown("### 3. Les 5 questions qui restent")
    st.caption("Ce que ni ton CV ni ton brief ne peuvent deviner.")

    existing = st.session_state.get("ob_answers") or {}

    st.markdown("**📍 Où tu cherches ?**")
    locations_str = st.text_input(
        "Villes / pays (séparés par des virgules)",
        value=", ".join(
            f"{loc.get('city', '')}"
            + (f" ({loc.get('country', '')})" if loc.get("country") else "")
            for loc in (existing.get("locations") or [])
        ),
        placeholder="Ex: Geneva (Switzerland), Lyon (France), London (UK)",
    )
    remote = st.radio(
        "Télétravail ?",
        options=list(REMOTE_LABELS.keys()),
        format_func=lambda k: REMOTE_LABELS[k],
        horizontal=True,
        index=list(REMOTE_LABELS.keys()).index(existing.get("remote", "hybrid_ok"))
        if existing.get("remote") in REMOTE_LABELS
        else 1,
    )
    radius_km = st.slider(
        "Rayon autour de chaque ville (km)",
        min_value=10,
        max_value=100,
        value=int((existing.get("locations") or [{}])[0].get("radius_km", 30))
        if existing.get("locations")
        else 30,
        step=10,
    )

    st.divider()
    st.markdown("**💰 Salaire annuel minimum brut**")
    skip_salary = st.checkbox(
        "Je préfère ne pas filtrer sur le salaire",
        value=existing.get("salary_min") is None,
    )
    col_s1, col_s2 = st.columns([2, 1])
    salary_min = col_s1.number_input(
        "Montant",
        min_value=0,
        max_value=500000,
        value=existing.get("salary_min") or 0,
        step=5000,
        disabled=skip_salary,
    )
    salary_currency = col_s2.selectbox(
        "Devise",
        options=CURRENCIES,
        index=CURRENCIES.index(existing.get("salary_currency", "EUR"))
        if existing.get("salary_currency") in CURRENCIES
        else 0,
        disabled=skip_salary,
    )

    st.divider()
    st.markdown("**📝 Type(s) de contrat**")
    contract_types = st.multiselect(
        "Sélectionne tous ceux qui te vont",
        options=CONTRACT_OPTIONS,
        default=existing.get("contract_types") or ["CDI"],
    )

    st.divider()
    st.markdown("**⏰ Je peux commencer…**")
    availability = st.radio(
        "",
        options=list(AVAILABILITY_LABELS.keys()),
        format_func=lambda k: AVAILABILITY_LABELS[k],
        horizontal=True,
        index=list(AVAILABILITY_LABELS.keys()).index(existing.get("availability", "flexible"))
        if existing.get("availability") in AVAILABILITY_LABELS
        else 3,
        label_visibility="collapsed",
    )

    st.divider()
    st.markdown("**🚫 Deal-breakers**")
    st.caption("Mots qui tuent une offre. Ex: 'cold calling', 'middle office', 'defense'.")
    deal_breakers_str = st.text_input(
        "Mots-clés séparés par des virgules",
        value=", ".join(existing.get("deal_breakers") or []),
        placeholder="back office, cold calling, on-call rotation",
    )

    locations = _parse_locations(locations_str, radius_km)
    st.session_state["ob_answers"] = {
        "locations": locations,
        "remote": remote,
        "salary_min": None if skip_salary else int(salary_min),
        "salary_currency": salary_currency,
        "contract_types": contract_types,
        "availability": availability,
        "deal_breakers": [s.strip() for s in deal_breakers_str.split(",") if s.strip()],
    }

    col_prev, _, col_next = st.columns([1, 2, 1])
    if col_prev.button("← Retour", use_container_width=True):
        st.session_state["ob_step"] = 2
        st.rerun()
    if col_next.button(
        "Générer ma config →",
        type="primary",
        disabled=not (locations and contract_types),
        use_container_width=True,
    ):
        st.session_state["ob_step"] = 4
        st.session_state["ob_config"] = None
        st.rerun()


def _parse_locations(raw: str, radius_km: int) -> list[dict]:
    out: list[dict] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        city, country = chunk, ""
        if "(" in chunk and chunk.endswith(")"):
            city, rest = chunk.split("(", 1)
            country = rest.rstrip(")").strip()
            city = city.strip()
        out.append({"city": city, "country": country, "radius_km": radius_km})
    return out


# ---------------------------------------------------------------------------
# STEP 4
# ---------------------------------------------------------------------------
def _step_4_review(user_id: str) -> None:
    st.markdown("### 4. Valide ta config")

    if st.session_state.get("ob_config") is None:
        with st.spinner("🧠 Groq analyse ton profil et construit ta config..."):
            cfg = extract_user_config(
                cv_text=st.session_state.get("ob_cv_text", ""),
                cover_letter_text=st.session_state.get("ob_cl_text", ""),
                free_brief=st.session_state.get("ob_brief", ""),
                structured_answers=st.session_state.get("ob_answers", {}),
            )
        if cfg is None:
            st.error(
                "L'extraction LLM a échoué. Tu peux remplir manuellement ci-dessous."
            )
            cfg = empty_config()
            ans = st.session_state.get("ob_answers", {})
            cfg["constraints"].update(
                {
                    "locations": ans.get("locations", []),
                    "remote": ans.get("remote", "any"),
                    "contract_types": ans.get("contract_types", ["CDI"]),
                    "salary_min": ans.get("salary_min"),
                    "salary_currency": ans.get("salary_currency", "EUR"),
                    "availability": ans.get("availability", "flexible"),
                }
            )
            cfg["scoring_hints"]["deal_breakers"] = ans.get("deal_breakers", [])
            cfg["raw_brief"] = st.session_state.get("ob_brief", "")
        st.session_state["ob_config"] = cfg

    cfg = st.session_state["ob_config"]
    st.caption("Ajuste chaque section si besoin, puis sauvegarde.")

    with st.expander("👤 Résumé du profil (extrait du CV)", expanded=True):
        prof = cfg.get("profile_summary", {})
        c1, c2 = st.columns(2)
        cfg["profile_summary"]["current_role"] = c1.text_input(
            "Poste actuel", value=prof.get("current_role") or ""
        )
        cfg["profile_summary"]["years_xp"] = c2.number_input(
            "Années d'XP", min_value=0, max_value=50, value=int(prof.get("years_xp") or 0)
        )
        cfg["profile_summary"]["top_skills"] = _tag_editor(
            "Compétences clés", prof.get("top_skills", []), key="ts_skills"
        )
        cfg["profile_summary"]["industries_past"] = _tag_editor(
            "Secteurs traversés", prof.get("industries_past", []), key="ts_indpast"
        )

    with st.expander("🎯 Ce que tu cibles", expanded=True):
        tgt = cfg.get("target", {})
        cfg["target"]["roles"] = _tag_editor(
            "Rôles visés (titres de poste)", tgt.get("roles", []), key="ts_roles"
        )
        cfg["target"]["seniority"] = _tag_editor(
            "Niveaux (junior / mid / senior / lead / head / director)",
            tgt.get("seniority", []),
            key="ts_sen",
        )
        cfg["target"]["industries_include"] = _tag_editor(
            "Secteurs à inclure", tgt.get("industries_include", []), key="ts_indi"
        )
        cfg["target"]["industries_exclude"] = _tag_editor(
            "Secteurs à exclure", tgt.get("industries_exclude", []), key="ts_inde"
        )
        cfg["target"]["target_companies"] = _tag_editor(
            "Boîtes que tu rêves de rejoindre (boost)",
            tgt.get("target_companies", []),
            key="ts_tc",
        )
        cfg["target"]["avoid_companies"] = _tag_editor(
            "Boîtes à éviter", tgt.get("avoid_companies", []), key="ts_ac"
        )

    with st.expander("⚙️ Contraintes", expanded=False):
        con = cfg.get("constraints", {})
        st.write(f"**Localisations** : {con.get('locations', [])}")
        st.write(f"**Remote** : {con.get('remote', 'any')}")
        st.write(f"**Contrats** : {con.get('contract_types', [])}")
        st.write(
            f"**Salaire min** : "
            + (
                f"{con.get('salary_min')} {con.get('salary_currency', '')}"
                if con.get("salary_min")
                else "non filtré"
            )
        )
        st.write(f"**Dispo** : {con.get('availability', '')}")
        st.caption("→ Reviens à l'étape 3 pour les modifier.")

    with st.expander("🔍 Scoring (boost / blacklist)", expanded=False):
        sh = cfg.get("scoring_hints", {})
        cfg["scoring_hints"]["must_have"] = _tag_editor(
            "Must-have (filtre dur)", sh.get("must_have", []), key="ts_mh"
        )
        cfg["scoring_hints"]["nice_to_have"] = _tag_editor(
            "Nice-to-have (boost)", sh.get("nice_to_have", []), key="ts_nh"
        )
        cfg["scoring_hints"]["deal_breakers"] = _tag_editor(
            "Deal-breakers (blacklist)", sh.get("deal_breakers", []), key="ts_db"
        )

    with st.expander("🧑‍💻 JSON brut", expanded=False):
        st.code(json.dumps(cfg, ensure_ascii=False, indent=2), language="json")

    st.divider()
    col_prev, col_regen, col_save = st.columns([1, 1, 2])
    if col_prev.button("← Retour", use_container_width=True):
        st.session_state["ob_step"] = 3
        st.rerun()
    if col_regen.button("🔄 Regénérer", use_container_width=True):
        st.session_state["ob_config"] = None
        st.rerun()
    if col_save.button(
        "✅ Sauvegarder ma config", type="primary", use_container_width=True
    ):
        save_user_config(user_id, cfg)
        if st.session_state.get("ob_cv_text"):
            save_cv_text(user_id, st.session_state["ob_cv_text"])
        if st.session_state.get("ob_cl_text"):
            save_cover_letter_text(user_id, st.session_state["ob_cl_text"])
        st.success("Config sauvegardée. Le scraper la prendra au prochain run.")
        st.balloons()


def _tag_editor(label: str, current: list[str], key: str) -> list[str]:
    raw = st.text_input(label, value=", ".join(current or []), key=f"tag_{key}")
    return [s.strip() for s in raw.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    user, profile = _guard()
    _init_state(user.user_id)

    step = st.session_state.get("ob_step", 1)
    _progress_bar(step)

    if step == 1:
        _step_1_upload()
    elif step == 2:
        _step_2_brief()
    elif step == 3:
        _step_3_questions()
    elif step == 4:
        _step_4_review(user.user_id)


if __name__ == "__main__":
    main()
