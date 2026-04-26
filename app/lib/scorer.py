"""Per-user LLM scoring of a job offer (Phase 3).

Mirrors the V1 `llm_scorer.analyze_offer` pattern, but builds the system prompt
dynamically from the user's stored config (profile_summary, target, constraints,
scoring_hints) instead of a hardcoded CV_SUMMARY.

Single public function:
    analyze_offer_for_user(user_config, cv_text, job) -> dict | None

Output shape (same as V1, kept stable so the dashboard can render either):
    {
      "score": int 0-10,
      "reason": str,
      "match_role": int 0-10,
      "match_geo": int 0-10,
      "match_seniority": int 0-10,
      "red_flags": [str, ...],
      "strengths": [str, ...],
      "salary": str | null,
      "contact": str | null,
      "deadline": str | null,
      "apply_hint": str
    }
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from app.lib.klog import log
from app.lib.supabase_client import _secret as _get_secret


# llama-3.3-70b-versatile → 100K TPD free, best reasoning quality.
# When Groq's TPD runs out (~25 jobs/day at this size), we automatically fall
# back to Gemini 2.0 Flash (see _call_llm). Both models deliver high-quality
# scoring, so we optimize for fit precision over throughput.
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Gemini fallback — used when Groq returns 429 (per-min) or exhausts its TPD.
# Gemini 2.0 Flash free tier: 1500 req/day, 1M tokens/min — plenty of headroom.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Module-level flags: once a provider reports "tokens per day" exhausted in
# this process, skip it for all subsequent calls (go straight to fallback).
# `_LLM_QUOTA` carries a coarse status (None / "groq_exhausted" / "all_exhausted")
# usable from the scraper / admin UI to surface a banner.
_GROQ_TPD_EXHAUSTED = False
_GEMINI_QUOTA_EXHAUSTED = False
_LLM_QUOTA: dict[str, Any] = {"groq_tpd": False, "gemini_quota": False}


def llm_quota_state() -> dict[str, Any]:
    """Snapshot of provider quota flags. Read-only — for telemetry / UI banners.

    Returns a dict with:
      - groq_tpd:       True if Groq's daily token cap was hit this process
      - gemini_quota:   True if Gemini returned a quota/billing 429 this process
      - all_exhausted:  True iff both providers are unavailable
    """
    return {
        "groq_tpd": bool(_GROQ_TPD_EXHAUSTED),
        "gemini_quota": bool(_GEMINI_QUOTA_EXHAUSTED),
        "all_exhausted": bool(_GROQ_TPD_EXHAUSTED and _GEMINI_QUOTA_EXHAUSTED),
    }


# ---------------------------------------------------------------------------
# JSON parsing helpers — LLMs occasionally return slightly malformed payloads
# (markdown fences, trailing prose, BOMs). Recover what we can; if recovery
# fails, surface a `parse_failed` analysis instead of crashing the run.
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json|javascript|js)?\s*\n?", re.IGNORECASE)
_TRAILING_FENCE_RE = re.compile(r"\n?```\s*$")


def _strip_fences(text: str) -> str:
    """Drop leading ```json / ```js / ``` and any trailing ``` fence."""
    if not text:
        return text
    text = _FENCE_RE.sub("", text, count=1)
    text = _TRAILING_FENCE_RE.sub("", text)
    return text.strip()


def _extract_json_object(text: str) -> str | None:
    """Find the outermost balanced {...} in `text`. Tolerates leading/trailing
    prose. Returns the JSON substring or None if no balanced block exists.
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_llm_json(text: str, *, provider: str) -> dict | None:
    """Strict-then-tolerant JSON parser.

    Pipeline:
      1. Strict ``json.loads`` on the trimmed text.
      2. Strip code fences (``` / ```json), retry strict.
      3. Extract the outermost balanced ``{...}`` block, retry strict.

    Returns the parsed dict, or None if every attempt failed (caller will
    insert a parse_failed marker).
    """
    if not text:
        return None
    candidates = [text.strip()]
    stripped = _strip_fences(text)
    if stripped and stripped != candidates[0]:
        candidates.append(stripped)
    extracted = _extract_json_object(stripped or text)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    last_err: Exception | None = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if isinstance(obj, dict):
            return obj
    log(
        "scorer.parse_failed",
        level="warn",
        provider=provider,
        error=str(last_err) if last_err else "no json object",
        head=(text[:200] if text else ""),
    )
    return None


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------
def _summarize_profile(config: dict[str, Any], cv_text: str) -> str:
    """Compact, LLM-friendly synthesis of the user's profile + search intent."""
    p = config.get("profile_summary") or {}
    t = config.get("target") or {}
    c = config.get("constraints") or {}
    s = config.get("scoring_hints") or {}

    langs = ", ".join(
        f"{l.get('lang', '?')}={l.get('level', '?')}"
        for l in (p.get("languages") or [])
    ) or "non spécifié"

    roles = ", ".join(t.get("roles") or []) or "non spécifié"
    seniority = ", ".join(t.get("seniority") or []) or "non spécifié"
    industries_in = ", ".join(t.get("industries_include") or []) or "aucune contrainte"
    industries_out = ", ".join(t.get("industries_exclude") or []) or "aucune"
    sizes = ", ".join(t.get("company_sizes") or []) or "toutes"
    avoid_co = ", ".join(t.get("avoid_companies") or []) or "aucune"
    target_co = ", ".join(t.get("target_companies") or []) or "aucune préférée"

    locs = c.get("locations") or []
    locs_str = (
        "; ".join(
            f"{loc.get('city', '?')}/{loc.get('country', '?')} (rayon {loc.get('radius_km', 30)}km)"
            for loc in locs
        )
        or "aucune ville"
    )
    remote = c.get("remote") or "any"
    contracts = ", ".join(c.get("contract_types") or []) or "CDI"
    salary_min = c.get("salary_min")
    salary_ccy = c.get("salary_currency") or "EUR"
    salary_str = f"{salary_min:,} {salary_ccy}" if salary_min else "non spécifié"

    must = ", ".join(s.get("must_have") or []) or "aucun"
    nice = ", ".join(s.get("nice_to_have") or []) or "aucun"
    breakers = ", ".join(s.get("deal_breakers") or []) or "aucun"

    cv_excerpt = (cv_text or "").strip()[:2500]
    cv_line = f"\nCV (extrait) :\n{cv_excerpt}" if cv_excerpt else ""

    return (
        f"Rôle actuel : {p.get('current_role') or '?'}\n"
        f"Expérience : {p.get('years_xp') or '?'} ans\n"
        f"Compétences top : {', '.join(p.get('top_skills') or []) or '?'}\n"
        f"Langues : {langs}\n"
        f"Éducation : {p.get('education_level') or '?'}\n"
        f"\n--- Recherche ---\n"
        f"Rôles visés : {roles}\n"
        f"Séniorité cible : {seniority}\n"
        f"Industries OK : {industries_in}\n"
        f"Industries exclues : {industries_out}\n"
        f"Tailles d'entreprise : {sizes}\n"
        f"Companies rêvées : {target_co}\n"
        f"Companies à éviter : {avoid_co}\n"
        f"\n--- Contraintes ---\n"
        f"Localisations : {locs_str}\n"
        f"Remote : {remote}\n"
        f"Contrats : {contracts}\n"
        f"Salaire min : {salary_str}\n"
        f"\n--- Signaux scoring ---\n"
        f"MUST (doit être présent) : {must}\n"
        f"NICE (bonus) : {nice}\n"
        f"DEAL-BREAKERS (tue le score) : {breakers}"
        f"{cv_line}"
    )


