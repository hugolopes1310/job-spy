-- =====================================================================
-- Kairo — Migration : colonne `next_action_at` (date de relance)
-- =====================================================================
-- À lancer dans Supabase → SQL Editor → New Query → Run.
-- Idempotent (IF NOT EXISTS / DROP+CREATE) — safe à re-run.
--
-- Prérequis : `supabase/migration_tracking.sql` déjà appliqué (colonnes
-- is_favorite, notes, applied_at, status_changed_at + vue étendue).
-- =====================================================================

-- 1) Nouvelle colonne : next_action_at (date de relance / prochaine action).
--    DATE (pas TIMESTAMPTZ) car c'est une intention utilisateur, pas un event.
ALTER TABLE public.user_job_matches
  ADD COLUMN IF NOT EXISTS next_action_at DATE;

-- 2) Index pour les scans "relances dues aujourd'hui".
CREATE INDEX IF NOT EXISTS idx_ujm_user_next_action
  ON public.user_job_matches(user_id, next_action_at)
  WHERE next_action_at IS NOT NULL;

-- 3) Recréer la vue enrichie pour exposer next_action_at.
--    DROP + CREATE pour éviter les erreurs "cannot change column order"
--    qui cassent CREATE OR REPLACE VIEW quand on ajoute une colonne.
DROP VIEW IF EXISTS public.user_matches_enriched;

CREATE VIEW public.user_matches_enriched
WITH (security_invoker = true) AS
  SELECT
    m.user_id,
    m.job_id,
    m.score,
    m.analysis,
    m.status,
    m.feedback,
    m.is_favorite,
    m.notes,
    m.applied_at,
    m.status_changed_at,
    m.next_action_at,
    m.scored_at,
    m.seen_at,
    j.url,
    j.title,
    j.company,
    j.location,
    j.description,
    j.date_posted,
    j.site,
    j.is_repost,
    j.repost_gap_days,
    j.first_seen AS job_first_seen
  FROM public.user_job_matches m
  JOIN public.jobs j ON j.id = m.job_id;

-- =====================================================================
-- DONE.
-- Sanity:
--   SELECT COUNT(*) FILTER (WHERE next_action_at IS NOT NULL) FROM public.user_job_matches;
--   SELECT COUNT(*) FILTER (WHERE next_action_at <= CURRENT_DATE) FROM public.user_job_matches;
-- =====================================================================
