"""Suivi — pipeline de candidatures.

Vue tableau éditable des offres que l'utilisateur track (favoris + candidatures
lancées). Les statuts sont pilotables directement dans le tableau, les notes
sont éditables inline, la persistance se fait à chaque validation via
jobs_store.update_match().
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.lib.jobs_store import (  # noqa: E402
    PIPELINE_STATUSES,
    STATUS_LABELS,
    STATUS_TONES,
    list_tracked_matches,
    set_status,
    toggle_favorite,
    update_match,
)
from app.lib.page_setup import setup_authed_page  # noqa: E402
from app.lib.theme import render_badge, render_score_badge  # noqa: E402

user, profile = setup_authed_page(
    page_title="Suivi",
    page_icon=":material/track_changes:",
    layout="wide",
    require_approved=True,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# "Applied" family — considered as an active candidature for stats & filters.
APPLIED_FAMILY = {"applied", "interview", "offer"}

# Human-readable label → DB status (for the data_editor SelectColumn).
LABEL_TO_STATUS: dict[str, str] = {v: k for k, v in STATUS_LABELS.items()}

# Sort options for the table. Keys are internal ; values are display labels.
# "relevance" = ordre natif retourné par list_tracked_matches (favoris en tête,
# puis status_changed_at DESC, puis scored_at DESC).
_SORT_OPTIONS: list[tuple[str, str]] = [
    ("relevance", "Pertinence"),
    ("score",     "Score (décroissant)"),
    ("updated",   "Dernière MAJ"),
    ("applied",   "Postulé récemment"),
    ("relance",   "Relance la plus proche"),
    ("company",   "Entreprise A → Z"),
]

# Pipeline columns rendered in kanban view, left → right.
# `archived` is intentionally hidden — once a card lands there, it leaves the
# active pipeline. `new` / `seen` are excluded because the kanban is about
# active candidatures (the table view still shows them).
_KANBAN_STATUSES: list[tuple[str, str]] = [
    ("applied",   "Postulé"),
    ("interview", "Entretien"),
    ("offer",     "Offre"),
    ("rejected",  "Refusé"),
]

# Linear progression triggered by the "→ Next" button on each card. Cards in
# `offer` or `rejected` move to `archived` (out of the kanban) — for non-linear
# moves (e.g. interview → rejected), the user falls back to the table view.
_NEXT_STATUS: dict[str, str] = {
    "applied":   "interview",
    "interview": "offer",
    "offer":     "archived",
    "rejected":  "archived",
}

# Display label used by the kanban "advance" button.
_ADVANCE_LABEL: dict[str, str] = {
    "interview": "→ Entretien",
    "offer":     "→ Offre",
    "archived":  "Archiver",
}

# Sane bounds for user-editable dates — guards against typos like "2099-01-01".
# Wide enough to cover any realistic CV history while still catching fat-fingers.
_APPLIED_FLOOR_YEARS_BACK     = 5
_APPLIED_CEIL_YEARS_FORWARD   = 1
_RELANCE_FLOOR_YEARS_BACK     = 1   # past relances make sense (overdue)
_RELANCE_CEIL_YEARS_FORWARD   = 1


def _date_floor(years_back: int) -> date:
    return date(date.today().year - years_back, 1, 1)


def _date_ceil(years_forward: int) -> date:
    return date(date.today().year + years_forward, 12, 31)


def _applied_bounds() -> tuple[date, date]:
    return _date_floor(_APPLIED_FLOOR_YEARS_BACK), _date_ceil(_APPLIED_CEIL_YEARS_FORWARD)


def _relance_bounds() -> tuple[date, date]:
    return _date_floor(_RELANCE_FLOOR_YEARS_BACK), _date_ceil(_RELANCE_CEIL_YEARS_FORWARD)


# Schema-error helpers: map missing column → migration to run, so the user gets
# a precise pointer instead of "run both migrations and pray".
_COLUMN_TO_MIGRATION: dict[str, str] = {
    "is_favorite":       "migration_tracking.sql",
    "notes":             "migration_tracking.sql",
    "applied_at":        "migration_tracking.sql",
    "status_changed_at": "migration_tracking.sql",
    "next_action_at":    "migration_next_action.sql",
}


def _parse_missing_column(err_str: str) -> str | None:
    """Extract a column name from a PostgREST/Postgres error, if recognisable.

    Examples we want to match:
      - column "next_action_at" of relation "user_job_matches" does not exist
      - column user_matches_enriched.next_action_at does not exist
      - 'next_action_at' column of relation 'user_matches_enriched' does not exist
    """
    if not err_str:
        return None
    patterns = [
        r"column\s+[\"'`]?([a-z_]+)[\"'`]?\s+(?:of|does)",
        r"column\s+\S+\.([a-z_]+)\s+does",
        r"[\"'`]([a-z_]+)[\"'`]\s+column\s+of",
    ]
    for p in patterns:
        m = re.search(p, err_str, flags=re.IGNORECASE)
        if m:
            col = m.group(1)
            if col in _COLUMN_TO_MIGRATION:
                return col
    return None


# ---------------------------------------------------------------------------
# View selector — tabs-like chips
# ---------------------------------------------------------------------------
_VIEWS: list[tuple[str, str]] = [
    ("pipeline",  "Mon pipeline"),
    ("favorites", "Favoris"),
    ("applied",   "Candidatures"),
    ("all",       "Tout"),
]


def _render_view_chips() -> str:
    current = st.session_state.get("suivi_view", "pipeline")
    cols = st.columns(len(_VIEWS))
    for i, (key, label) in enumerate(_VIEWS):
        if cols[i].button(
            label,
            key=f"suivi_view_{key}",
            use_container_width=True,
            type="primary" if current == key else "secondary",
        ):
            st.session_state["suivi_view"] = key
            st.rerun()
    return st.session_state.get("suivi_view", "pipeline")


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
def _render_kpis(rows: list[dict]) -> None:
    nb_fav       = sum(1 for r in rows if r.get("is_favorite"))
    nb_applied   = sum(1 for r in rows if r.get("status") == "applied")
    nb_interview = sum(1 for r in rows if r.get("status") == "interview")
    nb_offer     = sum(1 for r in rows if r.get("status") == "offer")
    nb_rejected  = sum(1 for r in rows if r.get("status") == "rejected")

    cards = [
        ("Favoris",      f"{nb_fav}",       "Offres gardées"),
        ("Postulées",    f"{nb_applied}",   "En attente de retour"),
        ("Entretiens",   f"{nb_interview}", "Process en cours"),
        ("Offres",       f"{nb_offer}",     "Propositions reçues"),
        ("Refusées",     f"{nb_rejected}",  "Closes sans suite"),
    ]
    st.markdown(
        """
