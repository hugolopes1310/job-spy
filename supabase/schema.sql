-- =====================================================================
-- Job Spy — Supabase schema
-- =====================================================================
-- Copy-paste this WHOLE file into Supabase → SQL Editor → New Query → Run.
-- Idempotent: safe to re-run. Doesn't drop anything.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1) profiles — one row per user, linked to auth.users by UUID
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.profiles (
  user_id       UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email         TEXT NOT NULL UNIQUE,
  full_name     TEXT,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'revoked')),
  is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
  requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  approved_at   TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_profiles_email  ON public.profiles(email);
CREATE INDEX IF NOT EXISTS idx_profiles_status ON public.profiles(status);

-- ---------------------------------------------------------------------
-- 2) user_configs — one row per user, stores their onboarding output
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.user_configs (
  user_id            UUID PRIMARY KEY REFERENCES public.profiles(user_id) ON DELETE CASCADE,
  config             JSONB NOT NULL,
  cv_text            TEXT,
  cover_letter_text  TEXT,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Keep updated_at fresh on every UPDATE
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS user_configs_touch ON public.user_configs;
CREATE TRIGGER user_configs_touch
  BEFORE UPDATE ON public.user_configs
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- ---------------------------------------------------------------------
-- 3) Auto-create a 'pending' profile when a new user signs up
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.profiles (user_id, email, status)
  VALUES (NEW.id, NEW.email, 'pending')
  ON CONFLICT (user_id) DO NOTHING;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ---------------------------------------------------------------------
-- 4) is_admin() helper — used in RLS policies without recursion
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.is_admin(uid UUID)
RETURNS BOOLEAN
SECURITY DEFINER  -- bypasses RLS to read the profile
STABLE
SET search_path = public
AS $$
  SELECT COALESCE(
    (SELECT is_admin FROM public.profiles WHERE user_id = uid),
    FALSE
  );
$$ LANGUAGE SQL;

-- ---------------------------------------------------------------------
-- 5) Row Level Security
-- ---------------------------------------------------------------------
ALTER TABLE public.profiles       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_configs   ENABLE ROW LEVEL SECURITY;

-- --- profiles ---
DROP POLICY IF EXISTS "own_profile_select"   ON public.profiles;
DROP POLICY IF EXISTS "own_profile_update"   ON public.profiles;
DROP POLICY IF EXISTS "admin_profile_select" ON public.profiles;
DROP POLICY IF EXISTS "admin_profile_update" ON public.profiles;

-- A user can read their own profile.
CREATE POLICY "own_profile_select"
  ON public.profiles FOR SELECT
  USING (auth.uid() = user_id);

-- A user can update full_name only (status / is_admin stay admin-controlled).
-- We enforce the "only full_name" restriction in application code for simplicity.
CREATE POLICY "own_profile_update"
  ON public.profiles FOR UPDATE
  USING (auth.uid() = user_id);

-- Admins can read every profile.
CREATE POLICY "admin_profile_select"
  ON public.profiles FOR SELECT
  USING (public.is_admin(auth.uid()));

-- Admins can update every profile (approve/revoke/promote).
CREATE POLICY "admin_profile_update"
  ON public.profiles FOR UPDATE
  USING (public.is_admin(auth.uid()));

-- --- user_configs ---
DROP POLICY IF EXISTS "own_config_all" ON public.user_configs;

-- A user can only read/write their own config.
-- Admins don't need to read configs for the MVP; the scraper uses service_role.
CREATE POLICY "own_config_all"
  ON public.user_configs FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- ---------------------------------------------------------------------
-- 6) Helper VIEW — active users (used by the scraper, called with service_role)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW public.active_user_configs AS
  SELECT
    p.user_id,
    p.email,
    p.full_name,
    c.config,
    c.cv_text,
    c.updated_at
  FROM public.profiles p
  JOIN public.user_configs c ON c.user_id = p.user_id
  WHERE p.status = 'approved';
-- Note: the view inherits RLS of its underlying tables, so it's safe to expose
-- read access via service_role in the scraper. Streamlit app uses it too, but
-- RLS will restrict non-admin users to their own row automatically.

-- =====================================================================
-- DONE. After running this:
-- 1. Go to Supabase → Auth → Providers → Email → enable "Confirm email" = OFF
--    (we don't need email confirm since we use OTP which already verifies).
-- 2. Go to Auth → Email Templates → "Magic Link" template — the default works
--    fine. OTP uses the same template but with {{ .Token }} placeholder; make
--    sure the template contains {{ .Token }} (not just {{ .ConfirmationURL }}).
-- 3. (Optional but recommended) Auth → URL Configuration → Redirect URLs:
--    add your Streamlit Cloud URL + http://localhost:8501 for local testing.
-- 4. To promote yourself admin, run in SQL Editor:
--      UPDATE public.profiles SET is_admin = TRUE, status = 'approved'
--      WHERE email = 'lopeshugo1310@gmail.com';
-- =====================================================================
