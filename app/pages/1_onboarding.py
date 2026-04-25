"""Onboarding wizard — Kairo v2.

Three screens. The goal is "fill the form for the user, let them confirm".
Everything we can infer from the CV is pre-filled via Gemini before the user
sees it; hard-coded examples have been removed.

    Step 1 — Upload     : CV (required) + cover-letter template (optional).
                          On CV parse we immediately run a Gemini extraction
                          so step 2 opens with fields already populated.
    Step 2 — Profil &   : Editable brief (pre-drafted by Gemini), formations,
             recherche   langues, motivations + all the structured questions
                         (localisation, remote, salaire, contrat, dispo,
                         deal-breakers). One long but scrollable screen.
    Step 3 — Validation : Groq consolidates everything into the scraping/
                          scoring config; user reviews and saves.

Session keys:
    ob_step, ob_cv_text, ob_cv_name, ob_cv_extract,
    ob_cl_template_bytes, ob_cl_template_name,
    ob_brief, ob_education, ob_languages, ob_motivations,
    ob_answers, ob_config
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.career_sites import canonical_label, detect_ats  # noqa: E402
from app.lib.config_extractor import empty_config, extract_user_config  # noqa: E402
from app.lib.cv_extractor import empty_cv_extraction, extract_cv_structured  # noqa: E402
from app.lib.cv_parser import (  # noqa: E402
    CorruptedFileError,
    CVParseConfigError,
    CVParseError,
    EmptyFileError,
    EncryptedPDFError,
    FileTooLargeError,
    ImageOnlyPDFError,
    UnsupportedFormatError,
    parse_cv,
)
from app.lib.page_setup import setup_authed_page  # noqa: E402
from app.lib.storage import (  # noqa: E402
    clear_cover_letter_docx,
    load_cover_letter_text,
    load_cv_text,
    load_user_config,
    save_cover_letter_docx,
    save_cover_letter_text,
    save_cv_text,
    save_user_config,
)
from app.lib.theme import render_stepper, render_wordmark  # noqa: E402


STEP_LABELS = ["Profil", "Recherche", "Validation"]

CONTRACT_OPTIONS = ["CDI", "CDD", "Freelance", "Apprentissage", "Stage"]
CURRENCIES = ["EUR", "CHF", "GBP", "USD"]
REMOTE_LABELS = {
    "full": "100% remote",
    "hybrid_ok": "Hybride OK",
    "onsite_only": "Présentiel",
    "any": "Peu importe",
}
AVAILABILITY_LABELS = {
    "immediate": "Immédiat",
    "1-3_months": "1-3 mois",
    "3-6_months": "3-6 mois",
    "flexible": "Flexible",
}
LANG_LEVEL_OPTIONS = ["native", "C2", "C1", "B2", "B1", "A2", "A1"]


def _guard():
    return setup_authed_page(
        page_title="Configuration",
        page_icon=":material/tune:",
        layout="centered",
        require_approved=True,
    )


# ---------------------------------------------------------------------------
# Session state bootstrap
# ---------------------------------------------------------------------------
def _init_state(user_id: str) -> None:
    if "ob_step" in st.session_state:
        return
    existing = load_user_config(user_id) or {}
    prof = existing.get("profile_summary", {}) if existing else {}
    con = existing.get("constraints", {}) if existing else {}
    sh = existing.get("scoring_hints", {}) if existing else {}

    # Pull previously-saved CV + cover-letter text so re-visiting onboarding
    # doesn't send empty text to the LLM on "Regénérer" (which historically
    # caused the config to be wiped to a near-empty shape on save).
    try:
        saved_cv_text = load_cv_text(user_id) or ""
    except Exception:  # noqa: BLE001
        saved_cv_text = ""
    try:
        saved_cl_text = load_cover_letter_text(user_id) or ""
    except Exception:  # noqa: BLE001
        saved_cl_text = ""

    st.session_state["ob_step"] = 1
    st.session_state["ob_cv_text"] = saved_cv_text
    st.session_state["ob_cv_name"] = "CV enregistré" if saved_cv_text else ""
    # CV extraction (filled by Gemini after CV upload, shape = empty_cv_extraction())
    st.session_state["ob_cv_extract"] = empty_cv_extraction()

    # Cover-letter template (binary DOCX the user uploads)
    st.session_state["ob_cl_template_bytes"] = None
    st.session_state["ob_cl_template_name"] = ""
    # Plain text of the CL template — kept for LLM style reference.
    st.session_state["ob_cl_text"] = saved_cl_text

    # Step 2 fields (pre-filled from CV extraction on first transition)
    st.session_state["ob_brief"] = existing.get("raw_brief", "") or ""
    st.session_state["ob_experiences"] = list(prof.get("experiences") or [])
    st.session_state["ob_education"] = list(prof.get("education") or [])
    st.session_state["ob_languages"] = list(prof.get("languages") or [])
    st.session_state["ob_motivations"] = prof.get("motivations") or ""

    # Structured answers (existing or defaults)
    st.session_state["ob_answers"] = {
        "locations": list(con.get("locations") or []),
        "remote": con.get("remote", "hybrid_ok"),
        "salary_min": con.get("salary_min"),
        "salary_currency": con.get("salary_currency", "EUR"),
        "contract_types": list(con.get("contract_types") or ["CDI"]),
        "availability": con.get("availability", "flexible"),
        "deal_breakers": list(sh.get("deal_breakers") or []),
    }
    st.session_state["ob_config"] = existing or None
    # Keep a pristine copy of the saved config so we can deep-merge on save
    # (never wipe a populated list with an empty one from the LLM).
    st.session_state["ob_existing_config"] = existing or {}


def _step_header(num: int, title: str, caption: str) -> None:
    st.markdown(
        f"""
