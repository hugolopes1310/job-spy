"""Build a structured user config from CV + cover letter + free-form brief.

Single Groq call (Llama 3.3 70B) that outputs strict JSON matching
`CONFIG_SCHEMA`. The schema is intentionally shaped to map 1:1 with the
scoring config the scraper will consume (title_boost, description_boost,
location filter, blacklist, etc.).

The HTTP logic mirrors `llm_scorer.py` so we keep a consistent retry /
fallback pattern across the codebase.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


# ---------------------------------------------------------------------------
# Schema — kept here as the single source of truth. The Streamlit review
# screen, the storage layer and the scraper all read this shape.
# ---------------------------------------------------------------------------
CONFIG_SCHEMA_DOC = """
{
  "profile_summary": {
    "current_role": "string",
    "years_xp": "int",
    "top_skills": ["string", ...],
    "languages": [{"lang": "fr|en|...", "level": "native|C2|C1|B2|B1|A2|A1"}, ...],
    "industries_past": ["string", ...],
    "education_level": "string (ex: Bachelor, Master, PhD)"
  },
  "target": {
    "roles": ["string", ...],                 // job titles the user wants
    "seniority": ["junior|mid|senior|lead|head|director"],
    "industries_include": ["string", ...],
    "industries_exclude": ["string", ...],
    "company_sizes": ["startup|scaleup|mid|large"],
    "target_companies": ["string", ...],      // dream companies
    "avoid_companies": ["string", ...]
  },
  "constraints": {
    "locations": [{"city": "string", "country": "string", "radius_km": "int"}],
    "remote": "full|hybrid_ok|onsite_only|any",
    "contract_types": ["CDI|CDD|Freelance|Internship|Apprenticeship|Contract"],
    "salary_min": "int or null",
    "salary_currency": "EUR|CHF|GBP|USD",
    "availability": "immediate|1-3_months|3-6_months|flexible"
  },
  "scoring_hints": {
    "must_have": ["string", ...],             // hard-filter keywords
    "nice_to_have": ["string", ...],          // boost keywords
    "deal_breakers": ["string", ...]          // blacklist keywords
  },
  "raw_brief": "string (verbatim free-form input)",
  "active_sources": ["linkedin", "indeed", "google_jobs"]
}
"""


SYSTEM_PROMPT = f"""Tu es un coach carrière senior qui extrait une config de recherche d'emploi structurée à partir du CV, de la cover letter et du brief libre d'un candidat.

Tu dois produire UNIQUEMENT un JSON strict au format suivant :

{CONFIG_SCHEMA_DOC}

RÈGLES D'EXTRACTION :

1. `profile_summary` vient du CV. Si une info manque, mets null ou liste vide. Ne jamais inventer. `years_xp` = somme des expériences pro post-études (estime au mieux).

2. `target.roles` vient du brief libre en priorité, du CV en fallback. Liste 3-6 titres de postes concrets (ex. "Structurer", "Cross-Asset Sales", "Quant Analyst"). Formule en anglais ET en français si le candidat semble bilingue — ça aide le scraper à matcher les offres dans les deux langues.

3. `target.seniority` : déduis depuis years_xp ET le brief. 0-1 ans = ["junior"], 2-5 ans = ["junior", "mid"], 5-8 ans = ["mid", "senior"], 8-15 ans = ["senior", "lead"], 15+ = ["lead", "head", "director"]. Le brief peut override (ex. "je cherche un poste de lead" → ["lead"]).

4. `target.industries_include` / `_exclude` : déduis du brief + des secteurs où le candidat a déjà travaillé (CV). Liste précise, pas générique.

5. `constraints.locations` : extrais CHAQUE ville mentionnée dans le brief. Si le brief dit "Genève/Zurich/Lyon", tu sors 3 entries. Pour radius_km, mets 30 par défaut en ville, 50 pour "région X". Si c'est un pays entier, mets country mais pas de city.

6. `constraints.remote` : "full" si le brief mentionne "100% remote" ou "télétravail complet". "hybrid_ok" si "hybride OK" ou rien de spécifique avec des villes listées. "onsite_only" si mention explicite "présentiel". "any" si le candidat ne spécifie rien et ne donne aucune ville.

