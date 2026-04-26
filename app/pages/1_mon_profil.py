"""Mon Profil — Kairo Phase 4.

Replaces the 3-step onboarding wizard. Center of gravity is the **profile
synthesis** (LLM-produced structured object stored in `profile_syntheses`),
not a hand-typed config.

What the page does
------------------
1. Loads the user's *active* synthesis (one row per user, status='active').
2. If absent + a CV is on file in `user_configs.cv_text` → lazy-migration :
   we run `synthesize_profile()` once with the existing config, insert as a
   draft and activate it. The user lands on a populated page on first visit
   instead of an empty form.
3. If absent + no CV → empty-state CV upload flow (parse CV → save → run
   synthesis → activate). Same flow used to onboard a brand-new account.
4. Otherwise → renders cards (summary, role_families, geo, deal_breakers,
   dream_companies, languages) with native widgets to edit, plus the
   open_questions block. Saving inserts a new draft + activates it
   atomically (keeps the previous version archived as audit trail).

Design choices
--------------
* No multi-step wizard. Everything on one page, edit-in-place.
* No JSON brut expander. The synthesis JSON is opaque to the user.
* No heuristic fallback if both LLMs fail — we keep the previous active
  synthesis and surface the error inline. See `ProfileSynthesisError`.
* `st.multiselect` is used for tag-like fields (decision §12 of
  PLAN_PROFILE_SYNTHESIS — native, accessible, no custom JS).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
from app.lib.profile_synthesis_store import (  # noqa: E402
    activate_synthesis,
    archive_active_synthesis,
    insert_synthesis_draft,
    list_synthesis_versions,
    load_active_synthesis,
)
from app.lib.profile_synthesizer import (  # noqa: E402
    PROMPT_VERSION,
    ProfileSynthesisError,
    synthesize_profile,
)
from app.lib.query_builder import build_queries_from_synthesis  # noqa: E402
from app.lib.scorer import build_system_prompt_from_synthesis  # noqa: E402
from app.lib.storage import (  # noqa: E402
    load_cv_text,
    load_user_config,
    save_cv_text,
    save_user_config,
)
from app.lib.theme import render_wordmark  # noqa: E402


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def _guard():
    return setup_authed_page(
        page_title="Mon profil",
        page_icon=":material/badge:",
        layout="centered",
        require_approved=True,
    )


def _section_header(title: str, caption: str = "") -> None:
    st.markdown(
        f"""
<div style="margin-bottom:1rem;">
  <h2 style="margin:0 0 0.3rem;">{title}</h2>
  {f'<p style="color:#64748B;margin:0;font-size:0.95rem;">{caption}</p>' if caption else ''}
