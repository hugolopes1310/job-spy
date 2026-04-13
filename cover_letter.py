"""Cover letter generation using Groq (Llama 3.3 70B).

Given an offer, produces bilingual (FR + EN) cover letter bodies adapted to
the role. Structure mirrors Hugo's Laplace template:
  - Objet / Subject line
  - Salutation
  - 4 body paragraphs
  - Closing handled statically by the docx writer

Returns a dict with fr_subject, fr_paragraphs[], en_subject, en_paragraphs[].
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CV_FULL = """
Hugo LOPES — Structureur Cross Asset Solutions chez Altitude Investment Solutions (Genève, depuis 2 ans).

Formation :
- MSc Finance de Marchés et Investissements, SKEMA Business School (FT #1 Master in Finance 2025), programme aligné CFA.
- Diplôme d'ingénieur Polytech Nice Sophia — Mathématiques Appliquées & Modélisation.

Expertise produit :
- Structuration de produits d'investissement : Autocall, Phoenix, Reverse Convertible, CLN, Callable,
  At-Risk Participation, Twin-Win, produits de taux.
- Pricing multi-émetteurs, RFQ, brochures commerciales, outils internes Python/VBA.
- Clientèle institutionnelle et distribution.

Réalisations techniques (Projet K2) :
- Full-stack Python, GenAI/LLM, REST API, chatbot, RAG, Nginx, OVH Cloud, Cloudflare.
- Automatisation brochures (-90% temps), onboarding (-90%), billing (-90%), optimisation paniers (+50%).

Compétences transverses : analyse besoins client (perf/risque/horizon), pédagogie, vulgarisation,
rigueur quantitative, gestion multi-projets, delivery sous contrainte.

Langues : français natif, anglais courant, portugais natif.

Positionnement de recherche : structurés / cross-asset Genève-Zurich, AM / PE Lyon, fintech Lyon.
"""

SYSTEM_PROMPT = f"""Tu es un expert en rédaction de lettres de motivation bilingues pour la finance.
Tu vas rédiger une lettre de motivation personnalisée pour Hugo Lopes, candidat à une offre précise.

Voici le profil complet d'Hugo :
{CV_FULL}

STYLE et STRUCTURE à respecter (inspirés d'une lettre existante utilisée pour Laplace) :
- Ton professionnel, confiant mais pas arrogant, orienté solution-client.
- Vouvoiement en français, "Dear Sir or Madam" en anglais.
- 4 paragraphes exactement :
  P1 (accroche) : "La curiosité et la motivation ont toujours guidé mon parcours..." + lien spécifique
      avec l'offre (rôle, entreprise, ce qui attire Hugo dans ce poste précis).
  P2 (expérience actuelle) : décrire comment son poste de Structureur Cross Asset chez Altitude est
      transférable aux exigences de cette offre (mentionner 2-3 éléments concrets : analyse besoin,
      outils Python, travail avec Sales, produits spécifiques pertinents pour l'offre).
  P3 (formation) : MSc SKEMA (aligné CFA) + diplôme d'ingénieur, en reliant aux compétences
      demandées dans l'offre (quanti, rigueur, vulgarisation, résolution de problèmes).
  P4 (projection) : ce qui attire Hugo précisément dans ce poste / cette entreprise, et la valeur
      qu'il compte apporter. Éviter les formules génériques.

RÈGLES :
- Ne jamais inventer d'expérience que Hugo n'a pas (pas de CFA obtenu, pas d'autre employeur).
- Adapter le champ lexical à l'offre : si c'est un rôle en AM/PE, parler allocation/stratégie ; si
  c'est dérivés/structuration, rester sur le vocabulaire pricing/payoff ; si c'est advisory/wealth,
  parler relation client/bilans.
- Paragraphes de 4 à 7 lignes maximum chacun.
- Ne PAS inclure le header sender, la date, la salutation, ni la signature — uniquement l'objet
  et les 4 paragraphes du corps.

FORMAT DE SORTIE : JSON strict avec cette structure exacte :
{{
  "fr_subject": "Candidature au poste de <titre adapté>",
  "fr_paragraphs": ["...P1 FR...", "...P2 FR...", "...P3 FR...", "...P4 FR..."],
  "en_subject": "Application for the position of <adapted title>",
  "en_paragraphs": ["...P1 EN...", "...P2 EN...", "...P3 EN...", "...P4 EN..."]
}}
"""


def generate_cover_letter(
    title: str, company: str, location: str, description: str
) -> dict | None:
    """Call Groq to generate bilingual CL bodies. Returns dict or None on failure."""
    api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[cover_letter] GROQ_API_KEY not set, skipping")
        return None

    user_msg = (
        f"Offre d'emploi à analyser :\n"
        f"Titre : {title}\n"
        f"Entreprise : {company}\n"
        f"Lieu : {location}\n"
        f"Description :\n{description[:3000]}\n\n"
        f"Génère la lettre de motivation bilingue au format JSON demandé."
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.4,
        "max_tokens": 2500,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "job-tracker/1.0",
    }

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(GROQ_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.loads(resp.read().decode())
            text = body["choices"][0]["message"]["content"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            result = json.loads(text)

            # Sanity check
            required = ["fr_subject", "fr_paragraphs", "en_subject", "en_paragraphs"]
            if not all(k in result for k in required):
                print(f"[cover_letter] Missing keys in response: {list(result.keys())}")
                return None
            if len(result["fr_paragraphs"]) != 4 or len(result["en_paragraphs"]) != 4:
                print(
                    f"[cover_letter] Expected 4 paragraphs, got "
                    f"FR={len(result['fr_paragraphs'])} EN={len(result['en_paragraphs'])}"
                )
                # Tolerate, don't fail
            return result
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                wait = 5 * (2 ** attempt)
                print(f"[cover_letter] 429, retrying in {wait}s")
                time.sleep(wait)
                continue
            try:
                err_body = e.read().decode()[:300]
            except Exception:
                err_body = ""
            print(f"[cover_letter] HTTP {e.code}: {err_body}")
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[cover_letter] Error: {e}")
            return None
    return None