def build_system_prompt(config: dict[str, Any], cv_text: str) -> str:
    """System prompt for `analyze_offer_for_user`, personalized to this user.

    v2 changes vs v1:
      - Explicit step-by-step process (forces structured reasoning).
      - Numerical formula instead of holistic "rubric feel".
      - `_reasoning` JSON-CoT field (3 sentences) → stripped post-parse,
        but forces the model to think before scoring.
      - 4 adversarial examples (orphan deal-breakers, dream-co + adjacent role,
        full-remote w/o geo, seniority above target).
      - Self-check rules at the end.
    """
    profile_block = _summarize_profile(config, cv_text)
    raw_brief = (config.get("raw_brief") or "").strip()
    brief_block = f"\n--- Brief libre du candidat ---\n{raw_brief[:1500]}\n" if raw_brief else ""

    return f"""Tu es un recruteur senior qui évalue une offre pour ce candidat précis :

{profile_block}
{brief_block}
Tu dois produire une analyse structurée EN FRANÇAIS, au format JSON strict :

{{
  "_reasoning": "<3 phrases — étapes 1→6 du processus ci-dessous, pour cadrer ton raisonnement>",
  "score": <int 0-10>,                   // score global de fit (issu de la FORMULE)
  "reason": "<1 phrase FR synthèse, lisible côté UI>",
  "match_role": <int 0-10>,              // match titre/fonction avec target.roles (sémantique)
  "match_geo": <int 0-10>,               // 10 si dans constraints.locations, 0 si totalement off
  "match_seniority": <int 0-10>,         // match avec target.seniority
  "red_flags": ["<3 max, très courts>"],
  "strengths": ["<3 max — atouts à mettre en avant en entretien>"],
  "salary": "<string ou null>",
  "contact": "<string ou null>",
  "deadline": "<YYYY-MM-DD ou string ou null>",
  "apply_hint": "<string courte — ex: 'Easy Apply', 'Via site carrière'>"
}}

═══════════════════════════════════════════════════════════════════
PROCESSUS À SUIVRE (raisonne dans `_reasoning` avant de scorer)
═══════════════════════════════════════════════════════════════════

ÉTAPE 1 — Identifier le titre exact du poste (champ Titre, ou 1ère phrase
          de la description si Titre est ambigu/générique).

ÉTAPE 2 — match_role (0-10) : à quel point le titre correspond
          SÉMANTIQUEMENT à un des "Rôles visés" ?
          - 10 = match exact ou synonyme strict (ex: "Equity Structuring
            Analyst" pour un cible "Structurer")
          - 7-9 = rôle voisin / famille de compétences identique
            (ex: "Customised Solutions Specialist" ≈ "Investment Solutions
            Structurer")
          - 4-6 = rôle adjacent en finance mais axe différent (Quant Risk
            pour cible Structuring)
          - 0-3 = métier différent (Sales pur, Compliance, Audit, IT…)

ÉTAPE 3 — match_geo (0-10) : la localisation tombe-t-elle dans
          constraints.locations (avec rayon) ?
          - 10 = même ville ou voisine évidente (Genève=Geneva=Nyon ;
            Zurich=Zürich=Zug à 30km près)
          - 6-9 = même pays, à <2h de train
          - 3-5 = même région large (Europe DACH si target=Suisse)
          - 0-2 = totalement off (US/Asie si target=EU)
          - REMOTE : si remote=any/yes ET poste 100% remote → 10
            indépendamment de la ville annoncée.

ÉTAPE 4 — match_seniority (0-10) :
          - 10 = pile dans target.seniority
          - 7-9 = un cran au-dessus ou en dessous (junior si target=mid)
          - 4-6 = deux crans d'écart
          - 0-3 = MD/VP si target=junior, ou intern si target=senior

ÉTAPE 5 — DEAL-BREAKER STRUCTURANT ? (vital — anti-spillover)
          Un mot deal-breaker n'a d'effet QUE s'il décrit le CŒUR du poste.
          → Cherche dans : Titre + 1ère phrase de la description.
          → Si le mot apparaît juste en passant ("travailler avec les équipes
             audit", "support compliance", "interface back-office") =
             ORPHELIN, on IGNORE.

          Cas où on cap le score à 0-2 :
            * Titre = "Auditor" / "Audit Manager" → audit
            * Titre = "Compliance Officer" / "AML Analyst" → compliance
            * Type contrat = stage/alternance/internship → cap (sauf si
              target.seniority inclut "stage")
            * Titre = "Middle Office" / "Back Office" / "Trade Support" /
              "Settlement" → cap
            * Titre ou famille = Software Engineer / IT / DevOps si target
              est finance → cap
            * Tout métier de target.deal_breakers explicitement nommé.

ÉTAPE 6 — BONUS dream company :
          - 10 si l'entreprise de l'offre figure dans target.companies
            (matching FUZZY : "Pictet Group" = "Pictet AM" = "Pictet")
          - 0 sinon.

═══════════════════════════════════════════════════════════════════
FORMULE DU SCORE GLOBAL (applique-la, ne devine pas)
═══════════════════════════════════════════════════════════════════

base = 0.40 × match_role
     + 0.30 × match_geo
     + 0.20 × match_seniority
     + 0.10 × bonus_dream_co

Puis :
  - Si DEAL-BREAKER STRUCTURANT (étape 5) : score = min(2, round(base))
  - Sinon, si bonus_dream_co = 10 ET match_role >= 7 : score = max(8, round(base))
  - Sinon, si bonus_dream_co = 10 ET match_role >= 4 : score = max(7, round(base))
  - Sinon : score = round(base), clampé à [0, 10]

═══════════════════════════════════════════════════════════════════
EXEMPLES (4 cas adversariaux)
═══════════════════════════════════════════════════════════════════

EX1 — DEAL-BREAKER ORPHELIN (anti-spill majeur)
"Equity Derivatives Structurer @ Pictet, Geneva. Description : ... vous
travaillerez en interaction étroite avec les équipes Audit et Compliance ..."
→ "Audit"/"Compliance" sont ORPHELINS (mention en passant, pas le métier).
   match_role=10, match_geo=10, match_seniority=8, dream_co=10.
   base = 4 + 3 + 1.6 + 1 = 9.6 → 10. Pas de cap. SCORE = **10**.

EX2 — DREAM CO + RÔLE ADJACENT
"Risk Manager Equity Derivatives @ Pictet, Geneva" (target = Structurer)
→ match_role=6 (adjacent : risk vs structuring, mais même desk),
   match_geo=10, match_seniority=8, dream_co=10.
   base = 2.4 + 3 + 1.6 + 1 = 8.0. Bonus dream_co + match_role>=4 force
   floor 7. Pas de cap. SCORE = **8**.

EX3 — FULL REMOTE SANS MATCH GÉO
"Quant Developer (100% remote) @ Trafigura, London" (target = Geneva,
remote=any). Match_role=8, match_seniority=9, dream_co=0.
match_geo=10 grâce au remote (override location). base = 3.2 + 3 + 1.8 + 0
= 8.0 → SCORE = **8**.

EX4 — SÉNIORITÉ TROP HAUTE
"Managing Director Equity Solutions @ JP Morgan, Geneva" (target=junior/mid)
→ match_role=9, match_geo=10, match_seniority=2 (MD = 4 crans au-dessus),
   dream_co=0. base = 3.6 + 3 + 0.4 + 0 = 7.0 → SCORE = **7**. Pas de
   cap : MD/Director ne déclenche un cap QUE si target ne couvre pas du tout
   ce niveau (ici junior/mid → cap n'est pas appliqué, mais base reflète déjà
   l'écart via match_seniority bas).

═══════════════════════════════════════════════════════════════════
VÉRIFICATION FINALE (avant de retourner le JSON)
═══════════════════════════════════════════════════════════════════

Avant de produire ta réponse, fais ces 3 contrôles mentaux :

V1 — Le score colle-t-il à la formule ? Recompute :
     round(0.4·match_role + 0.3·match_geo + 0.2·match_seniority + 0.1·dream_co)
     Si écart > 1 sans deal-breaker, recale.

V2 — As-tu confondu un mot deal-breaker ORPHELIN avec un deal-breaker
     STRUCTURANT ? Si le titre n'est PAS un titre d'audit/compliance/back-office
     et que tu as quand même cap à 2 → REMONTE le score à la base.

V3 — Si l'entreprise est dans Companies rêvées ET match_role >= 4, le score
     est >= 7. Si tu as mis < 7, recale.

═══════════════════════════════════════════════════════════════════
RÈGLES OPÉRATIONNELLES
═══════════════════════════════════════════════════════════════════

- Match sémantique, jamais littéral. "Customised Solutions Specialist" =
  match avec "Investment Solutions Structurer".
- MUST-have = signal positif (boost). L'absence ne CAP pas. Un rôle qui
  matche sémantiquement sans aucun must-have explicite reste scorable >= 7.
- Ne jamais inventer salary/contact/deadline → null si absent.
- Red flags & strengths : 3 max chacun, phrases courtes.
- Tu réponds UNIQUEMENT avec le JSON (commence par '{{', finis par '}}').
  Pas de markdown, pas de préambule, pas de commentaire après.
"""