<style>
.suivi-kpi-grid {
  display:grid; grid-template-columns:repeat(5,minmax(0,1fr));
  gap:0.7rem; margin-bottom:1.2rem;
}
.suivi-kpi {
  background:#FFFFFF; border:1px solid #E2E8F0; border-radius:14px;
  padding:0.95rem 1.1rem; box-shadow:0 1px 2px rgba(15,23,42,0.04);
  transition:box-shadow 0.2s ease, border-color 0.2s ease;
}
.suivi-kpi:hover { box-shadow:0 4px 14px rgba(15,23,42,0.06); border-color:#CBD5E1; }
.suivi-kpi-label { font-size:0.72rem; font-weight:600; letter-spacing:0.06em;
  text-transform:uppercase; color:#94A3B8; margin-bottom:0.35rem; }
.suivi-kpi-value { font-size:1.7rem; font-weight:700; color:#0F172A;
  letter-spacing:-0.02em; line-height:1.1; }
.suivi-kpi-hint { color:#64748B; font-size:0.76rem; font-weight:500; margin-top:0.25rem; }
@media (max-width:820px){ .suivi-kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))} }
</style>
        """,
        unsafe_allow_html=True,
    )
    cells = "".join(
        f"""
<div class="suivi-kpi">
  <div class="suivi-kpi-label">{lbl}</div>
  <div class="suivi-kpi-value">{val}</div>
  <div class="suivi-kpi-hint">{hint}</div>