</div>
        """,
        unsafe_allow_html=True,
    )


def _weight_bar(weight: float, *, slots: int = 10) -> str:
    """Tiny ASCII gauge for a 0-1 weight. Used in the scoring recap card.

    Renders as `██████░░░░` so the user grasps the relative priority of each
    role_family at a glance, without dragging in a chart lib.
    """
    try:
        w = max(0.0, min(1.0, float(weight)))
    except (TypeError, ValueError):
        w = 0.0
    filled = round(w * slots)
    return "█" * filled + "░" * (slots - filled)


def _render_scoring_recap(syn: dict[str, Any]) -> None:
    """Read-only "how I'll score" panel.

    Surfaces what the LLM understood from the user's CV/config AND the formula
    that drives the per-offer score. Pure visualization — does not introduce
    any st.session_state. Designed to sit between the summary card and the
    editable cards: the user reads the recap, then scrolls down to adjust.
    """
    role_families = list(syn.get("role_families") or [])
    families_active = sorted(
        [f for f in role_families if isinstance(f, dict) and f.get("active", True)],
        key=lambda f: float(f.get("weight") or 0.0),
        reverse=True,
    )
    families_inactive = [
        f for f in role_families
        if isinstance(f, dict) and not f.get("active", True)
    ]

    geo = syn.get("geo") or {}
    primary = [s for s in (geo.get("primary") or []) if s]
    acceptable = [s for s in (geo.get("acceptable") or []) if s]
    exclude = [s for s in (geo.get("exclude") or []) if s]

    sb = syn.get("seniority_band") or {}
    sb_label = (sb.get("label") or "?").strip() or "?"
    yoe_min = sb.get("yoe_min")
    yoe_max = sb.get("yoe_max")
    sb_str = (
        f"{sb_label} ({yoe_min}-{yoe_max} ans XP)"
        if yoe_min is not None and yoe_max is not None
        else sb_label
    )

    dream = [c for c in (syn.get("dream_companies") or []) if c]
    breakers = [b for b in (syn.get("deal_breakers") or []) if b]
    languages = [l for l in (syn.get("languages") or []) if l]

    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.3rem;">'
            "Comment Kairo va te scorer"
            "</div>"
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.8rem;">'
            "Chaque offre reçoit un score 0-10 selon cette formule. "
            "Édite les sections ci-dessous pour ajuster ce que l'IA optimise."
            "</div>",
            unsafe_allow_html=True,
        )

        # --- Formula ---------------------------------------------------------
        st.markdown(
            "**Formule**  \n"
            "`score = 0.4 × match rôle + 0.3 × match géo "
            "+ 0.2 × match séniorité + 0.1 × bonus dream-co`  \n"
            "À ce score on applique :  \n"
            "• un **plafond à 2** si le titre tombe sur un deal-breaker "
            "structurant ou si l'offre est dans une géo exclue,  \n"
            "• un **plancher à 7-8** si l'entreprise est dans tes dream "
            "companies."
        )
        st.markdown("&nbsp;", unsafe_allow_html=True)

        # --- Two-column recap ------------------------------------------------
        col_l, col_r = st.columns(2)

        with col_l:
            st.markdown("**Familles de rôles cibles** · _40 % du score_")
            if not families_active:
                st.caption("_Aucune famille active. Le scraper ne va rien ramener._")
            else:
                for f in families_active[:5]:
                    label = (f.get("label") or "?").strip() or "?"
                    w = float(f.get("weight") or 0.0)
                    st.markdown(
                        f"<div style='font-family:monospace;font-size:0.85rem;'>"
                        f"<span style='color:#0F766E;'>{_weight_bar(w)}</span> "
                        f"&nbsp;<b>{label}</b> "
                        f"<span style='color:#64748B;'>· poids {w:.2f}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                if len(families_active) > 5:
                    st.caption(f"+ {len(families_active) - 5} autres familles actives")
            if families_inactive:
                st.caption(
                    f"{len(families_inactive)} famille(s) désactivée(s) "
                    "(ignorée(s) par le scraper et le scoring)."
                )

            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.markdown("**Séniorité visée** · _20 % du score_")
            st.write(sb_str)

        with col_r:
            st.markdown("**Géographie** · _30 % du score_")
            if primary:
                st.markdown(
                    f"<span style='color:#15803D;'>● Primaire (10/10)</span> "
                    f"&nbsp;{', '.join(primary)}",
                    unsafe_allow_html=True,
                )
            if acceptable:
                st.markdown(
                    f"<span style='color:#B45309;'>● Acceptable (≈7/10)</span> "
                    f"&nbsp;{', '.join(acceptable)}",
                    unsafe_allow_html=True,
                )
            if exclude:
                st.markdown(
                    f"<span style='color:#B91C1C;'>● Exclu (cap à 2)</span> "
                    f"&nbsp;{', '.join(exclude)}",
                    unsafe_allow_html=True,
                )
            if not (primary or acceptable or exclude):
                st.caption("_Aucune géo configurée._")

            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.markdown("**Dream companies** · _booste le score (+10 % et plancher)_")
            if dream:
                head = ", ".join(dream[:6])
                tail = (
                    f" <span style='color:#64748B;'>(+{len(dream) - 6} autres)</span>"
                    if len(dream) > 6 else ""
                )
                st.markdown(head + tail, unsafe_allow_html=True)
            else:
                st.caption("_Aucune. Ajoute des entreprises ci-dessous pour les booster._")

            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.markdown("**Deal-breakers** · _plafond à 2 si dans le titre_")
            if breakers:
                head = ", ".join(breakers[:8])
                tail = (
                    f" <span style='color:#64748B;'>(+{len(breakers) - 8} autres)</span>"
                    if len(breakers) > 8 else ""
                )
                st.markdown(head + tail, unsafe_allow_html=True)
            else:
                st.caption("_Aucun. Toutes les offres seront éligibles au scoring complet._")

        # --- Footer : languages + total search reach -------------------------
        if languages:
            st.markdown("&nbsp;", unsafe_allow_html=True)
            st.caption(f"**Langues** : {', '.join(languages)}")


def _render_diagnostics(syn: dict[str, Any], cv_text: str) -> None:
    """Opt-in expander showing what the scraper and the scorer will actually
    do with this synthesis. Two sub-blocks:

    1. **Scraper preview** — runs `build_queries_from_synthesis()` in dry-run
       mode (no network), counts queries by family, multiplies by the
       per-query results cap to give a rough "jobs scanned per run" estimate.
    2. **Prompt preview** — renders the full system prompt that the LLM will
       see when scoring an offer (`build_system_prompt_from_synthesis`).

    Both are read-only. The expander is collapsed by default so the page
    stays clean for users who don't care about the internals.
    """
    with st.expander("Diagnostics — qu'est-ce que je fais avec ta synthèse ?", expanded=False):
        # --- 1. Scraper preview ---------------------------------------------
        # Defensive: build_queries_from_synthesis is pure and never raises,
        # but a corrupted synthesis could trip an attribute lookup. Guard so
        # the diagnostics block never blocks the rest of the page.
        try:
            queries = build_queries_from_synthesis(syn)
        except Exception as e:  # noqa: BLE001
            st.error(f"Impossible de générer les queries : {type(e).__name__} — {e}")
            queries = []

        results_per_query = 25  # mirror RESULTS_PER_QUERY in app/scraper/run.py
        total_jobs_estimate = len(queries) * results_per_query

        st.markdown("**1. Ce que le scraper va chercher**")
        if not queries:
            st.caption(
                "_Aucune query ne sera émise — vérifie qu'au moins une famille est "
                "active et que `geo.primary` ou `geo.acceptable` n'est pas vide._"
            )
        else:
            # Group queries by search_term (= title) for readability.
            by_title: dict[str, list[str]] = {}
            for q in queries:
                t = q["search_term"]
                by_title.setdefault(t, []).append(q.get("location") or "remote")
            col1, col2, col3 = st.columns(3)
            col1.metric("Queries émises", len(queries))
            col2.metric("Titres distincts", len(by_title))
            col3.metric("Jobs scannés / run (max)", f"~{total_jobs_estimate}")
            with st.expander(f"Détail des {len(queries)} queries", expanded=False):
                for title, locs in by_title.items():
                    st.markdown(
                        f"- **{title}** → "
                        + ", ".join(f"`{l}`" for l in locs)
                    )

        st.markdown("&nbsp;", unsafe_allow_html=True)

        # --- 2. Prompt preview ----------------------------------------------
        st.markdown("**2. Le prompt envoyé au LLM pour scorer chaque offre**")
        st.caption(
            "Ce texte est injecté en tant que `system message` à chaque appel "
            "Groq/Gemini. Le `user message` ajoute juste les champs de l'offre "
            "(titre, entreprise, lieu, description)."
        )
        try:
            # Truncate the CV excerpt to keep the previewed prompt readable —
            # the real scorer also caps it at 2500 chars, but for preview a
            # short stub is enough to convey shape.
            preview_cv = (cv_text or "")[:300]
            prompt = build_system_prompt_from_synthesis(syn, preview_cv)
        except Exception as e:  # noqa: BLE001
            st.error(f"Impossible de rendre le prompt : {type(e).__name__} — {e}")
            return
        st.code(prompt, language="markdown")


def _confidence_pill(value: float | None) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v >= 0.8:
        bg, fg, label = "#DCFCE7", "#15803D", f"Confiance haute · {int(v * 100)}%"
    elif v >= 0.5:
        bg, fg, label = "#FEF3C7", "#B45309", f"Confiance moyenne · {int(v * 100)}%"
    else:
        bg, fg, label = "#FEE2E2", "#B91C1C", f"Confiance faible · {int(v * 100)}%"
    return (
        f'<span style="display:inline-block;padding:0.25rem 0.7rem;'
        f'border-radius:999px;background:{bg};color:{fg};'
        f'font-size:0.78rem;font-weight:600;">{label}</span>'
    )


# ---------------------------------------------------------------------------
# Empty-state : no CV yet → upload + first synthesis
# ---------------------------------------------------------------------------
def _empty_state_no_cv(user_id: str) -> None:
    _section_header(
        "Bienvenue sur Kairo",
        "On commence par ton CV — l'IA en tire une synthèse de profil "
        "(rôles cibles, secteurs, deal-breakers) que tu pourras éditer "
        "après. Pas de questionnaire à remplir.",
    )

    with st.container(border=True):
        cv_file = st.file_uploader(
            "CV (PDF, DOCX ou TXT)",
            type=["pdf", "docx", "txt"],
            key="mp_cv_uploader_initial",
        )

    if cv_file is None:
        return

    try:
        cv_text = parse_cv(cv_file.getvalue(), cv_file.name)
    except EncryptedPDFError as e:
        st.error(f":lock: {e}")
        return
    except ImageOnlyPDFError as e:
        st.error(f":frame_with_picture: {e}")
        return
    except FileTooLargeError as e:
        st.error(f":package: {e}")
        return
    except (EmptyFileError, UnsupportedFormatError) as e:
        st.error(str(e))
        return
    except CorruptedFileError as e:
        st.error(f":warning: {e}")
        return
    except (CVParseError, CVParseConfigError, RuntimeError) as e:
        st.error(f"Erreur lors de la lecture du CV : {e}")
        return

    if len(cv_text) < 200:
        st.warning(
            f"Texte extrait court ({len(cv_text)} caractères). "
            "Si ton CV est un PDF image, fournis plutôt une version texte ou DOCX."
        )

    # Persist the CV text so future re-syntheses don't ask for it again.
    # `save_cv_text` updates an existing user_configs row, so we must ensure
    # one exists first — empty config is enough.
    if load_user_config(user_id) is None:
        save_user_config(user_id, {})
    save_cv_text(user_id, cv_text)
    st.session_state["mp_cv_text_cached"] = cv_text
    st.success(f"CV enregistré : {cv_file.name} — {len(cv_text)} caractères.")

    # Synthèse initiale — bouton explicite pour que l'user voie ce qui se passe.
    if st.button(
        "Synthétiser mon profil",
        type="primary",
        use_container_width=True,
        key="mp_btn_initial_synth",
    ):
        _run_initial_synthesis(user_id, cv_text=cv_text, user_config={})
        st.rerun()


# ---------------------------------------------------------------------------
# Lazy migration : has CV + config but no synthesis yet
# ---------------------------------------------------------------------------
def _lazy_migration_banner(user_id: str, cv_text: str, user_config: dict[str, Any]) -> None:
    _section_header(
        "Synthèse à générer",
        "Tu as un CV et une configuration en base mais pas encore de synthèse "
        "de profil. On la génère en un clic — l'IA s'appuie sur ton CV + ta "
        "config existante.",
    )

    with st.container(border=True):
        st.markdown(
            "**Ce qui va se passer :**\n\n"
            "1. L'IA lit ton CV + ta config existante.\n"
            "2. Elle produit une synthèse structurée "
            "(rôles cibles, secteurs, deal-breakers, dream companies).\n"
            "3. Tu pourras tout éditer après — c'est un point de départ, pas une décision finale."
        )
        if st.button(
            "Générer ma synthèse",
            type="primary",
            use_container_width=True,
            key="mp_btn_lazy_migration",
        ):
            _run_initial_synthesis(user_id, cv_text=cv_text, user_config=user_config)
            st.rerun()


def _run_initial_synthesis(
    user_id: str,
    *,
    cv_text: str,
    user_config: dict[str, Any],
) -> None:
    """Call the LLM, insert as draft, activate. Surfaces errors inline."""
    with st.spinner("L'IA synthétise ton profil…"):
        try:
            synthesis = synthesize_profile(cv_text, user_config or {})
        except ProfileSynthesisError as e:
            st.error(
                "La synthèse a échoué côté IA. Aucune version n'a été créée — "
                "réessaie dans quelques minutes."
                f"\n\nDétail : {e}"
            )
            return

    draft_id = insert_synthesis_draft(
        user_id,
        synthesis,
        source_signals={
            "cv_len": len(cv_text or ""),
            "had_user_config": bool(user_config),
            "trigger": "initial",
        },
        llm_model=None,  # _call_llm doesn't surface which provider answered yet
        prompt_version=PROMPT_VERSION,
    )
    activate_synthesis(draft_id)
    st.success("Synthèse créée et activée.")


# ---------------------------------------------------------------------------
# Main view : synthesis exists → render cards + edit
# ---------------------------------------------------------------------------
def _render_synthesis_view(user_id: str, row: dict[str, Any]) -> None:
    syn = row.get("synthesis") or {}

    # --- Header bar : version + confidence + reset ----------------------------
    col_l, col_r = st.columns([3, 2])
    with col_l:
        st.markdown(
            f"### Mon profil"
            f"<span style='color:#94A3B8;font-weight:500;font-size:0.85rem;"
            f"margin-left:0.6rem;'>v{row.get('version') or '?'} · "
            f"{(row.get('llm_model') or '—')}</span>",
            unsafe_allow_html=True,
        )
    with col_r:
        st.markdown(
            f'<div style="text-align:right;padding-top:0.45rem;">'
            f'{_confidence_pill(syn.get("confidence"))}</div>',
            unsafe_allow_html=True,
        )

    # --- Summary card --------------------------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.4rem;">Synthèse</div>',
            unsafe_allow_html=True,
        )
        st.write(syn.get("summary_fr") or "_Pas de synthèse rédigée._")

    # --- Scoring recap : "Comment Kairo va te scorer" -----------------------
    _render_scoring_recap(syn)

    # --- Diagnostics : scraper preview + prompt preview ---------------------
    # Cheap lookup. If the CV row is missing or the call blows up we still
    # render the block (scraper preview doesn't need it; the prompt preview
    # falls back to an empty CV excerpt).
    try:
        cv_text_for_diag = load_cv_text(user_id) or ""
    except Exception:  # noqa: BLE001
        cv_text_for_diag = ""
    _render_diagnostics(syn, cv_text_for_diag)

    # --- Role families -------------------------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.4rem;">Familles de rôles</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.6rem;">'
            "Le scraper combine ces familles avec ta géo pour interroger LinkedIn, "
            "Indeed, etc. Désactive une famille pour la sortir de la recherche "
            "(elle reste en historique). Décoche `Active` plutôt que de supprimer."
            "</div>",
            unsafe_allow_html=True,
        )
        rfs = list(syn.get("role_families") or [])
        edited_rfs: list[dict[str, Any]] = []
        for i, fam in enumerate(rfs):
            label = fam.get("label") or f"Famille {i+1}"
            with st.expander(
                f"{label}" + (" · désactivée" if not fam.get("active", True) else ""),
                expanded=False,
            ):
                src_t = (fam.get("source") or {}).get("type", "inferred")
                src_e = (fam.get("source") or {}).get("evidence", "") or ""
                st.caption(
                    f"Source : `{src_t}`" + (f" — {src_e}" if src_e else "")
                )
                col_a, col_b = st.columns([3, 1])
                titles = col_a.multiselect(
                    "Titres de poste recherchés",
                    options=list(fam.get("titles") or []),
                    default=list(fam.get("titles") or []),
                    key=f"mp_rf_titles_{i}",
                    help=(
                        "Ces titres sont envoyés tels quels au scraper. "
                        "Pour en ajouter, tape directement dans le champ."
                    ),
                    accept_new_options=True,
                )
                weight = col_b.slider(
                    "Poids",
                    min_value=0.0,
                    max_value=1.0,
                    value=float(fam.get("weight") or 0.7),
                    step=0.1,
                    key=f"mp_rf_w_{i}",
                )
                active = st.checkbox(
                    "Active dans la recherche",
                    value=bool(fam.get("active", True)),
                    key=f"mp_rf_a_{i}",
                )
                edited_rfs.append({
                    **fam,
                    "label": label,
                    "titles": [t.strip() for t in titles if t and t.strip()],
                    "weight": float(weight),
                    "active": bool(active),
                })

    # --- Geo -----------------------------------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.4rem;">Localisations</div>'
            '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.6rem;">'
            "Format `Ville, Pays` (ex: `Geneva, Switzerland`). "
            "`Primary` = priorité haute (scrap intensif), "
            "`Acceptable` = élargissement, `Exclude` = jamais."
            "</div>",
            unsafe_allow_html=True,
        )
        geo = syn.get("geo") or {}
        col1, col2, col3 = st.columns(3)
        primary = col1.multiselect(
            "Primary",
            options=list(geo.get("primary") or []),
            default=list(geo.get("primary") or []),
            key="mp_geo_primary",
            accept_new_options=True,
        )
        acceptable = col2.multiselect(
            "Acceptable",
            options=list(geo.get("acceptable") or []),
            default=list(geo.get("acceptable") or []),
            key="mp_geo_accept",
            accept_new_options=True,
        )
        exclude = col3.multiselect(
            "Exclude",
            options=list(geo.get("exclude") or []),
            default=list(geo.get("exclude") or []),
            key="mp_geo_exclude",
            accept_new_options=True,
        )

    # --- Seniority -----------------------------------------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.4rem;">Niveau (séniorité)</div>',
            unsafe_allow_html=True,
        )
        sen = syn.get("seniority_band") or {}
        col_l, col_a, col_b = st.columns([2, 1, 1])
        SEN_OPTS = ["junior", "mid", "mid-senior", "senior", "exec"]
        sen_label_default = sen.get("label") if sen.get("label") in SEN_OPTS else "mid"
        sen_label = col_l.selectbox(
            "Label",
            options=SEN_OPTS,
            index=SEN_OPTS.index(sen_label_default),
            key="mp_sen_label",
        )
        yoe_min = col_a.number_input(
            "YoE min",
            min_value=0,
            max_value=50,
            value=int(sen.get("yoe_min") or 0),
            step=1,
            key="mp_sen_min",
        )
        yoe_max = col_b.number_input(
            "YoE max",
            min_value=0,
            max_value=60,
            value=int(sen.get("yoe_max") or max(int(sen.get("yoe_min") or 0), 5)),
            step=1,
            key="mp_sen_max",
        )

    # --- Deal breakers / dream companies / languages -------------------------
    with st.container(border=True):
        st.markdown(
            '<div style="font-weight:600;margin-bottom:0.4rem;">Filtres et signaux</div>',
            unsafe_allow_html=True,
        )
        deal_breakers = st.multiselect(
            "Deal-breakers (tokens minuscules — substring match côté scoring)",
            options=list(syn.get("deal_breakers") or []),
            default=list(syn.get("deal_breakers") or []),
            key="mp_deal_breakers",
            accept_new_options=True,
            help="Ex: `cold calling`, `intern`, `back office`, `defense`.",
        )
        dream_companies = st.multiselect(
            "Boîtes à booster (dream companies)",
            options=list(syn.get("dream_companies") or []),
            default=list(syn.get("dream_companies") or []),
            key="mp_dream_companies",
            accept_new_options=True,
            help="Toute offre d'une de ces boîtes reçoit un boost de scoring.",
        )
        languages = st.multiselect(
            "Langues (format `FR-native`, `EN-C1`, etc.)",
            options=list(syn.get("languages") or []),
            default=list(syn.get("languages") or []),
            key="mp_languages",
            accept_new_options=True,
        )

    # --- Open questions ------------------------------------------------------
    open_qs = list(syn.get("open_questions") or [])
    open_q_answers: dict[str, str] = {}
    if open_qs:
        with st.container(border=True):
            st.markdown(
                '<div style="font-weight:600;margin-bottom:0.4rem;">Questions ouvertes</div>'
                '<div style="color:#64748B;font-size:0.85rem;margin-bottom:0.6rem;">'
                "L'IA a identifié ces ambiguïtés. Réponds (1 mot ou phrase courte) — "
                "tes réponses seront ré-injectées dans la prochaine synthèse."
                "</div>",
                unsafe_allow_html=True,
            )
            for q in open_qs:
                qid = q.get("id") or ""
                qtxt = q.get("text") or qid
                ans = q.get("answer") or ""
                v = st.text_input(qtxt, value=ans, key=f"mp_oq_{qid}")
                open_q_answers[qid] = v.strip()

    # --- Action bar ----------------------------------------------------------
    st.markdown('<div style="height:0.6rem;"></div>', unsafe_allow_html=True)
    col_save, col_resyn, col_reset = st.columns([2, 2, 1])

    if col_save.button(
        "Enregistrer mes modifs",
        type="primary",
        use_container_width=True,
        key="mp_btn_save",
        help="Crée une nouvelle version (la précédente est archivée).",
    ):
        new_syn = _build_edited_synthesis(
            base=syn,
            role_families=edited_rfs,
            geo={"primary": primary, "acceptable": acceptable, "exclude": exclude},
            seniority_band={
                "label": sen_label,
                "yoe_min": int(yoe_min),
                "yoe_max": int(yoe_max),
            },
            deal_breakers=[s.lower().strip() for s in deal_breakers if s and s.strip()],
            dream_companies=[s.strip() for s in dream_companies if s and s.strip()],
            languages=[s.strip() for s in languages if s and s.strip()],
            open_q_answers=open_q_answers,
        )
        new_id = insert_synthesis_draft(
            user_id,
            new_syn,
            source_signals={"trigger": "user_edit", "previous_id": row.get("id")},
            llm_model=row.get("llm_model"),
            prompt_version=PROMPT_VERSION,
        )
        activate_synthesis(new_id)
        st.success("Modifications enregistrées (nouvelle version active).")
        st.rerun()

    if col_resyn.button(
        "Re-synthétiser depuis le CV",
        use_container_width=True,
        key="mp_btn_resyn",
        help=(
            "Relance le LLM avec ton CV + ta config + tes réponses aux open_questions. "
            "L'ancienne synthèse reste en historique."
        ),
    ):
        _resynthesize(user_id, current_row=row, open_q_answers=open_q_answers)
        st.rerun()

    if col_reset.button(
        "Reset",
        use_container_width=True,
        key="mp_btn_reset",
        help=(
            "Archive la synthèse active sans en créer de nouvelle. Ton kanban "
            "(Suivi) reste intact. Tu pourras re-synthétiser ensuite."
        ),
    ):
        st.session_state["mp_confirm_reset"] = True

    if st.session_state.get("mp_confirm_reset"):
        with st.container(border=True):
            st.warning(
                "Confirmer le reset ? La synthèse active sera archivée. "
                "Tes offres et ton suivi (kanban) ne sont **pas** affectés."
            )
            cc1, cc2 = st.columns(2)
            if cc1.button(
                "Oui, archiver",
                type="primary",
                use_container_width=True,
                key="mp_btn_reset_yes",
            ):
                archive_active_synthesis(user_id)
                st.session_state.pop("mp_confirm_reset", None)
                st.success("Synthèse archivée.")
                st.rerun()
            if cc2.button("Annuler", use_container_width=True, key="mp_btn_reset_no"):
                st.session_state.pop("mp_confirm_reset", None)
                st.rerun()

    # --- Versions history ----------------------------------------------------
    with st.expander("Historique des versions", expanded=False):
        versions = list_synthesis_versions(user_id)
        if not versions:
            st.caption("Aucune version archivée.")
        else:
            for v in versions:
                badge = {
                    "active":   "🟢 active",
                    "draft":    "⚪ brouillon",
                    "archived": "📦 archivée",
                }.get(v.get("status"), v.get("status"))
                st.markdown(
                    f"- **v{v.get('version')}** — {badge} · "
                    f"{(v.get('activated_at') or v.get('created_at') or '')[:16]} "
                    f"· `{v.get('llm_model') or '—'}`"
                )


def _build_edited_synthesis(
    *,
    base: dict[str, Any],
    role_families: list[dict[str, Any]],
    geo: dict[str, list[str]],
    seniority_band: dict[str, Any],
    deal_breakers: list[str],
    dream_companies: list[str],
    languages: list[str],
    open_q_answers: dict[str, str],
) -> dict[str, Any]:
    """Apply the user's UI edits onto the previous synthesis. We don't call
    the LLM — this is a manual edit, the structured fields are the truth."""
    out = dict(base)  # shallow copy is fine, we replace top-level keys
    out["role_families"] = role_families
    out["geo"] = {
        "primary":    list(geo.get("primary") or []),
        "acceptable": list(geo.get("acceptable") or []),
        "exclude":    list(geo.get("exclude") or []),
    }
    out["seniority_band"] = seniority_band
    out["deal_breakers"]   = deal_breakers
    out["dream_companies"] = dream_companies
    out["languages"]       = languages

    # Fold answers into open_questions (preserve IDs, drop answered ones from
    # the next pass — the synthesizer respects this when re-synthesizing).
    new_oq: list[dict[str, Any]] = []
    for q in (base.get("open_questions") or []):
        qid = q.get("id")
        if not qid:
            continue
        ans = open_q_answers.get(qid, "").strip()
        new_oq.append({
            "id": qid,
            "text": q.get("text", ""),
            "answer": ans or None,
        })
    out["open_questions"] = new_oq
    return out


def _resynthesize(
    user_id: str,
    *,
    current_row: dict[str, Any],
    open_q_answers: dict[str, str],
) -> None:
    """Re-run the LLM with previous_synthesis + open_question_answers."""
    cv_text = ""
    try:
        cv_text = load_cv_text(user_id) or ""
    except Exception:  # noqa: BLE001
        cv_text = ""
    user_config = load_user_config(user_id) or {}

    with st.spinner("L'IA met à jour ta synthèse…"):
        try:
            new_syn = synthesize_profile(
                cv_text,
                user_config,
                previous_synthesis=current_row.get("synthesis") or {},
                open_question_answers={k: v for k, v in open_q_answers.items() if v},
            )
        except ProfileSynthesisError as e:
            st.error(
                "Re-synthèse échouée. La version actuelle reste active."
                f"\n\nDétail : {e}"
            )
            return

    new_id = insert_synthesis_draft(
        user_id,
        new_syn,
        source_signals={
            "trigger": "user_resynthesize",
            "previous_id": current_row.get("id"),
            "n_open_q_answered": sum(1 for v in open_q_answers.values() if v),
        },
        llm_model=None,
        prompt_version=PROMPT_VERSION,
    )
    activate_synthesis(new_id)
    st.success("Nouvelle synthèse activée.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    user, _profile = _guard()

    col_hdr, col_right = st.columns([4, 1])
    with col_hdr:
        render_wordmark(size=30)
    with col_right:
        st.markdown('<div style="padding-top:8px;"></div>', unsafe_allow_html=True)
        st.page_link("streamlit_app.py", label="← Accueil")

    st.markdown('<div style="height:1rem;"></div>', unsafe_allow_html=True)

    user_id = user.user_id

    # 1) Active synthesis ?
    active = load_active_synthesis(user_id)

    if active is not None:
        _render_synthesis_view(user_id, active)
        return

    # 2) No active synthesis — do we have a CV to lazy-migrate from ?
    try:
        cv_text = load_cv_text(user_id) or ""
    except Exception:  # noqa: BLE001
        cv_text = ""
    user_config = load_user_config(user_id) or {}

    if cv_text.strip():
        _lazy_migration_banner(user_id, cv_text=cv_text, user_config=user_config)
        return

    # 3) Brand-new account — empty state
    _empty_state_no_cv(user_id)


if __name__ == "__main__":
    main()
else:
    main()