# ---------------------------------------------------------------------------
# Phase 4 — synthesis-driven prompt builders
# ---------------------------------------------------------------------------
def _summarize_synthesis(synthesis: dict[str, Any], cv_text: str) -> str:
    """Compact, LLM-friendly synthesis dump (Phase 4 path).

    Same shape and tone as `_summarize_profile`, but consumes the structured
    synthesis (role_families, geo.{primary,acceptable,exclude}, seniority_band,
    deal_breakers, dream_companies, languages). Inactive role_families are
    skipped so their titles don't leak into the role match.
    """
    role_families = synthesis.get("role_families") or []
    active_families = [
        f for f in role_families if isinstance(f, dict) and f.get("active", True)
    ]
    active_families.sort(key=lambda f: float(f.get("weight") or 0.0), reverse=True)

    if active_families:
        family_lines = []
        for f in active_families:
            label = (f.get("label") or "").strip() or "?"
            titles = ", ".join(t for t in (f.get("titles") or []) if (t or "").strip())
            weight = float(f.get("weight") or 0.0)
            family_lines.append(
                f"  - {label} (poids {weight:.2f}) : {titles or '?'}"
            )
        roles_block = "\n".join(family_lines)
    else:
        roles_block = "  (aucune famille active)"

    geo = synthesis.get("geo") or {}
    primary = ", ".join(s for s in (geo.get("primary") or []) if s) or "non spécifié"
    acceptable = ", ".join(s for s in (geo.get("acceptable") or []) if s) or "aucune"
    exclude = ", ".join(s for s in (geo.get("exclude") or []) if s) or "aucune"

    sb = synthesis.get("seniority_band") or {}
    sb_label = (sb.get("label") or "?").strip() or "?"
    yoe_min = sb.get("yoe_min")
    yoe_max = sb.get("yoe_max")
    if yoe_min is not None and yoe_max is not None:
        sb_str = f"{sb_label} ({yoe_min}-{yoe_max} ans XP)"
    else:
        sb_str = sb_label

    breakers = ", ".join(d for d in (synthesis.get("deal_breakers") or []) if d) or "aucun"
    dream = ", ".join(c for c in (synthesis.get("dream_companies") or []) if c) or "aucune"
    languages = ", ".join(l for l in (synthesis.get("languages") or []) if l) or "non spécifié"
    summary_fr = (synthesis.get("summary_fr") or "").strip()

    # Open questions answered by the user are signal — fold them in as raw text
    # so the LLM can read them as user-stated facts.
    answered_oq = []
    for q in synthesis.get("open_questions") or []:
        if not isinstance(q, dict):
            continue
        a = (q.get("answer") or "").strip()
        if a:
            answered_oq.append(f"  - {q.get('text', '?')} → {a}")
    oq_block = ""
    if answered_oq:
        oq_block = "\n--- Réponses user (open questions) ---\n" + "\n".join(answered_oq)

    cv_excerpt = (cv_text or "").strip()[:2500]
    cv_line = f"\n--- CV (extrait) ---\n{cv_excerpt}" if cv_excerpt else ""

    summary_line = f"Synthèse profil : {summary_fr}\n\n" if summary_fr else ""

    return (
        f"{summary_line}"
        f"--- Familles de rôles cibles (par priorité) ---\n"
        f"{roles_block}\n"
        f"\n--- Séniorité visée ---\n"
        f"{sb_str}\n"
        f"\n--- Géo ---\n"
        f"Primaire : {primary}\n"
        f"Acceptable : {acceptable}\n"
        f"Exclu : {exclude}\n"
        f"\n--- Langues ---\n{languages}\n"
        f"\n--- Companies rêvées ---\n{dream}\n"
        f"\n--- DEAL-BREAKERS (tue le score si STRUCTURANTS) ---\n{breakers}"
        f"{oq_block}"
        f"{cv_line}"
    )


