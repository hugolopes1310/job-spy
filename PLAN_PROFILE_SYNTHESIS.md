# Plan — Profile Synthesis Loop v2

**Statut :** draft, en attente de validation Hugo avant implémentation.
**Effort estimé :** ~4.5 jours homme, livrable en 6 PRs.
**Décisions cadres déjà prises :** option "full", reset = synthesis-only (matches kept), plan-first.

---

## TL;DR

On ajoute un objet **`profile_synthesis`** versionné, produit par LLM à partir du CV + préférences + feedback, qui devient la source de vérité pour piloter le scraper (search_terms exploded depuis `role_families`) et le scorer (boost dream_co, cap deal_breakers). Une nouvelle page Streamlit "Mon Profil" affiche la synthèse, permet édition par cards, expose les `open_questions` du LLM, et inclut un reset doux qui archive la synthèse sans toucher au kanban. Un job nightly observe le feedback (status changes, rejects) et propose des diffs au profil que l'user accepte/refuse.

---

## 0. Réalité du repo (ce sur quoi on construit)

Cartographie validée :

- `user_configs` (table existante) contient déjà `config` JSONB + `cv_text`. **On garde** : c'est l'input. La synthèse est une couche de plus, pas un remplaçant.
- `query_builder.py:83-100` consomme `target.roles` brut → c'est ce point qu'on remplace par `role_families`.
- `scorer.py` consomme déjà `user_config` → on lui passe `synthesis` en plus, sans casser le contrat existant.
- `user_job_matches` n'a pas de `profile_version` → on en ajoute un (FK).
- Pages Streamlit : `1_onboarding`, `2_dashboard`, `3_suivi`, `99_admin` → on ajoute `4_mon_profil`.
- Tests : pytest dans `app/lib/test_*.py` → on suit le pattern.

---

## 1. Schéma de données

### 1.1 Nouvelle table `profile_syntheses`

```sql
CREATE TABLE public.profile_syntheses (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
  version INT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','active','archived')) DEFAULT 'draft',
  synthesis JSONB NOT NULL,
  source_signals JSONB,            -- {cv_text_hash, config_hash, feedback_window_days, signal_count}
  llm_model TEXT,                  -- 'gemini-2.5-flash' | 'groq-llama-3.3-70b'
  prompt_version TEXT,             -- 'v1.0' pour audit
  created_at TIMESTAMPTZ DEFAULT NOW(),
  activated_at TIMESTAMPTZ,
  archived_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX one_active_synthesis_per_user
  ON profile_syntheses (user_id) WHERE status = 'active';

CREATE INDEX idx_synthesis_user_status ON profile_syntheses (user_id, status);
```

Versioning à 3 états (`draft` → `active` → `archived`) parce que la création d'une synthèse via LLM peut échouer. On insère en `draft`, on flip en `active` seulement si le LLM call réussit.

### 1.2 Modification `user_job_matches`

```sql
ALTER TABLE user_job_matches
  ADD COLUMN profile_synthesis_id UUID REFERENCES profile_syntheses(id);
CREATE INDEX idx_match_synthesis ON user_job_matches (profile_synthesis_id);
```

Trace par quel profil la match a été scorée. Permet plus tard "re-scorer matches scorées avec un profil obsolète".

### 1.3 Nouvelle table `profile_synthesis_proposals` (loop continu)

```sql
CREATE TABLE public.profile_synthesis_proposals (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
  current_synthesis_id UUID REFERENCES profile_syntheses(id),
  diff JSONB NOT NULL,             -- {add_deal_breakers, remove_role_families, ...}
  rationale_fr TEXT,               -- "5 rejects sur 'consulting' cette semaine"
  status TEXT NOT NULL CHECK (status IN ('pending','accepted','dismissed')) DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);

CREATE INDEX idx_proposal_user_pending
  ON profile_synthesis_proposals (user_id) WHERE status = 'pending';
```

---

## 2. Schéma de l'objet `synthesis` (JSON)

