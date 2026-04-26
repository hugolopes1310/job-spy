"""Dashboard — Kairo. Liste des offres scorées par l'IA pour l'utilisateur courant.

Le scraper (app/scraper/run.py) alimente `user_job_matches`. Cette page lit la
vue `user_matches_enriched` (jobs × matches) sous RLS : chaque utilisateur ne
voit que ses propres matches.
"""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.cover_letter import (  # noqa: E402
    build_cover_letter_docx,
    generate_cover_letter,
    render_from_template,
)
from app.lib.jobs_store import (  # noqa: E402
    STATUS_LABELS,
    STATUS_TONES,
    list_matches_for_user,
    set_status,
    toggle_favorite,
    update_match,
)
from app.lib.page_setup import setup_authed_page  # noqa: E402
from app.lib.storage import (  # noqa: E402
    load_cover_letter_docx,
    load_cover_letter_text,
    load_cv_text,
    load_user_config,
)
from app.lib.theme import render_badge, render_score_badge  # noqa: E402

user, profile = setup_authed_page(
    page_title="Mes offres",
    page_icon=":material/dashboard:",
    layout="wide",
    require_approved=True,
)


# ---------------------------------------------------------------------------
# Time-range chips state
# ---------------------------------------------------------------------------
_TIME_RANGES: list[tuple[str, int | None]] = [
    ("Dernière heure",  1),
    ("Dernières 3h",    3),
    ("Dernières 24h",  24),
    ("Derniers 7j",   168),
    ("Tout",          None),
]


def _init_filter_state() -> None:
    st.session_state.setdefault("f_time_idx",   4)   # default: Tout
    st.session_state.setdefault("f_status",     "tous")
    st.session_state.setdefault("f_min_score",  5)
    st.session_state.setdefault("f_fav_only",   False)
    st.session_state.setdefault("f_limit",      100)
    st.session_state.setdefault("f_role_family", None)  # None = "Toutes"
    # Re-entrancy lock for "Lancer une recherche". Prevents a double-click
    # (or a second tab) from kicking off two parallel run_for_user() calls,
    # which would double LLM consumption + open two scraper_runs rows.
    st.session_state.setdefault("scrape_in_flight", False)


# ---------------------------------------------------------------------------
# "Last run" helpers — used by the action bar to surface freshness
# ---------------------------------------------------------------------------
_NO_LAST_RUN = "Jamais lancé"