def build_system_prompt_from_synthesis(synthesis: dict[str, Any], cv_text: str) -> str:
    """System prompt for `analyze_offer_with_synthesis` — same formula and
    adversarial examples as `build_system_prompt`, but the profile block is
    sourced from the structured synthesis instead of user_config.

    Keeps the contract (output schema, scoring formula, deal-breaker rules)
    identical so the dashboard can render results from either path.
    """
    profile_block = _summarize_synthesis(synthesis, cv_text)

    return f"""Tu es un recruteur senior qui évalue une offre pour ce candidat précis :

{profile_block}

Tu dois produire une analyse structurée EN FRANÇAIS, au format JSON strict :

{{
  "_reasoning": "<3 phrases — étapes 1→6 du processus ci-dessous, pour cadrer ton raisonnement>",
  "score": <int 0-10>,                   // score global de fit (issu de la FORMULE)
  "reason": "<1 phrase FR synthèse, lisible côté UI>",
  "match_role": <int 0-10>,              // match titre/fonction avec une famille active (sémantique)
  "match_geo": <int 0-10>,               // 10 si dans geo.primary, 6-9 si geo.acceptable, 0 si geo.exclude
  "match_seniority": <int 0-10>,         // match avec seniority_band
  "red_flags": ["<3 max, très courts>"],
  "strengths": ["<3 max — atouts à mettre en avant en entretien>"],
  "salary": "<string ou null>",
  "contact": "<string ou null>",
  "deadline": "<YYYY-MM-DD ou string ou null>",
  "apply_hint": "<string courte — ex: 'Easy Apply', 'Via site carrière'>"
}}

═══════════════════════════════════════════════════════════════════
PROCESSUS À SUIVRE (raisonne dans `_reasoning` avant de scorer)
═══════════════════════════════════════════════════════════════════

ÉTAPE 1 — Identifier le titre exact du poste (champ Titre, ou 1ère phrase
          de la description si Titre est ambigu/générique).

ÉTAPE 2 — match_role (0-10) : à quel point le titre correspond
          SÉMANTIQUEMENT à un des titres listés dans une famille active ?
          - 10 = match exact ou synonyme strict avec une famille de poids >= 0.8
          - 7-9 = match avec une famille de poids 0.5-0.8, OU rôle voisin
            d'une famille forte (même desk / compétences identiques)
          - 4-6 = rôle adjacent au sein du même secteur mais axe différent
          - 0-3 = métier différent (Sales pur, Compliance, Audit, IT…
            quand aucune famille ne couvre ces verticales)

          IMPORTANT : pondère toujours par le `poids` de la famille qui matche.
          Un match exact sur une famille de poids 0.4 plafonne match_role à 7.

ÉTAPE 3 — match_geo (0-10) :
          - 10 = la localisation est dans `geo.primary` (ou ville voisine
            évidente : Genève=Geneva=Nyon ; Zurich=Zürich=Zug à 30km près)
          - 6-9 = dans `geo.acceptable`, ou même pays à <2h de train
          - 3-5 = même région large
          - 0-2 = totalement off, OU dans `geo.exclude` (cap à 0 si exclude)
          - REMOTE : si l'offre est 100% remote ET les open_questions
            indiquent que le user accepte le remote → 10 indépendamment
            de la ville annoncée.

ÉTAPE 4 — match_seniority (0-10) :
          - 10 = pile dans seniority_band (label ET fourchette yoe)
          - 7-9 = un cran au-dessus ou en dessous
          - 4-6 = deux crans d'écart
          - 0-3 = MD/VP si target=junior, ou intern si target=senior

ÉTAPE 5 — DEAL-BREAKER STRUCTURANT ? (vital — anti-spillover)
          Un deal_breaker n'a d'effet QUE s'il décrit le CŒUR du poste.
          → Cherche dans : Titre + 1ère phrase de la description.
          → Si le mot apparaît juste en passant ("travailler avec les équipes
             audit", "support compliance", "interface back-office") =
             ORPHELIN, on IGNORE.

          Cas où on cap le score à 0-2 :
            * Titre contient un deal_breaker en position structurante
              (Auditor, Compliance Officer, Middle Office, Settlement…)
            * Type contrat = stage/alternance/internship si "intern"/"stage"
              est dans deal_breakers
            * Software Engineer / IT / DevOps si target finance + ces tokens
              sont en deal_breakers
            * Géo dans `geo.exclude` (override score)

ÉTAPE 6 — BONUS dream company :
          - 10 si l'entreprise figure dans `dream_companies`
            (matching FUZZY : "Pictet Group" = "Pictet AM" = "Pictet")
          - 0 sinon.

═══════════════════════════════════════════════════════════════════
FORMULE DU SCORE GLOBAL (applique-la, ne devine pas)
═══════════════════════════════════════════════════════════════════

base = 0.40 × match_role
     + 0.30 × match_geo
     + 0.20 × match_seniority
     + 0.10 × bonus_dream_co

Puis :
  - Si DEAL-BREAKER STRUCTURANT (étape 5) : score = min(2, round(base))
  - Sinon, si géo dans `exclude` : score = min(2, round(base))
  - Sinon, si bonus_dream_co = 10 ET match_role >= 7 : score = max(8, round(base))
  - Sinon, si bonus_dream_co = 10 ET match_role >= 4 : score = max(7, round(base))
  - Sinon : score = round(base), clampé à [0, 10]

═══════════════════════════════════════════════════════════════════
EXEMPLES (4 cas adversariaux)
═══════════════════════════════════════════════════════════════════

EX1 — DEAL-BREAKER ORPHELIN (anti-spill majeur)
"Equity Derivatives Structurer @ Pictet, Geneva. Description : ... vous
travaillerez en interaction étroite avec les équipes Audit et Compliance ..."
→ "audit"/"compliance" sont ORPHELINS (mention en passant, pas le métier).
   match_role=10, match_geo=10, match_seniority=8, dream_co=10.
   base = 4 + 3 + 1.6 + 1 = 9.6 → 10. Pas de cap. SCORE = **10**.

EX2 — DREAM CO + RÔLE ADJACENT
"Risk Manager Equity Derivatives @ Pictet, Geneva" (familles : Structurer
poids 1.0, Quant Risk poids 0.6) → match_role=8 (match direct famille 0.6
+ adjacent à Structurer), match_geo=10, match_seniority=8, dream_co=10.
   base = 3.2 + 3 + 1.6 + 1 = 8.8. Bonus dream_co + match_role>=7 force
   floor 8. SCORE = **9**.

EX3 — FULL REMOTE SANS MATCH GÉO
"Quant Developer (100% remote) @ Trafigura, London" (geo.primary=Geneva,
open_question "remote ok"=oui). Match_role=8, match_seniority=9, dream_co=0.
match_geo=10 grâce au remote (override location). base = 3.2 + 3 + 1.8 + 0
= 8.0 → SCORE = **8**.

EX4 — SÉNIORITÉ TROP HAUTE
"Managing Director Equity Solutions @ JP Morgan, Geneva"
(seniority_band=mid, yoe 3-7) → match_role=9, match_geo=10,
match_seniority=2 (MD = 4 crans au-dessus), dream_co=0.
base = 3.6 + 3 + 0.4 + 0 = 7.0 → SCORE = **7**.

═══════════════════════════════════════════════════════════════════
VÉRIFICATION FINALE (avant de retourner le JSON)
═══════════════════════════════════════════════════════════════════

V1 — Le score colle-t-il à la formule ? Recompute :
     round(0.4·match_role + 0.3·match_geo + 0.2·match_seniority + 0.1·dream_co)
     Si écart > 1 sans deal-breaker, recale.

V2 — As-tu confondu un deal_breaker ORPHELIN avec un STRUCTURANT ? Si le
     titre n'est PAS un titre du métier deal-breakeur et que tu as quand
     même cap à 2 → REMONTE le score à la base.

V3 — Si l'entreprise est dans dream_companies ET match_role >= 4, le score
     est >= 7. Si tu as mis < 7, recale.

V4 — Si la localisation tombe dans geo.exclude, le score est <= 2 quoi
     qu'il arrive (sauf remote explicite override).

═══════════════════════════════════════════════════════════════════
RÈGLES OPÉRATIONNELLES
═══════════════════════════════════════════════════════════════════

- Match sémantique, jamais littéral. "Customised Solutions Specialist" =
  match avec "Investment Solutions Structurer".
- Le poids d'une famille module match_role : un match parfait sur famille
  de poids 0.4 ≠ un match parfait sur famille de poids 1.0.
- Ne jamais inventer salary/contact/deadline → null si absent.
- Red flags & strengths : 3 max chacun, phrases courtes.
- Tu réponds UNIQUEMENT avec le JSON (commence par '{{', finis par '}}').
  Pas de markdown, pas de préambule, pas de commentaire après.
"""