```json
{
  "version": 3,
  "summary_fr": "Mid-senior pharma, ~7 ans en clinical research. Ouverte CRA / Reg Affairs / Pharmacovigilance. Genève prio, Bâle/Lausanne ok, full-remote CH ok. Pas de sales, pas de junior.",
  "role_families": [
    {
      "label": "Clinical Research",
      "titles": ["CRA", "Senior CRA", "Clinical Project Manager", "Clinical Trial Manager", "Attaché de recherche clinique"],
      "weight": 0.9,
      "active": true,
      "source": {"type": "cv", "evidence": "5 ans en CRO Quintiles"}
    },
    {
      "label": "Regulatory Affairs",
      "titles": ["Regulatory Affairs Specialist", "RA Manager", "RA Associate", "Affaires réglementaires"],
      "weight": 0.7,
      "active": true,
      "source": {"type": "inferred", "evidence": "industrie pharma + niveau senior"}
    }
  ],
  "seniority_band": {"label": "mid-senior", "yoe_min": 5, "yoe_max": 12},
  "geo": {
    "primary": ["Geneva, Switzerland"],
    "acceptable": ["Basel", "Lausanne", "Zurich", "remote-CH"],
    "exclude": ["United States", "United Kingdom"]
  },
  "deal_breakers": ["sales", "intern", "junior", "consulting"],
  "dream_companies": ["Roche", "Novartis", "Lonza", "Ferring"],
  "languages": ["FR-native", "EN-C1"],
  "confidence": 0.72,
  "open_questions": [
    {"id": "q_contract", "text": "CDD 12+ mois acceptés ou CDI uniquement ?", "answer": null},
    {"id": "q_field", "text": "OK pour 50% terrain (visites sites cliniques) ?", "answer": null}
  ]
}
```

Conventions :
- `source.type ∈ {cv, stated, inferred, feedback}` → audit trail anti-hallucination.
- `weight ∈ [0,1]` : multiplicateur appliqué côté scraper (queries faibles weight = moins de results_per_query) et scorer (boost match_role).
- `active: false` permet de désactiver une famille sans la supprimer (history préservée).

---

## 3. Module `app/lib/profile_synthesizer.py`

```python
def synthesize_profile(
    cv_text: str,
    user_config: dict,
    feedback_signals: list[dict] | None = None,
    previous_synthesis: dict | None = None,
    open_question_answers: dict[str, str] | None = None,
) -> dict:
    """LLM call → structured profile object. Returns synthesis JSON.
    Raises ProfileSynthesisError if both LLM providers fail JSON-strict mode."""

def propose_diff(
    current_synthesis: dict,
    feedback_signals: list[dict],
) -> dict | None:
    """LLM call → {diff, rationale_fr}. Returns None if no significant signal."""

def apply_diff(synthesis: dict, diff: dict) -> dict:
    """Pure function : applique un diff accepté à la synthèse, retourne nouvelle synthèse."""
```

### Stratégie LLM
- **Primaire :** Gemini 2.5 Flash, `response_mime_type="application/json"` strict, `seed=42`.
- **Fallback :** Groq llama-3.3-70b, même prompt + suffixe "RAPPEL: JSON strict".
- **Pas d'heuristique de fallback ici** : si les deux échouent, on garde `previous_synthesis` (ou throw si aucune n'existe pour un nouvel user). Le user retry manuellement. Différent du scorer parce qu'une mauvaise synthèse contamine TOUT le pipeline downstream.

### Outline du prompt
- ROLE : coach carrière qui synthétise un profil pour piloter une recherche d'emploi.
- INPUT : CV + config user + (opt) feedback signals + (opt) previous_synthesis + (opt) answers aux open_questions.
- TASK : produire le JSON ci-dessus, strict.
- RULES :
  - 3-5 role_families max, chacune avec **≥4 titres concrets** (synonymes, niveaux, FR+EN si CV bilingue).
  - Si CV mentionne pharma → DOIT inclure au minimum les titres CRA, Reg Affairs, Pharmacovigilance, MSL, Medical Affairs (idem pour autres secteurs : finance, tech, etc. → taxonomie hardcodée dans le prompt).
  - Open_questions sur **toute ambiguïté détectée** : type de contrat, terrain vs office, langue, taille d'entreprise, déplacements. Cap à 5.
  - `source` obligatoire sur chaque inférence.
  - `confidence` bas si le CV est court ou si beaucoup d'open_questions.