<div style="margin-bottom:1.2rem;">
  <h2 style="margin:0 0 0.3rem;">{title}</h2>
  <p style="color:#64748B;margin:0;font-size:0.95rem;">{caption}</p>
</div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# STEP 1 — Upload CV + CL template, then fire Gemini extraction
# ---------------------------------------------------------------------------
def _step_1_upload() -> None:
    _step_header(
        1,
        "Ton CV et ton template",
        "Ton CV nous sert à te pré-remplir tout le reste. Le template de cover letter sera respecté pour les générations futures.",
    )

    with st.container(border=True):
        cv_file = st.file_uploader(
            "CV (obligatoire)", type=["pdf", "docx", "txt"], key="ob_cv_uploader"
        )
        cl_file = st.file_uploader(
            "Template de cover letter (optionnel, .docx recommandé)",
            type=["pdf", "docx", "txt"],
            key="ob_cl_uploader",
            help=(
                "On va réutiliser ta mise en page à chaque génération. "
                "Ajoute {{BODY}} dans ton .docx à l'endroit où le texte doit "
                "être injecté — sinon on remplace automatiquement le plus "
                "gros bloc de texte détecté."
            ),
        )

    # --- CV handling : parse + Gemini extraction ---------------------------
    if cv_file is not None and cv_file.name != st.session_state.get("ob_cv_name"):
        try:
            text = parse_cv(cv_file.getvalue(), cv_file.name)
        except EncryptedPDFError as e:
            st.error(f":lock: {e}")
            return
        except ImageOnlyPDFError as e:
            st.error(f":frame_with_picture: {e}")
            return
        except FileTooLargeError as e:
            st.error(f":package: {e}")
            return
        except EmptyFileError as e:
            st.error(str(e))
            return
        except UnsupportedFormatError as e:
            st.error(str(e))
            return
        except CorruptedFileError as e:
            st.error(f":warning: {e}")
            return
        except (CVParseError, CVParseConfigError, RuntimeError) as e:
            st.error(f"Erreur lors de la lecture du CV : {e}")
            return
        if len(text) < 200:
            st.warning(
                f"Texte extrait très court ({len(text)} caractères). "
                "Si ton CV est un PDF image, fournis plutôt une version texte ou DOCX."
            )
        st.session_state["ob_cv_text"] = text
        st.session_state["ob_cv_name"] = cv_file.name
        st.success(f"CV parsé : {cv_file.name} — {len(text)} caractères")

        # Fire LLM extraction synchronously (spinner visible to the user).
        with st.spinner("Analyse du CV par l'IA…"):
            extract = extract_cv_structured(text)
        st.session_state["ob_cv_extract"] = extract

        # New CV = overwrite the profile fields with what the LLM just found.
        # We keep the user's existing `ob_brief` ONLY if they already typed
        # something custom (because the brief accepts refinement beyond the CV).
        if extract.get("experiences"):
            st.session_state["ob_experiences"] = list(extract["experiences"])
        if extract.get("education"):
            st.session_state["ob_education"] = list(extract["education"])
        if extract.get("languages"):
            st.session_state["ob_languages"] = list(extract["languages"])
        if extract.get("motivations_from_cv"):
            st.session_state["ob_motivations"] = extract["motivations_from_cv"]
        if not st.session_state.get("ob_brief"):
            st.session_state["ob_brief"] = extract.get("draft_brief", "")

        # Wipe cached data_editor widget state so each editor re-initializes
        # from the refreshed ob_* values above.
        for k in (
            "ob_experiences_editor",
            "ob_education_editor",
            "ob_languages_editor",
        ):
            st.session_state.pop(k, None)

    # --- CL template handling : keep the raw .docx bytes -------------------
    if cl_file is not None and cl_file.name != st.session_state.get("ob_cl_template_name"):
        raw_bytes = cl_file.getvalue()
        is_docx = cl_file.name.lower().endswith(".docx")
        try:
            cl_text = parse_cv(raw_bytes, cl_file.name)
        except (CVParseError, CVParseConfigError, RuntimeError) as e:
            st.error(f"Erreur lors de la lecture du template : {e}")
            return
        st.session_state["ob_cl_text"] = cl_text
        if is_docx:
            st.session_state["ob_cl_template_bytes"] = raw_bytes
            st.session_state["ob_cl_template_name"] = cl_file.name
            st.success(
                f"Template enregistré : {cl_file.name}. Il sera réutilisé comme coque "
                "pour toutes tes futures cover letters."
            )
        else:
            st.info(
                f"Cover letter parsée ({len(cl_text)} caractères). "
                "La mise en page ne sera pas conservée (format non-DOCX)."
            )

    # --- Preview CV extraction (or error state) ----------------------------
    extract = st.session_state.get("ob_cv_extract") or empty_cv_extraction()
    has_data = bool(
        extract.get("current_role")
        or extract.get("years_xp")
        or extract.get("top_skills")
        or extract.get("education")
        or extract.get("languages")
    )
    err = extract.get("_error") or ""

    if err and st.session_state.get("ob_cv_text"):
        st.warning(
            f"Analyse IA indisponible — {err}",
            icon=":material/warning:",
        )
        if st.button(
            "Re-lancer l'analyse", key="retry_gemini", use_container_width=False
        ):
            with st.spinner("Nouvelle tentative d'analyse…"):
                new_extract = extract_cv_structured(
                    st.session_state.get("ob_cv_text", "")
                )
            st.session_state["ob_cv_extract"] = new_extract
            # Re-seed step 2 fields if they were empty
            if not st.session_state.get("ob_brief"):
                st.session_state["ob_brief"] = new_extract.get("draft_brief", "")
            if not st.session_state.get("ob_experiences"):
                st.session_state["ob_experiences"] = list(
                    new_extract.get("experiences") or []
                )
            if not st.session_state.get("ob_education"):
                st.session_state["ob_education"] = list(
                    new_extract.get("education") or []
                )
            if not st.session_state.get("ob_languages"):
                st.session_state["ob_languages"] = list(
                    new_extract.get("languages") or []
                )
            if not st.session_state.get("ob_motivations"):
                st.session_state["ob_motivations"] = new_extract.get(
                    "motivations_from_cv", ""
                )
            st.rerun()

    if has_data:
        with st.expander("Ce qu'on a compris de ton CV", expanded=True):
            c1, c2 = st.columns([2, 1])
            c1.markdown(
                f"**Rôle actuel** : {extract.get('current_role') or '—'}"
            )
            c2.markdown(f"**XP** : {extract.get('years_xp', 0)} ans")
            if extract.get("top_skills"):
                st.markdown(
                    "**Compétences repérées** : "
                    + ", ".join(extract["top_skills"])
                )
            if extract.get("industries_past"):
                st.markdown(
                    "**Secteurs** : " + ", ".join(extract["industries_past"])
                )
            if extract.get("experiences"):
                st.markdown("**Expériences** :")
                for x in extract["experiences"]:
                    dates = " → ".join(
                        s for s in [x.get("start"), x.get("end")] if s
                    )
                    st.markdown(
                        f"• {x.get('title', '')} @ {x.get('company', '')}"
                        + (f" ({dates})" if dates else "")
                    )
            if extract.get("education"):
                st.markdown(
                    "**Formations** : "
                    + " • ".join(
                        f"{e.get('degree', '')} — {e.get('school', '')}"
                        + (f" ({e.get('year')})" if e.get("year") else "")
                        for e in extract["education"]
                    )
                )
            if extract.get("languages"):
                st.markdown(
                    "**Langues** : "
                    + ", ".join(
                        f"{l.get('lang')} ({l.get('level')})"
                        for l in extract["languages"]
                    )
                )

    st.markdown('<div style="height:1rem;"></div>', unsafe_allow_html=True)
    _, col_next = st.columns([3, 1])
    can_next = bool(st.session_state.get("ob_cv_text", "").strip())
    if col_next.button(
        "Suivant →", disabled=not can_next, type="primary", use_container_width=True
    ):
        st.session_state["ob_step"] = 2
        st.rerun()