def _parse_iso(value) -> datetime | None:
    """Coerce Supabase 'scored_at' (ISO string OR datetime) to UTC datetime.
    Returns None if the value is unparseable — better to show "—" than crash.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Supabase serializes timestamptz with a trailing 'Z' or '+00:00'. Python's
    # fromisoformat accepts the latter natively but not 'Z' before 3.11.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_relative(dt: datetime | None, *, now: datetime | None = None) -> str:
    """Human-readable 'il y a Xh / Xj' for the last-run badge.

    Returns "—" if `dt` is None. Designed to read at a glance:
      < 1 min : "à l'instant"
      < 1 h   : "il y a 23 min"
      < 24 h  : "il y a 5 h"
      < 30 j  : "il y a 3 j"
      else    : ISO date "le 2026-03-12"
    """
    if dt is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    delta = now - dt
    s = int(delta.total_seconds())
    if s < 0:
        return "à l'instant"  # clock skew → don't display "in N min"
    if s < 60:
        return "à l'instant"
    if s < 3600:
        return f"il y a {s // 60} min"
    if s < 86_400:
        return f"il y a {s // 3600} h"
    if s < 30 * 86_400:
        return f"il y a {s // 86_400} j"
    return f"le {dt.strftime('%Y-%m-%d')}"


def _last_run_summary(
    matches: list[dict],
    *,
    user_id: str | None = None,
) -> tuple[str, int]:
    """Return (relative_when, jobs_in_last_24h) for the dashboard freshness pill.

    Two-tier resolution :
      1. If `user_id` is provided AND `scraper_runs` exposes a finished row
         relevant to this user (cron OR manual triggered_by_user_id), use its
         `started_at` for the "last run" label. This is the source of truth
         post-PR5 — it captures runs that produced zero new matches too.
      2. Otherwise fall back to `max(scored_at)` across the user's matches —
         the legacy heuristic from PR4 that's still right whenever telemetry
         is unavailable (Supabase outage, RLS quirk, …).

    `fresh` (matches in the last 24h) is always computed off `scored_at` —
    it's a count of new offers, not run timestamps.
    """
    # ---- fresh count (matches in last 24h) ------------------------------------
    parsed_matches = [_parse_iso(m.get("scored_at")) for m in matches]
    parsed_matches = [p for p in parsed_matches if p is not None]
    cutoff = datetime.now(timezone.utc).timestamp() - 86_400
    fresh = sum(1 for p in parsed_matches if p.timestamp() >= cutoff)

    # ---- last-run label (telemetry first, scored_at fallback) -----------------
    last_dt: datetime | None = None
    if user_id:
        try:
            from app.lib.scraper_runs_store import get_last_run_for_user
            row = get_last_run_for_user(user_id)
        except Exception:  # noqa: BLE001
            row = None
        if row:
            last_dt = _parse_iso(row.get("started_at"))

    if last_dt is None and parsed_matches:
        last_dt = max(parsed_matches)

    if last_dt is None:
        return (_NO_LAST_RUN, fresh)
    return (_format_relative(last_dt), fresh)


# ---------------------------------------------------------------------------
# Role-family chips helpers (PR4.c)
# ---------------------------------------------------------------------------
_NO_FAMILY_LABEL = "Sans famille"


def _group_by_family(matches: list[dict]) -> list[tuple[str | None, int]]:
    """Return [(family_label_or_None, count)] sorted by count desc.

    `analysis.matched_role_family` is the source of truth — if absent or null,
    the match falls into the "Sans famille" bucket. Used to render the chip
    strip with counts.
    """
    counts: dict[str | None, int] = {}
    for m in matches:
        a = m.get("analysis") or {}
        label = a.get("matched_role_family")
        if isinstance(label, str):
            label = label.strip() or None
        else:
            label = None
        counts[label] = counts.get(label, 0) + 1
    # Sort: real labels first (by count desc, then alpha), then "Sans famille".
    real = sorted(
        [(k, v) for k, v in counts.items() if k is not None],
        key=lambda kv: (-kv[1], kv[0]),
    )
    no_fam = [(None, counts[None])] if None in counts else []
    return real + no_fam


def _render_action_bar(
    user_id: str,
    *,
    last_run_label: str,
    fresh_24h: int,
) -> None:
    """Top-of-page action bar : "Lancer une recherche" + freshness summary.

    Sync inline trigger : we call run_for_user() with a spinner. The call
    typically takes 30-90s for ~30 jobs (one LLM call per job). Acceptable
    for V1 — a job queue would be the right fix once we have multiple users.
    """
    # Re-entrancy guard : if a previous click is still in flight (e.g. user
    # clicked, then opened a second tab), disable the button. Streamlit serializes
    # widgets per session but NOT across tabs, so the disabled state is the
    # cheap user-side signal — combined with the spinner it's enough.
    in_flight = bool(st.session_state.get("scrape_in_flight"))
    cols = st.columns([1.6, 3.4])
    with cols[0]:
        clicked = st.button(
            "Recherche en cours…" if in_flight else "Lancer une recherche",
            type="primary",
            use_container_width=True,
            icon=":material/hourglass_top:" if in_flight else ":material/search:",
            key="action_run_search",
            disabled=in_flight,
        )
    with cols[1]:
        # Vertical centering inside the column (button is ~38px tall).
        fresh_str = (
            f"<span style=\"color:#0F172A;font-weight:600;\">{fresh_24h}</span> "
            f"<span style=\"color:#64748B;\">"
            f"{'nouvelle offre' if fresh_24h == 1 else 'nouvelles offres'} (24h)"
            f"</span>"
        )
        st.markdown(
            f"""
<div style="height:100%;display:flex;align-items:center;
            font-size:0.92rem;color:#475569;padding-left:0.5rem;">
  <span>Dernière exécution&nbsp;:</span>
  <span style="margin-left:0.4rem;color:#0F172A;font-weight:600;">{last_run_label}</span>
  <span style="color:#CBD5E1;margin:0 0.7rem;">·</span>
  {fresh_str}
