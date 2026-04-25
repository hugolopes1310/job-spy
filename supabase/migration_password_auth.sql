-- =====================================================================
-- Kairo — Migration : ajout de l'auth par mot de passe
-- =====================================================================
-- À lancer UNE fois dans Supabase → SQL Editor → New Query → Run.
-- Idempotent (IF NOT EXISTS), donc safe à re-run si besoin.
-- =====================================================================

-- 1) Nouvelle colonne : has_password
--    FALSE par défaut → le user devra passer par l'OTP la 1re fois,
--    puis choisir un mot de passe à la 1re connexion réussie.
ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS has_password BOOLEAN NOT NULL DEFAULT FALSE;

-- 2) Politique RLS : un user peut set son propre has_password
--    (on garde les checks métier côté Python, cf. set_password())
--    La policy "own_profile_update" existe déjà et couvre ça.

-- 3) (Optionnel) Backfill : si tu as des comptes historiques qui ont
--    déjà un password côté auth.users, flip-les à TRUE. Sinon ils seront
--    forcés à refaire l'OTP + set_password à leur prochaine connexion.
--    Pour un MVP où personne n'a encore de password, skip cette étape.
-- UPDATE public.profiles p
--   SET has_password = TRUE
--   WHERE EXISTS (
--     SELECT 1 FROM auth.users u
--     WHERE u.id = p.user_id
--       AND u.encrypted_password IS NOT NULL
--       AND u.encrypted_password <> ''
--   );

-- =====================================================================
-- DONE. Prochaines étapes côté Supabase :
--   1. Authentication → Providers → Email : vérifier que "Enable Email
--      provider" est ON (il l'est par défaut).
--   2. Authentication → Policies → vérifier que "Minimum password length"
--      = 8 (c'est le défaut).
-- =====================================================================