- OUTPUT : JSON strict, single root key `synthesis`.

---

## 4. Page Streamlit `app/pages/1_mon_profil.py` (remplace `1_onboarding.py`)

### 4.1 Structure visuelle (top → bottom)

```
┌─────────────────────────────────────────────────────────┐
│ Mon profil                              [Reset profil] │
├─────────────────────────────────────────────────────────┤
│ Synthèse                                                │
│ "Mid-senior pharma, 7 ans en clinical..."              │
│ Confiance 72% • v3 • mis à jour il y a 2j              │
├─────────────────────────────────────────────────────────┤
│ ⚠ 3 questions pour affiner ton profil   [développer]   │
├─────────────────────────────────────────────────────────┤
│ ▼ Rôles ciblés                                          │
│   ☑ Clinical Research   [CRA] [Senior CRA] [+ ajouter] │
│   ☑ Regulatory Affairs  [RA Specialist] [...] [+]      │
│   [+ Ajouter une famille de rôle]                      │
├─────────────────────────────────────────────────────────┤
│ ▼ Géographie                                            │
│   Primary: [Geneva]                                     │
│   Acceptable: [Basel] [Lausanne] [remote-CH]           │
│   Exclude: [US] [UK]                                   │
├─────────────────────────────────────────────────────────┤
│ ▼ Entreprises cibles    [Roche] [Novartis] [Lonza]    │
├─────────────────────────────────────────────────────────┤
│ ▼ Deal-breakers         [sales] [intern] [junior]     │
├─────────────────────────────────────────────────────────┤
│ Sticky bottom:                                          │
│ [Lancer une recherche avec ce profil]                  │
│ Clinical Research: 23 jobs • RA: 8 • PV: 0            │
└─────────────────────────────────────────────────────────┘
```

### 4.2 Composants Streamlit

- `st.subheader` + `st.caption` pour le header
- `st.info` orange pour banner open_questions, expandable avec form pour répondre
- `st.expander` par bloc édition
- Tags : `st.multiselect` natif (décision §12).
- Sticky bottom action bar : container avec CSS injection (`st.markdown` + style position:fixed)
- Modal de confirmation reset : `st.dialog` (Streamlit ≥1.32)

### 4.3 Flow d'édition (pas de reset)

User édite une card → bouton "Sauvegarder ces changements" sous la card.
Backend :
1. Charge synthesis active courante (deep copy)
2. Applique le delta
3. INSERT nouvelle row `profile_syntheses` status=`active`, version=N+1
4. UPDATE ancienne row status=`archived`, archived_at=NOW()
5. (Race-safe via transaction + unique index)
6. Refresh page

### 4.4 Flow Reset (synthesis-only)

Bouton flottant top-right "Reset profil" → modal confirmation :

> "Cette action archive ton profil actuel et lance une nouvelle synthèse depuis ton CV. Ton historique de candidatures (kanban Suivi) reste intact."
>
> [Reset depuis mon CV existant]   [Reset + uploader un nouveau CV]   [Annuler]

Étapes :
1. UPDATE current synthesis status=`archived`
2. (si nouveau CV) save_user_config update cv_text
3. INSERT new synthesis row status=`draft`
4. Call `synthesize_profile()` → flip status=`active` si OK, sinon rollback ancienne en active + toast erreur
5. Redirect Mon Profil

**Important :** une édition de card ≠ reset. Le reset sert au cas où l'user change de carrière ou veut repartir de zéro. Édition = upsert version+1.

### 4.5 Action bar : "Lancer une recherche"

Trigger un scraper run async (existing pattern `app/scraper/run.py`) en passant la synthesis active.
Pendant le run : progress bar + counts par role_family qui s'incrémentent en temps réel (lecture polling de `scraper_runs` table si existante, sinon log streaming).
Une fois fini : lien vers Dashboard avec filtre `synthesis_id=<current>`.

### 4.6 Banner diff proposal

Au load : SELECT `profile_synthesis_proposals` WHERE user_id=X AND status='pending'.
Si présente : banner bleu top de page :

> 💡 **Suggestion** : ajouter "consulting" à tes deal-breakers ? Raison : 5 rejects sur des offres de consulting cette semaine.
> [Accepter]   [Ignorer]

