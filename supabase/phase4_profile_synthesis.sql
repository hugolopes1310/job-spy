-- =====================================================================
-- Kairo — Phase 4 : Profile Synthesis Loop
-- =====================================================================
-- À lancer dans Supabase → SQL Editor → New Query → Run.
-- Idempotent (IF NOT EXISTS / DROP+CREATE) — safe à re-run.
--
-- Dépend de : schema.sql (profiles, user_configs) + phase3_jobs.sql
--             (jobs, user_job_matches) + migration_tracking.sql.
--
-- Ce script ajoute :
--   1. profile_syntheses           — synthèse LLM versionnée par user
--   2. profile_synthesis_proposals — diffs proposés par le loop continu
--   3. ALTER user_job_matches      — FK profile_synthesis_id (audit trail)
--   4. VIEW active_profile_synthesis — pour le scraper / scorer
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) profile_syntheses — un objet par version, une seule 'active' par user
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.profile_syntheses (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES public.profiles(user_id) ON DELETE CASCADE,
  version         INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'active', 'archived')),
  synthesis       JSONB NOT NULL,                    -- objet structuré (role_families, geo, ...)
  source_signals  JSONB,                             -- {cv_text_hash, config_hash, feedback_window_days, signal_count}
  llm_model       TEXT,                              -- 'gemini-2.5-flash' | 'groq-llama-3.3-70b'
  prompt_version  TEXT,                              -- 'v1.0' pour audit / rollback
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  activated_at    TIMESTAMPTZ,
  archived_at     TIMESTAMPTZ,
  UNIQUE (user_id, version)
);

-- Une seule synthèse 'active' par user à un instant T.
-- Index unique partiel : applique le contraint uniquement quand status='active'.
CREATE UNIQUE INDEX IF NOT EXISTS uq_profile_synthesis_active
  ON public.profile_syntheses(user_id)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_profile_synthesis_user_status
  ON public.profile_syntheses(user_id, status);

CREATE INDEX IF NOT EXISTS idx_profile_synthesis_created_at
  ON public.profile_syntheses(created_at DESC);

-- ---------------------------------------------------------------------
-- 2) profile_synthesis_proposals — diffs proposés par le loop continu
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.profile_synthesis_proposals (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               UUID NOT NULL REFERENCES public.profiles(user_id) ON DELETE CASCADE,
  current_synthesis_id  UUID REFERENCES public.profile_syntheses(id) ON DELETE SET NULL,
  diff                  JSONB NOT NULL,              -- {add_deal_breakers, remove_role_families, ...}
  rationale_fr          TEXT,                        -- "5 rejects sur consulting cette semaine"
  status                TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'accepted', 'dismissed')),
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at           TIMESTAMPTZ
);

-- Au plus 1 proposal pending par user (anti-spam du loop nightly).
CREATE UNIQUE INDEX IF NOT EXISTS uq_proposal_pending_per_user
  ON public.profile_synthesis_proposals(user_id)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_proposal_user_status
  ON public.profile_synthesis_proposals(user_id, status);

-- ---------------------------------------------------------------------
-- 3) ALTER user_job_matches — FK vers la synthèse qui a scoré la match
-- ---------------------------------------------------------------------
ALTER TABLE public.user_job_matches
  ADD COLUMN IF NOT EXISTS profile_synthesis_id UUID
    REFERENCES public.profile_syntheses(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_ujm_synthesis
  ON public.user_job_matches(profile_synthesis_id);

-- ---------------------------------------------------------------------
-- 4) Row Level Security
-- ---------------------------------------------------------------------
ALTER TABLE public.profile_syntheses              ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profile_synthesis_proposals    ENABLE ROW LEVEL SECURITY;

-- --- profile_syntheses ---
DROP POLICY IF EXISTS "own_synthesis_select" ON public.profile_syntheses;
DROP POLICY IF EXISTS "own_synthesis_update" ON public.profile_syntheses;
DROP POLICY IF EXISTS "admin_synthesis_select" ON public.profile_syntheses;

-- Un user peut lire ses propres synthèses (toutes versions).
CREATE POLICY "own_synthesis_select"
  ON public.profile_syntheses FOR SELECT
  USING (auth.uid() = user_id);