</div>
        """
        for lbl, val, hint in cards
    )
    st.markdown(f'<div class="suivi-kpi-grid">{cells}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Search + sort
# ---------------------------------------------------------------------------
def _render_search_sort() -> tuple[str, str]:
    """Render the search bar + sort dropdown. Returns (query, sort_key)."""
    c_search, c_sort = st.columns([3, 1.4])
    with c_search:
        query = st.text_input(
            "Recherche",
            placeholder="Rechercher (poste, entreprise, lieu, notes)…",
            key="suivi_search",
            label_visibility="collapsed",
        )
    with c_sort:
        sort_key = st.selectbox(
            "Tri",
            options=[k for k, _ in _SORT_OPTIONS],
            format_func=lambda k: dict(_SORT_OPTIONS).get(k, k),
            key="suivi_sort",
            label_visibility="collapsed",
        )
    return (query or "").strip(), sort_key


def _fold(text: str) -> str:
    """Lowercase + strip diacritics, so 'Genève' and 'geneve' are equal."""
    import unicodedata
    t = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def _apply_search_sort(
    rows: list[dict], query: str, sort_key: str,
) -> list[dict]:
    """Filter by query (substring, case + accent-insensitive) then sort."""
    if query:
        q = _fold(query)
        def _match(r: dict) -> bool:
            blob = _fold(" ".join(
                (r.get(k) or "") for k in ("title", "company", "location", "notes")
            ))
            return q in blob
        rows = [r for r in rows if _match(r)]

    if sort_key == "score":
        rows = sorted(
            rows,
            key=lambda r: (r.get("score") if r.get("score") is not None else -1),
            reverse=True,
        )
    elif sort_key == "updated":
        rows = sorted(
            rows,
            key=lambda r: (r.get("status_changed_at") or r.get("scored_at") or ""),
            reverse=True,
        )
    elif sort_key == "applied":
        # Rows with applied_at first (desc), rows without at the bottom.
        rows = sorted(
            rows,
            key=lambda r: (r.get("applied_at") is not None, r.get("applied_at") or ""),
            reverse=True,
        )
    elif sort_key == "relance":
        # Overdue + upcoming relances first (ascending by date). Rows with no
        # next_action_at sink to the bottom regardless.
        rows = sorted(
            rows,
            key=lambda r: (
                r.get("next_action_at") is None,     # False (has date) < True (null)
                r.get("next_action_at") or "",       # asc on non-null dates
            ),
        )
    elif sort_key == "company":
        rows = sorted(rows, key=lambda r: (r.get("company") or "").lower())
    # "relevance" → keep incoming order from list_tracked_matches.
    return rows


# ---------------------------------------------------------------------------
# Mode toggle — Tableau vs Kanban
# ---------------------------------------------------------------------------
def _render_mode_toggle() -> str:
    """Render the Tableau/Kanban toggle. Returns 'table' or 'kanban'."""
    current = st.session_state.get("suivi_mode", "table")
    chosen = st.segmented_control(
        "Affichage",
        options=["Tableau", "Kanban"],
        default="Tableau" if current == "table" else "Kanban",
        key="suivi_mode_seg",
        label_visibility="collapsed",
    )
    new_mode = "kanban" if chosen == "Kanban" else "table"
    st.session_state["suivi_mode"] = new_mode
    return new_mode


# ---------------------------------------------------------------------------
# Detail view — modal dialog opened from a selectbox below the table.
# Reuses `analysis` JSONB produced by the scorer + the theme badges for a
# look consistent with the dashboard cards.
# ---------------------------------------------------------------------------
def _detail_label(row: dict) -> str:
    """Build a one-line label for the detail picker: 'Poste — Entreprise (X/10)'."""
    title   = (row.get("title")   or "(sans titre)")[:55]
    company = (row.get("company") or "—")[:35]
    score   = row.get("score")
    score_s = f" ({int(score)}/10)" if isinstance(score, (int, float)) else ""
    return f"{title} — {company}{score_s}"


def _render_analysis_block(analysis: dict) -> None:
    """Render the scorer output — same sections as dashboard cards, wider."""
    reason    = analysis.get("reason") or ""
    strengths = analysis.get("strengths") or []
    red_flags = analysis.get("red_flags") or []
    salary    = analysis.get("salary")
    deadline  = analysis.get("deadline")
    apply_hint = analysis.get("apply_hint")

    if reason:
        st.markdown(
            f'<div style="color:#334155;font-size:0.95rem;line-height:1.6;'
            f'margin:0.5rem 0 0.9rem;padding:0.75rem 1rem;background:#F8FAFC;'
            f'border-left:3px solid #667eea;border-radius:6px;">{reason}</div>',
            unsafe_allow_html=True,
        )

    if strengths or red_flags:
        c1, c2 = st.columns(2)
        with c1:
            if strengths:
                st.markdown(
                    '<div style="font-size:0.78rem;font-weight:600;color:#059669;'
                    'margin-bottom:0.4rem;text-transform:uppercase;letter-spacing:0.06em;">'
                    'Atouts</div>',
                    unsafe_allow_html=True,
                )
                for s in strengths:
                    st.markdown(
                        f'<div style="color:#334155;font-size:0.9rem;margin-bottom:0.25rem;">• {s}</div>',
                        unsafe_allow_html=True,
                    )
        with c2:
            if red_flags:
                st.markdown(
                    '<div style="font-size:0.78rem;font-weight:600;color:#DC2626;'
                    'margin-bottom:0.4rem;text-transform:uppercase;letter-spacing:0.06em;">'
                    "Points d'attention</div>",
                    unsafe_allow_html=True,
                )
                for r in red_flags:
                    st.markdown(
                        f'<div style="color:#334155;font-size:0.9rem;margin-bottom:0.25rem;">• {r}</div>',
                        unsafe_allow_html=True,
                    )

    meta_bits: list[str] = []
    if salary:     meta_bits.append(f"<b>Salaire</b> : {salary}")
    if deadline:   meta_bits.append(f"<b>Deadline</b> : {deadline}")
    if apply_hint: meta_bits.append(f"<b>Candidature</b> : {apply_hint}")
    if meta_bits:
        sep = '<span style="color:#CBD5E1;margin:0 0.6rem;">·</span>'
        st.markdown(
            f'<div style="color:#64748B;font-size:0.88rem;margin-top:0.8rem;">'
            f'{sep.join(meta_bits)}</div>',
            unsafe_allow_html=True,
        )


@st.dialog("Détail de l'offre", width="large")
def _detail_dialog(user_id: str, match: dict) -> None:
    """Full-offer drawer: header + AI analysis + description + notes editor."""
    job_id   = match["job_id"]
    title    = match.get("title") or "(sans titre)"
    company  = match.get("company") or "—"
    location = match.get("location") or ""
    url      = match.get("url") or ""
    site     = (match.get("site") or "").lower()
    status   = match.get("status") or "new"
    score    = match.get("score")
    is_fav   = bool(match.get("is_favorite"))
    analysis = match.get("analysis") or {}
    description = match.get("description") or ""

    # Header
    pills: list[str] = [
        render_badge(STATUS_LABELS.get(status, status), tone=STATUS_TONES.get(status, "muted")),
    ]
    if is_fav:
        pills.append(render_badge("★ Favori", tone="warn"))
    if match.get("is_repost"):
        pills.append(render_badge("Republiée", tone="warn"))
    if site:
        pills.append(render_badge(site.capitalize(), tone="muted"))

    c_head, c_score = st.columns([5, 1])
    with c_head:
        st.markdown(
            f"""
<div style="margin-bottom:0.35rem;">
  <div style="font-size:1.2rem;font-weight:600;color:#0F172A;line-height:1.3;">{title}</div>
</div>
<div style="color:#475569;font-size:0.95rem;margin-bottom:0.55rem;">
  <b style="color:#0F172A;">{company}</b>
  {' · ' + location if location else ''}
