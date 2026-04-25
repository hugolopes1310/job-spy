-- =====================================================================
-- Kairo — Migration : favoris + pipeline de candidature enrichi
-- =====================================================================
-- À lancer dans Supabase → SQL Editor → New Query → Run.
-- Idempotent (IF NOT EXISTS / DROP+CREATE) — safe à re-run.
-- =====================================================================

-- 1) Nouvelles colonnes : is_favorite, notes, applied_at, status_changed_at
ALTER TABLE public.user_job_matches
  ADD COLUMN IF NOT EXISTS is_favorite       BOOLEAN     NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS notes             TEXT,
  ADD COLUMN IF NOT EXISTS applied_at        TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ;

-- 2) Index sur favoris pour la page Suivi
CREATE INDEX IF NOT EXISTS idx_ujm_user_favorite
  ON public.user_job_matches(user_id, is_favorite)
  WHERE is_favorite = TRUE;

CREATE INDEX IF NOT EXISTS idx_ujm_user_applied
  ON public.user_job_matches(user_id, status)
  WHERE status IN ('applied', 'interview', 'offer');

-- 3) Élargir le CHECK de status pour le pipeline complet.
--    PG ne permet pas d'altérer une CHECK existante en place — on drop / recreate.
ALTER TABLE public.user_job_matches
  DROP CONSTRAINT IF EXISTS user_job_matches_status_check;

ALTER TABLE public.user_job_matches
  ADD CONSTRAINT user_job_matches_status_check
  CHECK (status IN (
    'new',          -- jamais ouverte
    'seen',         -- ouverte au moins une fois
    'applied',      -- l'utilisateur a postulé
    'interview',    -- entretien planifié / en cours
    'offer',        -- offre reçue
    'rejected',     -- refusée (par l'user ou par l'entreprise)
    'archived'      -- mise de côté
  ));

-- 4) Trigger : `status_changed_at` se met à jour quand status change
CREATE OR REPLACE FUNCTION public.touch_status_changed_at()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    NEW.status_changed_at = NOW();
    -- Si on passe à 'applied' pour la 1re fois, fixe applied_at.
    IF NEW.status = 'applied' AND NEW.applied_at IS NULL THEN
      NEW.applied_at = NOW();
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ujm_touch_status ON public.user_job_matches;
CREATE TRIGGER ujm_touch_status
  BEFORE UPDATE ON public.user_job_matches
  FOR EACH ROW EXECUTE FUNCTION public.touch_status_changed_at();

-- 5) Mettre à jour la vue enrichie pour exposer les nouvelles colonnes.
--    /!\ On DROP puis CREATE (pas CREATE OR REPLACE) car Postgres refuse
--    `CREATE OR REPLACE VIEW` dès qu'on insère une colonne au milieu ou qu'on
--    modifie l'ordre — il exige que les nouvelles colonnes soient ajoutées
--    uniquement à la fin avec le même type. DROP + CREATE évite ce piège.
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
--   SELECT status, COUNT(*) FROM public.user_job_matches GROUP BY status;
--   SELECT COUNT(*) FILTER (WHERE is_favorite) FROM public.user_job_matches;
-- =====================================================================