</div>
            """,
            unsafe_allow_html=True,
        )

    if clicked:
        # Lazy import — pulls in jobspy / requests / Supabase service client.
        # Keeping it out of the module-level imports means the dashboard loads
        # snappy when the button isn't clicked.
        from app.scraper.run import run_for_user

        # Set the in-flight flag BEFORE the spinner so a re-render in another
        # tab sees the disabled button. The `finally` clears it whether the
        # call returned cleanly, raised, or returned _status="error" — without
        # this the button stays stuck disabled forever on the unhappy path.
        st.session_state["scrape_in_flight"] = True
        try:
            with st.spinner(
                "Kairo scrape les sites et score les nouvelles offres… "
                "(30-90 s selon le nombre de queries)"
            ):
                try:
                    result = run_for_user(user_id)
                except Exception as e:  # noqa: BLE001
                    # Defensive: run_for_user already absorbs most errors and
                    # returns _status="error", but we don't want a top-level
                    # crash to render a Streamlit traceback to the user.
                    result = {"_status": "error", "error": f"{type(e).__name__}: {e}"}
        finally:
            st.session_state["scrape_in_flight"] = False

        status = result.get("_status")
        if status == "user_not_found":
            st.error("Utilisateur introuvable côté scraper. Re-déconnecte / reconnecte-toi.")
        elif status == "error":
            st.error(f"La recherche a échoué : {result.get('error', 'erreur inconnue')}")
        else:
            scored = int(result.get("scored", 0))
            new_jobs = int(result.get("new_jobs", 0))
            failed_llm = int(result.get("failed_llm", 0))
            if scored == 0 and new_jobs == 0:
                st.info(
                    "Aucune nouvelle offre n'a été trouvée cette fois — "
                    "les sites sont déjà à jour. Réessaye dans quelques heures."
                )
            else:
                msg = (
                    f"Recherche terminée : **{scored}** nouvelles offres scorées "
                    f"sur **{new_jobs}** offres uniques."
                )
                if failed_llm:
                    msg += f" ({failed_llm} sans score — LLM indisponible.)"
                st.success(msg)
        # Force a rerun so the listing below reflects the freshly inserted matches.
        st.rerun()


def _render_role_family_chips(matches: list[dict]) -> None:
    """Render the role-family filter strip ("Toutes (42) · Data Eng (12) · …").

    Selection is mutually exclusive : clicking a chip sets `f_role_family` to
    that label (or to None for "Toutes") and reruns. The actual filtering is
    applied at render time on the matches list — we don't refetch.
    """
    groups = _group_by_family(matches)
    if not groups:
        return
    total = sum(c for _, c in groups)

    selected = st.session_state.get("f_role_family")
    # "Toutes" + one chip per family. Single row, wraps if many families.
    n_chips = 1 + len(groups)
    cols = st.columns(n_chips)

    if cols[0].button(
        f"Toutes ({total})",
        key="chip_fam_all",
        use_container_width=True,
        type="primary" if selected is None else "secondary",
    ):
        st.session_state["f_role_family"] = None
        st.rerun()

    for i, (label, count) in enumerate(groups):
        display = label if label is not None else _NO_FAMILY_LABEL
        # Truncate long labels so the chip stays readable.
        if len(display) > 24:
            display = display[:21] + "…"
        is_active = (selected == label)
        if cols[i + 1].button(
            f"{display} ({count})",
            key=f"chip_fam_{i}",
            use_container_width=True,
            type="primary" if is_active else "secondary",
        ):
            st.session_state["f_role_family"] = label
            st.rerun()


def _render_time_chips() -> None:
    """Render 5 radio-like chips for time range."""
    st.markdown(
        """
<style>
.kairo-chip-row { display:flex; gap:0.5rem; flex-wrap:wrap; margin-bottom:0.4rem; }
</style>
        """,
        unsafe_allow_html=True,
    )
    cols = st.columns(len(_TIME_RANGES))
    for i, (label, _) in enumerate(_TIME_RANGES):
        active = (st.session_state["f_time_idx"] == i)
        if cols[i].button(
            label,
            key=f"chip_time_{i}",
            use_container_width=True,
            type="primary" if active else "secondary",
        ):
            st.session_state["f_time_idx"] = i
            st.rerun()


# ---------------------------------------------------------------------------
# Cover letter modal (uses st.dialog — Streamlit >=1.35)
# ---------------------------------------------------------------------------
@st.dialog("Lettre de motivation", width="large")
def _cover_letter_dialog(
    user_id: str,
    job: dict,
    candidate_name: str,
    candidate_email: str,
    cv_text: str,
    user_template: str | None,
) -> None:
    st.markdown(
        f"""
