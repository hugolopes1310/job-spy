"""Second-pass LLM scoring using Gemini Flash (free tier).

Called only on offers that already passed the keyword threshold.
Returns a fit score (0-10) and a short explanation in French.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

CV_SUMMARY = """
Hugo Lopes — Structureur chez Altitude Investment Solutions (Genève).
- MSc Finance SKEMA Business School (FT #1 Master in Finance 2025)
- MSc Ingénieur Polytech Nice Sophia (Mathématiques Appliquées & Modélisation)
- Expérience : structuration produits (Autocall, Phoenix, Reverse Convertible, CLN,
  Callable, At-Risk Participation, Twin-Win, produits de taux), pricing multi-émetteurs,
  RFQ, brochures commerciales, outils internes Python/VBA.
- Projet K2 : déploiement full-stack (Python, GenAI/LLM, REST API, chatbot, RAG,
  Nginx, OVH Cloud, Cloudflare). Automatisation brochures (-90%), onboarding (-90%),
  billing (-90%), optimisation paniers (+50%).
- Recherche : structurés/cross-asset Genève-Zurich, AM/PE Lyon, fintech Lyon.
- Langues : français natif, anglais courant, portugais natif.
"""

SYSTEM_PROMPT = f"""Tu es un recruteur spécialisé en finance de marché.
Tu évalues la pertinence d'une offre d'emploi pour ce candidat :

{CV_SUMMARY}

Réponds UNIQUEMENT en JSON valide avec cette structure :
{{"score": <int 0-10>, "reason": "<1 phrase en français expliquant le score>"}}

Critères de scoring :
- 9-10 : match parfait (structureur, cross-asset sales, equity derivatives à Genève/Zurich)
- 7-8 : très pertinent (advisory, investment solutions, AM obligataire, PE mid-cap Lyon)
- 5-6 : intéressant mais pas idéal (fintech finance, product manager finance)
- 3-4 : lien indirect (tech pure avec composante finance, middle office évolué)
- 0-2 : pas pertinent (middle office pur, ops, compliance, dev sans finance)
"""


def score_with_llm(title: str, company: str, location: str, description: str) -> tuple[int, str]:
    """Call Gemini Flash to get a fit score + reason.

    Returns (score, reason). On failure returns (-1, error_message).
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return -1, "GEMINI_API_KEY not set"

    user_msg = (
        f"Titre : {title}\n"
        f"Entreprise : {company}\n"
        f"Lieu : {location}\n"
        f"Description :\n{description[:2000]}"  # cap to save tokens
    )

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": SYSTEM_PROMPT + "\n\n" + user_msg}]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 150,
        },
    }

    url = f"{GEMINI_URL}?key={api_key}"
    data = json.dumps(payload).encode("utf-8")

    # Retry with exponential backoff on rate limit (429) / transient errors
    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode())
            text = body["candidates"][0]["content"]["parts"][0]["text"]
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]
            result = json.loads(text.strip())
            return int(result["score"]), result.get("reason", "")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                wait = 10 * (2 ** attempt)  # 10s, 20s, 40s
                print(f"[llm] 429 rate limit, retrying in {wait}s (attempt {attempt+1}/{max_attempts})")
                time.sleep(wait)
                continue
            return -1, f"LLM error: HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return -1, f"LLM error: {e}"
    return -1, "LLM error: rate limit"