# ---------------------------------------------------------------------------
# Groq HTTP
# ---------------------------------------------------------------------------
def _call_groq(system: str, user_msg: str, *, max_tokens: int = 700) -> dict | None:
    """Call Groq. Returns None on any failure (caller falls back to Gemini).

    Sets module-level `_GROQ_TPD_EXHAUSTED` when daily token cap is hit, so the
    rest of the run skips Groq entirely.
    """
    global _GROQ_TPD_EXHAUSTED
    api_key = _get_secret("GROQ_API_KEY")
    if not api_key:
        return None

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.15,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        # Determinism: same offer + same prompt → same score across runs.
        # Groq supports `seed` (OpenAI-compatible). Gemini has its own seed below.
        "seed": 42,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "job-spy-scorer/1.0",
    }

    for attempt in range(4):
        try:
            req = urllib.request.Request(GROQ_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
            text = body["choices"][0]["message"]["content"]
            return _parse_llm_json(text, provider="groq")
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:400]
            except Exception:
                err_body = ""
            if e.code == 429:
                err_lower = err_body.lower()
                # Daily cap? Flip the flag and bail — don't waste retries here.
                if "tokens per day" in err_lower or "tpd" in err_lower or "daily" in err_lower:
                    if not _GROQ_TPD_EXHAUSTED:
                        log("scorer.groq.tpd_exhausted", level="warn", body=err_body[:200])
                    _GROQ_TPD_EXHAUSTED = True
                    _LLM_QUOTA["groq_tpd"] = True
                    return None
                if attempt < 3:
                    wait = 5 * (2 ** attempt)
                    log(
                        "scorer.groq.rate_limited",
                        level="warn",
                        attempt=attempt + 1,
                        retry_in_s=wait,
                    )
                    time.sleep(wait)
                    continue
            log(
                "scorer.groq.http_error",
                level="error",
                code=e.code,
                body=err_body[:200],
            )
            return None
        except Exception as e:  # noqa: BLE001
            log("scorer.groq.error", level="error", error=str(e))
            return None
    return None