<div style="margin-bottom:0.8rem;">
  <div style="font-weight:600;color:#0F172A;">{job.get('title') or 'Offre'}</div>
  <div style="color:#64748B;font-size:0.88rem;">
    {job.get('company') or '—'} · {job.get('location') or ''}
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )

    # Language selector — segmented_control keeps the dialog open on change
    # (unlike buttons + st.rerun(), which would close the @st.dialog context).
    _LANG_LABELS = {"fr": "Français", "en": "English"}
    current_lang = st.session_state.get("cl_lang", "fr")
    selected_label = st.segmented_control(
        "Langue",
        options=[_LANG_LABELS["fr"], _LANG_LABELS["en"]],
        default=_LANG_LABELS[current_lang],
        key="cl_lang_seg",
        label_visibility="collapsed",
    )
    new_lang = "en" if selected_label == _LANG_LABELS["en"] else "fr"
    if new_lang != current_lang:
        # Language changed → drop cached text so the body is regenerated below.
        st.session_state["cl_lang"] = new_lang
        st.session_state.pop("cl_text", None)
    lang = st.session_state.get("cl_lang", "fr")

    if "cl_text" not in st.session_state:
        if not cv_text:
            st.error(
                "Impossible de générer la lettre : ton CV n'est pas enregistré. "
                "Retourne dans la Configuration pour l'importer."
            )
            return
        with st.spinner("Kairo rédige ta lettre..."):
            text = generate_cover_letter(
                cv_text=cv_text,
                job={
                    "title":       job.get("title"),
                    "company":     job.get("company"),
                    "location":    job.get("location"),
                    "description": job.get("description") or "",
                },
                candidate_name=candidate_name,
                language=lang,
                user_template=user_template,
            )
        if not text:
            st.error(
                "La génération a échoué (Groq indisponible ou clé API manquante). "
                "Réessaye dans quelques secondes."
            )
            return
        st.session_state["cl_text"] = text

    text = st.session_state["cl_text"]

    edited = st.text_area(
        "Lettre générée (éditable avant téléchargement)",
        value=text,
        height=360,
        label_visibility="collapsed",
    )
    st.session_state["cl_text"] = edited

    # Actions — if the user uploaded a DOCX template during onboarding, we
    # clone *that* as the shell (fidèle à sa mise en page). Otherwise we
    # render our Kairo-default layout from scratch.
    user_template_docx = load_cover_letter_docx(user_id)
    if user_template_docx is not None:
        template_bytes, _template_filename = user_template_docx
        try:
            docx_bytes = render_from_template(
                template_bytes,
                body_text=edited,
                candidate_name=candidate_name,
                candidate_email=candidate_email,
                job_title=job.get("title"),
                job_company=job.get("company"),
                job_location=job.get("location"),
                language=lang,
            )
        except Exception as e:  # noqa: BLE001
            # Fallback : template corrupted or unexpected structure → default renderer.
            st.warning(
                f"Impossible d'injecter dans ton template ({e}). "
                "On génère avec la mise en page Kairo par défaut."
            )
            docx_bytes = build_cover_letter_docx(
                text=edited,
                candidate_name=candidate_name,
                candidate_email=candidate_email,
                job_title=job.get("title"),
                job_company=job.get("company"),
                job_location=job.get("location"),
                language=lang,
            )
    else:
        docx_bytes = build_cover_letter_docx(
            text=edited,
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            job_title=job.get("title"),
            job_company=job.get("company"),
            job_location=job.get("location"),
            language=lang,
        )
    filename_company = (job.get("company") or "entreprise").replace(" ", "_")[:30]
    filename = f"lettre_{filename_company}_{lang}.docx"

    b1, b2, b3 = st.columns([1.4, 1, 1])
    b1.download_button(
        "Télécharger (.docx)",
        data=docx_bytes,
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        use_container_width=True,
        type="primary",
        icon=":material/download:",
    )
    if b2.button("Régénérer", use_container_width=True, icon=":material/refresh:"):
        st.session_state.pop("cl_text", None)
        st.rerun()
    if b3.button("Fermer", use_container_width=True, icon=":material/close:"):
        st.session_state.pop("cl_text", None)
        st.session_state.pop("cl_lang", None)
        st.rerun()


