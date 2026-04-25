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
    """System prompt for `analyze_offer_for_user`, personalized to this user."""
    profile_block = _summarize_profile(config, cv_text)
    raw_brief = (config.get("raw_brief") or "").strip()
    brief_block = f"\n--- Brief libre du candidat ---\n{raw_brief[:1500]}\n" if raw_brief else ""

    return f"""Tu es un recruteur senior qui évalue une offre pour ce candidat précis :

{profile_block}
{brief_block}
Tu dois produire une analyse structurée EN FRANÇAIS, au format JSON strict :

{{
  "score": <int 0-10>,                   // score global de fit
  "reason": "<1 phrase FR synthèse>",
  "match_role": <int 0-10>,              // match titre/fonction avec target.roles
  "match_geo": <int 0-10>,               // 10 si dans constraints.locations, 0 si deal-breaker géo
  "match_seniority": <int 0-10>,         // match avec target.seniority
  "red_flags": ["<3 max, très courts>"],
  "strengths": ["<3 max — atouts à mettre en avant en entretien>"],
  "salary": "<string ou null>",
  "contact": "<string ou null>",
  "deadline": "<YYYY-MM-DD ou string ou null>",
  "apply_hint": "<string courte — ex: 'Easy Apply', 'Via site carrière'>"
}}

BARÈME DU SCORE GLOBAL :
- 9-10 : match parfait — rôle cible (même SYNONYME/ROLE VOISIN) + bonne géo + séniorité OK, aucun deal-breaker structurant
- 7-8 : très pertinent — ex: entreprise cible + rôle adjacent, OU rôle cible exact avec 1 axe légèrement faible
- 5-6 : intéressant mais 1 axe clairement faible (géo 2h de train, séniorité off d'un cran, rôle voisin)
- 3-4 : lien ténu — rôle décalé ET pas dans "Companies rêvées", OU 2 axes faibles
- 0-2 : deal-breaker STRUCTURANT présent (le rôle EST un stage/alternance/middle-office/etc.), ou géo/rôle totalement off

RÈGLES DE MATCHING — TRÈS IMPORTANT :

1. **MATCH SÉMANTIQUE, PAS LITTÉRAL**. "Customised Solutions Specialist" = match avec "Investment Solutions Structurer". "Structured Products Sales" = "Derivatives Sales" = "Solutions Sales". "Equity Derivatives" couvre aussi "Equity Structuring", "Index / Rates / Hybrids Structurer". Les MUST-have sont des FAMILLES de compétences — pas des chaînes exactes. Un match sémantique compte comme un match.

2. **BOOST TARGET COMPANIES**. Si l'entreprise de l'offre figure dans "Companies rêvées" (même fuzzy : "Pictet Group" = "Pictet", "Pictet AM" = "Pictet"), le score MINIMUM est 7 à moins que le rôle soit un deal-breaker structurant (stage/intern/middle-office/audit primaire). Si en plus le rôle matche sémantiquement → 9-10.

3. **DEAL-BREAKERS ≠ mots-clés orphelins**. Un deal-breaker ne déclenche un cap à 2 QUE si c'est le cœur du poste :
   - "audit" cap le score SEULEMENT si c'est un poste d'audit (titre = "Auditor", "Audit Manager"). Si "audit" apparaît juste dans "travailler avec les équipes audit" → on ignore.
   - "compliance" cap SEULEMENT si titre = "Compliance Officer" ou similaire. Mentionné dans une description de poste de structuration → on ignore.
   - "stage", "alternance", "intern", "junior (stage)" → cap si c'est un contrat stage/alternance (check type de contrat et titre).
   - "MD", "Senior Director", "Head of" → cap si titre EST "MD / Director / Head" (pas si "MD" apparaît comme abréviation ailleurs).
   - "middle-office", "back-office", "settlement", "NAV", "custodian", "trade support" → cap SEULEMENT si c'est le métier du poste (titre ou 1ère phrase de la description).
   - Dans le doute (mention ponctuelle dans du texte long, sans lien au poste) → tu N'appliques PAS le cap.

4. **MUST-HAVE = signal positif, pas obligation**. Un rôle qui match sémantiquement un des rôles cibles, dans une entreprise cible, SANS qu'aucun must-have littéral apparaisse → toujours scorable à 7-9 si l'alignement global est clair. Les must-have boostent si présents (sémantiquement), ils ne capent PAS l'absence.

5. **GEO** : les villes dans constraints.locations (et leur rayon) matchent leurs voisines évidentes (Genève = Geneva = Nyon = Lausanne à 30km près ; Zurich = Zürich = Zug à 30km près).

EXEMPLES :
- "Customised Solutions Specialist @ Pictet Group, Geneva" avec Hugo qui cible "Investment Solutions Structurer" et "Pictet" comme dream company → score **9** (match sémantique rôle + dream company + géo parfaite).
- "Equity Structuring Analyst @ Vontobel, Zurich" → **9-10** (rôle sémantiquement identique + dream company + géo).
- "Risk Quant Analyst @ Trafigura, Geneva" sans "Trafigura" dans les cibles → **5-6** (rôle voisin quant en finance, bonne géo, mais pas dans les dream companies et pas structuring).
- "Alternance Assistant Commercial @ Framatome" → **0-1** (contrat stage = deal-breaker structurant).
- "Software Engineer COBOL @ VESTRALA, Lyon" → **0** (software engineer = deal-breaker structurant).

AUTRES :
- Ne jamais inventer — si une info manque (salaire, contact...), mets null.
- Red flags et strengths : 3 max chacun, phrases très courtes.
- Tu réponds UNIQUEMENT avec le JSON. Pas de markdown, pas de commentaire.
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
# Public API
# ---------------------------------------------------------------------------
def analyze_offer_for_user(
    user_config: dict[str, Any],
    cv_text: str,
    job: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a structured analysis dict, or None on failure.

    `job` is a row from `public.jobs`, or any dict with keys
    title / company / location / description.
    """
    system = build_system_prompt(user_config, cv_text)
    user_msg = (
        f"Titre : {job.get('title') or ''}\n"
        f"Entreprise : {job.get('company') or ''}\n"
        f"Lieu : {job.get('location') or ''}\n"
        f"Description :\n{(job.get('description') or '')[:2500]}"
    )
    result = _call_llm(system, user_msg)
    if result is None:
        return None

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