# ---------------------------------------------------------------------------
# STEP 2 — Profile details (editable pre-fill) + structured search criteria
# ---------------------------------------------------------------------------
def _step_2_details() -> None:
    _step_header(
        2,
        "Ton profil et ce que tu cherches",
        "On a pré-rempli à partir de ton CV. Vérifie, complète, et ajoute où tu cherches.",
    )

    # ---- 2.a Brief libre --------------------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.25rem;">Ton brief</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.5rem;">'
            "Quelques phrases libres sur le poste, le type de boîte et ce qui te branche. "
            "On l'a rédigé à partir de ton CV — complète et corrige."
            "</div>",
            unsafe_allow_html=True,
        )
        brief = st.text_area(
            "brief",
            value=st.session_state.get("ob_brief", ""),
            height=180,
            key="ob_brief_input",
            label_visibility="collapsed",
            placeholder=(
                "Ex : Je cherche un poste de [role] dans [type de boîte] à [villes]. "
                "J'ai X ans d'XP en [A] et [B]. Ce qui me branche : [motivations]. "
                "Je ne veux pas : [deal-breakers]."
            ),
        )
        st.session_state["ob_brief"] = brief

    # ---- 2.b Expériences professionnelles (éditable) ----------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.25rem;">Expériences professionnelles</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.5rem;">'
            "Extraites de ton CV, du plus récent au plus ancien. "
            "Dates format YYYY-MM ou YYYY ; \"present\" pour l'expérience en cours."
            "</div>",
            unsafe_allow_html=True,
        )
        exp = st.data_editor(
            _ensure_experience_rows(st.session_state.get("ob_experiences") or []),
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "company": st.column_config.TextColumn("Entreprise", width="medium"),
                "title": st.column_config.TextColumn("Intitulé de poste", width="medium"),
                "start": st.column_config.TextColumn("Début", width="small"),
                "end": st.column_config.TextColumn("Fin", width="small"),
                "location": st.column_config.TextColumn("Localisation", width="small"),
            },
            key="ob_experiences_editor",
        )
        st.session_state["ob_experiences"] = _dataeditor_to_list(exp)

    # ---- 2.c Formations (éditable) ----------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.25rem;">Formations</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.5rem;">'
            "Extrait de ton CV. Ajoute / corrige si besoin."
            "</div>",
            unsafe_allow_html=True,
        )
        edu = st.data_editor(
            _ensure_education_rows(st.session_state.get("ob_education") or []),
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "school": st.column_config.TextColumn("École / Université", width="medium"),
                "degree": st.column_config.TextColumn("Diplôme", width="medium"),
                "year": st.column_config.NumberColumn(
                    "Année", min_value=1950, max_value=2100, step=1, format="%d"
                ),
            },
            key="ob_education_editor",
        )
        st.session_state["ob_education"] = _dataeditor_to_list(edu)

    # ---- 2.c Langues -------------------------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.25rem;">Langues parlées</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.5rem;">'
            "Niveau CEFR (C2 = bilingue, C1 = courant, B2 = intermédiaire avancé)."
            "</div>",
            unsafe_allow_html=True,
        )
        langs = st.data_editor(
            _ensure_language_rows(st.session_state.get("ob_languages") or []),
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "lang": st.column_config.TextColumn(
                    "Langue (fr / en / es…)", width="small"
                ),
                "level": st.column_config.SelectboxColumn(
                    "Niveau", options=LANG_LEVEL_OPTIONS, required=True, width="small"
                ),
            },
            key="ob_languages_editor",
        )
        st.session_state["ob_languages"] = _dataeditor_to_list(langs)

    # ---- 2.d Motivations / projets ----------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.25rem;">Motivations & projet</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.5rem;">'
            "Ce qui te tire, où tu te vois dans 3 ans, ce que tu veux éviter. "
            "Utilisé pour personnaliser tes cover letters."
            "</div>",
            unsafe_allow_html=True,
        )
        motiv = st.text_area(
            "motivations",
            value=st.session_state.get("ob_motivations", ""),
            height=110,
            key="ob_motivations_input",
            label_visibility="collapsed",
            placeholder=(
                "Ex : J'aime la structuration produit + le contact client. "
                "Je veux une boîte où j'apprends encore techniquement, pas un pur rôle "
                "management. À terme, je veux basculer vers un rôle hybride tech/produit."
            ),
        )
        st.session_state["ob_motivations"] = motiv

    # ---- 2.e Questions structurées (localisation, remote, salaire…) -------
    _structured_questions_block()

    # ---- Nav --------------------------------------------------------------
    st.markdown('<div style="height:1rem;"></div>', unsafe_allow_html=True)
    col_prev, _, col_next = st.columns([1, 2, 1])
    if col_prev.button("← Retour", use_container_width=True):
        st.session_state["ob_step"] = 1
        st.rerun()
    can_next = (
        len(st.session_state.get("ob_brief", "").strip()) >= 20
        and bool(st.session_state["ob_answers"].get("locations"))
        and bool(st.session_state["ob_answers"].get("contract_types"))
    )
    if col_next.button(
        "Générer ma config →",
        type="primary",
        disabled=not can_next,
        use_container_width=True,
    ):
        st.session_state["ob_step"] = 3
        st.session_state["ob_config"] = None
        st.rerun()
    if not can_next:
        st.caption(
            "→ Complète au minimum : brief (≥ 20 caractères), une ville, un type de contrat."
        )