# ---------------------------------------------------------------------------
# Score decomposition strip
# ---------------------------------------------------------------------------
def _score_chip(label: str, value: int | float | None) -> str:
    """Render a tiny "label N/10" chip with a color tied to the value bucket.

    Returns a self-contained HTML span. Designed to sit inline in a thin row.
    Returns empty string if `value` is missing/invalid — callers can filter.
    """
    try:
        if value is None:
            return ""
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v < 0:
        return ""  # heuristic placeholder (-1 means "couldn't compute")
    if v >= 8:
        bg, fg = "#DCFCE7", "#15803D"  # green
    elif v >= 5:
        bg, fg = "#FEF3C7", "#B45309"  # amber
    else:
        bg, fg = "#FEE2E2", "#B91C1C"  # red
    return (
        f'<span style="display:inline-flex;align-items:center;gap:0.25rem;'
        f'padding:0.2rem 0.55rem;border-radius:999px;background:{bg};color:{fg};'
        f'font-size:0.78rem;font-weight:600;">'
        f'<span style="opacity:0.8;font-weight:500;">{label}</span> '
        f'<span style="font-variant-numeric:tabular-nums;">{int(round(v))}/10</span>'
        f'</span>'
    )


def _render_score_decomposition(analysis: dict) -> None:
    """Show match_role / match_geo / match_seniority as colored chips.

    The synthesis-aware scorer (analyze_offer_with_synthesis) emits these three
    fields; older matches scored via the legacy analyze_offer_for_user path may
    or may not carry them. We render only the chips that are actually present
    so legacy rows don't show empty placeholders.
    """
    role = analysis.get("match_role")
    geo  = analysis.get("match_geo")
    sen  = analysis.get("match_seniority")
    chips = [
        _score_chip("Rôle",      role),
        _score_chip("Géo",       geo),
        _score_chip("Séniorité", sen),
    ]
    chips = [c for c in chips if c]
    if not chips:
        return  # legacy match — keep the card clean
    st.markdown(
        '<div style="display:flex;gap:0.4rem;flex-wrap:wrap;align-items:center;'
        'margin:0.1rem 0 0.7rem;">'
        '<span style="font-size:0.72rem;font-weight:600;color:#94A3B8;'
        "letter-spacing:0.06em;text-transform:uppercase;margin-right:0.2rem;\">"
        "Décomposition</span>"
        + "".join(chips)
        + "</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Match card
# ---------------------------------------------------------------------------
def _render_match(
    user_id: str,
    m: dict,
    *,
    candidate_name: str,
    candidate_email: str,
    cv_text: str,
    user_template: str | None,
) -> None:
    analysis = m.get("analysis") or {}
    score    = m.get("score")
    job_id   = m["job_id"]
    title    = m.get("title") or "(sans titre)"
    company  = m.get("company") or "?"
    location = m.get("location") or "—"
    url      = m.get("url") or "#"
    site     = (m.get("site") or "").lower()
    reason   = analysis.get("reason") or ""
    red_flags = analysis.get("red_flags") or []
    strengths = analysis.get("strengths") or []
    status    = m.get("status") or "new"
    is_fav    = bool(m.get("is_favorite"))

    pills: list[str] = [
        render_badge(
            STATUS_LABELS.get(status, status),
            tone=STATUS_TONES.get(status, "muted"),
        )
    ]
    if m.get("is_repost"):
        pills.append(render_badge("Republiée", tone="warn"))
    if site:
        pills.append(render_badge(site.capitalize(), tone="muted"))

    with st.container(border=True):
        c_title, c_score = st.columns([5, 1])
        with c_title:
            fav_mark = (
                '<span style="color:#F59E0B;margin-right:0.4rem;">★</span>'
                if is_fav else ""
            )
            st.markdown(
                f"""
<div style="margin-bottom:0.35rem;">
  <a href="{url}" target="_blank" style="font-size:1.1rem;font-weight:600;color:#0F172A;text-decoration:none;">
    {fav_mark}{title}
  </a>
</div>
<div style="color:#64748B;font-size:0.92rem;margin-bottom:0.5rem;">
  <b style="color:#334155;">{company}</b>
  <span style="color:#CBD5E1;margin:0 0.4rem;">·</span>
  {location}
</div>
<div style="display:flex;gap:0.35rem;flex-wrap:wrap;margin-bottom:0.7rem;">
  {''.join(pills)}
</div>
                """,
                unsafe_allow_html=True,
            )
        with c_score:
            st.markdown(
                f'<div style="text-align:right;padding-top:6px;">{render_score_badge(score)}</div>',
                unsafe_allow_html=True,
            )

        if reason:
            st.markdown(
                f'<div style="color:#475569;font-size:0.93rem;line-height:1.55;margin-bottom:0.8rem;padding:0.6rem 0.8rem;background:#F8FAFC;border-left:3px solid #667eea;border-radius:6px;">'
                f'{reason}'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Decomposition: shows what drove the score (rôle / géo / séniorité).
        # Only renders if the analysis dict carries the sub-scores — legacy
        # matches scored before PR3 stay clean.
        _render_score_decomposition(analysis)

        if strengths or red_flags:
            c1, c2 = st.columns(2)
            with c1:
                if strengths:
                    st.markdown(
                        '<div style="font-size:0.78rem;font-weight:600;color:#059669;margin-bottom:0.3rem;text-transform:uppercase;letter-spacing:0.06em;">Atouts</div>',
                        unsafe_allow_html=True,
                    )
                    for s in strengths[:3]:
                        st.markdown(
                            f'<div style="color:#475569;font-size:0.89rem;margin-bottom:0.2rem;">• {s}</div>',
                            unsafe_allow_html=True,
                        )
            with c2:
                if red_flags:
                    st.markdown(
                        '<div style="font-size:0.78rem;font-weight:600;color:#DC2626;margin-bottom:0.3rem;text-transform:uppercase;letter-spacing:0.06em;">Points d\'attention</div>',
                        unsafe_allow_html=True,
                    )
                    for r in red_flags[:3]:
                        st.markdown(
                            f'<div style="color:#475569;font-size:0.89rem;margin-bottom:0.2rem;">• {r}</div>',
                            unsafe_allow_html=True,
                        )

        salary = analysis.get("salary")
        deadline = analysis.get("deadline")
        apply_hint = analysis.get("apply_hint")
        meta_bits: list[str] = []
        if salary:      meta_bits.append(f"Salaire : {salary}")
        if deadline:    meta_bits.append(f"Deadline : {deadline}")
        if apply_hint:  meta_bits.append(f"Candidature : {apply_hint}")
        if meta_bits:
            sep = '<span style="color:#CBD5E1;margin:0 0.5rem;">·</span>'
            st.markdown(
                f'<div style="color:#64748B;font-size:0.85rem;margin:0.7rem 0 0.5rem;">{sep.join(meta_bits)}</div>',
                unsafe_allow_html=True,
            )

        st.markdown(
            '<hr style="margin:0.8rem 0 0.9rem;border:none;border-top:1px solid #F1F5F9;">',
            unsafe_allow_html=True,
        )

        b1, b2, b3, b4, b5 = st.columns([1.1, 1.3, 1.3, 1.1, 1])

        # -- Favori toggle ---------------------------------------------------
        fav_label = "Favori ★" if is_fav else "Favori ☆"
        if b1.button(
            fav_label,
            key=f"fav_{job_id}",
            use_container_width=True,
            type="primary" if is_fav else "secondary",
        ):
            toggle_favorite(user_id, job_id, not is_fav)
            st.rerun()

        # -- Lettre de motivation -------------------------------------------
        if b2.button(
            "Lettre de motivation",
            key=f"cl_{job_id}",
            use_container_width=True,
            icon=":material/description:",
        ):
            # Reset modal state and open
            st.session_state.pop("cl_text", None)
            st.session_state["cl_lang"] = "fr"
            _cover_letter_dialog(
                user_id=user_id,
                job=m,
                candidate_name=candidate_name,
                candidate_email=candidate_email,
                cv_text=cv_text,
                user_template=user_template,
            )

        # -- J'ai postulé ---------------------------------------------------
        already_applied = status == "applied"
        if b3.button(
            "J'ai postulé" + (" ✓" if already_applied else ""),
            key=f"applied_{job_id}",
            use_container_width=True,
            type="primary" if already_applied else "secondary",
            icon=":material/send:",
        ):
            set_status(user_id, job_id, "applied")
            st.rerun()

        # -- Refuser (dismiss) ----------------------------------------------
        if b4.button(
            "Non merci",
            key=f"reject_{job_id}",
            use_container_width=True,
            icon=":material/close:",
        ):
            update_match(user_id, job_id, status="rejected", feedback="bad")
            st.rerun()

        # -- Ouvrir l'offre -------------------------------------------------
        b5.markdown(
            f'<a href="{url}" target="_blank" style="display:inline-block;padding:0.48rem 0;text-align:center;width:100%;'
            f'background:linear-gradient(135deg, #667eea 0%, #764ba2 100%);color:white;font-weight:600;font-size:0.9rem;'
            f'border-radius:10px;text-decoration:none;box-shadow:0 2px 8px rgba(102,126,234,0.25);">'
            f'Ouvrir</a>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Page body
# ---------------------------------------------------------------------------
def main() -> None:
    user_id = user.user_id
    first_name = (profile.get("full_name") or profile["email"]).split()[0]

    st.markdown(
        f"""
<div style="margin-bottom:1.2rem;">
  <h2 style="margin:0 0 0.2rem 0;">Mes offres</h2>
  <p style="color:#64748B;margin:0;font-size:0.94rem;">
    Les matches scorés par l'IA pour {first_name}.
  </p>
</div>
        """,
        unsafe_allow_html=True,
    )

    cfg = load_user_config(user_id)
    if cfg is None:
        with st.container(border=True):
            st.markdown(
                """
<div style="text-align:center;padding:1.5rem 1rem;">
  <h3 style="margin:0 0 0.4rem 0;">Recherche non configurée</h3>
  <p style="color:#64748B;margin:0 0 1rem 0;">
    Lance la configuration pour qu'on commence à scraper des offres pour toi.
  </p>
</div>
                """,
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/1_mon_profil.py",
                label="Démarrer la configuration",
                icon=":material/arrow_forward:",
            )
        return

    _init_filter_state()

    # ---- Action bar (Lancer recherche + last run) --------------------------
    # Computed from a cheap unfiltered fetch (limited to recent rows for the
    # "freshness" stat). We accept up to 200 rows here; the real listing fetch
    # below applies the user's filters separately.
    _ab_matches = list_matches_for_user(user_id, limit=200)
    _last_run_label, _fresh_24h = _last_run_summary(_ab_matches, user_id=user_id)
    _render_action_bar(
        user_id,
        last_run_label=_last_run_label,
        fresh_24h=_fresh_24h,
    )

    st.markdown('<div style="height:0.9rem;"></div>', unsafe_allow_html=True)

    # ---- Time-range chips (row 1) ------------------------------------------
    st.markdown(
        '<div style="font-size:0.78rem;font-weight:600;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#94A3B8;margin-bottom:0.45rem;">'
        'Période</div>',
        unsafe_allow_html=True,
    )
    _render_time_chips()

    # ---- Secondary filters (row 2) -----------------------------------------
    with st.expander("Filtres avancés", expanded=False, icon=":material/tune:"):
        c1, c2, c3, c4 = st.columns([2, 2, 1.4, 1.4])
        with c1:
            st.session_state["f_min_score"] = st.slider(
                "Score minimum",
                min_value=0, max_value=10,
                value=int(st.session_state["f_min_score"]),
                step=1,
            )
        with c2:
            st.session_state["f_status"] = st.selectbox(
                "Statut",
                ["tous", "nouveaux", "vus", "postulés", "refusés", "archivés"],
                index=["tous", "nouveaux", "vus", "postulés", "refusés", "archivés"]
                .index(st.session_state["f_status"]),
            )
        with c3:
            st.session_state["f_fav_only"] = st.toggle(
                "Favoris uniquement",
                value=bool(st.session_state["f_fav_only"]),
            )
        with c4:
            st.session_state["f_limit"] = st.number_input(
                "Afficher max",
                min_value=10, max_value=500,
                value=int(st.session_state["f_limit"]),
                step=10,
            )

    st.markdown('<div style="height:0.8rem;"></div>', unsafe_allow_html=True)

    status_map = {
        "tous":       None,
        "nouveaux":   "new",
        "vus":        "seen",
        "postulés":   "applied",
        "refusés":    "rejected",
        "archivés":   "archived",
    }
    since_hours = _TIME_RANGES[st.session_state["f_time_idx"]][1]
    min_score = st.session_state["f_min_score"]

    matches = list_matches_for_user(
        user_id,
        min_score=min_score if min_score > 0 else None,
        status=status_map[st.session_state["f_status"]],
        favorites_only=bool(st.session_state["f_fav_only"]),
        since_hours=since_hours,
        limit=int(st.session_state["f_limit"]),
    )

    # ---- Role-family chips (counts on the time/score/status-filtered set) ---
    # These let the user narrow further by which role_family the LLM put each
    # offer in. Counts always reflect the post-fetch (pre-family-filter) set
    # so the user can see the full distribution before clicking a chip.
    if matches:
        st.markdown(
            '<div style="font-size:0.78rem;font-weight:600;letter-spacing:0.08em;'
            'text-transform:uppercase;color:#94A3B8;margin:0.4rem 0 0.45rem;">'
            'Famille de rôle</div>',
            unsafe_allow_html=True,
        )
        _render_role_family_chips(matches)

        # Apply the family filter to the displayed list. Done AFTER chip
        # rendering so the counts reflect the full post-fetch set — clicking
        # a chip narrows the list, not the counts (which would trap the user
        # at zero everywhere except the active chip).
        # f_role_family conventions:
        #   None     → "Toutes" (no narrowing)
        #   "<lbl>"  → narrow to matches whose analysis.matched_role_family == lbl
        # The "Sans famille" bucket is currently visible in the chip strip
        # for context but isn't clickable (we'd need a sentinel to round-trip
        # None as a click target). Acceptable for V1.
        sel_family = st.session_state.get("f_role_family")
        if isinstance(sel_family, str) and sel_family:
            matches = [
                m for m in matches
                if (m.get("analysis") or {}).get("matched_role_family") == sel_family
            ]

    if not matches:
        with st.container(border=True):
            st.markdown(
                """
<div style="text-align:center;padding:2rem 1rem;">
  <h3 style="margin:0 0 0.4rem 0;">Aucune offre ne correspond à ces filtres</h3>
  <p style="color:#64748B;margin:0 0 1rem 0;font-size:0.92rem;line-height:1.55;">
    Élargis la période en haut, ou descends le score minimum dans les filtres avancés.<br>
    Le scraper tourne plusieurs fois par jour : les offres arrivent progressivement.
  </p>
</div>
                """,
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/1_mon_profil.py",
                label="Ajuster mon profil",
                icon=":material/badge:",
            )
        return

    # ---- KPIs ---------------------------------------------------------------
    avg_score = sum(m.get("score") or 0 for m in matches) / max(len(matches), 1)
    top_score = max((m.get("score") or 0) for m in matches)
    nb_new    = sum(1 for m in matches if m.get("status") == "new")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Affichés",     len(matches))
    m2.metric("Score moyen",  f"{avg_score:.1f}/10")
    m3.metric("Top score",    f"{top_score}/10")
    m4.metric("Nouveaux",     nb_new)

    st.markdown('<div style="height:1.2rem;"></div>', unsafe_allow_html=True)

    # ---- Candidate context (needed by cover-letter modal) ------------------
    candidate_name  = (profile.get("full_name") or profile.get("email") or "Candidat").strip()
    candidate_email = profile.get("email") or ""
    try:
        cv_text = load_cv_text(user_id) or ""
    except Exception:  # noqa: BLE001
        cv_text = ""
    try:
        user_template = load_cover_letter_text(user_id) or None
    except Exception:  # noqa: BLE001
        user_template = None

    for m in matches:
        _render_match(
            user_id, m,
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            cv_text=cv_text,
            user_template=user_template,
        )


if __name__ == "__main__":
    main()
else:
    main()
