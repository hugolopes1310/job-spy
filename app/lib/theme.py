"""Kairo — design system & Streamlit theming.

Usage — at the very top of each page, right after `st.set_page_config(...)`:

    from app.lib.theme import inject_theme, render_hero, render_score_badge
    inject_theme()

Everything is concentrated here so the visual identity can be iterated on in
one place without touching the pages themselves.
"""
from __future__ import annotations

import streamlit as st


# ---------------------------------------------------------------------------
# Brand tokens (keep in sync with .streamlit/config.toml)
# ---------------------------------------------------------------------------
BRAND = {
    "name":        "Kairo",
    "tagline":     "Le bon job, au bon moment.",
    "primary":     "#667eea",   # indigo-violet
    "primary_dk":  "#764ba2",   # deeper purple
    "primary_lt":  "#a5b4fc",   # lavender
    "bg":          "#FAFAFB",
    "surface":     "#FFFFFF",
    "fg":          "#0F172A",   # slate-900
    "muted_fg":    "#64748B",   # slate-500
    "border":      "#E2E8F0",   # slate-200
    "success":     "#10B981",
    "warning":     "#F59E0B",
    "danger":      "#EF4444",
    "info":        "#3B82F6",
    "gradient":    "linear-gradient(135deg, #667eea 0%, #764ba2 100%)",
    "gradient_soft": "linear-gradient(135deg, rgba(102,126,234,0.08) 0%, rgba(118,75,162,0.08) 100%)",
}


# ---------------------------------------------------------------------------
# Global CSS — injected once per page
# ---------------------------------------------------------------------------
_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@500&display=swap');

/* ---------- Base ---------- */
html, body, [class*="css"], .stApp {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    color: #0F172A;
}
.stApp {
    background-color: #FAFAFB;
    background-image:
      radial-gradient(circle at 88% -8%, rgba(102,126,234,0.14), transparent 42%),
      radial-gradient(circle at -6% 18%, rgba(118,75,162,0.10), transparent 45%),
      radial-gradient(circle at 20% 95%, rgba(102,126,234,0.08), transparent 48%);
    background-attachment: fixed;
}

/* Reduce Streamlit's default top padding so hero sections breathe */
.main .block-container { padding-top: 2.5rem; padding-bottom: 4rem; max-width: 1080px; }

/* Hide Streamlit's "Made with Streamlit" footer and deploy button */
#MainMenu, footer, .stDeployButton { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent; }

/* Hide Streamlit's auto-generated page navigator — we build our own below */
[data-testid="stSidebarNav"] { display: none !important; }
[data-testid="stSidebarNavSeparator"] { display: none !important; }

/* ---------- Headings ---------- */
h1, h2, h3, h4, h5, h6 {
    font-family: 'Inter', sans-serif;
    color: #0F172A;
    letter-spacing: -0.02em;
    font-weight: 700;
}
h1 { font-size: 2.4rem; line-height: 1.15; }
h2 { font-size: 1.75rem; }
h3 { font-size: 1.3rem; font-weight: 600; }

