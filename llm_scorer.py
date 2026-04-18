"""Structured offer analysis via Groq (Llama 3.3 70B).

Returns a rich dict combining:
  - overall fit score (0-10)
  - structured sub-scores (technique, geo, seniorite)
  - red flags and atouts (useful for interview prep)
  - extracted facts (salary, stack, contact, deadline, ATS keywords, apply hint)

Backward-compat: `score_with_llm(title, company, location, description) -> (score, reason)`
is kept as a thin wrapper over `analyze_offer()`.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CV_SUMMARY = """
Hugo Lopes — Structureur Cross Asset Solutions chez Altitude Investment Solutions (Paris, CDI depuis janvier 2024).
Expériences : Altitude (actuel) structureur cross-asset full spectrum (Autocall, Callable, CLN, Reverse Convertible,
Twin-Win, payoffs de taux), pricing/RFQ auprès de 20+ banques, lifecycle, secondaire, plateforme interne full-stack
+ GenAI conçue seul (brochures -90%, onboarding -90%, billing -90%, basket opt +50%, chatbot RAG).
Avant : Credit Suisse Wealth Management Paris (stage, Investment Advisor UHNW).
Formation : MSc SKEMA FMI (2ᵉ mondial FT 2025, CFA L1+L2 coverage), Ingénieur Polytech Clermont-Ferrand Génie Civil.
Compétences : Python, VBA, Bloomberg, GenAI/LLM, RAG, Nginx, OVH, Cloudflare.
Langues : FR natif, EN C1 (TOEIC 965), PT+ES B2.
Recherche : structurés/cross-asset Genève-Zurich, AM/PE Lyon, fintech Lyon. Pas Paris.
"""

SYSTEM_PROMPT = f"""Tu es un recruteur senior en finance de marché qui évalue une offre pour ce candidat :

{CV_SUMMARY}

Tu dois produire une analyse structurée EN FRANÇAIS, au format JSON strict :

{{
  "score": <int 0-10>,                         // score global de fit
  "reason": "<1 phrase FR synthèse>",
  "match_finance": <int 0-10>,                 // match métier finance (produits structurés, dérivés, AM, PE, advisory, wealth, sales...)
  "match_geo": <int 0-10>,                     // Genève/Zurich/Lyon = 10 ; Paris/autre = bas ; remote = neutre
  "match_seniorite": <int 0-10>,               // 2 ans d'XP → 10 si junior-mid, 0 si senior/head of/MD
  "red_flags": ["<3 max, très courts>"],       // ex: "Rôle senior 10+ ans", "100% back-office"
  "atouts": ["<3 max, très courts>"],          // atouts à mettre en avant en entretien
  "salary": "<string ou null>",                // ex: "€80-110k" ou null si absent
  "contact": "<string ou null>",               // ex: "John Smith, Head of Structuring" ou null
  "deadline": "<YYYY-MM-DD ou string ou null>",
  "apply_hint": "<string courte>"              // ex: "Apply via LinkedIn", "Via site carrière", "Easy Apply"
}}

IMPORTANT — PONDÉRATION DES CRITÈRES :
Le score global doit refléter avant tout le FIT MÉTIER FINANCE, la GÉOGRAPHIE et la SÉNIORITÉ.
Les compétences tech (Python, VBA, IA, LLM, etc.) sont un BONUS qui peut ajouter +1 au score,
mais ne doivent JAMAIS être le critère principal. Un poste 100% tech/développeur sans dimension
finance doit scorer 0-2 même s'il mentionne Python ou IA.

Le candidat cherche un poste EN FINANCE (structuration, sales, advisory, AM, PE, private debt,
banque privée, wealth management, fintech à dimension finance). Son profil tech est un plus,
pas sa spécialité.

Barème score global :
- 9-10 : match parfait (structureur dérivés Genève/Zurich, cross-asset solutions Suisse, SP sales)
- 7-8 : très pertinent (advisory/investment solutions, AM, PE mid-cap Lyon, banque privée Lyon/Suisse, fintech Lyon avec dimension finance)
- 5-6 : intéressant mais un axe faible (geo ou seniorité ou produit décalé)
- 3-4 : lien ténu (rôle finance mais mauvaise géo, ou bonne géo mais rôle éloigné)
- 0-2 : middle/back office, tech pur sans finance, rôle fiscal/juridique, assurance, courtier, développeur logiciel

