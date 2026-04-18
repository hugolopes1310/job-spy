# Job Spy — multi-user app (Phase 2)

Pipeline side-car **à côté** de ton pipeline perso existant (`tracker.py`, `config.yaml`, Telegram...). Rien de l'ancien stack n'est touché.

**Stack Phase 2 :**

- **Frontend** : Streamlit (multi-pages).
- **Auth** : Supabase Auth — OTP 6 chiffres par email (free tier).
- **DB** : Supabase Postgres avec RLS.
- **Hébergement** : Streamlit Community Cloud (gratuit, URL publique).
- **LLM** : Groq (Llama 3.3 70B) pour le parsing CV → config JSON.
- **Scraper** (Phase 3) : tournera sur GitHub Actions, écrira dans Supabase.

Tout ça reste **100 % gratuit** tant qu'on ne dépasse pas les quotas gratuits.

---

## 1. Setup Supabase (à faire une seule fois)

1. **Créer un projet** sur <https://supabase.com> → note l'URL et les 2 clés (`anon` + `service_role`).
2. **Charger le schéma** : ouvre *SQL Editor* → *New query* → copie-colle le contenu de `supabase/schema.sql` → *Run*. Ça crée les tables `profiles` / `user_configs`, les triggers, les policies RLS et la vue `active_user_configs`.
3. **Désactiver la confirmation d'email** : *Auth → Providers → Email* → `Confirm email` = OFF. (L'OTP fait déjà office de vérification.)
4. **Vérifier le template OTP** : *Auth → Email Templates → "Magic Link"* → s'assurer qu'il contient `{{ .Token }}` (pas seulement `{{ .ConfirmationURL }}`). Sinon l'utilisateur recevra un lien au lieu d'un code.
5. **Se déclarer admin** : dans *SQL Editor*, exécute :

   ```sql
   UPDATE public.profiles
      SET is_admin = TRUE, status = 'approved', approved_at = NOW()
    WHERE email = 'lopeshugo1310@gmail.com';
   ```

   ⚠️ La ligne `profiles` pour ton email n'existera qu'après ta première tentative de connexion — le trigger la crée automatiquement à ce moment-là. Donc l'ordre c'est : tu essaies de te connecter → OTP reçu → tu le rentres → tu arrives en `pending` → tu files en SQL Editor te promouvoir admin → tu recharges.

---

## 2. Setup local

```bash
cd job_spy
source .venv/bin/activate
pip install -r requirements.txt

# Créer le fichier secrets à partir du template
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# puis éditer .streamlit/secrets.toml et remplir les vraies valeurs
```

Lance :

```bash
streamlit run app/streamlit_app.py
```

Ouvre <http://localhost:8501>.

**Flow de test :**

1. Entre ton email → tu reçois un code par email.
2. Tu rentres le code → tu arrives en `pending`.
3. Via SQL Editor Supabase (voir étape 5 ci-dessus), tu te mets admin → tu recharges → tu accèdes à l'onboarding + `/admin`.

---

## 3. Déploiement sur Streamlit Cloud

1. Pousse ton repo sur GitHub (`hugolopes1310/job-tracker`) — vérifie que `.streamlit/secrets.toml` **n'est pas** commité (il est ignoré par `.gitignore`).
2. Va sur <https://share.streamlit.io> → *New app* → connecte GitHub → choisis le repo + branche `main`.
3. **Main file path** : `app/streamlit_app.py`.
4. **Advanced settings → Secrets** : colle le contenu de ton `.streamlit/secrets.toml` (même format).
5. *Deploy*. Après ~1 min tu as une URL publique type `https://job-spy-xxxx.streamlit.app` — partage-la à tes amis.
6. Retourne dans Supabase → *Auth → URL Configuration → Redirect URLs* → ajoute l'URL Streamlit Cloud + `http://localhost:8501`.

---

## 4. Admin — gérer l'accès des amis

Deux options :

### A) Depuis l'UI (`/admin`)

Une fois connecté avec un compte admin, clic sur **🛡 Panneau admin** sur la page d'accueil. Tu vois 3 onglets : *En attente / Approuvés / Révoqués* — un bouton par ligne pour approuver / rejeter / révoquer.

### B) En CLI (dépannage)

Depuis la racine du repo, avec `.streamlit/secrets.toml` rempli (ou les variables d'env `SUPABASE_*`) :

```bash
# Lister tous les profils
python -m app.lib.storage list

# Approuver un email
python -m app.lib.storage approve alex@exemple.com

# Révoquer un email (= "kick")
python -m app.lib.storage revoke alex@exemple.com

# Promouvoir un admin
python -m app.lib.storage admin autre.admin@exemple.com
```

Tous ces commandes passent par le `service_role` et ignorent la RLS.

---

## 5. Structure

```
job_spy/
├── app/
│   ├── streamlit_app.py          # entry point — login OTP + routing par status
│   ├── pages/
│   │   ├── 1_onboarding.py       # 4 écrans : upload → brief → questions → review
│   │   ├── 2_dashboard.py        # récap config (offres en Phase 3)
│   │   └── 99_admin.py           # approve / revoke users
│   ├── lib/
│   │   ├── supabase_client.py    # clients Supabase (anon + service_role)
│   │   ├── auth.py               # OTP send/verify + session Streamlit
│   │   ├── storage.py            # CRUD profiles + user_configs via Supabase
│   │   ├── cv_parser.py          # PDF/DOCX → texte
│   │   └── config_extractor.py   # Groq : CV + brief → config JSON
│   └── data/                     # (vide — plus utilisé en Phase 2)
├── supabase/
│   └── schema.sql                # à charger dans Supabase SQL Editor
├── .streamlit/
│   ├── secrets.toml              # (local, non commité)
│   └── secrets.toml.example      # template
├── requirements.txt
└── README.md                     # (ce fichier) — doc multi-user
```

Ton ancien stack perso (`tracker.py`, `config.yaml`, `jobs.db`, etc. à la racine) continue de tourner comme avant — aucun couplage.

---

## 6. Roadmap

- **Phase 2 ✅** : Supabase auth OTP + DB + UI admin + déploiement Streamlit Cloud.
- **Phase 3** : scraper multi-user sur GitHub Actions — lit `active_user_configs`, écrit une table `jobs` partagée, scoring LLM par user.
- **Phase 4** : dashboard de vraies offres avec feedback 👍👎, digest email quotidien (Resend ou Gmail SMTP).
- **Phase 5** : few-shot LLM par user (depuis les 👍👎 accumulés).

---

## 7. Smoke tests manuels

```bash
# 1. CV parser (aucune dépendance réseau)
python -m app.lib.cv_parser /chemin/vers/ton-cv.pdf

# 2. Config extractor (nécessite GROQ_API_KEY)
python -m app.lib.config_extractor /chemin/vers/ton-cv.pdf

# 3. Supabase — lister les profils (nécessite SUPABASE_URL + SUPABASE_SERVICE_KEY)
python -m app.lib.storage list
```
