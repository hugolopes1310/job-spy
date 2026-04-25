-- =====================================================================
-- Kairo — Migration : template cover letter (DOCX binaire)
-- =====================================================================
-- Permet de stocker le .docx que l'utilisateur upload dans l'onboarding,
-- pour le réutiliser comme coque (mise en page fidèle) lors des
-- générations de cover letters personnalisées.
--
-- À lancer dans Supabase → SQL Editor → New Query → Run.
-- Idempotent (IF NOT EXISTS) — safe à re-run.
-- =====================================================================

ALTER TABLE public.user_configs
  ADD COLUMN IF NOT EXISTS cover_letter_docx      BYTEA,
  ADD COLUMN IF NOT EXISTS cover_letter_filename  TEXT;

-- Expose aussi les colonnes binaires dans la vue `active_user_configs`
-- pour que le générateur de cover letter côté app (pas côté scraper)
-- puisse accéder au template via la même vue.
-- NB: on évite BYTEA ici parce que postgrest transporte ça en base64 →
-- inutile de le tirer sur chaque run de scoring. Le générateur de CL
-- charge le template à la demande via load_cover_letter_docx(user_id).

-- =====================================================================
-- DONE.
-- Sanity:
--   SELECT user_id, cover_letter_filename,
--          octet_length(cover_letter_docx) AS docx_bytes
--   FROM public.user_configs
--   WHERE cover_letter_docx IS NOT NULL;
-- =====================================================================
