-- =====================================================================
-- Kairo — Phase 5 : Scraper run telemetry
-- =====================================================================
-- À lancer dans Supabase → SQL Editor → New Query → Run.
-- Idempotent (IF NOT EXISTS / DROP+CREATE) — safe à re-run.
--
-- Dépend de : schema.sql (profiles + is_admin).
--
-- Ce script ajoute :
--   1. scraper_runs — une row par exécution du scraper (cron OU button)
--   2. RLS : service_role write, admin read
--   3. VIEW scraper_runs_recent — derniers 50 runs (pour admin panel)
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) scraper_runs — une row par run
-- ---------------------------------------------------------------------
-- Conventions :
--   * runner    = 'cron' (GitHub Actions) | 'manual' (dashboard button) | 'cli'
--   * status    = 'running' (set au start) | 'ok' | 'failed' | 'partial'
--                  'partial' = certains users OK, d'autres en erreur
--   * totals    = { queries, scraped, new_jobs, scored, failed_llm,
--                    failed_upsert, failed_insert, failed_queries,
--                    custom_sources_ok, custom_sources_failed,
--                    users_processed }
--   * errors    = [{ user_id?, stage, error, traceback? }]
--                  bornée (max 50 entries) côté app pour éviter les payloads géants.
--   * triggered_by_user_id : pour les runs 'manual' (action bar dashboard),
--                            on stocke qui a cliqué. NULL pour 'cron'.
CREATE TABLE IF NOT EXISTS public.scraper_runs (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at           TIMESTAMPTZ,
  runner                TEXT NOT NULL DEFAULT 'cron'
                        CHECK (runner IN ('cron', 'manual', 'cli')),
  status                TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'ok', 'failed', 'partial')),
  triggered_by_user_id  UUID REFERENCES public.profiles(user_id) ON DELETE SET NULL,
  totals                JSONB NOT NULL DEFAULT '{}'::jsonb,
  errors                JSONB NOT NULL DEFAULT '[]'::jsonb,
  llm_quota             JSONB,                          -- { groq_tpd, gemini_quota, all_exhausted }
  notes                 TEXT,                           -- free-text (e.g. workflow run URL)
  duration_ms           INTEGER GENERATED ALWAYS AS (
    CASE
      WHEN finished_at IS NOT NULL
        THEN (EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000)::INTEGER
      ELSE NULL
    END
  ) STORED
);

CREATE INDEX IF NOT EXISTS idx_scraper_runs_started_at
  ON public.scraper_runs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_scraper_runs_status
  ON public.scraper_runs(status, started_at DESC);

-- ---------------------------------------------------------------------
-- 2) Row Level Security
-- ---------------------------------------------------------------------
-- Aucune RLS user-facing : seul le service_role écrit (scraper) et seuls
-- les admins lisent (futur admin panel). Les users normaux n'ont aucune
-- raison de voir cette table.
ALTER TABLE public.scraper_runs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "admin_scraper_runs_select" ON public.scraper_runs;
DROP POLICY IF EXISTS "service_scraper_runs_all" ON public.scraper_runs;

-- Admins lisent tout (UI admin future).
CREATE POLICY "admin_scraper_runs_select"
  ON public.scraper_runs FOR SELECT
  USING (public.is_admin(auth.uid()));

-- service_role bypasses RLS by default ; on n'a donc PAS besoin d'une policy
-- dédiée pour les writes du scraper. La policy ci-dessus suffit.

-- ---------------------------------------------------------------------
-- 3) VIEW scraper_runs_recent — derniers 50 runs
-- ---------------------------------------------------------------------
-- Pratique pour l'admin panel : SELECT * FROM scraper_runs_recent;
CREATE OR REPLACE VIEW public.scraper_runs_recent
WITH (security_invoker = true) AS
  SELECT
    id,
    started_at,
    finished_at,
    duration_ms,
    runner,
    status,
    triggered_by_user_id,
    totals,
    errors,
    llm_quota,
    notes
  FROM public.scraper_runs
  ORDER BY started_at DESC
  LIMIT 50;

-- =====================================================================
-- DONE.
-- Sanity checks :
--   SELECT status, COUNT(*), MAX(started_at)
--     FROM public.scraper_runs GROUP BY status;
--   SELECT id, runner, status, duration_ms, started_at
--     FROM public.scraper_runs_recent LIMIT 10;
-- =====================================================================