def _structured_questions_block() -> None:
    existing = st.session_state.get("ob_answers") or {}

    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.5rem;">Où tu cherches ?</div>',
            unsafe_allow_html=True,
        )
        locations_str = st.text_input(
            "Villes / pays (séparés par des virgules)",
            value=", ".join(
                f"{loc.get('city', '')}"
                + (f" ({loc.get('country', '')})" if loc.get("country") else "")
                for loc in (existing.get("locations") or [])
            ),
            placeholder="Ex : Geneva (Switzerland), Lyon (France), London (UK)",
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

    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.5rem;">Salaire annuel minimum brut</div>',
            unsafe_allow_html=True,
        )
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

    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.5rem;">Type(s) de contrat</div>',
            unsafe_allow_html=True,
        )
        contract_types = st.multiselect(
            "Sélectionne tous ceux qui te vont",
            options=CONTRACT_OPTIONS,
            default=existing.get("contract_types") or ["CDI"],
            label_visibility="collapsed",
        )

    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.5rem;">Je peux commencer…</div>',
            unsafe_allow_html=True,
        )
        availability = st.radio(
            "availability",
            options=list(AVAILABILITY_LABELS.keys()),
            format_func=lambda k: AVAILABILITY_LABELS[k],
            horizontal=True,
            index=list(AVAILABILITY_LABELS.keys()).index(existing.get("availability", "flexible"))
            if existing.get("availability") in AVAILABILITY_LABELS
            else 3,
            label_visibility="collapsed",
        )

    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.25rem;">Deal-breakers</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.5rem;">'
            "Mots qui tuent une offre (ex : \"cold calling\", \"middle office\", \"defense\")."
            "</div>",
            unsafe_allow_html=True,
        )
        deal_breakers_str = st.text_input(
            "Mots-clés séparés par des virgules",
            value=", ".join(existing.get("deal_breakers") or []),
            placeholder="back office, cold calling, on-call rotation",
            label_visibility="collapsed",
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


# ---------------------------------------------------------------------------
# STEP 3 — Validation + save
# ---------------------------------------------------------------------------
def _step_3_review(user_id: str) -> None:
    _step_header(
        3, "Valide ta config", "L'IA a consolidé tout ça. Ajuste si besoin, puis sauvegarde."
    )

    if st.session_state.get("ob_config") is None:
        with st.spinner("L'IA analyse ton profil et construit ta config…"):
            cfg = extract_user_config(
                cv_text=st.session_state.get("ob_cv_text", ""),
                cover_letter_text=st.session_state.get("ob_cl_text", ""),
                free_brief=st.session_state.get("ob_brief", ""),
                structured_answers=st.session_state.get("ob_answers", {}),
            )
        if cfg is None:
            st.warning(
                "L'extraction LLM a échoué. On garde tes réponses et tu pourras éditer ci-dessous."
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

        # Merge in the step 2 enriched fields (Groq didn't know about them).
        cfg.setdefault("profile_summary", {})
        if st.session_state.get("ob_experiences"):
            cfg["profile_summary"]["experiences"] = st.session_state["ob_experiences"]
        if st.session_state.get("ob_education"):
            cfg["profile_summary"]["education"] = st.session_state["ob_education"]
        if st.session_state.get("ob_languages"):
            cfg["profile_summary"]["languages"] = st.session_state["ob_languages"]
        if st.session_state.get("ob_motivations"):
            cfg["profile_summary"]["motivations"] = st.session_state["ob_motivations"]

        st.session_state["ob_config"] = cfg

    cfg = st.session_state["ob_config"]

    with st.expander("Résumé du profil (extrait du CV)", expanded=True):
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
        if prof.get("experiences"):
            st.markdown("**Expériences** :")
            for x in prof.get("experiences", []):
                dates = " — ".join(
                    s for s in [x.get("start"), x.get("end")] if s
                )
                st.markdown(
                    f"• {x.get('title', '')} @ {x.get('company', '')}"
                    + (f" ({dates})" if dates else "")
                    + (f" — {x.get('location')}" if x.get("location") else "")
                )
        if prof.get("education"):
            st.markdown("**Formations** :")
            for e in prof.get("education", []):
                st.markdown(
                    f"• {e.get('degree', '')} — {e.get('school', '')}"
                    + (f" ({e.get('year')})" if e.get("year") else "")
                )
        if prof.get("languages"):
            st.markdown(
                "**Langues** : "
                + ", ".join(f"{l.get('lang')} ({l.get('level')})" for l in prof["languages"])
            )
        if prof.get("motivations"):
            st.markdown(f"**Motivations** : {prof['motivations']}")

    with st.expander("Ce que tu cibles", expanded=True):
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

    with st.expander("Contraintes", expanded=False):
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
        st.caption("→ Reviens à l'étape 2 pour les modifier.")

    with st.expander("Sites carrières perso", expanded=False):
        st.caption(
            "Ajoute les pages carrières d'entreprises qui t'intéressent. "
            "On reconnaît automatiquement Greenhouse, Lever, Workable et Ashby "
            "(scraping fiable via leur API). Pour les autres, on fait un best-effort "
            "avec le LLM — si ça échoue, le site sera marqué “non scrappable”."
        )
        existing_sources = list(cfg.get("custom_career_sources") or [])

        # Show status pills for any source that has run at least once.
        if existing_sources:
            for src in existing_sources:
                if not isinstance(src, dict):
                    continue
                status = (src.get("status") or "pending").lower()
                lbl = src.get("label") or src.get("url") or "?"
                if status == "ok":
                    badge = "✅ ok"
                elif status == "not_scrapable":
                    last_err = (src.get("last_error") or "")[:120]
                    last_failed = (src.get("last_failure_at") or "")[:10]
                    badge = (
                        f"⚠️ non scrappable"
                        + (f" (dernier échec {last_failed})" if last_failed else "")
                        + (f" — {last_err}" if last_err else "")
                    )
                else:
                    badge = "⏳ jamais tenté"
                st.markdown(f"- **{lbl}** — {badge}")

        # Build editor rows.
        editor_rows = [
            {
                "url": (s or {}).get("url", ""),
                "label": (s or {}).get("label", ""),
            }
            for s in existing_sources
            if isinstance(s, dict) and (s.get("url") or s.get("label"))
        ]
        if not editor_rows:
            editor_rows = [{"url": "", "label": ""}]

        edited = st.data_editor(
            editor_rows,
            num_rows="dynamic",
            use_container_width=True,
            key="ob_custom_sources_editor",
            column_config={
                "url": st.column_config.TextColumn(
                    "URL carrières",
                    help="Ex: https://boards.greenhouse.io/stripe, https://jobs.lever.co/loft",
                    required=False,
                ),
                "label": st.column_config.TextColumn(
                    "Libellé (optionnel)",
                    help="Sinon on déduit depuis l'URL",
                ),
            },
        )

        # Preview ATS detection on the current edits — helpful feedback.
        prev_by_url = {
            (s.get("url") or "").strip().rstrip("/").lower(): s
            for s in existing_sources
            if isinstance(s, dict) and s.get("url")
        }
        normalized: list[dict] = []
        previews: list[str] = []
        for row in edited or []:
            if not isinstance(row, dict):
                continue
            url = (row.get("url") or "").strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            ats, slug = detect_ats(url)
            label = (row.get("label") or "").strip() or canonical_label(url, ats, slug)
            prev = prev_by_url.get(url.rstrip("/").lower(), {})
            merged = {
                "url": url,
                "label": label,
                "ats_type": ats,
                "slug": slug or "",
                "status": prev.get("status") or "pending",
                "last_error": prev.get("last_error") or "",
                "last_success_at": prev.get("last_success_at") or "",
                "last_failure_at": prev.get("last_failure_at") or "",
                "error_count": int(prev.get("error_count") or 0),
            }
            normalized.append(merged)
            previews.append(
                f"• **{label}** → `{ats}`"
                + (f" ({slug})" if slug else "")
                + (" — LLM fallback" if ats == "generic" else "")
            )

        if previews:
            st.markdown("**Détection** :\n" + "\n".join(previews))

        cfg["custom_career_sources"] = normalized

    with st.expander("Scoring (boost / blacklist)", expanded=False):
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

    with st.expander("JSON brut", expanded=False):
        st.code(json.dumps(cfg, ensure_ascii=False, indent=2), language="json")

    # Compute what's about to change vs the last saved version — if the user
    # is about to drop populated fields, surface a clear warning before save.
    baseline_cfg = st.session_state.get("ob_existing_config") or {}
    before = _count_meaningful_fields(baseline_cfg)
    after = _count_meaningful_fields(cfg)
    losing: list[str] = []
    for field, before_n in before.items():
        if before_n > 0 and after.get(field, 0) < before_n:
            losing.append(f"{field} ({before_n} → {after.get(field, 0)})")

    st.markdown('<div style="height:1rem;"></div>', unsafe_allow_html=True)

    if losing:
        st.warning(
            "⚠️ Certains champs vont perdre du contenu si tu sauvegardes maintenant : "
            + ", ".join(losing)
            + ". Si c'est pas voulu, clique \"Restaurer\" pour ré-injecter la config précédente."
        )
        if st.button("↩︎ Restaurer la config précédente", use_container_width=False):
            # Hard-reset the in-memory cfg to the pristine DB snapshot.
            st.session_state["ob_config"] = dict(baseline_cfg)
            st.rerun()

    col_prev, col_regen, col_save = st.columns([1, 1, 2])
    if col_prev.button("← Retour", use_container_width=True):
        st.session_state["ob_step"] = 2
        st.rerun()
    if col_regen.button(
        "Regénérer",
        use_container_width=True,
        help=(
            "Relance le LLM à partir de ton CV + brief pour reconstruire la "
            "config. L'ancienne config reste en backup — les champs que le "
            "LLM ne trouve pas seront préservés."
        ),
    ):
        st.session_state["ob_config"] = None
        st.rerun()
    if col_save.button(
        "Sauvegarder ma config", type="primary", use_container_width=True
    ):
        # Defensive deep-merge: NEVER replace a populated baseline field with
        # an empty candidate value (was the cause of the 2026-04-19 wipe).
        merged = _deep_merge_preserve(baseline_cfg, cfg)
        save_user_config(user_id, merged)
        if st.session_state.get("ob_cv_text"):
            save_cv_text(user_id, st.session_state["ob_cv_text"])
        if st.session_state.get("ob_cl_text"):
            save_cover_letter_text(user_id, st.session_state["ob_cl_text"])
        # Binary template — fidèle layout preserved.
        if st.session_state.get("ob_cl_template_bytes"):
            save_cover_letter_docx(
                user_id,
                st.session_state["ob_cl_template_bytes"],
                st.session_state.get("ob_cl_template_name") or "template.docx",
            )
        st.success("Config sauvegardée. Le scraper la prendra au prochain run.")
        st.balloons()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _tag_editor(label: str, current: list[str], key: str) -> list[str]:
    raw = st.text_input(label, value=", ".join(current or []), key=f"tag_{key}")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _deep_merge_preserve(baseline: dict, candidate: dict) -> dict:
    """Merge `candidate` on top of `baseline` — but NEVER overwrite a populated
    value in `baseline` with an empty/null value from `candidate`.

    Used to protect the user's existing config from being wiped when the LLM
    returns an incomplete extraction (e.g. because cv_text was empty in the
    session state on "Regénérer").

    Rules:
      - dict ∩ dict           → recurse
      - non-empty baseline    → keep baseline if candidate is empty/None
      - otherwise             → candidate wins (including populated→populated)
    """
    if not isinstance(baseline, dict):
        return candidate if candidate not in (None, "", [], {}) else baseline

    out = dict(baseline)  # start from baseline
    for k, cand_v in (candidate or {}).items():
        base_v = baseline.get(k)
        if isinstance(base_v, dict) and isinstance(cand_v, dict):
            out[k] = _deep_merge_preserve(base_v, cand_v)
        elif cand_v in (None, "", [], {}):
            # Candidate is empty → preserve whatever was in baseline.
            out[k] = base_v if k in baseline else cand_v
        else:
            out[k] = cand_v
    return out


def _count_meaningful_fields(cfg: dict) -> dict[str, int]:
    """Tiny summary used in the save-confirmation dialog."""
    tgt = cfg.get("target", {}) or {}
    sh = cfg.get("scoring_hints", {}) or {}
    prof = cfg.get("profile_summary", {}) or {}
    return {
        "roles": len(tgt.get("roles", []) or []),
        "target_companies": len(tgt.get("target_companies", []) or []),
        "industries_include": len(tgt.get("industries_include", []) or []),
        "must_have": len(sh.get("must_have", []) or []),
        "nice_to_have": len(sh.get("nice_to_have", []) or []),
        "top_skills": len(prof.get("top_skills", []) or []),
    }


def _ensure_experience_rows(rows: list) -> list[dict]:
    """Normalize experience rows for the data_editor widget."""
    normed = []
    for r in rows or []:
        if isinstance(r, dict):
            normed.append(
                {
                    "company": r.get("company") or "",
                    "title": r.get("title") or "",
                    "start": r.get("start") or "",
                    "end": r.get("end") or "",
                    "location": r.get("location") or "",
                }
            )
    if not normed:
        normed.append(
            {"company": "", "title": "", "start": "", "end": "", "location": ""}
        )
    return normed


def _ensure_education_rows(rows: list) -> list[dict]:
    """Normalize education rows for the data_editor widget (needs dicts with
    consistent keys, even for new empty rows)."""
    normed = []
    for r in rows or []:
        if isinstance(r, dict):
            normed.append(
                {
                    "school": r.get("school") or "",
                    "degree": r.get("degree") or "",
                    "year": r.get("year"),
                }
            )
    if not normed:
        normed.append({"school": "", "degree": "", "year": None})
    return normed


def _ensure_language_rows(rows: list) -> list[dict]:
    normed = []
    for r in rows or []:
        if isinstance(r, dict) and (r.get("lang") or r.get("level")):
            lvl_raw = (r.get("level") or "B2").strip()
            # "native" stays lowercase to match LANG_LEVEL_OPTIONS; CEFR levels uppercased.
            lvl = "native" if lvl_raw.lower() == "native" else lvl_raw.upper()
            if lvl != "native" and lvl not in ("C2", "C1", "B2", "B1", "A2", "A1"):
                lvl = "B2"
            normed.append(
                {
                    "lang": (r.get("lang") or "").lower(),
                    "level": lvl,
                }
            )
    if not normed:
        normed.append({"lang": "", "level": "B2"})
    return normed


def _dataeditor_to_list(value) -> list[dict]:
    """st.data_editor returns a pandas DataFrame OR the same list we passed.
    Normalise it to a list of clean dicts, dropping empty rows."""
    try:
        import pandas as pd  # noqa: F401
        if hasattr(value, "to_dict"):
            rows = value.to_dict(orient="records")
        else:
            rows = list(value)
    except Exception:  # noqa: BLE001
        rows = list(value) if value is not None else []
    cleaned = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # Keep only rows where at least one meaningful value is non-empty.
        if any((v is not None and str(v).strip()) for v in r.values()):
            cleaned.append({k: (v if v is not None else "") for k, v in r.items()})
    return cleaned


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    user, profile = _guard()
    _init_state(user.user_id)

    # Header
    col_hdr, col_right = st.columns([4, 1])
    with col_hdr:
        render_wordmark(size=30)
    with col_right:
        st.markdown('<div style="padding-top:8px;"></div>', unsafe_allow_html=True)
        st.page_link("streamlit_app.py", label="← Accueil")

    step = st.session_state.get("ob_step", 1)
    st.markdown('<div style="height:1rem;"></div>', unsafe_allow_html=True)
    render_stepper(STEP_LABELS, current_idx=step - 1)

    if step == 1:
        _step_1_upload()
    elif step == 2:
        _step_2_details()
    else:
        _step_3_review(user.user_id)


if __name__ == "__main__":
    main()
else:
    main()