</div>
<div style="display:flex;gap:0.35rem;flex-wrap:wrap;margin-bottom:0.8rem;">
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

    # AI analysis
    if analysis:
        _render_analysis_block(analysis)
    else:
        st.caption("Pas d'analyse IA disponible pour cette offre.")

    # Job description
    if description:
        with st.expander("Description complète", expanded=False):
            st.markdown(description)

    st.markdown(
        '<hr style="margin:1rem 0 0.7rem;border:none;border-top:1px solid #F1F5F9;">',
        unsafe_allow_html=True,
    )

    # Notes — bigger textarea than the inline table column
    st.markdown(
        '<div style="font-size:0.78rem;font-weight:600;color:#475569;'
        'margin-bottom:0.3rem;text-transform:uppercase;letter-spacing:0.06em;">'
        'Notes personnelles</div>',
        unsafe_allow_html=True,
    )
    current_notes = match.get("notes") or ""
    new_notes = st.text_area(
        "Notes",
        value=current_notes,
        height=170,
        key=f"detail_notes_{job_id}",
        label_visibility="collapsed",
        placeholder="Contacts, relances, feedback d'entretien, salaire discuté…",
    )

    # Actions
    c1, c2, c3 = st.columns([1.6, 1.2, 1])
    save_disabled = (new_notes or "").strip() == (current_notes or "").strip()
    if c1.button(
        "Enregistrer les notes",
        disabled=save_disabled,
        use_container_width=True,
        type="primary",
        icon=":material/save:",
        key=f"detail_save_{job_id}",
    ):
        update_match(user_id, job_id, notes=new_notes)
        st.session_state.pop("suivi_detail_job_id", None)
        st.rerun()
    if url:
        c2.link_button(
            "Voir l'annonce",
            url,
            use_container_width=True,
            icon=":material/open_in_new:",
        )
    if c3.button(
        "Fermer",
        use_container_width=True,
        icon=":material/close:",
        key=f"detail_close_{job_id}",
    ):
        st.session_state.pop("suivi_detail_job_id", None)
        st.rerun()


def _render_detail_picker(filtered_rows: list[dict]) -> None:
    """Selectbox + 'Ouvrir' button. Stores the picked job_id in session_state."""
    if not filtered_rows:
        return
    st.markdown(
        '<div style="font-size:0.78rem;font-weight:600;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#94A3B8;margin:1.2rem 0 0.45rem;">'
        "Détail d'une offre</div>",
        unsafe_allow_html=True,
    )
    options: list[tuple[str, str]] = [("", "— Choisir une offre —")] + [
        (r["job_id"], _detail_label(r)) for r in filtered_rows
    ]
    c_sel, c_btn = st.columns([4, 1])
    with c_sel:
        sel = st.selectbox(
            "Sélection détail",
            options=options,
            format_func=lambda t: t[1],
            key="suivi_detail_pick",
            label_visibility="collapsed",
        )
    with c_btn:
        if st.button(
            "Ouvrir",
            key="suivi_detail_open",
            use_container_width=True,
            type="primary",
            disabled=not sel[0],
            icon=":material/open_in_full:",
        ):
            st.session_state["suivi_detail_job_id"] = sel[0]
            st.rerun()


# ---------------------------------------------------------------------------
# Kanban view
# ---------------------------------------------------------------------------
def _kanban_buckets(rows: list[dict]) -> dict[str, list[dict]]:
    """Group rows by status, keeping only kanban-visible buckets.

    Rows with status not in `_KANBAN_STATUSES` (e.g. 'new', 'seen', 'archived')
    are silently dropped — the kanban is only about active candidatures.
    """
    buckets: dict[str, list[dict]] = {key: [] for key, _ in _KANBAN_STATUSES}
    for r in rows:
        s = r.get("status") or "new"
        if s in buckets:
            buckets[s].append(r)
    return buckets


def _render_kanban_card(user_id: str, match: dict) -> None:
    """One card in the kanban. Header (company + ★ + score), title, lieu,
    relance pill, then "advance" + "Détail" buttons.
    """
    job_id   = match["job_id"]
    title    = (match.get("title")   or "(sans titre)").strip()
    company  = (match.get("company") or "—").strip()
    location = (match.get("location") or "").strip()
    score    = match.get("score")
    status   = match.get("status") or "new"
    is_fav   = bool(match.get("is_favorite"))
    next_at  = _to_date(match.get("next_action_at"))

    score_html = render_score_badge(score) if score is not None else ""
    star_html  = (
        '<span style="color:#F59E0B;font-size:0.95rem;margin-left:0.25rem;">★</span>'
        if is_fav else ""
    )

    with st.container(border=True):
        st.markdown(
            f"""
<div style="display:flex;justify-content:space-between;align-items:center;
            gap:0.4rem;margin-bottom:0.3rem;">
  <span class="kanban-card-company">{company}{star_html}</span>
  {score_html}
</div>
<div class="kanban-card-title">{title}</div>
<div class="kanban-card-meta">{location or '—'}</div>
            """,
            unsafe_allow_html=True,
        )

        if next_at is not None:
            css = "due" if next_at <= date.today() else "up"
            st.markdown(
                f'<span class="kanban-relance {css}">'
                f'Relance : {next_at.isoformat()}</span>',
                unsafe_allow_html=True,
            )

        next_status   = _NEXT_STATUS.get(status)
        primary_label = _ADVANCE_LABEL.get(next_status) if next_status else None

        c_advance, c_detail = st.columns([1.2, 1])
        if primary_label and c_advance.button(
            primary_label,
            key=f"kanban_adv_{job_id}",
            use_container_width=True,
            type="primary",
        ):
            set_status(user_id, job_id, next_status)
            st.toast(
                f"Statut → {STATUS_LABELS.get(next_status, next_status)}",
                icon="✅",
            )
            st.rerun()
        if c_detail.button(
            "Détail",
            key=f"kanban_det_{job_id}",
            use_container_width=True,
            icon=":material/open_in_new:",
        ):
            st.session_state["suivi_detail_job_id"] = job_id
            st.rerun()


