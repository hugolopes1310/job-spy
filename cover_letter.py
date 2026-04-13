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
Hugo LOPES — Structureur Cross Asset Solutions chez Altitude Investment Solutions
(Paris, CDI depuis janvier 2024).

Expérience professionnelle :
- Altitude Investment Solutions, Paris — Structureur Cross Asset Solutions (janv. 2024 – aujourd'hui).
  • Structuration & idées d'investissement cross-asset (actions, taux, crédit, matières premières, FX) :
    conception de payoffs sur mesure (Autocallable, Callable, At-Risk Participation, Reverse Convertible,
    Credit Linked Note, Digital, Twin-Win, payoffs de taux) alignés avec les objectifs et contraintes clients.
  • Veille marché & innovation produit : suivi cross-asset, activité des pairs, évolutions réglementaires,
    anticipation des besoins clients.
  • Pricing & RFQ multi-émetteurs : gestion et comparaison de cotations auprès de plus de 20 banques
    d'investissement ; arbitrage volatilité, marges, sensibilités, conditions juridiques.
  • Exécution, négociation & lifecycle : ordres après validation client, termsheets, DIC, coordination
    émetteurs pour règlement et documentation.
  • Marché secondaire : prix BID/ASK, rolls, rachats.
  • Développement quant & intégration IA : pilotage d'un projet stratégique interne (plateforme
    full-stack + GenAI) pour industrialiser les workflows de structuration, pricing et commerciaux.

- Credit Suisse Wealth Management, Paris — Conseiller en Investissement, stage de fin d'études
  (juin 2023 – déc. 2023).
  • Accompagnement des conseillers auprès de clients UHNW sur toutes classes d'actifs (obligations,
    actions, matières premières, private equity, crédit) ; allocations de portefeuille personnalisées.
  • Analyses fondamentales, développement Python pour backtests et automatisation front-office
    (notamment reporting de portefeuilles clients).

Projet stratégique — Plateforme interne Quant & IA (Altitude, 2024 – aujourd'hui, développeur unique) :
- Architecture full-stack sur OVH Cloud (Ubuntu VPS + stockage), Nginx reverse proxy, Cloudflare, 2FA,
  déployée pour toute l'équipe structuration et commerciale.
- Génération de brochures commerciales par pipeline LLM (-90% de temps).
- Automatisation onboarding client via API Pappers (-90% de délai).
- Automatisation facturation des trades (-90% de temps).
- Assistant IA commercial : chatbot RAG + module call-intelligence transformant les enregistrements en
  tâches CRM.
- Moteur d'optimisation de paniers worst-of (+50% de productivité sur la génération d'idées),
  scoring IA de fidélisation client.

Formation :
- MSc Financial Markets & Investments, SKEMA Business School (Raleigh NC, sep. 2022 – juin 2023).
  2ᵉ Master en Finance au monde — FT Global Masters in Finance Ranking 2025. Cours couvrant les
  programmes CFA niveaux I et II : produits dérivés (options, futures, forwards, swaps), grecques,
  analyse financière, Python & VBA pour la finance (what-if, backtests, call-on-a-call, smile de vol).
- Diplôme d'Ingénieur, Polytech Clermont-Ferrand — Génie Civil (sep. 2017 – juin 2022).

Compétences :
- Outils : Python & VBA (pro complète), Excel & PowerPoint (pro complète), Bloomberg (pro complète).
- Stack tech : Python, PyQt5, Jinja2/HTML, REST API ; GenAI/LLM APIs, RAG, chatbot ; Linux (Ubuntu),
  Nginx, OVH Cloud, Cloudflare, 2FA.
- Langues : français natif ; anglais C1 courant (TOEIC 965/990) ; portugais et espagnol B2.
- Centres d'intérêt : trail (UTMB Index 480), natation, ski ; ancien water-polo niveau National (N3).

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
  P2 (expérience actuelle) : décrire comment son poste de Structureur Cross Asset chez Altitude
      (Paris, depuis janvier 2024) est transférable aux exigences de l'offre. Piocher 2-3 éléments
      concrets parmi : structuration cross-asset (Autocall, Callable, CLN, Reverse Convertible,
      Twin-Win, payoffs de taux), pricing / RFQ multi-émetteurs (20+ banques), exécution et
      lifecycle (termsheets, DIC), marché secondaire, plateforme interne full-stack + GenAI qu'il
      a conçue et déployée seul (brochures -90%, onboarding -90%, billing -90%, optimisation
      paniers +50%, chatbot RAG).
  P3 (formation) : MSc Financial Markets & Investments SKEMA (2ᵉ au monde — FT 2025, programme
      couvrant CFA L1 & L2, dérivés, Python/VBA pour la finance) + expérience antérieure chez
      Credit Suisse Wealth Management (UHNW, toutes classes d'actifs) + diplôme d'ingénieur
      Polytech Clermont-Ferrand (Génie Civil) qui a forgé sa rigueur et sa capacité de résolution
      de problèmes. Relier aux compétences demandées dans l'offre.
  P4 (projection) : ce qui attire Hugo précisément dans ce poste / cette entreprise, et la valeur
      qu'il compte apporter. Éviter les formules génériques.

RÈGLES STRICTES :
- Ne jamais inventer d'expérience que Hugo n'a pas (pas de CFA obtenu, seuls employeurs :
  Altitude Investment Solutions et Credit Suisse Wealth Management en stage).
- Ne jamais mentionner une mobilité ou résidence actuelle à Genève : Hugo est à Paris.
- Adapter le champ lexical à l'offre : si c'est un rôle en AM/PE, parler allocation/stratégie ; si
  c'est dérivés/structuration, rester sur le vocabulaire pricing/payoff ; si c'est advisory/wealth,
  parler relation client / allocation UHNW / bilans patrimoniaux.
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