Accept → `apply_diff()` + nouvelle synthesis active + UPDATE proposal status=accepted.
Dismiss → UPDATE status=dismissed.

---

## 5. Wire-up scraper + scorer

### 5.1 Scraper (`app/lib/query_builder.py`)

Avant :
```python
roles = config.get("target", {}).get("roles") or []
```
Après :
```python
synthesis = load_active_synthesis(user_id)
if synthesis:
    titles = []
    for fam in synthesis["role_families"]:
        if fam.get("active", True):
            titles.extend(fam["titles"])
    locations = synthesis["geo"]["primary"] + synthesis["geo"]["acceptable"]
else:
    # backward compat
    titles = config["target"]["roles"]
    locations = config["constraints"]["locations"]

# Cap pour éviter l'explosion JobSpy : max 30 queries totales
queries = expand_queries(titles, locations, sites, max_queries=30)
```

### 5.2 Scorer (`app/lib/scorer.py`)

`build_system_prompt(user_config, synthesis)` reçoit synthesis en plus.
Substitutions dans le prompt :
- `target.roles` → flatten de tous les titres des role_families actives
- `target.target_companies` → `synthesis.dream_companies`
- `scoring_hints.deal_breakers` → `synthesis.deal_breakers`

Nouvelle règle (heuristique post-LLM) :
- Si `job.company.lower()` ∈ `dream_companies.lower()` ET `match_role >= 6` → score floor à 7 (déjà partiellement présent en heuristique, on étend).
- Si `job.title.lower()` matche un `deal_breaker` token → score capped à 2 (déjà présent, on garde).

### 5.3 Lien match → synthesis

`insert_match()` accepte un nouvel argument `profile_synthesis_id` et le persiste. Lecture via `list_matches_for_user` retourne ce champ pour permettre filtrage UI.

---

## 6. Loop continu (feedback → diff)

### 6.1 Job nightly `app/scraper/profile_diff_proposer.py`

Cron GitHub Actions, **04:00 UTC** (après le re-scoring nightly de 03:00).

```python
def propose_for_user(user_id):
    synthesis = load_active_synthesis(user_id)
    signals = load_feedback_signals(user_id, days=7)
    # signals = list of {job_id, status_changed_to, status_changed_at, feedback, job_title, job_company}
    if len(signals) < 5 and rejects_count(signals) < 3:
        return  # pas assez de signal
    proposal = propose_diff(synthesis, signals)
    if proposal is None:
        return
    insert_proposal(user_id, synthesis_id=synthesis["id"], diff=proposal["diff"], rationale_fr=proposal["rationale_fr"])
```

Workflow YAML : `.github/workflows/profile_diff_proposer_nightly.yml`, schedule 04:00 UTC, secrets identiques au re-scoring.

### 6.2 Cap anti-spam