Les listes peuvent être vides ([]) si rien de notable. Les champs null si info absente. Ne rien inventer.
"""


def _build_feedback_context(limit_each: int = 5) -> str:
    """Fetch recent user feedback from the DB and format as few-shot context.

    Returns a string to append to the system prompt. Fails soft — if the DB is
    not reachable or no feedback exists yet, returns an empty string.
    """
    try:
        from db import DB_PATH, connect, fetch_recent_feedback_examples
    except Exception:
        return ""
    try:
        with connect(DB_PATH) as c:
            good = fetch_recent_feedback_examples(c, "good", limit_each)
            applied = fetch_recent_feedback_examples(c, "applied", limit_each)
            bad = fetch_recent_feedback_examples(c, "bad", limit_each)
    except Exception:
        return ""
    if not (good or applied or bad):
        return ""
    parts = ["", "Retour utilisateur sur les offres passées — utilise ces patterns :"]
    for row in applied:
        parts.append(f"  ✅ APPLIED : « {row['title']} » @ {row['company']} ({row['location']}) — axe={row['axe']}")
    for row in good:
        parts.append(f"  👍 GOOD : « {row['title']} » @ {row['company']} ({row['location']}) — axe={row['axe']}")
    for row in bad:
        parts.append(f"  👎 BAD (à ne PAS recommander) : « {row['title']} » @ {row['company']} ({row['location']}) — axe={row['axe']}")
    parts.append(
        "\nCES RETOURS SONT CRITIQUES — ils reflètent les préférences RÉELLES du candidat :"
        "\n- Si l'offre ressemble à un pattern BAD (titre/secteur/type de rôle proches), "
        "baisse le score d'au moins 3 points."
        "\n- Si elle ressemble à un pattern GOOD/APPLIED (même type de rôle, même secteur, "
        "même type d'entreprise), monte le score d'au moins 2 points."
        "\n- Priorise ces signaux par-dessus le barème théorique : le candidat sait mieux "
        "que toi ce qui l'intéresse."
    )
    return "\n".join(parts)


def analyze_offer(title: str, company: str, location: str, description: str) -> dict | None:
    """Call Groq and return the rich analysis dict, or None on failure."""
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None

    user_msg = (
        f"Titre : {title}\n"
        f"Entreprise : {company}\n"
        f"Lieu : {location}\n"
        f"Description :\n{description[:2500]}"
    )
    feedback_context = _build_feedback_context()
    system = SYSTEM_PROMPT + (feedback_context or "")
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.15,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "job-tracker/1.0",
    }

    for attempt in range(4):
        try:
            req = urllib.request.Request(GROQ_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode())
            text = body["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)
            # Ensure required keys + types
            result.setdefault("score", 0)
            result.setdefault("reason", "")
            for k in ("match_finance", "match_geo", "match_seniorite"):
                result.setdefault(k, -1)
            for k in ("red_flags", "atouts"):
                v = result.get(k)
                if not isinstance(v, list):
                    result[k] = []
            for k in ("salary", "contact", "deadline", "apply_hint"):
                result.setdefault(k, None)
            return result
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 5 * (2 ** attempt)
                print(f"[llm] 429 rate limit, retry in {wait}s")
                time.sleep(wait)
                continue
            try:
                err_body = e.read().decode()[:200]
            except Exception:
                err_body = ""
            print(f"[llm] HTTP {e.code}: {err_body}")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[llm] error: {e}")
            return None
    return None


def score_with_llm(title: str, company: str, location: str, description: str):
    """Backward-compat wrapper. Returns (score, reason), -1 on failure."""
    result = analyze_offer(title, company, location, description)
    if result is None:
        return -1, "LLM error"
    return int(result.get("score", 0)), str(result.get("reason", ""))