/* ---------- Links ---------- */
a { color: #667eea; text-decoration: none; font-weight: 500; }
a:hover { color: #764ba2; text-decoration: underline; text-decoration-thickness: 1.5px; text-underline-offset: 3px; }

/* ---------- Buttons ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
    border-radius: 10px;
    font-weight: 500;
    font-size: 0.95rem;
    padding: 0.55rem 1.1rem;
    border: 1px solid #E2E8F0;
    background: #FFFFFF;
    color: #0F172A;
    transition: all 0.15s ease;
    box-shadow: 0 1px 2px rgba(15,23,42,0.03);
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
    border-color: #667eea;
    color: #667eea;
    box-shadow: 0 2px 8px rgba(102,126,234,0.12);
    transform: translateY(-1px);
}

/* Primary button (type="primary") — gradient */
.stButton > button[kind="primary"],
.stFormSubmitButton > button[kind="primary"],
.stDownloadButton > button[kind="primary"] {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white !important;
    border: none;
    font-weight: 600;
    box-shadow: 0 4px 12px rgba(102,126,234,0.28);
}
.stButton > button[kind="primary"]:hover,
.stFormSubmitButton > button[kind="primary"]:hover,
.stDownloadButton > button[kind="primary"]:hover {
    box-shadow: 0 6px 20px rgba(102,126,234,0.4);
    transform: translateY(-1px);
    color: white !important;
}

/* ---------- Inputs ---------- */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stTextArea > div > textarea,
.stSelectbox > div > div,
.stDateInput > div > div > input {
    border-radius: 8px !important;
    border: 1px solid #E2E8F0 !important;
    font-size: 0.95rem;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus,
.stTextArea > div > textarea:focus {
    border-color: #667eea !important;
    box-shadow: 0 0 0 3px rgba(102,126,234,0.12) !important;
}

/* ---------- Cards (st.container(border=True)) ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 14px !important;
    border: 1px solid #E2E8F0 !important;
    background: #FFFFFF;
    box-shadow: 0 1px 2px rgba(15,23,42,0.04);
    transition: box-shadow 0.2s ease, transform 0.2s ease;
}
[data-testid="stVerticalBlockBorderWrapper"]:hover {
    box-shadow: 0 4px 16px rgba(15,23,42,0.06);
}

/* ---------- Alerts (info/success/warning/error) — softer, more modern ---------- */
div[data-testid="stAlert"] {
    border-radius: 10px;
    border: none;
    padding: 1rem 1.1rem;
    font-size: 0.92rem;
}

/* ---------- Metrics ---------- */
[data-testid="stMetric"] {
    background: #FFFFFF;
    border: 1px solid #E2E8F0;
    border-radius: 12px;
    padding: 1rem 1.2rem;
}
[data-testid="stMetricLabel"] { color: #64748B; font-weight: 500; }
[data-testid="stMetricValue"] { font-weight: 700; color: #0F172A; }

/* ---------- Dividers ---------- */
hr { border-color: #E2E8F0 !important; margin: 2rem 0 !important; }

/* ---------- Sidebar (custom Kairo layout) ---------- */
[data-testid="stSidebar"] {
    background: #FFFFFF;
    border-right: 1px solid #E2E8F0;
}
[data-testid="stSidebar"] > div:first-child {
    padding-top: 1.6rem !important;
}
/* Sidebar page_links : look épuré type navigation app */
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"] {
    border-radius: 10px;
    padding: 0.5rem 0.75rem !important;
    margin: 0.15rem 0 !important;
    color: #475569 !important;
    font-weight: 500 !important;
    font-size: 0.92rem !important;
    transition: background-color 0.12s ease, color 0.12s ease;
}
[data-testid="stSidebar"] [data-testid="stPageLink-NavLink"]:hover {
    background: rgba(102,126,234,0.08) !important;
    color: #667eea !important;
    text-decoration: none !important;
}
.kairo-sidebar-section {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    color: #94A3B8;
    margin: 1.2rem 0 0.4rem 0.4rem;
}
.kairo-sidebar-brand {
    display:flex; align-items:center; gap:0.55rem;
    padding: 0 0.5rem 0.5rem;
    margin-bottom: 0.4rem;
}
.kairo-sidebar-brand-text {
    font-weight: 700; font-size: 1.15rem; letter-spacing: -0.02em;
    color: #0F172A;
}
.kairo-sidebar-brand-text .grad {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.kairo-sidebar-user {
    display:flex; align-items:center; gap:0.6rem;
    padding:0.75rem 0.6rem; border-radius:12px;
    background:#F8FAFC; border:1px solid #E2E8F0;
    margin: 0.4rem 0 0.8rem 0;
}
.kairo-sidebar-avatar {
    width:32px; height:32px; border-radius:50%;
    background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);
    color:white; font-weight:600; font-size:0.86rem;
    display:inline-flex; align-items:center; justify-content:center;
    flex-shrink:0;
}
.kairo-sidebar-user-meta {
    min-width:0; line-height:1.2;
}
.kairo-sidebar-user-name {
    font-weight:600; font-size:0.88rem; color:#0F172A;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
    max-width:160px;
}
.kairo-sidebar-user-role {
    color:#64748B; font-size:0.75rem; font-weight:500;
}

/* ---------- Kairo custom classes (used by render_* helpers) ---------- */
.kairo-hero {
    padding: 2.5rem 2rem 2rem;
    border-radius: 20px;
    background: linear-gradient(135deg, rgba(102,126,234,0.08) 0%, rgba(118,75,162,0.08) 100%);
    border: 1px solid rgba(102,126,234,0.15);
    margin-bottom: 2rem;
    text-align: center;
}
.kairo-hero-logo {
    display: inline-block;
    margin-bottom: 1rem;
}
.kairo-hero-title {
    font-size: 2.6rem;
    font-weight: 800;
    letter-spacing: -0.035em;
    margin: 0 0 0.5rem 0;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}
.kairo-hero-subtitle {
    color: #64748B;
    font-size: 1.05rem;
    font-weight: 400;
    max-width: 520px;
    margin: 0 auto;
    line-height: 1.55;
}

.kairo-wordmark {
    display: inline-flex;
    align-items: center;
    gap: 0.55rem;
    font-weight: 700;
    font-size: 1.35rem;
    letter-spacing: -0.025em;
    color: #0F172A;
    text-decoration: none;
}
.kairo-wordmark-k {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}

.kairo-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.25rem 0.65rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.01em;
}
.kairo-badge-muted { background: #F1F5F9; color: #64748B; }
.kairo-badge-success { background: rgba(16,185,129,0.1); color: #059669; }
.kairo-badge-warn { background: rgba(245,158,11,0.1); color: #D97706; }
.kairo-badge-danger { background: rgba(239,68,68,0.1); color: #DC2626; }

.kairo-score {
    display: inline-flex;
    align-items: baseline;
    gap: 0.2rem;
    padding: 0.4rem 0.85rem;
    border-radius: 12px;
    font-family: 'JetBrains Mono', monospace;
    font-weight: 600;
    font-size: 0.95rem;
    letter-spacing: -0.01em;
}
.kairo-score-num { font-size: 1.3rem; font-weight: 700; }
.kairo-score-max { opacity: 0.55; font-size: 0.85rem; }
.kairo-score-top    { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; box-shadow: 0 2px 8px rgba(102,126,234,0.3); }
.kairo-score-good   { background: rgba(16,185,129,0.12); color: #059669; }
.kairo-score-mid    { background: rgba(245,158,11,0.12); color: #D97706; }
.kairo-score-low    { background: rgba(239,68,68,0.1); color: #DC2626; }
.kairo-score-na     { background: #F1F5F9; color: #94A3B8; }

.kairo-meta {
    color: #64748B;
    font-size: 0.88rem;
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    align-items: center;
}
.kairo-meta-dot { color: #CBD5E1; }

.kairo-stepper {
    display: flex;
    gap: 0;
    align-items: center;
    margin: 0.5rem 0 2rem 0;
}
.kairo-step {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 0.7rem;
    border-radius: 8px;
    font-size: 0.85rem;
    font-weight: 500;
    color: #94A3B8;
}
.kairo-step-dot {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px; height: 22px;
    border-radius: 50%;
    background: #E2E8F0;
    color: #94A3B8;
    font-size: 0.78rem;
    font-weight: 600;
}
.kairo-step.active { color: #0F172A; }
.kairo-step.active .kairo-step-dot {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white;
    box-shadow: 0 2px 6px rgba(102,126,234,0.35);
}
.kairo-step.done .kairo-step-dot { background: #10B981; color: white; }
.kairo-step-sep {
    flex: 1;
    height: 2px;
    background: #E2E8F0;
    min-width: 16px;
    margin: 0 4px;
}
.kairo-step.done + .kairo-step-sep, .kairo-step-sep.done { background: #10B981; }

/* ---------- Tiny niceties ---------- */
::selection { background: rgba(102,126,234,0.22); }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: #CBD5E1; border-radius: 6px; }
::-webkit-scrollbar-thumb:hover { background: #94A3B8; }
</style>
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def inject_theme() -> None:
    """Inject the Kairo CSS once. Idempotent (Streamlit dedupes identical markdown)."""
    st.markdown(_CSS, unsafe_allow_html=True)


# Inline SVG logo — a stylized "K" with a crescent arc suggesting the moment
# seized (kairos). Pure SVG so it scales everywhere and matches the gradient.
LOGO_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="{size}" height="{size}" role="img" aria-label="Kairo">
  <defs>
    <linearGradient id="kairo-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#667eea"/>
      <stop offset="100%" stop-color="#764ba2"/>
    </linearGradient>
  </defs>
  <rect x="1" y="1" width="46" height="46" rx="12" fill="url(#kairo-grad)"/>
  <!-- Stylized K: vertical bar + diagonals -->
  <path d="M16 12 L16 36" stroke="white" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M16 24 L30 12" stroke="white" stroke-width="3.2" stroke-linecap="round"/>
  <path d="M16 24 L32 36" stroke="white" stroke-width="3.2" stroke-linecap="round"/>
  <!-- Kairos moment: small arc/dot catching the right time -->
  <circle cx="35" cy="14" r="2.4" fill="white"/>
</svg>
"""


def logo_svg(size: int = 44) -> str:
    return LOGO_SVG.format(size=size)


def render_hero(
    title: str | None = None,
    subtitle: str | None = None,
    *,
    show_logo: bool = True,
    logo_size: int = 64,
) -> None:
    """Branded hero block with gradient background, centered logo + title."""
    title = title or BRAND["name"]
    subtitle = subtitle or BRAND["tagline"]
    logo_html = (
        f'<div class="kairo-hero-logo">{logo_svg(logo_size)}</div>' if show_logo else ""
    )
    st.markdown(
        f"""
<div class="kairo-hero">
  {logo_html}
  <h1 class="kairo-hero-title">{title}</h1>
  <p class="kairo-hero-subtitle">{subtitle}</p>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_wordmark(size: int = 28) -> None:
    """Compact wordmark for the top-left of pages (logo + 'Kairo')."""
    st.markdown(
        f"""
<div class="kairo-wordmark">
  {logo_svg(size)}
  <span>Kair<span class="kairo-wordmark-k">o</span></span>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_score_badge(score: int | None) -> str:
    """HTML for a score badge (returns string to embed inline)."""
    if score is None:
        return (
            '<span class="kairo-score kairo-score-na">'
            '<span class="kairo-score-num">—</span>'
            '<span class="kairo-score-max">/10</span></span>'
        )
    if score >= 9:
        cls = "kairo-score-top"
    elif score >= 7:
        cls = "kairo-score-good"
    elif score >= 4:
        cls = "kairo-score-mid"
    else:
        cls = "kairo-score-low"
    return (
        f'<span class="kairo-score {cls}">'
        f'<span class="kairo-score-num">{score}</span>'
        f'<span class="kairo-score-max">/10</span></span>'
    )


def render_stepper(steps: list[str], current_idx: int) -> None:
    """Horizontal stepper. `current_idx` is 0-based. Steps before it are 'done'."""
    parts: list[str] = []
    for i, label in enumerate(steps):
        state = "done" if i < current_idx else "active" if i == current_idx else ""
        dot = "✓" if state == "done" else str(i + 1)
        parts.append(
            f'<div class="kairo-step {state}">'
            f'<span class="kairo-step-dot">{dot}</span>'
            f'<span>{label}</span>'
            f'</div>'
        )
        if i < len(steps) - 1:
            sep_state = "done" if i < current_idx else ""
            parts.append(f'<div class="kairo-step-sep {sep_state}"></div>')
    st.markdown(
        f'<div class="kairo-stepper">{"".join(parts)}</div>',
        unsafe_allow_html=True,
    )


def render_sidebar(
    *,
    email: str | None = None,
    full_name: str | None = None,
    is_admin: bool = False,
    on_logout: callable | None = None,  # type: ignore[valid-type]
) -> None:
    """Custom Kairo navigation sidebar (replaces Streamlit's auto page-nav).

    Items shown :
        - Accueil           pages "/"
        - Mes offres        pages/2_dashboard.py
        - Configuration     pages/1_onboarding.py
        - Panneau admin     pages/99_admin.py   (admin only)
        - Footer            avatar + email + bouton Sortir
    """
    with st.sidebar:
        # Brand row
        st.markdown(
            f"""
<div class="kairo-sidebar-brand">
  {logo_svg(28)}
  <div class="kairo-sidebar-brand-text">Kair<span class="grad">o</span></div>
</div>
            """,
            unsafe_allow_html=True,
        )

        # User card
        if email:
            initial  = (full_name or email or "?").strip()[:1].upper()
            display  = (full_name or email).strip()
            role     = "Administrateur" if is_admin else "Membre"
            st.markdown(
                f"""
<div class="kairo-sidebar-user">
  <div class="kairo-sidebar-avatar">{initial}</div>
  <div class="kairo-sidebar-user-meta">
    <div class="kairo-sidebar-user-name">{display}</div>
    <div class="kairo-sidebar-user-role">{role}</div>
  </div>
</div>
                """,
                unsafe_allow_html=True,
            )

        # Navigation
        st.markdown(
            '<div class="kairo-sidebar-section">Navigation</div>',
            unsafe_allow_html=True,
        )
        st.page_link("streamlit_app.py", label="Accueil",
                     icon=":material/home:")
        st.page_link("pages/2_dashboard.py", label="Mes offres",
                     icon=":material/dashboard:")
        st.page_link("pages/3_suivi.py", label="Suivi",
                     icon=":material/track_changes:")
        st.page_link("pages/1_onboarding.py", label="Configuration",
                     icon=":material/tune:")

        if is_admin:
            st.markdown(
                '<div class="kairo-sidebar-section">Administration</div>',
                unsafe_allow_html=True,
            )
            st.page_link("pages/99_admin.py", label="Panneau admin",
                         icon=":material/shield:")

        # Spacer + logout
        st.markdown(
            '<div style="height:2rem;"></div>'
            '<hr style="border:none;border-top:1px solid #E2E8F0;margin:0.5rem 0;">',
            unsafe_allow_html=True,
        )
        if on_logout is not None:
            if st.button(
                "Se déconnecter",
                use_container_width=True,
                key="sidebar_logout_btn",
                icon=":material/logout:",
            ):
                on_logout()


def render_badge(text: str, tone: str = "muted") -> str:
    """Small pill badge. Tones: muted, success, warn, danger."""
    tone_cls = {
        "muted":   "kairo-badge-muted",
        "success": "kairo-badge-success",
        "warn":    "kairo-badge-warn",
        "danger":  "kairo-badge-danger",
    }.get(tone, "kairo-badge-muted")
    return f'<span class="kairo-badge {tone_cls}">{text}</span>'