# ---------------------------------------------------------------------------
# Gemini HTTP (fallback)
# ---------------------------------------------------------------------------
def _call_gemini(system: str, user_msg: str, *, max_tokens: int = 700) -> dict | None:
    """Call Gemini 2.0 Flash. Structured JSON output via responseMimeType."""
    api_key = _get_secret("GEMINI_API_KEY")
    if not api_key:
        return None

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            # Determinism: same input → same score.
            "seed": 42,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    url = f"{GEMINI_URL.format(model=GEMINI_MODEL)}?key={api_key}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "job-spy-scorer/1.0",
    }

    global _GEMINI_QUOTA_EXHAUSTED
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
            candidates = body.get("candidates") or []
            if not candidates:
                log("scorer.gemini.empty_candidates", level="warn", body_head=str(body)[:200])
                return None
            parts = candidates[0].get("content", {}).get("parts") or []
            if not parts:
                return None
            text = parts[0].get("text") or ""
            return _parse_llm_json(text, provider="gemini")
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:300]
            except Exception:
                err_body = ""
            if e.code == 429:
                err_lower = err_body.lower()
                # Daily quota → flag + bail. Per-min throttle → retry.
                if "quota" in err_lower or "billing" in err_lower or "exceeded" in err_lower:
                    if not _GEMINI_QUOTA_EXHAUSTED:
                        log("scorer.gemini.quota_exhausted", level="error", body=err_body[:200])
                    _GEMINI_QUOTA_EXHAUSTED = True
                    _LLM_QUOTA["gemini_quota"] = True
                    return None
                if attempt < 2:
                    wait = 3 * (2 ** attempt)
                    log(
                        "scorer.gemini.rate_limited",
                        level="warn",
                        attempt=attempt + 1,
                        retry_in_s=wait,
                    )
                    time.sleep(wait)
                    continue
            log(
                "scorer.gemini.http_error",
                level="error",
                code=e.code,
                body=err_body[:200],
            )
            return None
        except Exception as e:  # noqa: BLE001
            log("scorer.gemini.error", level="error", error=str(e))
            return None
    return None


# ---------------------------------------------------------------------------
# Provider dispatcher: Groq first (fast, cheap), Gemini on failure
# ---------------------------------------------------------------------------
def _call_llm(system: str, user_msg: str, *, max_tokens: int = 700) -> dict | None:
    """Route a call to Groq → Gemini fallback chain.

    - If Groq has TPD exhausted this run, skip straight to Gemini.
    - If both fail, return None.
    """
    if not _GROQ_TPD_EXHAUSTED and _get_secret("GROQ_API_KEY"):
        result = _call_groq(system, user_msg, max_tokens=max_tokens)
        if result is not None:
            return result
    # Groq unavailable, exhausted, or failed → try Gemini.
    if _get_secret("GEMINI_API_KEY"):
        return _call_gemini(system, user_msg, max_tokens=max_tokens)
    return None


