"""Profile synthesis — LLM produces a structured profile object that pilots
the scraper (search_terms exploded from role_families) and the scorer
(boost dream_companies, cap deal_breakers).

Public functions:
    synthesize_profile(cv_text, user_config, ...) -> dict
    propose_diff(synthesis, feedback_signals) -> dict | None
    apply_diff(synthesis, diff) -> dict   # pure

Reuses Groq + Gemini HTTP plumbing from scorer.py (same quota state).

Design choice (different from scorer):
    NO heuristic fallback. A bad synthesis contaminates the entire pipeline
    downstream (wrong search_terms, wrong scoring weights). If both LLM
    providers fail, raise ProfileSynthesisError so the caller can keep the
    previous_synthesis active and surface an alert. Better to keep a stale
    profile than to ship garbage that destroys recall.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from app.lib.klog import log
from app.lib.scorer import _call_llm, _parse_llm_json  # noqa: F401  (parser exposed for tests)

PROMPT_VERSION = "v1.0"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProfileSynthesisError(RuntimeError):
    """Raised when both LLM providers fail and we cannot produce a valid
    synthesis. Caller should keep the previous active synthesis (if any)
    and surface an alert to the user.
    """


# ---------------------------------------------------------------------------
# System prompt — hardcoded in Python (decision §12 of PLAN_PROFILE_SYNTHESIS).
# Edit + deploy. Bumped via PROMPT_VERSION above.
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """Tu es un coach carrière qui synthétise le profil d'un candidat
pour piloter une recherche d'emploi automatisée. Ta synthèse alimente :
  - un scraper (qui transforme `role_families.titles` × `geo` en requêtes JobSpy)
  - un scorer LLM (qui boost `dream_companies` et cap `deal_breakers`)

Tu produis UNIQUEMENT un objet JSON strict, conforme au schéma ci-dessous.
Pas de markdown, pas de préambule, pas de commentaire après. Commence par '{'.

═══════════════════════════════════════════════════════════════════
SCHEMA DE SORTIE
═══════════════════════════════════════════════════════════════════

{
  "summary_fr": "<2-3 phrases — synthèse rédigée, lisible en UI>",
  "role_families": [
    {
      "label": "<nom court de la famille de rôles, FR ou EN>",
      "titles": ["<au moins 4 titres concrets, mix EN+FR si CV bilingue>"],
      "weight": <float 0.0-1.0 — priorité relative dans la recherche>,
      "active": true,
      "source": {
        "type": "cv|stated|inferred|feedback",
        "evidence": "<phrase courte expliquant l'origine de cette famille>"
      }
    }
  ],
  "seniority_band": {
    "label": "<junior|mid|mid-senior|senior|exec>",
    "yoe_min": <int>,
    "yoe_max": <int>
  },
  "geo": {
    "primary":    ["<villes ou pays prioritaires, format 'City, Country'>"],
    "acceptable": ["<localisations acceptables, plus larges>"],
    "exclude":    ["<localisations à exclure>"]
  },
  "deal_breakers":   ["<tokens minuscules à filtrer côté scoring>"],
  "dream_companies": ["<noms d'entreprises cibles>"],
  "languages":       ["<format 'FR-native', 'EN-C1', etc.>"],
  "confidence":      <float 0.0-1.0>,
  "open_questions": [
    {
      "id":     "<snake_case stable, ex: q_contract_type, q_field_work>",
      "text":   "<question en FR, claire et answerable en 1 mot/phrase>",
      "answer": null
    }
  ]
}

═══════════════════════════════════════════════════════════════════
RÈGLES IMPÉRATIVES
═══════════════════════════════════════════════════════════════════

R1. role_families : 3 à 5 familles MAX. Chacune DOIT contenir ≥4 titres concrets
    (synonymes, niveaux, langues). Évite les titres trop génériques type "Manager"
    seul — préfère "Clinical Project Manager", "Pharmacovigilance Manager", etc.