-- Un user peut updater ses propres synthèses (status flip via app).
-- Les inserts passent par service_role (LLM call côté serveur) ou l'app
-- elle-même via le service_role embarqué dans le scorer / page.
CREATE POLICY "own_synthesis_update"
  ON public.profile_syntheses FOR UPDATE
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Admins lisent tout.
CREATE POLICY "admin_synthesis_select"
  ON public.profile_syntheses FOR SELECT
  USING (public.is_admin(auth.uid()));

-- --- profile_synthesis_proposals ---
DROP POLICY IF EXISTS "own_proposal_select" ON public.profile_synthesis_proposals;
DROP POLICY IF EXISTS "own_proposal_update" ON public.profile_synthesis_proposals;
DROP POLICY IF EXISTS "admin_proposal_select" ON public.profile_synthesis_proposals;

CREATE POLICY "own_proposal_select"
  ON public.profile_synthesis_proposals FOR SELECT
  USING (auth.uid() = user_id);

CREATE POLICY "own_proposal_update"
  ON public.profile_synthesis_proposals FOR UPDATE
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "admin_proposal_select"
  ON public.profile_synthesis_proposals FOR SELECT
  USING (public.is_admin(auth.uid()));

-- ---------------------------------------------------------------------
-- 5) Fonctions atomiques (activation, archivage)
-- ---------------------------------------------------------------------
-- Activer une synthèse (draft → active) ET archiver l'ancienne active
-- du même user, dans une seule transaction. Évite les race conditions
-- + les états "deux actives" qu'un appel séquentiel client pourrait
-- laisser en cas de crash entre deux UPDATE.
CREATE OR REPLACE FUNCTION public.activate_profile_synthesis(p_synthesis_id UUID)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_user_id UUID;
BEGIN
  SELECT user_id INTO v_user_id
    FROM public.profile_syntheses
    WHERE id = p_synthesis_id;

  IF v_user_id IS NULL THEN
    RAISE EXCEPTION 'profile_synthesis % introuvable', p_synthesis_id;
  END IF;

  -- Archive l'ancienne active (s'il y en a une et que ce n'est pas celle-ci).
  UPDATE public.profile_syntheses
    SET status = 'archived', archived_at = NOW()
    WHERE user_id = v_user_id
      AND status = 'active'
      AND id <> p_synthesis_id;

  -- Active la nouvelle.
  UPDATE public.profile_syntheses
    SET status = 'active', activated_at = NOW()
    WHERE id = p_synthesis_id
      AND status IN ('draft', 'archived');
END;
$$;

-- Reset doux : archive la synthèse active sans en activer de nouvelle.
-- Le client (page Mon Profil) appelle ensuite synthesize_profile() puis
-- activate_profile_synthesis() pour créer la prochaine version.
CREATE OR REPLACE FUNCTION public.archive_active_profile_synthesis(p_user_id UUID)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  UPDATE public.profile_syntheses
    SET status = 'archived', archived_at = NOW()
    WHERE user_id = p_user_id
      AND status = 'active';
END;
$$;

-- ---------------------------------------------------------------------
-- 6) VIEW active_profile_synthesis — pour scraper + scorer
-- ---------------------------------------------------------------------
-- Expose la synthèse active de chaque user. Utilisée par le scraper
-- (query_builder) et le scorer (build_system_prompt). RLS hérite du
-- caller (security_invoker). Le scraper appelle avec service_role et
-- voit donc tout le monde ; un user voit sa propre row uniquement.
CREATE OR REPLACE VIEW public.active_profile_synthesis
WITH (security_invoker = true) AS
  SELECT
    s.user_id,
    s.id            AS synthesis_id,
    s.version,
    s.synthesis,
    s.activated_at,
    s.llm_model,
    s.prompt_version
  FROM public.profile_syntheses s
  WHERE s.status = 'active';

-- ---------------------------------------------------------------------
-- 7) Mise à jour de user_matches_enriched pour exposer profile_synthesis_id
-- ---------------------------------------------------------------------
-- DROP + CREATE (Postgres refuse REPLACE quand on ajoute une colonne).
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
    m.profile_synthesis_id,
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
-- Sanity checks :
--   SELECT COUNT(*), status FROM public.profile_syntheses GROUP BY status;
--   SELECT user_id, version FROM public.profile_syntheses
--     WHERE status = 'active' ORDER BY user_id;
--   SELECT COUNT(*) FROM public.profile_synthesis_proposals WHERE status = 'pending';
-- =====================================================================
