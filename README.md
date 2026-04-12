# Job Tracker — Hugo

Pipeline ultra léger pour ne plus louper une offre pertinente sur tes 3 axes (Genève structurés / Lyon AM-PE / Fintech).

## Architecture

```
                   ┌─────────────┐
   config.yaml ─── │  tracker.py │  scrape 3 axes (python-jobspy)
                   └─────┬───────┘
                         │
                   ┌─────▼──────┐
                   │  scorer.py │  scoring mots-clés (+ blacklist)
                   └─────┬──────┘
                         │
                   ┌─────▼──────┐
                   │   db.py    │  SQLite (jobs.db) — source de vérité
                   └─────┬──────┘
                         │
               ┌─────────┴──────────┐
               │                    │
         ┌─────▼──────┐       ┌─────▼──────┐
         │ notifier.py│       │   app.py   │
         │  Telegram  │       │Flask dash  │
         └────────────┘       └────────────┘
```

Pas de framework lourd, pas de compte cloud : tu lances tout en local, tu cron le scrape, et tu ouvres le dashboard Flask quand tu veux.

## Setup — 15 min

### 1) Installer les dépendances

```bash
cd job_tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Créer ton bot Telegram

1. Dans Telegram, cherche `@BotFather` et envoie `/newbot`.
2. Donne un nom (ex. `HugoJobBot`) et un username (ex. `hugo_job_bot`).
3. Copie le token qu'il te renvoie — c'est ton `TELEGRAM_BOT_TOKEN`.
4. Envoie un message à ton bot pour l'activer.
5. Pour récupérer ton `chat_id`, ouvre dans un navigateur :
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` — tu verras `"chat":{"id":123456789,...}`. C'est ton `TELEGRAM_CHAT_ID`.

### 3) Exposer les variables d'environnement

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

Ajoute ces deux lignes dans ton `~/.zshrc` (ou `~/.bashrc`) pour qu'elles persistent.

### 4) Tester

```bash
python tracker.py --test-telegram   # ping Telegram
python tracker.py --once            # un scan complet
python tracker.py --stats           # voir la BDD
```

### 5) Automatiser (3 scans par jour)

```bash
crontab -e
```

Ajoute :

```cron
0 8,13,19 * * * cd /Users/hugo/path/to/job_tracker && ./.venv/bin/python tracker.py --once >> tracker.log 2>&1
```

### 6) Lancer le dashboard

```bash
python app.py
# → http://127.0.0.1:5000
```

Tu y verras les offres triées par score, avec des boutons pour passer une offre en `applied`, `interview`, `ignored`.

## Tuning

Tout se joue dans `config.yaml` :

- **`scoring.notify_threshold`** : augmente (7, 8) si tu as trop de bruit sur Telegram, baisse (3, 4) si tu veux tout voir.
- **`scoring.title_boost` / `description_boost`** : ajoute les mots-clés spécifiques aux boîtes que tu cibles (ex. `pictet`, `siparex`, `autocallable`).
- **`scoring.blacklist`** : tu peux y mettre `"head of"` à -10 pour filtrer tout ce qui est trop senior.
- **`axes`** : ajoute/retire une requête LinkedIn. `results_wanted: 30` et `hours_old: 48` sont de bons défauts.

## Quick wins complémentaires (à faire tout de suite, en parallèle)

Ces canaux ne passent pas par le scraper mais sont à activer le jour même :

1. **LinkedIn Saved Searches** — crée 3 saved searches (une par axe) avec notification *Daily*. Ça capture les offres boostées sponsorisées que jobspy peut rater.
2. **eFinancialCareers** — 2 alertes par axe (EN + FR). Source #1 sur Genève.
3. **Welcome to the Jungle + APEC** — 1 alerte par axe pour le marché français.
4. **Jobup.ch / jobs.ch** — 1 alerte "structured products" Suisse.
5. **Visualping** (gratuit ≤5 pages) — abonne-toi aux pages carrières de : Pictet, Lombard Odier, UBP, Mirabaud, Syz, Pandat, Siparex. Tu reçois un email dès qu'une page bouge.
6. **Chasseurs de tête spécialisés** — envoie ton CV à Dartmouth Partners, Selby Jennings, Robert Walters Finance, PSD Group, Michael Page Banking & Finance. Ils ne sont pas sur LinkedIn Jobs.

Ces 6 sources + le tracker Python = couverture quasi-exhaustive.

## Extensions envisageables (phase 3)

- **AI scoring** : au lieu de mots-clés bruts, passer la description dans Claude/GPT avec ton CV en prompt et sortir un score + une fit analysis (3 lignes par offre). Tu réutilises exactement ce que tu as construit côté K2.
- **Multi-sources** : jobspy supporte déjà Indeed, Glassdoor, ZipRecruiter, Google Jobs. Ajoute un axe par source.
- **Cover letter auto** : pour chaque offre `applied`, générer une première ébauche de lettre de motivation adaptée.
- **Deployment** : packager en Docker et push sur ton VPS OVH (même infra que K2) pour que le scraper tourne même ordi fermé.