7. `constraints.contract_types` : ["CDI"] par défaut pour la France/Suisse si rien de spécifié. Le brief peut dire "stage", "alternance", "freelance", adapte.

8. `constraints.salary_min` / `salary_currency` : extrait SEULEMENT si explicitement dans le brief. Devine la currency depuis la 1ère ville (Suisse=CHF, France=EUR, UK=GBP, US=USD). Si rien, mets null.

9. `scoring_hints.must_have` : mots-clés que l'offre DOIT contenir (filtre dur). Mets-y les compétences centrales du rôle visé (ex. pour un structureur: ["derivatives", "structured products"]). 2-4 entrées max.

10. `scoring_hints.nice_to_have` : boost. Compétences techniques mentionnées dans le CV qui sont un plus (ex. ["python", "bloomberg", "autocallable"]). 5-15 entrées.

11. `scoring_hints.deal_breakers` : extrait OBLIGATOIREMENT du brief (l'utilisateur a répondu à une question dédiée). Ajoute aussi les évidences depuis le CV+brief : si le candidat cherche finance front office, ajoute ["back office", "middle office", "kyc"]. Si il cherche un rôle senior, ajoute ["intern", "stagiaire"]. 5-15 entrées.

12. `active_sources` : toujours ["linkedin", "indeed", "google_jobs"] sauf indication contraire.

13. `raw_brief` : copie VERBATIM le texte du brief de l'utilisateur.

14. Si le CV est en anglais et le candidat cible la France, génère les roles ET les keywords dans les DEUX langues.

Réponds UNIQUEMENT avec le JSON, pas de markdown, pas de commentaire.
"""


def _http_call_groq(api_key: str, payload: dict) -> dict | None:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "job-tracker-app/1.0",
    }
    for attempt in range(4):
        try:
            req = urllib.request.Request(GROQ_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 5 * (2 ** attempt)
                print(f"[config_extractor] 429 rate limit, retry in {wait}s")
                time.sleep(wait)
                continue
            try:
                err_body = e.read().decode()[:300]
            except Exception:
                err_body = ""
            print(f"[config_extractor] HTTP {e.code}: {err_body}")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[config_extractor] error: {e}")
            return None
    return None


def extract_user_config(
    cv_text: str,
    cover_letter_text: str,
    free_brief: str,
    structured_answers: dict[str, Any],
) -> dict[str, Any] | None:
    """Call Groq to build a structured user config.

    Args:
        cv_text: Plain text of the CV (from cv_parser.parse_cv).
        cover_letter_text: Plain text of the cover letter, or "" if none.
        free_brief: Free-form text the user typed about what they want.
        structured_answers: The 5 explicit questions answered on screen 3.
            Shape:
              {
                "locations": [{"city": str, "country": str}, ...],
                "remote": "full|hybrid_ok|onsite_only|any",
                "salary_min": int or None,
                "salary_currency": str,
                "contract_types": [str, ...],
                "availability": str,
                "deal_breakers": [str, ...],
              }

    Returns:
        A dict matching CONFIG_SCHEMA_DOC, or None on failure.
        The structured answers always take precedence over the LLM output for
        the fields they cover — the LLM's job is to infer everything ELSE.
    """
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[config_extractor] Missing GROQ_API_KEY / GEMINI_API_KEY env var")
        return None

    # Truncate inputs to stay under context window.
    cv_slice = (cv_text or "")[:8000]
    cl_slice = (cover_letter_text or "")[:3000]
    brief_slice = (free_brief or "")[:3000]

    user_msg = (
        "=== CV ===\n"
        f"{cv_slice}\n\n"
        "=== COVER LETTER ===\n"
        f"{cl_slice or '(aucune fournie)'}\n\n"
        "=== BRIEF LIBRE DU CANDIDAT ===\n"
        f"{brief_slice or '(aucun brief)'}\n\n"
        "=== RÉPONSES EXPLICITES DU FORMULAIRE ===\n"
        f"{json.dumps(structured_answers, ensure_ascii=False, indent=2)}\n"
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.15,
        "max_tokens": 2200,
        "response_format": {"type": "json_object"},
    }

    body = _http_call_groq(api_key, payload)
    if not body:
        return None

    try:
        text = body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        print(f"[config_extractor] unexpected response shape: {e}")
        return None

    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]

    try:
        config = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[config_extractor] invalid JSON: {e}\n--- raw ---\n{text[:500]}")
        return None

    # --- Override LLM output with explicit user answers where applicable ---
    config.setdefault("constraints", {})
    if structured_answers.get("locations"):
        config["constraints"]["locations"] = [
            {
                "city": loc.get("city", "").strip(),
                "country": loc.get("country", "").strip(),
                "radius_km": int(loc.get("radius_km", 30) or 30),
            }
            for loc in structured_answers["locations"]
            if loc.get("city")
        ]
    if structured_answers.get("remote"):
        config["constraints"]["remote"] = structured_answers["remote"]
    if structured_answers.get("contract_types"):
        config["constraints"]["contract_types"] = list(structured_answers["contract_types"])
    if structured_answers.get("salary_min") is not None:
        config["constraints"]["salary_min"] = structured_answers["salary_min"]
        config["constraints"]["salary_currency"] = structured_answers.get(
            "salary_currency", config["constraints"].get("salary_currency", "EUR")
        )
    if structured_answers.get("availability"):
        config["constraints"]["availability"] = structured_answers["availability"]

    # Merge deal_breakers: LLM proposals + explicit user entries, dedup.
    config.setdefault("scoring_hints", {})
    llm_db = [s.strip() for s in config["scoring_hints"].get("deal_breakers") or []]
    user_db = [s.strip() for s in structured_answers.get("deal_breakers", []) or []]
    seen = set()
    merged: list[str] = []
    for item in user_db + llm_db:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            merged.append(item)
    config["scoring_hints"]["deal_breakers"] = merged

    config["raw_brief"] = free_brief or ""

    return config


def empty_config() -> dict[str, Any]:
    """Return a blank config shell — useful when the LLM call fails so the UI
    still has something to render and the user can fill in manually."""
    return {
        "profile_summary": {
            "current_role": "",
            "years_xp": 0,
            "top_skills": [],
            "languages": [],
            "industries_past": [],
            "education_level": "",
        },
        "target": {
            "roles": [],
            "seniority": [],
            "industries_include": [],
            "industries_exclude": [],
            "company_sizes": [],
            "target_companies": [],
            "avoid_companies": [],
        },
        "constraints": {
            "locations": [],
            "remote": "any",
            "contract_types": ["CDI"],
            "salary_min": None,
            "salary_currency": "EUR",
            "availability": "flexible",
        },
        "scoring_hints": {
            "must_have": [],
            "nice_to_have": [],
            "deal_breakers": [],
        },
        "raw_brief": "",
        "active_sources": ["linkedin", "indeed", "google_jobs"],
    }


if __name__ == "__main__":
    # Smoke test: python -m app.lib.config_extractor <cv.pdf>
    import sys
    from app.lib.cv_parser import parse_cv_from_path

    if len(sys.argv) < 2:
        print("usage: python -m app.lib.config_extractor <cv.pdf> [cover.pdf]")
        sys.exit(1)

    cv = parse_cv_from_path(sys.argv[1])
    cl = parse_cv_from_path(sys.argv[2]) if len(sys.argv) >= 3 else ""
    brief = (
        "Je cherche un poste de structureur / cross-asset en Suisse "
        "(Genève ou Zurich). Pas Paris. Je veux rester junior/mid. "
        "Je n'aime pas le back-office ni les rôles 100% tech."
    )
    answers = {
        "locations": [
            {"city": "Geneva", "country": "Switzerland", "radius_km": 30},
            {"city": "Zurich", "country": "Switzerland", "radius_km": 30},
        ],
        "remote": "hybrid_ok",
        "salary_min": 90000,
        "salary_currency": "CHF",
        "contract_types": ["CDI"],
        "availability": "1-3_months",
        "deal_breakers": ["cold calling", "back office"],
    }
    cfg = extract_user_config(cv, cl, brief, answers)
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