R2. Taxonomies sectorielles à connaître (utilise-les si le CV / config y entre) :
    - Pharma / biotech : CRA, Senior CRA, Clinical Project Manager, Clinical
      Trial Manager, Regulatory Affairs Specialist, RA Manager, Pharmacovigilance
      Officer, Drug Safety Associate, Medical Science Liaison (MSL), Medical
      Affairs Manager, CMC Specialist, QA/QC Pharma.
    - Finance / banque privée : Equity Structurer, Investment Solutions Specialist,
      Sales Trader, Cross-Asset Sales, Portfolio Manager, Risk Manager Equity,
      Quant Analyst, Wealth Manager, Relationship Manager.
    - Tech / SaaS : Software Engineer, Backend Engineer, Full-Stack Engineer,
      DevOps Engineer, SRE, Data Engineer, ML Engineer, Product Manager Tech.
    - Consulting / strat : Strategy Consultant, Management Consultant, Senior
      Associate, Engagement Manager, Principal.
    - Tu peux ajouter tes propres familles si le CV indique d'autres secteurs.

R3. Source obligatoire sur chaque role_family :
    - "cv"       : extrait directement du CV (mention ou expérience explicite)
    - "stated"   : l'user l'a déclaré dans target.roles ou raw_brief
    - "inferred" : ton inférence basée sur le contexte (industrie + niveau)
    - "feedback" : déduit du feedback_signals fourni (rejects, accepts récents)