Max 1 proposal pending par user à un instant T. Si une est déjà pending, le job nightly skip (évite d'empiler).

---

## 7. Migration users existants

Approche **lazy** retenue (plus simple que one-shot script) :

Au premier load de Mon Profil pour un user n'ayant pas de synthesis active :
1. Affiche spinner "Création de ton profil…"
2. `synthesize_profile(cv_text, user_config, None, None)` 
3. INSERT version 1 active
4. Render normal

Idempotent par construction (unique index `one_active_synthesis_per_user`).

Backup plan si lazy ne tient pas : `app/scripts/migrate_initial_synthesis.py` one-shot, lancé via workflow_dispatch, batch 10 users à la fois.

---

## 8. Tests

`app/lib/test_profile_synthesizer.py` :
- `test_synthesize_returns_valid_schema_against_jsonschema`
- `test_synthesize_with_pharma_cv_includes_required_role_families`
- `test_synthesize_with_feedback_lowers_confidence_when_signals_contradict`
- `test_synthesize_falls_back_to_groq_when_gemini_fails`
- `test_synthesize_raises_when_both_llms_fail_and_no_previous`
- `test_propose_diff_with_3_consulting_rejects_suggests_deal_breaker`
- `test_propose_diff_returns_none_when_signal_below_threshold`
- `test_apply_diff_idempotent_when_applied_twice`

`app/lib/test_storage_synthesis.py` :
- `test_insert_active_archives_previous`
- `test_unique_active_constraint_enforced`
- `test_load_active_returns_none_when_only_drafts`

`app/lib/test_query_builder_synthesis.py` :
- `test_query_builder_explodes_role_families_into_queries`
- `test_query_builder_caps_at_30_queries`
- `test_query_builder_falls_back_to_config_when_no_synthesis`

---

## 9. Découpage en PRs

| PR | Périmètre | Effort | Bloque sur |
|----|-----------|--------|------------|
| **PR1** Foundation | migrations SQL + storage CRUD + `profile_synthesizer.py` + tests unit | 0.75j | rien |
| **PR2** Page Mon Profil v1 | rename `1_onboarding.py` → `1_mon_profil.py`, gestion empty-state (upload CV intégré) + summary + cards éditables + open_questions + lazy migration + maj router `streamlit_app.py` | 1j | PR1 |
| **PR3** Reset + wire-up | reset modal + `query_builder` consume synthesis + `scorer` consume synthesis | 0.75j | PR1, PR2 |
| **PR4** Action bar + counts | bouton "Lancer recherche" + display counts par famille | 0.5j | PR3 |
| **PR5** Loop continu | `profile_diff_proposer.py` + cron + UI banner accept/dismiss | 1j | PR1-3 |
| **PR6** Health dashboard admin | dans `99_admin` : par user, jobs_scraped_7d, scored_7d, score_p50/p90, last_run, fail_rate | 0.5j | indépendant |

Total ~4.5j homme, ~6 jours calendaire avec relectures.

---

## 10. Risques et mitigations

| Risque | Mitigation |
|--------|------------|
| Hallucinations LLM sur synthesis | `source.type` + `source.evidence` sur chaque inférence, UI affiche l'origine |
| Synthesis qui drift | versioning + rollback (UPDATE status='active' sur ancienne row) |
| Coût LLM | Gemini Flash gratuit, ~50 calls/user/an, négligeable |
| Race reset → re-synthesis | transaction + unique index `one_active_synthesis_per_user` |
| Recall reste mauvais malgré bons titres (= problème board coverage) | PR6 health dashboard expose `jobs_scraped_per_role_family` → on voit quelles familles JobSpy ne couvre pas |
| User upload mauvais CV | option "uploader nouveau CV" dans modal reset |
| Open_questions infinies | cap 5 dans le prompt, IDs stables (`q_contract`, `q_field`, …) → pas régénérés à chaque LLM call |
| Régression scraper / scorer | backward compat conservée : si pas de synthesis, fallback sur `user_config` actuel. Tests `test_*_falls_back_to_config_when_no_synthesis` |

---

## 11. Out-of-scope v1 (à acter)

- Re-score automatique des matches existantes au changement de synthesis. → bouton manuel "Re-scorer tout" dans Dashboard, pas auto.
- Scraper boards alternatifs (PharmiWeb, eFinancialCareers, Naturejobs). → après PR6, quand on aura les chiffres pour décider.
- Export profil PDF.
- Partage de profil (collaboratif).

---

## 12. Décisions actées (validées par Hugo)

1. **Stockage du contrat de prompt** : dans le code Python (constants en haut de `profile_synthesizer.py`, pattern actuel scorer.py). Edit = deploy.
2. **Composant tags UI** : `st.multiselect` natif. Zéro dépendance externe ajoutée.
3. **Threshold du diff_proposer** : 5 status changes OU 3 rejects sur 7 jours, conservé en l'état. À retuner après collecte de données réelles.
4. **Page Mon Profil dans la nav** : **remplace** `1_onboarding`. Implication : la page Mon Profil gère le **empty state** (user nouveau, pas de CV, pas de synthesis) avec un flow d'upload CV intégré, en plus du flow normal d'édition. Le router `streamlit_app.py` envoie les nouveaux users directement vers Mon Profil au lieu du wizard onboarding. Le fichier `1_onboarding.py` est renommé en `1_mon_profil.py` (garde la position 1 dans la nav, qui reste le point d'entrée par défaut pour un user sans matches).
