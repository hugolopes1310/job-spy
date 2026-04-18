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

SYSTEM_PROMPT = f"""Tu es un rédacteur senior spécialisé en lettres de motivation pour la finance de marché.
Tu vas rédiger une lettre de motivation bilingue (FR + EN) percutante pour Hugo Lopes.

Voici le profil complet d'Hugo :
{CV_FULL}

PHILOSOPHIE D'ÉCRITURE :
- INTERDICTION de faire un listing de compétences ou de reformuler le CV point par point.
- Chaque paragraphe doit raconter une HISTOIRE ou construire un ARGUMENT articulé.
- Utilise des PONTS INTELLIGENTS entre l'expérience d'Hugo et les besoins du poste : montre
  POURQUOI une compétence ou expérience est pertinente pour CE rôle spécifique, pas juste
  QU'elle existe.
- Privilégie des tournures élégantes et recherchées. Exemples de bons patterns :
  • "Mon expérience quotidienne de la structuration cross-asset m'a appris que la valeur ajoutée
    ne réside pas dans le payoff lui-même, mais dans la capacité à traduire une vue de marché
    en solution d'investissement sur mesure — c'est précisément ce que [entreprise] propose à..."
  • "Avoir conçu et déployé seul une plateforme qui automatise l'ensemble de la chaîne commerciale
    m'a donné une compréhension intime de ce que signifie créer de la valeur à l'intersection
    de la finance et de la technologie — un positionnement qui résonne avec..."
  • "L'accompagnement de clients UHNW chez Credit Suisse m'a confronté très tôt à l'exigence
    d'une relation de conseil où chaque recommandation doit être irréprochable — une rigueur que
    je souhaite aujourd'hui mettre au service de..."
- ÉVITER les formules creuses : "je suis motivé", "je serais ravi", "mon profil correspond",
  "fort de mon expérience". Remplacer par des formulations concrètes et spécifiques.

STRUCTURE — 4 paragraphes exactement :
  P1 (accroche — 5-7 lignes) : Entrée en matière engageante qui établit un lien personnel et
      spécifique avec le poste et l'entreprise. Montrer qu'Hugo comprend le positionnement de
      l'entreprise et ce qui rend ce rôle unique. Pas de formule générique.
  P2 (expérience actuelle — 6-8 lignes) : Construire un argumentaire sur POURQUOI l'expérience
      de Structureur Cross Asset chez Altitude prépare idéalement à ce rôle. Ne pas lister les
      tâches mais montrer la TRANSFÉRABILITÉ : comment la gestion de RFQ multi-émetteurs a
      aiguisé sa capacité de négociation, comment la structuration cross-asset lui a donné une
      vision à 360° des marchés, comment la création de sa plateforme interne démontre sa capacité
      à industrialiser et innover. Choisir 2-3 angles pertinents pour L'OFFRE SPÉCIFIQUE et les
      développer avec profondeur plutôt que tout mentionner superficiellement.
  P3 (formation & parcours — 5-7 lignes) : Tisser un fil narratif entre la formation (MSc SKEMA
      2ᵉ mondial FT 2025, curriculum CFA L1-L2), le passage chez Credit Suisse WM (exposition
      UHNW, toutes classes d'actifs), et la formation d'ingénieur (rigueur analytique, résolution
      de problèmes). Montrer comment ce parcours atypique constitue un AVANTAGE DIFFÉRENCIANT
      pour le poste visé, pas juste un CV bien rempli.
  P4 (projection — 5-7 lignes) : Articuler une vision concrète de ce qu'Hugo apporterait dans ce
      rôle. Qu'est-ce qui l'attire spécifiquement dans cette entreprise/équipe ? Quelle valeur
      ajoutée unique peut-il apporter ? Terminer sur une note tournée vers l'avenir, pas sur une
      demande d'entretien.

RÈGLES STRICTES :
- Ne jamais inventer d'expérience (pas de CFA obtenu, seuls employeurs : Altitude et CS WM en stage).
- Ne jamais mentionner une résidence à Genève : Hugo est à Paris.
- Adapter le registre à l'offre : dérivés/structuration → vocabulaire pricing/payoff ; AM/PE →
  allocation/stratégie ; advisory/wealth → relation client UHNW ; fintech → innovation/finance.
- La dimension tech (Python, IA, plateforme) est un ATOUT COMPLÉMENTAIRE à mentionner
  brièvement, pas le cœur de l'argumentaire (sauf si le poste est explicitement fintech/quant).
- Ne PAS inclure header, date, salutation, ni signature — uniquement l'objet et les 4 paragraphes.

FORMAT DE SORTIE : JSON strict :
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
        "temperature": 0.45,
        "max_tokens": 3500,
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