def _render_kanban(user_id: str, rows: list[dict]) -> None:
    """Render the pipeline as 4 columns of cards.

    Falls back to a friendly empty state if no row maps to a kanban-visible
    status (e.g. user only has favorites in 'new' / 'seen').
    """
    buckets = _kanban_buckets(rows)
    visible_total = sum(len(v) for v in buckets.values())

    if visible_total == 0:
        with st.container(border=True):
            st.markdown(
                """
<div style="text-align:center;padding:1.6rem 1rem;">
  <h4 style="margin:0 0 0.3rem 0;">Pipeline vide</h4>
  <p style="color:#64748B;margin:0;font-size:0.9rem;line-height:1.55;">
    Le kanban affiche les statuts <i>Postulé / Entretien / Offre / Refusé</i>.
    Marque une offre « J'ai postulé » sur la page Mes offres ou change un statut
    dans la vue Tableau pour faire apparaître les cartes ici.
  </p>
</div>
                """,
                unsafe_allow_html=True,
            )
        return

    # One-shot styles for the cards (idempotent — Streamlit dedupes <style>).
    # Includes responsive shrinking so the 4 columns stay readable on narrow
    # viewports — Streamlit's `st.columns` doesn't reflow to fewer columns
    # automatically, so we trade a bit of typography to keep cards usable.
    st.markdown(
        """
<style>
.kanban-col-head {
  font-size: 0.78rem; font-weight: 700; letter-spacing: 0.06em;
  text-transform: uppercase; color: #475569;
  padding: 0.5rem 0.75rem; margin-bottom: 0.65rem;
  background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px;
  display: flex; justify-content: space-between; align-items: center;
}
.kanban-col-head .count {
  font-size: 0.72rem; font-weight: 600; color: #64748B;
  background: #FFFFFF; padding: 0.1rem 0.55rem; border-radius: 999px;
  border: 1px solid #E2E8F0;
}
.kanban-card-company {
  font-size: 0.82rem; font-weight: 600; color: #334155;
  overflow-wrap: break-word; word-break: break-word;
}
.kanban-card-title {
  font-size: 0.92rem; font-weight: 600; color: #0F172A;
  line-height: 1.35; margin-bottom: 0.25rem;
  overflow-wrap: break-word; word-break: break-word;
}
.kanban-card-meta {
  color: #64748B; font-size: 0.78rem; line-height: 1.4;
  margin-bottom: 0.5rem;
}
.kanban-relance {
  display: inline-block; font-size: 0.72rem; font-weight: 600;
  padding: 0.18rem 0.55rem; border-radius: 999px; margin-bottom: 0.5rem;
}
.kanban-relance.due { background:#FEE2E2; color:#991B1B; border:1px solid #FCA5A5; }
.kanban-relance.up  { background:#EFF6FF; color:#1E40AF; border:1px solid #BFDBFE; }

@media (max-width: 768px) {
  .kanban-col-head { padding: 0.35rem 0.5rem; font-size: 0.68rem; letter-spacing: 0.04em; }
  .kanban-col-head .count { font-size: 0.66rem; padding: 0.05rem 0.4rem; }
  .kanban-card-title   { font-size: 0.82rem; line-height: 1.3; }
  .kanban-card-meta    { font-size: 0.7rem; }
  .kanban-card-company { font-size: 0.74rem; }
  .kanban-relance      { font-size: 0.65rem; padding: 0.12rem 0.4rem; }
}
</style>
        """,
        unsafe_allow_html=True,
    )

    cols = st.columns(len(_KANBAN_STATUSES), gap="small")
    for col, (status_key, status_label) in zip(cols, _KANBAN_STATUSES):
        bucket = buckets[status_key]
        with col:
            st.markdown(
                f'<div class="kanban-col-head">'
                f'<span>{status_label}</span>'
                f'<span class="count">{len(bucket)}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            if not bucket:
                st.caption("—")
                continue
            for match in bucket:
                _render_kanban_card(user_id, match)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------
def _build_dataframe(rows: list[dict]) -> pd.DataFrame:
    records = []
    for r in rows:
        records.append({
            "job_id":       r.get("job_id"),
            "Favori":       bool(r.get("is_favorite")),
            "Poste":        r.get("title") or "",
            "Entreprise":   r.get("company") or "",
            "Lieu":         r.get("location") or "",
            "Score":        r.get("score"),
            "Statut":       STATUS_LABELS.get(r.get("status") or "new", "Nouveau"),
            "Postulé le":   _to_date(r.get("applied_at")),
            "Relance":      _to_date(r.get("next_action_at")),
            "MAJ":          _short_date(r.get("status_changed_at")
                                         or r.get("scored_at")),
            "Notes":        r.get("notes") or "",
            "Lien":         r.get("url") or "",
        })
    df = pd.DataFrame(records)
    return df


def _short_date(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    try:
        return iso_ts[:10]  # 'YYYY-MM-DD'
    except Exception:  # noqa: BLE001
        return ""


def _to_date(value) -> date | None:
    """Best-effort conversion from ISO/datetime/date → date (for DateColumn).

    Handles: None, '', pd.NaT, datetime.date, datetime.datetime, pd.Timestamp,
    and ISO-8601 strings. Returns None for unparseable inputs.
    """
    if value is None or value == "":
        return None
    # pd.NaT doesn't compare equal to anything (NaT == NaT → False), so use
    # pd.isna which returns True for NaT/NaN but safely False for everything
    # else (strings, dates, etc.).
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):     # datetime / pd.Timestamp (subclass)
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _overdue_count(rows: list[dict]) -> int:
    """Count rows whose next_action_at falls on or before today."""
    today = date.today()
    n = 0
    for r in rows:
        d = _to_date(r.get("next_action_at"))
        if d is not None and d <= today:
            n += 1
    return n


def _render_editor(df: pd.DataFrame) -> pd.DataFrame | None:
    """Render st.data_editor with proper column configs. Returns the edited df."""
    if df.empty:
        return None

    # Date bounds enforced both UI-side (DateColumn) and server-side
    # (_persist_changes) — defense-in-depth against fat-fingered years.
    applied_min, applied_max = _applied_bounds()
    relance_min, relance_max = _relance_bounds()

    # Column config — pilotable statuses, checkbox favori, read-only meta, link.
    column_config = {
        "job_id": None,  # hidden
        "Favori": st.column_config.CheckboxColumn(
            "★",
            help="Marquer / démarquer comme favori",
            width="small",
            default=False,
        ),
        "Poste": st.column_config.TextColumn(
            "Poste",
            disabled=True,
            width="medium",
        ),
        "Entreprise": st.column_config.TextColumn(
            "Entreprise",
            disabled=True,
            width="small",
        ),
        "Lieu": st.column_config.TextColumn(
            "Lieu",
            disabled=True,
            width="small",
        ),
        "Score": st.column_config.NumberColumn(
            "Score",
            help="Score IA (/10)",
            format="%d",
            disabled=True,
            width="small",
        ),
        "Statut": st.column_config.SelectboxColumn(
            "Statut",
            options=[STATUS_LABELS[s] for s in PIPELINE_STATUSES],
            required=True,
            width="small",
        ),
        "Postulé le": st.column_config.DateColumn(
            "Postulé le",
            help=(
                "Date de candidature — éditable. Laissée vide tant que tu n'as "
                "pas postulé ; remplie automatiquement quand tu passes au statut "
                "« Postulé »."
            ),
            format="YYYY-MM-DD",
            min_value=applied_min,
            max_value=applied_max,
            width="small",
        ),
        "Relance": st.column_config.DateColumn(
            "Relance",
            help=(
                "Date à laquelle tu veux relancer l'entreprise. Les relances "
                "échues apparaissent en tête du résumé au-dessus du tableau."
            ),
            format="YYYY-MM-DD",
            min_value=relance_min,
            max_value=relance_max,
            width="small",
        ),
        "MAJ": st.column_config.TextColumn(
            "Dernière MAJ",
            disabled=True,
            width="small",
        ),
        "Notes": st.column_config.TextColumn(
            "Notes",
            help="Relance, contact, feedback entretien, etc.",
            width="large",
        ),
        "Lien": st.column_config.LinkColumn(
            "Lien",
            display_text="Ouvrir",
            width="small",
        ),
    }

    edited = st.data_editor(
        df,
        column_config=column_config,
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
        key="suivi_table",
        height=min(560, 68 + 38 * len(df)),
    )
    return edited


def _validate_date_in_range(label: str, d: date | None, bounds: tuple[date, date]) -> None:
    """Raise ValueError if `d` is outside `bounds`. None is always valid."""
    if d is None:
        return
    floor, ceil = bounds
    if d < floor or d > ceil:
        raise ValueError(
            f"{label} hors plage [{floor.isoformat()} ; {ceil.isoformat()}] : "
            f"{d.isoformat()}"
        )


def _row_label(row: pd.Series) -> str:
    """Human-friendly row identifier for error messages."""
    title = str(row.get("Poste") or "?")[:40]
    company = str(row.get("Entreprise") or "?")[:25]
    return f"{title} — {company}"


def _persist_changes(
    user_id: str, original: pd.DataFrame, edited: pd.DataFrame,
) -> tuple[int, list[tuple[str, str]]]:
    """Compare original vs edited and send a minimal patch per changed row.

    Matching is done by `job_id` rather than positional index — so a change of
    sort order or search filter between save-clicks won't corrupt the diff.

    Returns:
      (n_ok, errors) where:
        n_ok    = number of successful per-field DB writes
        errors  = list of (row_label, error_message) for rows that crashed.

    Errors in one row do NOT abort the whole batch — partial success is the
    expected behaviour. The caller is responsible for surfacing the report.
    """
    if edited is None or original.equals(edited):
        return 0, []

    o_by_id = {row["job_id"]: row for _, row in original.iterrows() if row.get("job_id")}
    e_by_id = {row["job_id"]: row for _, row in edited.iterrows() if row.get("job_id")}

    n_ok = 0
    errors: list[tuple[str, str]] = []

    applied_bounds = _applied_bounds()
    relance_bounds = _relance_bounds()

    for job_id, e in e_by_id.items():
        o = o_by_id.get(job_id)
        if o is None:
            continue

        label = _row_label(e)
        try:
            # Favori
            if bool(o["Favori"]) != bool(e["Favori"]):
                toggle_favorite(user_id, job_id, bool(e["Favori"]))
                n_ok += 1

            # Statut
            old_status = LABEL_TO_STATUS.get(o["Statut"], "new")
            new_status = LABEL_TO_STATUS.get(e["Statut"], old_status)
            if old_status != new_status:
                update_match(user_id, job_id, status=new_status)
                n_ok += 1

            # Notes
            old_notes = (o["Notes"] or "").strip()
            new_notes = (e["Notes"] or "").strip()
            if old_notes != new_notes:
                update_match(user_id, job_id, notes=new_notes)
                n_ok += 1

            # Postulé le (editable date) — validate range before write.
            old_applied = _to_date(o.get("Postulé le"))
            new_applied = _to_date(e.get("Postulé le"))
            if old_applied != new_applied:
                _validate_date_in_range("« Postulé le »", new_applied, applied_bounds)
                update_match(user_id, job_id, applied_at=new_applied)
                n_ok += 1

            # Relance (editable date) — validate range before write.
            old_next = _to_date(o.get("Relance"))
            new_next = _to_date(e.get("Relance"))
            if old_next != new_next:
                _validate_date_in_range("« Relance »", new_next, relance_bounds)
                update_match(user_id, job_id, next_action_at=new_next)
                n_ok += 1
        except Exception as exc:  # noqa: BLE001 — surface to UI, don't crash batch
            errors.append((label, str(exc)[:160] or type(exc).__name__))

    return n_ok, errors


# ---------------------------------------------------------------------------
# Error rendering — migration pas appliquée
# ---------------------------------------------------------------------------
def _render_schema_error(err: Exception) -> None:
    """Render a helpful error when the DB query fails.

    Tries to extract the missing column name from the error string and points
    the user at the exact migration to run. Falls back to listing both
    migrations if the parser can't identify the column.
    """
    err_str = str(err)
    missing_col = _parse_missing_column(err_str)
    targeted = _COLUMN_TO_MIGRATION.get(missing_col) if missing_col else None

    if targeted:
        body = (
            f"<p style='color:#64748B;margin:0 0 0.8rem 0;font-size:0.92rem;"
            f"line-height:1.6;'>La colonne <code>{missing_col}</code> est "
            f"manquante sur <code>user_job_matches</code> ou la vue "
            f"<code>user_matches_enriched</code>.</p>"
            f"<p style='color:#64748B;margin:0 0 0.8rem 0;font-size:0.92rem;"
            f"line-height:1.6;'>Lance <code>supabase/{targeted}</code> dans "
            f"<b>Supabase → SQL Editor → New Query → Run</b>. Le script est "
            f"idempotent — safe à relancer.</p>"
        )
    else:
        body = (
            "<p style='color:#64748B;margin:0 0 1rem 0;font-size:0.92rem;"
            "line-height:1.6;'>La page Suivi a besoin des colonnes "
            "<code>is_favorite</code>, <code>notes</code>, <code>applied_at</code>, "
            "<code>status_changed_at</code> et <code>next_action_at</code> sur "
            "<code>user_job_matches</code>, plus la vue "
            "<code>user_matches_enriched</code> à jour.</p>"
            "<p style='color:#64748B;margin:0 0 0.8rem 0;font-size:0.92rem;"
            "line-height:1.6;'>Ouvre <b>Supabase → SQL Editor → New Query</b>, "
            "colle successivement le contenu de "
            "<code>supabase/migration_tracking.sql</code> puis "
            "<code>supabase/migration_next_action.sql</code>, et lance "
            "<b>Run</b> pour chacun. Les deux sont idempotents — safe à relancer.</p>"
        )

    with st.container(border=True):
        st.markdown(
            f"""
<div style="padding:1.2rem 0.4rem;">
  <h3 style="margin:0 0 0.4rem 0;">Schéma de base de données incomplet</h3>
  {body}
</div>
            """,
            unsafe_allow_html=True,
        )
        with st.expander("Détail technique", expanded=False):
            st.code(err_str[:800] or type(err).__name__, language="text")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def main() -> None:
    user_id = user.user_id
    first_name = (profile.get("full_name") or profile["email"]).split()[0]

    st.markdown(
        f"""
<div style="margin-bottom:1.4rem;">
  <h2 style="margin:0 0 0.2rem 0;">Suivi des candidatures</h2>
  <p style="color:#64748B;margin:0;font-size:0.94rem;">
    Pilote ton pipeline — {first_name}. Tous les favoris et candidatures lancées sont ici.
  </p>
</div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div style="font-size:0.78rem;font-weight:600;letter-spacing:0.08em;'
        'text-transform:uppercase;color:#94A3B8;margin-bottom:0.45rem;">'
        'Vue</div>',
        unsafe_allow_html=True,
    )
    view = _render_view_chips()

    # Full dataset for KPIs + table rows. Wrapped in one try/except so a DB
    # schema mismatch (e.g. migration_tracking.sql pas encore passé en prod)
    # affiche un message actionable plutôt qu'une stack trace redactée.
    try:
        all_tracked = list_tracked_matches(user_id, view="pipeline")
        rows = (
            all_tracked
            if view == "pipeline"
            else list_tracked_matches(user_id, view=view)
        )
    except Exception as e:  # noqa: BLE001
        _render_schema_error(e)
        return

    _render_kpis(all_tracked)

    if not rows:
        with st.container(border=True):
            empty_msgs = {
                "pipeline": (
                    "Rien à afficher pour l'instant",
                    "Marque des offres en favori ou clique sur « J'ai postulé » depuis la page Mes offres "
                    "pour qu'elles apparaissent ici.",
                ),
                "favorites": (
                    "Aucun favori",
                    "Clique sur l'étoile sur une carte d'offre pour la mettre en favori.",
                ),
                "applied": (
                    "Aucune candidature active",
                    "Les offres marquées « J'ai postulé » apparaissent ici.",
                ),
                "all": (
                    "Aucune offre trackée",
                    "Tu n'as encore aucune interaction enregistrée.",
                ),
            }
            title_msg, body_msg = empty_msgs.get(view, empty_msgs["pipeline"])
            st.markdown(
                f"""
<div style="text-align:center;padding:2rem 1rem;">
  <h3 style="margin:0 0 0.4rem 0;">{title_msg}</h3>
  <p style="color:#64748B;margin:0 0 1rem 0;font-size:0.92rem;line-height:1.55;">{body_msg}</p>
</div>
                """,
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/2_dashboard.py",
                label="Voir mes offres",
                icon=":material/dashboard:",
            )
        return

    # Overdue reminder — computed on `rows` (the active view) so the warning
    # reflects reality regardless of a live search/sort filter below.
    nb_overdue = _overdue_count(rows)
    if nb_overdue:
        st.markdown(
            f"""
<div style="margin:0.2rem 0 1rem;padding:0.65rem 0.95rem;border-radius:10px;
            background:#FEF3C7;border:1px solid #FCD34D;color:#78350F;
            font-size:0.9rem;font-weight:500;">
  <span style="margin-right:0.4rem;">🔔</span>
  {nb_overdue} relance(s) à faire aujourd'hui ou en retard — trie par
  <i>« Relance la plus proche »</i> pour les remonter en tête.
</div>
            """,
            unsafe_allow_html=True,
        )

    # Mode toggle (Tableau / Kanban). Right-aligned via a spacer column so it
    # sits unobtrusively above the search/sort row.
    _spacer, c_mode = st.columns([3, 1])
    with c_mode:
        mode = _render_mode_toggle()

    query, sort_key = _render_search_sort()
    filtered_rows = _apply_search_sort(rows, query, sort_key)

    if not filtered_rows:
        # Rows exist but the search killed them all — different empty state.
        msg = f"Aucun résultat pour « {query} »." if query else "Aucun résultat."
        with st.container(border=True):
            st.markdown(
                f"""
<div style="text-align:center;padding:1.6rem 1rem;">
  <h4 style="margin:0 0 0.3rem 0;">{msg}</h4>
  <p style="color:#64748B;margin:0;font-size:0.9rem;">
    Essaye un autre mot-clé ou change le tri pour retrouver tes offres.
  </p>
</div>
                """,
                unsafe_allow_html=True,
            )
        return

    # Small "X résultats" hint only when a filter is active, so the UI stays
    # calm by default.
    if query:
        st.caption(f"{len(filtered_rows)} résultat(s) sur {len(rows)} suivis.")

    if mode == "kanban":
        _render_kanban(user_id, filtered_rows)
        st.caption(
            "Astuce : clique « → Entretien / Offre » pour faire avancer une carte "
            "dans le pipeline. Pour les transitions non-linéaires (refus en cours "
            "d'entretien, archivage anticipé…), bascule sur la vue **Tableau**."
        )
    else:
        df = _build_dataframe(filtered_rows)
        # Preserve original for diffing on persist.
        st.session_state["suivi_original"] = df.copy()

        edited = _render_editor(df)

        c_save, c_reset, _ = st.columns([1.4, 1, 4])
        with c_save:
            if st.button(
                "Enregistrer les modifications",
                use_container_width=True,
                type="primary",
                icon=":material/save:",
            ):
                n_ok, errors = _persist_changes(
                    user_id, st.session_state["suivi_original"], edited
                )
                if n_ok == 0 and not errors:
                    st.info("Aucune modification à enregistrer.")
                elif errors and n_ok == 0:
                    st.error(
                        f"Échec de l'enregistrement : {len(errors)} ligne(s) "
                        "en erreur, aucune modification persistée."
                    )
                    with st.expander("Détails des erreurs", expanded=True):
                        for lbl, msg in errors:
                            st.markdown(f"- **{lbl}** — `{msg}`")
                elif errors:
                    st.warning(
                        f"{n_ok} modification(s) enregistrée(s) — "
                        f"{len(errors)} ligne(s) en erreur (voir détails)."
                    )
                    with st.expander("Détails des erreurs", expanded=False):
                        for lbl, msg in errors:
                            st.markdown(f"- **{lbl}** — `{msg}`")
                    st.toast(
                        f"{n_ok} OK · {len(errors)} échec(s)", icon="⚠️",
                    )
                else:
                    st.success(f"{n_ok} modification(s) enregistrée(s).")
                    st.toast(
                        f"{n_ok} modification(s) enregistrée(s)", icon="✅",
                    )
                    st.rerun()
        with c_reset:
            if st.button(
                "Annuler",
                use_container_width=True,
                icon=":material/undo:",
            ):
                st.rerun()

        st.caption(
            "Astuce : coche « ★ » pour épingler, passe le statut à « Entretien » quand "
            "tu décroches un call, et pose une date dans la colonne **Relance** pour "
            "que Kairo te rappelle les suivis en retard. Clique « Enregistrer » pour persister."
        )

        # Detail picker only useful in table mode — kanban cards already expose
        # a "Détail" button per card.
        _render_detail_picker(filtered_rows)

    detail_job_id = st.session_state.get("suivi_detail_job_id")
    if detail_job_id:
        # Lookup in the full (unfiltered) tracked set so a filter change
        # doesn't orphan an open detail.
        match = next((r for r in rows if r.get("job_id") == detail_job_id), None)
        if match is None:
            # The match is no longer tracked (e.g. user archived it elsewhere).
            # Clear silently.
            st.session_state.pop("suivi_detail_job_id", None)
        else:
            _detail_dialog(user_id, match)


if __name__ == "__main__":
    main()
else:
    main()
