# Kairo (job_spy) — Handoff brief

## Contexte projet

Plateforme multi-user de matching d'offres d'emploi. Scraping LinkedIn/Indeed/Google Jobs via `python-jobspy` + scoring LLM (Groq Llama 3.3 70B, fallback Gemini 2.0 Flash) contre le profil utilisateur (config JSON + CV). Stockage Supabase, UI Streamlit, cron GitHub Actions.

Deux repos :
- `/Users/hugo/PycharmProjects/PythonProject/job_spy` — dev privé (c'est ici qu'on code)
- `/Users/hugo/PycharmProjects/PythonProject/job_spy_public` — miroir public déployé sur Streamlit Community Cloud

Déploiement = copier les fichiers changés de `job_spy/` vers `job_spy_public/`, puis `git commit + push` dans le public. Streamlit Cloud redéploie auto (~30s).

URL actuelle : `https://job-spy.streamlit.app` — en cours de rename vers `kairo.streamlit.app`.

## Architecture

**App Streamlit** (`app/pages/`) :
- `streamlit_app.py` — landing + login OTP / password (2 écrans)
- `1_onboarding.py` — wizard config profil (rôles, entreprises cibles, CV, lettre, sites carrières persos)
- `2_dashboard.py` — cartes matches avec filtres (score, période, statut, favoris)
- `3_suivi.py` — pipeline candidatures (en cours, task #41)

**Modules** (`app/lib/`) :
- `auth.py` — Supabase auth OTP + password
- `storage.py` — CRUD profiles + user_configs + CV/CL
- `supabase_client.py` — clients anon (RLS) + service_role (scraper)
- `scorer.py` — prompt + appel LLM (Groq→Gemini fallback) retourne score 0-10 + analyse structurée
- `scrapers.py` — wrapper python-jobspy
- `career_sites.py` — ATS detection (Greenhouse/Lever/Workable/Ashby) + fetch JSON APIs + LLM generic fallback
- `query_builder.py` — dérive les queries de scraping depuis la config user
- `jobs_store.py` — CRUD jobs + user_job_matches, dedup par fingerprint, détection repost (45j+)
- `theme.py` — badges, design system
- `logo.py` — SVG Kairo

**Scraper CLI** (`app/scraper/`) :
- `run.py` — cron multi-user : `python -m app.scraper.run [--user EMAIL] [--dry-run] [--cleanup]`
- `rescore.py` — **NOUVEAU** — relance le scoring sur les matches existants avec le prompt courant : `python -m app.scraper.rescore --user EMAIL [--dry-run] [--max-old-score N] [--limit N]`

**Supabase** :
- Tables : `profiles`, `user_configs` (JSONB config + cv_text + cover_letter_text + cover_letter_docx bytea), `jobs` (shared), `user_job_matches` (per-user scoring)
- Vue : `active_user_configs` (profils approved joints aux configs), `user_matches_enriched` (matches + job metadata)
- RLS activé partout, le scraper bypass via service_role
- Status enum enrichi : new / seen / applied / interview / offer / rejected / archived

**GitHub Actions** : cron 30-60min qui exécute `python -m app.scraper.run --cleanup`.

## Ce qu'on a fait dans cette session

### 1. Custom career sites (terminé, déployé)
Feature pour ajouter des pages carrière d'entreprises spécifiques. Module `app/lib/career_sites.py` : detect ATS par regex sur l'URL, fetch via JSON API publique (Greenhouse, Lever, Workable, Ashby), fallback LLM sur HTML générique. Le scraper itère sur `config["custom_career_sources"]` après les queries jobspy. En cas d'échec, flag "not_scrapable" avec compteur d'erreurs — garde le site dans la liste avec un badge. UI dans onboarding step 3 : `st.data_editor` avec preview live du ATS détecté. Smoke tests : 10/10 détections + live fetch Stripe OK.

### 2. Patch défensif onboarding (terminé, déployé)
Hugo a cru que sa config avait été wipée. Diagnostic : la config était intacte, mais on a ajouté par précaution un `_deep_merge_preserve()` dans `1_onboarding.py` qui garantit qu'une valeur baseline populaté ne peut jamais être écrasée par du vide/None/[] provenant du LLM "Regénérer". Si la sauvegarde détecte une perte de champs, warning + bouton "Restaurer".

### 3. Fix prompt scorer (terminé, déployé)
Bug : 58/66 matches de Hugo notés <4/10, Pictet "Customised Solutions Specialist" à 2/10 alors que Pictet est dans ses target_companies et que c'est littéralement le job qu'il fait. Cause : l'ancien prompt avait des règles littérales type "si le must-have n'apparaît pas dans le texte, score max=4". Réécrit `build_system_prompt()` dans `app/lib/scorer.py` avec :
- matching sémantique explicite ("Customised Solutions Specialist" = "Investment Solutions Structurer")
- boost target_companies : score minimum 7 si l'entreprise matche, 9-10 si le rôle matche aussi
- deal-breakers ne cappent que s'ils décrivent le rôle core
- must-have = signal positif, pas obligation
- fuzzy géo : Genève=Geneva=Nyon=Lausanne dans un rayon de 30km, Zurich=Zürich=Zug
- exemples concrets dans le prompt (Pictet → 9, Framatome alternance assistant → 0-1)

### 4. Rescore CLI (terminé, déployé)
`app/scraper/rescore.py` pour re-noter les 66 matches existants de Hugo sans attendre que le scraper revoie les mêmes jobs. Seulement `score` + `analysis` updatés, le reste (status, is_favorite, notes, scored_at) préservé. Respecte le cooldown Groq 4s. Affiche diff before/after + buckets.

### 5. Default score filter 5 (terminé, déployé)
Dashboard : `f_min_score` initialisé à 5 au lieu de 0 pour ne pas afficher direct les matches pas pertinents.

### 6. Rename Streamlit app (en cours)
Changement `job-spy.streamlit.app` → `kairo.streamlit.app` via Settings → General → Custom subdomain. Après ça, impératif de mettre à jour les Redirect URLs Supabase (Site URL + Redirect URLs) sinon les magic links OTP cassent. C'est la task #15 pending.

## Tâches qui restent

- **#15 [pending]** — Ajouter l'URL Streamlit Cloud aux Redirect URLs Supabase (à faire une fois le rename effectué)
- **#23 [pending]** — Vérifier les matches scorés dans le dashboard (après le rescore)
- **#41 [in_progress]** — Nouvelle page "Suivi" : pipeline de candidatures. Déjà une base `app/pages/3_suivi.py` + `list_tracked_matches()` dans `jobs_store.py`. Reste : finir l'UI (colonnes par statut, drag-drop ? ou boutons de transition), notes éditables, dates clés visibles.

## Secrets / env

Stockés dans `.streamlit/secrets.toml` (local) et Streamlit Cloud Secrets (prod) :
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`
- `GROQ_API_KEY`, `GEMINI_API_KEY`
- (GitHub Actions : mêmes secrets en repo secrets)

## Prochaines étapes recommandées

Après le rename Kairo + mise à jour Supabase Redirect URLs, lancer le rescore pour Hugo :
```bash
cd /Users/hugo/PycharmProjects/PythonProject/job_spy
python -m app.scraper.rescore --user lopeshugo1310@gmail.com --dry-run
# vérifier que Pictet remonte, puis :
python -m app.scraper.rescore --user lopeshugo1310@gmail.com
```

Puis finir task #41 (page Suivi).
