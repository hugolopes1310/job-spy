-- =====================================================================
-- Job Spy — Phase 3 schema extension: jobs + user_job_matches
-- =====================================================================
-- Run ONCE in Supabase → SQL Editor after supabase/schema.sql.
-- Idempotent: safe to re-run.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) jobs — shared, one row per unique job URL
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.jobs (
  id               TEXT PRIMARY KEY,                       -- sha1(url)
  url              TEXT NOT NULL UNIQUE,
  fingerprint      TEXT,                                   -- normalize(title+company)
  title            TEXT NOT NULL,
  company          TEXT,
  location         TEXT,
  description      TEXT,
  date_posted      TEXT,                                   -- free-form, scraper-dependent
  site             TEXT,                                   -- linkedin | indeed | google | ...
  is_repost        BOOLEAN NOT NULL DEFAULT FALSE,
  repost_of        TEXT REFERENCES public.jobs(id) ON DELETE SET NULL,
  repost_gap_days  INTEGER,
  first_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON public.jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen  ON public.jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_site        ON public.jobs(site);

-- ---------------------------------------------------------------------
-- 2) user_job_matches — per-user scoring + lifecycle
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_job_matches (
  user_id      UUID NOT NULL REFERENCES public.profiles(user_id) ON DELETE CASCADE,
  job_id       TEXT NOT NULL REFERENCES public.jobs(id) ON DELETE CASCADE,
  score        INTEGER,                                   -- 0-10 AI score, NULL if scoring failed
  analysis     JSONB,                                     -- full Groq output (sub-scores, red_flags, ...)
  status       TEXT NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'seen', 'applied', 'rejected')),
  feedback     TEXT                                        -- 'good' | 'bad' | 'applied' | NULL
                CHECK (feedback IS NULL OR feedback IN ('good', 'bad', 'applied')),
  scored_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  seen_at      TIMESTAMPTZ,
  PRIMARY KEY (user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_ujm_user_score  ON public.user_job_matches(user_id, score DESC);
CREATE INDEX IF NOT EXISTS idx_ujm_user_status ON public.user_job_matches(user_id, status);

-- ---------------------------------------------------------------------
-- 3) Row Level Security
-- ---------------------------------------------------------------------
ALTER TABLE public.jobs              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_job_matches  ENABLE ROW LEVEL SECURITY;

-- --- jobs ---
-- Any authenticated user can SELECT (needed to JOIN in dashboard queries).
-- Writes are service_role only (scraper).
DROP POLICY IF EXISTS "auth_read_jobs" ON public.jobs;
CREATE POLICY "auth_read_jobs"
  ON public.jobs FOR SELECT
  TO authenticated
  USING (TRUE);

-- --- user_job_matches ---
-- A user can only read/update their own matches.
DROP POLICY IF EXISTS "ujm_own_select"  ON public.user_job_matches;
DROP POLICY IF EXISTS "ujm_own_update"  ON public.user_job_matches;

CREATE POLICY "ujm_own_select"
  ON public.user_job_matches FOR SELECT
  USING (auth.uid() = user_id);

CREATE POLICY "ujm_own_update"
  ON public.user_job_matches FOR UPDATE
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Admins can see everything (for moderation / debugging).
DROP POLICY IF EXISTS "ujm_admin_select" ON public.user_job_matches;
CREATE POLICY "ujm_admin_select"
  ON public.user_job_matches FOR SELECT
  USING (public.is_admin(auth.uid()));

-- ---------------------------------------------------------------------
-- 4) Convenience VIEW — dashboard query (joins jobs + matches)
-- ---------------------------------------------------------------------
-- security_invoker = true so the VIEW inherits RLS from the CALLER, not
-- from the view owner (default in PG15+). Without it, the view would
-- bypass RLS and every user could see every match.
CREATE OR REPLACE VIEW public.user_matches_enriched
WITH (security_invoker = true) AS
  SELECT
    m.user_id,
    m.job_id,
    m.score,
    m.analysis,
    m.status,
    m.feedback,
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

-- RLS flows through: a user sees their matches × jobs automatically.

-- =====================================================================
-- DONE.
-- Quick sanity check:
--   SELECT COUNT(*) FROM public.jobs;
--   SELECT COUNT(*) FROM public.user_job_matches;
-- =====================================================================