R4. open_questions :
    - 0 à 5 questions MAX, uniquement sur ce qui est ambigu et qui changerait
      la recherche (type de contrat CDI/CDD, OK pour terrain, langues
      obligatoires, taille d'entreprise préférée, mobilité géographique).
    - id en snake_case, stable. Exemples : q_contract_type, q_field_work,
      q_languages_required, q_company_size, q_relocation.
    - Si previous_synthesis fournit des open_questions déjà répondues
      (answer != null), NE LES RÉGÉNÈRE PAS — porte-les telles quelles.
    - Si previous_synthesis fournit des open_questions non répondues, garde
      les MÊMES IDs (ne renomme pas q_contract_type en q_contract).

R5. confidence :
    - <0.5 si le CV est très court (<300 caractères), si beaucoup d'open_questions
      ouvertes, ou si CV/config se contredisent.
    - 0.5-0.7 si CV moyen et plusieurs open_questions.
    - >0.8 si le CV est clair, peu d'open_questions, et tout converge.

R6. deal_breakers :
    - Tokens en MINUSCULES (le scoring fait substring match insensible).
    - Inclure systématiquement : "intern", "internship", "stagiaire", "stage"
      si le user n'est PAS junior/student.
    - Inclure "sales" si le CV / target indique fonction technique ou
      analytique sans appétence commerciale.

R7. geo :
    - primary    = 1-3 villes/pays maximum (priorité haute pour scraper)
    - acceptable = 2-6 alternatives (élargissement raisonnable)
    - exclude    = pays/régions à fuir (ex: ["United States"] si target=Europe)
    - Si remote=any/yes dans config, ajouter "remote-CH" (ou pays correspondant)
      à acceptable.

R8. weight d'une role_family :
    - 1.0 = exactement ce que l'user fait aujourd'hui / cible explicitement.
    - 0.7-0.9 = pivot évident / rôle voisin que l'user accepterait.
    - 0.4-0.6 = rôle adjacent moins prioritaire mais à scrap quand même.
    - <0.4 : ne crée pas la famille, c'est trop loin.

═══════════════════════════════════════════════════════════════════
SI feedback_signals EST FOURNI
═══════════════════════════════════════════════════════════════════

Tu utilises ces signaux pour AFFINER la synthèse :
  - Plusieurs rejects sur des offres avec un même token (ex: "consulting") →
    ajoute le token à deal_breakers, source="feedback".
  - Plusieurs accepts/applies sur une même entreprise non listée → ajoute-la
    à dream_companies, source="feedback".
  - Plusieurs accepts sur des titres d'une famille non listée → crée la
    role_family avec source="feedback".

═══════════════════════════════════════════════════════════════════
SI previous_synthesis EST FOURNIE
═══════════════════════════════════════════════════════════════════

  - Préserve les IDs des open_questions (cf. R4).
  - Préserve les answers déjà fournies (open_question_answers).
  - Si l'user a manuellement édité une card (ajout d'un dream_company,
    désactivation d'une role_family), respecte ces edits — ne les sur-écris
    pas avec une inférence contraire.
"""


# ---------------------------------------------------------------------------
# User message builder
# ---------------------------------------------------------------------------
def _build_user_message(
    cv_text: str,
    user_config: dict[str, Any],
    feedback_signals: list[dict[str, Any]] | None,
    previous_synthesis: dict[str, Any] | None,
    open_question_answers: dict[str, str] | None,
) -> str:
    """Assemble the user-turn payload (CV + config + optional context)."""
    parts: list[str] = []

    parts.append("=== CV brut ===")
    cv_excerpt = (cv_text or "").strip()[:6000]  # ~1500 tokens, plenty
    parts.append(cv_excerpt or "(CV vide)")

    parts.append("\n=== Préférences déclarées (config) ===")
    parts.append(json.dumps(user_config or {}, ensure_ascii=False, indent=2)[:4000])

    if previous_synthesis:
        parts.append("\n=== Synthèse précédente (à enrichir, pas remplacer) ===")
        parts.append(json.dumps(previous_synthesis, ensure_ascii=False, indent=2)[:4000])

    if open_question_answers:
        parts.append("\n=== Réponses de l'user aux open_questions ===")
        parts.append(
            json.dumps(open_question_answers, ensure_ascii=False, indent=2)
        )

    if feedback_signals:
        parts.append("\n=== Feedback récent (7 derniers jours) ===")
        # Cap to 30 most recent signals to keep prompt size bounded.
        capped = feedback_signals[-30:]
        parts.append(json.dumps(capped, ensure_ascii=False, indent=2)[:3000])

    parts.append("\n=== TÂCHE ===")
    parts.append(
        "Produis maintenant le JSON strict de la synthèse selon le schéma "
        "et les règles données dans le system prompt."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------
_REQUIRED_TOP_KEYS = (
    "summary_fr",
    "role_families",
    "seniority_band",
    "geo",
    "deal_breakers",
    "dream_companies",
    "languages",
    "confidence",
    "open_questions",
)


def _validate_synthesis(obj: Any) -> dict[str, Any]:
    """Best-effort schema check. Raises ValueError on hard violations,
    coerces softer ones (missing optional list → []).
    """
    if not isinstance(obj, dict):
        raise ValueError(f"synthesis is not a dict: {type(obj).__name__}")

    for k in _REQUIRED_TOP_KEYS:
        if k not in obj:
            raise ValueError(f"synthesis missing required key: {k!r}")

    # role_families
    rf = obj.get("role_families")
    if not isinstance(rf, list) or not rf:
        raise ValueError("role_families must be a non-empty list")
    for fam in rf:
        if not isinstance(fam, dict):
            raise ValueError("role_families entry is not a dict")
        if not fam.get("label"):
            raise ValueError("role_family missing label")
        titles = fam.get("titles") or []
        if not isinstance(titles, list) or len(titles) < 1:
            raise ValueError(f"role_family {fam.get('label')!r} has no titles")
        # Coerce missing source to inferred (don't fail the whole synth).
        if "source" not in fam or not isinstance(fam["source"], dict):
            fam["source"] = {"type": "inferred", "evidence": ""}
        fam.setdefault("active", True)
        fam.setdefault("weight", 0.7)

    # geo
    geo = obj.get("geo")
    if not isinstance(geo, dict):
        raise ValueError("geo must be a dict")
    for k in ("primary", "acceptable", "exclude"):
        v = geo.get(k)
        if v is None:
            geo[k] = []
        elif not isinstance(v, list):
            raise ValueError(f"geo.{k} must be a list")

    # deal_breakers / dream_companies / languages : coerce to lower / list
    for k in ("deal_breakers", "dream_companies", "languages"):
        v = obj.get(k)
        if v is None:
            obj[k] = []
        elif not isinstance(v, list):
            raise ValueError(f"{k} must be a list")
    obj["deal_breakers"] = [str(x).lower().strip() for x in obj["deal_breakers"]]

    # confidence
    try:
        c = float(obj.get("confidence", 0.0))
        obj["confidence"] = max(0.0, min(1.0, c))
    except (TypeError, ValueError):
        obj["confidence"] = 0.5

    # open_questions
    oq = obj.get("open_questions")
    if oq is None:
        obj["open_questions"] = []
    elif not isinstance(oq, list):
        raise ValueError("open_questions must be a list")
    else:
        for q in oq:
            if not isinstance(q, dict) or not q.get("id") or not q.get("text"):
                raise ValueError("open_question entries need id+text")
            q.setdefault("answer", None)

    return obj


# ---------------------------------------------------------------------------
# Public API — synthesize_profile
# ---------------------------------------------------------------------------
def synthesize_profile(
    cv_text: str,
    user_config: dict[str, Any] | None,
    *,
    feedback_signals: list[dict[str, Any]] | None = None,
    previous_synthesis: dict[str, Any] | None = None,
    open_question_answers: dict[str, str] | None = None,
    max_tokens: int = 2200,
) -> dict[str, Any]:
    """Single LLM call → structured profile synthesis.

    Routes through the same Groq → Gemini chain as scorer.py (`_call_llm`),
    so quota state is shared (no double-spending the daily cap).

    Raises ProfileSynthesisError if both providers fail or output cannot be
    coerced to a valid synthesis schema. Caller MUST handle this case
    (typically: keep previous_synthesis as 'active', surface alert to user).
    """
    user_msg = _build_user_message(
        cv_text=cv_text,
        user_config=user_config or {},
        feedback_signals=feedback_signals,
        previous_synthesis=previous_synthesis,
        open_question_answers=open_question_answers,
    )

    raw = _call_llm(_SYSTEM_PROMPT, user_msg, max_tokens=max_tokens)
    if raw is None:
        log(
            "profile_synthesis.llm_all_providers_failed",
            level="error",
            cv_len=len(cv_text or ""),
            has_previous=previous_synthesis is not None,
        )
        raise ProfileSynthesisError(
            "Tous les providers LLM ont échoué pendant la synthèse. "
            "Garder la synthèse précédente active si elle existe."
        )

    try:
        synthesis = _validate_synthesis(raw)
    except ValueError as e:
        log(
            "profile_synthesis.validation_failed",
            level="error",
            error=str(e),
            head=str(raw)[:300],
        )
        raise ProfileSynthesisError(f"Schema invalide : {e}") from e

    log(
        "profile_synthesis.synthesized",
        n_role_families=len(synthesis.get("role_families", [])),
        confidence=synthesis.get("confidence"),
        n_open_questions=len(synthesis.get("open_questions", [])),
        has_previous=previous_synthesis is not None,
        has_feedback=feedback_signals is not None,
    )
    return synthesis


# ---------------------------------------------------------------------------
# Public API — propose_diff (continuous loop, run by nightly job)
# ---------------------------------------------------------------------------
_DIFF_SYSTEM_PROMPT = """Tu es un coach carrière qui observe le feedback récent
d'un user (status changes, rejects, accepts dans le kanban Suivi) et qui
PROPOSE une mise à jour minime de la synthèse de profil.

Tu ne fais PAS une nouvelle synthèse. Tu produis un DIFF JSON strict :

{
  "diff": {
    "add_deal_breakers":     ["<tokens à ajouter>"],
    "remove_deal_breakers":  ["<tokens à retirer>"],
    "add_dream_companies":   ["<companies à ajouter>"],
    "remove_dream_companies":["<companies à retirer>"],
    "add_role_families":     [{"label": "...", "titles": [...], "weight": 0.7,
                               "active": true,
                               "source": {"type": "feedback", "evidence": "..."}}],
    "deactivate_role_families": ["<label exact d'une famille à désactiver>"]
  },
  "rationale_fr": "<1 phrase qui explique le diff à l'user>"
}

RÈGLES :
  - Tu ne proposes un diff QUE si le signal est solide (≥3 rejects sur le
    même token, ≥2 accepts sur la même entreprise, etc.).
  - Si rien de net : retourne {"diff": {}, "rationale_fr": ""}.
  - rationale_fr DOIT être actionnable et chiffré ("5 rejects sur consulting
    cette semaine") — pas une généralité.
  - Ne supprime jamais une role_family : utilise deactivate (réversible).
"""


def propose_diff(
    synthesis: dict[str, Any],
    feedback_signals: list[dict[str, Any]],
    *,
    min_signal_count: int = 5,
    min_rejects: int = 3,
) -> dict[str, Any] | None:
    """Pour le job nightly. Retourne {diff, rationale_fr} ou None si pas
    assez de signal pour proposer quoi que ce soit.

    `min_signal_count` et `min_rejects` sont les seuils d'activation côté
    Python (gating avant LLM call). Le LLM applique ses propres filtres
    de pertinence côté serveur.
    """
    if not feedback_signals:
        return None
    rejects = sum(1 for s in feedback_signals if s.get("status_changed_to") == "rejected")
    if len(feedback_signals) < min_signal_count and rejects < min_rejects:
        return None

    user_msg = (
        "=== Synthèse active ===\n"
        + json.dumps(synthesis, ensure_ascii=False, indent=2)[:4000]
        + "\n\n=== Feedback (7 derniers jours) ===\n"
        + json.dumps(feedback_signals[-30:], ensure_ascii=False, indent=2)[:3000]
        + "\n\n=== TÂCHE ===\n"
        "Propose un diff minimal et chiffré, ou {} si rien de net."
    )

    raw = _call_llm(_DIFF_SYSTEM_PROMPT, user_msg, max_tokens=900)
    if raw is None:
        log("profile_synthesis.diff_llm_failed", level="warn")
        return None

    if not isinstance(raw, dict) or "diff" not in raw:
        log("profile_synthesis.diff_invalid_shape", level="warn", head=str(raw)[:200])
        return None

    diff = raw.get("diff") or {}
    if not isinstance(diff, dict) or not any(diff.values()):
        # LLM agreed there's nothing actionable.
        return None

    return {
        "diff": diff,
        "rationale_fr": (raw.get("rationale_fr") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Public API — apply_diff (pure)
# ---------------------------------------------------------------------------
def apply_diff(synthesis: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    """Pure function : applique un diff à une synthèse, retourne une nouvelle
    synthèse. Idempotent (appliquer 2× = appliquer 1×).
    """
    out = copy.deepcopy(synthesis)

    # deal_breakers : add / remove
    breakers = list(out.get("deal_breakers") or [])
    for tok in diff.get("add_deal_breakers", []) or []:
        tok_norm = str(tok).lower().strip()
        if tok_norm and tok_norm not in breakers:
            breakers.append(tok_norm)
    for tok in diff.get("remove_deal_breakers", []) or []:
        tok_norm = str(tok).lower().strip()
        if tok_norm in breakers:
            breakers.remove(tok_norm)
    out["deal_breakers"] = breakers

    # dream_companies : add / remove (case-preserving on add, case-insensitive on remove)
    co = list(out.get("dream_companies") or [])
    co_lower = [c.lower() for c in co]
    for c in diff.get("add_dream_companies", []) or []:
        if c and c.lower() not in co_lower:
            co.append(c)
            co_lower.append(c.lower())
    for c in diff.get("remove_dream_companies", []) or []:
        if c.lower() in co_lower:
            idx = co_lower.index(c.lower())
            co.pop(idx)
            co_lower.pop(idx)
    out["dream_companies"] = co

    # role_families : add / deactivate
    fams = list(out.get("role_families") or [])
    fam_labels = {f.get("label"): f for f in fams if isinstance(f, dict)}
    for new_fam in diff.get("add_role_families", []) or []:
        if not isinstance(new_fam, dict) or not new_fam.get("label"):
            continue
        if new_fam["label"] in fam_labels:
            continue  # idempotent: don't duplicate
        new_fam.setdefault("active", True)
        new_fam.setdefault("weight", 0.7)
        new_fam.setdefault("source", {"type": "feedback", "evidence": ""})
        fams.append(new_fam)
        fam_labels[new_fam["label"]] = new_fam
    for label in diff.get("deactivate_role_families", []) or []:
        fam = fam_labels.get(label)
        if fam is not None:
            fam["active"] = False
    out["role_families"] = fams

    return out