# ---------------------------------------------------------------------------
# Heuristic fallback — used when EVERY LLM provider has failed.
# ---------------------------------------------------------------------------
# Cheap, deterministic, fully local. Better than None, worse than the LLM.
# The dashboard surfaces `_method = "heuristic"` so the user knows it's
# best-effort.
_DEAL_BREAKER_TITLE_TOKENS = (
    # Title-level deal-breakers (English + French). Substring match against
    # the job title only — never the description (avoids the orphan trap).
    "audit", "auditor", "auditeur",
    "compliance", "aml", "kyc",
    "back office", "back-office", "middle office", "middle-office",
    "settlement", "trade support", "post-trade", "operations analyst",
    "stagiaire", "stage", "alternance", "intern ", "internship", "apprenti",
    "software engineer", "devops", "front-end", "backend", "full-stack",
)


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _heuristic_score(
    user_config: dict[str, Any],
    cv_text: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    """Local rules-based fallback when all LLM providers are down.

    Returns a stable dict with the same shape as the LLM output, plus
    `_method = "heuristic"` so the UI / scraper can flag it as best-effort.

    Algorithm (intentionally simple, predictable, fast):
      - role: % of target.roles tokens present in title (max 10).
      - geo:  10 if any target city substring in location, else 5 if same
              country can be inferred, else 2.
      - seniority: 10 if target seniority token is in title, else 5.
      - dream company bonus: +3 floor if company in target.companies.
      - deal-breaker check on the title.
    """
    title = _normalize(job.get("title"))
    company = _normalize(job.get("company"))
    location = _normalize(job.get("location"))

    target = user_config.get("target") or {}
    constraints = user_config.get("constraints") or {}
    target_roles = [_normalize(r) for r in (target.get("roles") or []) if r]
    target_companies = [_normalize(c) for c in (target.get("companies") or []) if c]
    seniority_targets = [_normalize(s) for s in (target.get("seniority") or []) if s]
    target_cities = [_normalize(loc.get("city"))
                     for loc in (constraints.get("locations") or [])
                     if loc.get("city")]

    # match_role : naive token overlap on the title (capped at 10).
    role_hits = sum(1 for r in target_roles if r and r in title)
    if role_hits >= 2:
        match_role = 10
    elif role_hits == 1:
        match_role = 7
    elif any(tok in title for tok in ("structurer", "structuring", "solutions",
                                     "derivatives", "quant")):
        match_role = 5
    else:
        match_role = 2

    # match_geo : substring of any target city in the location string.
    if target_cities and any(city and city in location for city in target_cities):
        match_geo = 10
    elif location and any(city and any(part in location for part in city.split())
                          for city in target_cities if city):
        match_geo = 6
    else:
        match_geo = 3

    # match_seniority : token overlap on the title.
    if not seniority_targets:
        match_seniority = 5  # no info → neutral
    elif any(s and s in title for s in seniority_targets):
        match_seniority = 10
    else:
        match_seniority = 4

    dream_bonus = 10 if any(c and c in company for c in target_companies) else 0

    # Deal-breaker check on the TITLE only (avoids orphan-keyword trap).
    deal_breaker = any(tok in title for tok in _DEAL_BREAKER_TITLE_TOKENS)

    base = (
        0.40 * match_role
        + 0.30 * match_geo
        + 0.20 * match_seniority
        + 0.10 * dream_bonus
    )
    if deal_breaker:
        score = min(2, round(base))
    elif dream_bonus == 10 and match_role >= 7:
        score = max(8, round(base))
    elif dream_bonus == 10 and match_role >= 4:
        score = max(7, round(base))
    else:
        score = round(base)
    score = max(0, min(10, score))

    log("scorer.heuristic_fallback", level="warn",
        title=title[:60], company=company[:30], score=score)

    return {
        "score": score,
        "reason": "Score calculé localement (tous les LLMs indisponibles).",
        "match_role": match_role,
        "match_geo": match_geo,
        "match_seniority": match_seniority,
        "red_flags": ["Analyse heuristique (LLMs indisponibles)"],
        "strengths": [],
        "salary": None,
        "contact": None,
        "deadline": None,
        "apply_hint": None,
        "_method": "heuristic",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
# Suffix appended to user_msg on the self-correction retry. Strong, explicit:
# we want the model to commit to STRICT JSON only, no fences, no preamble.
_SELF_CORRECTION_SUFFIX = (
    "\n\n---\nRAPPEL CRITIQUE : ta réponse précédente n'a pas pu être parsée "
    "comme du JSON valide. Cette fois, tu DOIS répondre UNIQUEMENT avec un "
    "objet JSON conforme au schéma — commence par '{' et termine par '}'. "
    "Pas de markdown (```), pas de préambule, pas de commentaire après. "
    "Si tu hésites sur un champ, mets null plutôt que d'inventer une syntaxe."
)


def _strip_internal_keys(result: dict[str, Any]) -> dict[str, Any]:
    """Drop transient fields the LLM emits for reasoning purposes.

    `_reasoning` is part of the JSON schema (forces chain-of-thought) but it
    has no value to the UI — strip before persistence to keep the analysis
    payload compact. We KEEP `_method` and `_error` (set by us, not the LLM).
    """
    result.pop("_reasoning", None)
    return result


def analyze_offer_for_user(
    user_config: dict[str, Any],
    cv_text: str,
    job: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a structured analysis dict, or None on total failure.

    Reliability layers (in order):
      1. LLM call: Groq → Gemini fallback chain (existing).
      2. Self-correction: if parse failed, retry once with a stronger
         "JSON strict only" instruction.
      3. Heuristic fallback: when both LLMs returned None, compute a local
         best-effort score from target keywords + geo + seniority. The result
         carries `_method = "heuristic"` so the UI can flag it.

    Returns None ONLY if the heuristic also short-circuits (shouldn't happen
    in practice — kept as a safety valve).
    """
    system = build_system_prompt(user_config, cv_text)
    user_msg = (
        f"Titre : {job.get('title') or ''}\n"
        f"Entreprise : {job.get('company') or ''}\n"
        f"Lieu : {job.get('location') or ''}\n"
        f"Description :\n{(job.get('description') or '')[:2500]}"
    )

    # Layer 1 — normal LLM call.
    result = _call_llm(system, user_msg)

    # Layer 2 — self-correction. Only retry if it's worth it: skip when both
    # providers are quota-exhausted (no LLM left to ask).
    if result is None and not (_GROQ_TPD_EXHAUSTED and _GEMINI_QUOTA_EXHAUSTED):
        log("scorer.self_correction_retry", level="info")
        result = _call_llm(system, user_msg + _SELF_CORRECTION_SUFFIX)

    # Layer 3 — heuristic fallback when all LLMs are exhausted or unparseable.
    if result is None:
        log("scorer.fallback_to_heuristic", level="warn",
            title=(job.get("title") or "")[:60])
        result = _heuristic_score(user_config, cv_text, job)
        # Heuristic returns a fully-formed dict; skip normalization below.
        return result

    # Strip CoT reasoning before persistence (keeps the analysis payload tight).
    result = _strip_internal_keys(result)

    # Normalize keys so the dashboard can rely on them.
    result.setdefault("score", 0)
    result.setdefault("reason", "")
    for k in ("match_role", "match_geo", "match_seniority"):
        result.setdefault(k, -1)
    for k in ("red_flags", "strengths"):
        v = result.get(k)
        if not isinstance(v, list):
            result[k] = []
    for k in ("salary", "contact", "deadline", "apply_hint"):
        result.setdefault(k, None)
    # Score: coerce to int and clamp to [0, 10].
    try:
        s = int(result.get("score") or 0)
    except (TypeError, ValueError):
        s = 0
    result["score"] = max(0, min(10, s))
    # Sub-scores: clamp too. -1 stays as the "not provided" sentinel.
    for k in ("match_role", "match_geo", "match_seniority"):
        try:
            v = int(result.get(k) if result.get(k) is not None else -1)
        except (TypeError, ValueError):
            v = -1
        if v != -1:
            v = max(0, min(10, v))
        result[k] = v
    return result


# ---------------------------------------------------------------------------
# Phase 4 — synthesis-driven heuristic fallback + public API
# ---------------------------------------------------------------------------
def _heuristic_score_from_synthesis(
    synthesis: dict[str, Any],
    cv_text: str,
    job: dict[str, Any],
) -> dict[str, Any]:
    """Local rules-based fallback when all LLMs are down (synthesis path).

    Mirrors `_heuristic_score` but consumes the structured synthesis. Same
    output schema, with `_method = "heuristic"` so the UI flags it.
    """
    title = _normalize(job.get("title"))
    company = _normalize(job.get("company"))
    location = _normalize(job.get("location"))

    # Active families × their titles, weighted.
    role_families = synthesis.get("role_families") or []
    active = [
        f for f in role_families if isinstance(f, dict) and f.get("active", True)
    ]
    family_hits: list[float] = []
    for f in active:
        weight = float(f.get("weight") or 0.0)
        for t in (f.get("titles") or []):
            tn = _normalize(t)
            if tn and tn in title:
                family_hits.append(weight)
                break  # one hit per family suffices
    if family_hits:
        # Best family wins; cap influences ceiling.
        best_w = max(family_hits)
        match_role = max(2, min(10, round(10 * best_w)))
        # Multi-family hit gets a small boost.
        if len(family_hits) >= 2:
            match_role = min(10, match_role + 1)
    else:
        match_role = 2

    # Geo : primary > acceptable > exclude.
    geo = synthesis.get("geo") or {}
    primary = [_normalize(s) for s in (geo.get("primary") or []) if s]
    acceptable = [_normalize(s) for s in (geo.get("acceptable") or []) if s]
    exclude = [_normalize(s) for s in (geo.get("exclude") or []) if s]
    in_exclude = bool(location) and any(s and s in location for s in exclude)
    if location and any(s and s in location for s in primary):
        match_geo = 10
    elif location and any(s and s in location for s in acceptable):
        match_geo = 7
    elif in_exclude:
        match_geo = 0
    else:
        match_geo = 4

    # Seniority — heuristic on title tokens against seniority_band.label.
    sb = synthesis.get("seniority_band") or {}
    sb_label = _normalize(sb.get("label"))
    if sb_label and sb_label in title:
        match_seniority = 10
    elif not sb_label:
        match_seniority = 5
    else:
        match_seniority = 5

    # Dream company bonus (substring, fuzzy).
    dream = [_normalize(c) for c in (synthesis.get("dream_companies") or []) if c]
    dream_bonus = 10 if any(c and c in company for c in dream) else 0

    # Deal-breakers — synthesis-defined tokens, title-only match.
    deal_breakers = [_normalize(d) for d in (synthesis.get("deal_breakers") or []) if d]
    deal_breaker_hit = any(d and d in title for d in deal_breakers)

    base = (
        0.40 * match_role
        + 0.30 * match_geo
        + 0.20 * match_seniority
        + 0.10 * dream_bonus
    )
    if deal_breaker_hit or in_exclude:
        score = min(2, round(base))
    elif dream_bonus == 10 and match_role >= 7:
        score = max(8, round(base))
    elif dream_bonus == 10 and match_role >= 4:
        score = max(7, round(base))
    else:
        score = round(base)
    score = max(0, min(10, score))

    log(
        "scorer.heuristic_fallback_synthesis",
        level="warn",
        title=title[:60],
        company=company[:30],
        score=score,
    )

    return {
        "score": score,
        "reason": "Score calculé localement depuis la synthèse (LLMs indisponibles).",
        "match_role": match_role,
        "match_geo": match_geo,
        "match_seniority": match_seniority,
        "red_flags": ["Analyse heuristique (LLMs indisponibles)"],
        "strengths": [],
        "salary": None,
        "contact": None,
        "deadline": None,
        "apply_hint": None,
        "_method": "heuristic",
    }


def analyze_offer_with_synthesis(
    synthesis: dict[str, Any],
    cv_text: str,
    job: dict[str, Any],
) -> dict[str, Any] | None:
    """Score one (synthesis × job) pair. Same 3-layer reliability as
    `analyze_offer_for_user` :
      1. LLM call (Groq → Gemini) with the synthesis-aware prompt.
      2. Self-correction retry on parse failure (skip if both providers exhausted).
      3. Heuristic fallback driven by the synthesis.
    """
    system = build_system_prompt_from_synthesis(synthesis, cv_text)
    user_msg = (
        f"Titre : {job.get('title') or ''}\n"
        f"Entreprise : {job.get('company') or ''}\n"
        f"Lieu : {job.get('location') or ''}\n"
        f"Description :\n{(job.get('description') or '')[:2500]}"
    )

    result = _call_llm(system, user_msg)

    if result is None and not (_GROQ_TPD_EXHAUSTED and _GEMINI_QUOTA_EXHAUSTED):
        log("scorer.self_correction_retry", level="info", path="synthesis")
        result = _call_llm(system, user_msg + _SELF_CORRECTION_SUFFIX)

    if result is None:
        log(
            "scorer.fallback_to_heuristic",
            level="warn",
            path="synthesis",
            title=(job.get("title") or "")[:60],
        )
        return _heuristic_score_from_synthesis(synthesis, cv_text, job)

    result = _strip_internal_keys(result)

    # Same normalization tail as analyze_offer_for_user.
    result.setdefault("score", 0)
    result.setdefault("reason", "")
    for k in ("match_role", "match_geo", "match_seniority"):
        result.setdefault(k, -1)
    for k in ("red_flags", "strengths"):
        v = result.get(k)
        if not isinstance(v, list):
            result[k] = []
    for k in ("salary", "contact", "deadline", "apply_hint"):
        result.setdefault(k, None)
    try:
        s = int(result.get("score") or 0)
    except (TypeError, ValueError):
        s = 0
    result["score"] = max(0, min(10, s))
    for k in ("match_role", "match_geo", "match_seniority"):
        try:
            v = int(result.get(k) if result.get(k) is not None else -1)
        except (TypeError, ValueError):
            v = -1
        if v != -1:
            v = max(0, min(10, v))
        result[k] = v
    return result


def make_parse_failed_analysis(reason: str = "parse_failed") -> dict[str, Any]:
    """Sentinel analysis used when the LLM responded but we couldn't parse it.

    Stored on the match row so the UI can show "analyse indisponible" instead
    of crashing or pretending the job has no analysis at all.
    """
    return {
        "score": None,
        "reason": "Analyse indisponible (parsing).",
        "match_role": -1,
        "match_geo": -1,
        "match_seniority": -1,
        "red_flags": [],
        "strengths": [],
        "salary": None,
        "contact": None,
        "deadline": None,
        "apply_hint": None,
        "_error": reason,
    }
