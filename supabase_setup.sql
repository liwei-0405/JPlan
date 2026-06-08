-- JPlan fresh Supabase setup
-- Run this whole file once in a new Supabase project SQL editor.
-- This is a fresh schema setup, not a migration file.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Enums
DO $$
BEGIN
  CREATE TYPE public.user_role AS ENUM ('user', 'admin');
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
  CREATE TYPE public.user_status AS ENUM ('active', 'suspended', 'pending');
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;

-- Shared timestamp helper
CREATE OR REPLACE FUNCTION public.update_updated_at_column()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = timezone('utc'::text, now());
  RETURN NEW;
END;
$$;

-- User profiles
CREATE TABLE IF NOT EXISTS public.profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  email TEXT UNIQUE NOT NULL,
  full_name TEXT,
  avatar_url TEXT,
  role public.user_role NOT NULL DEFAULT 'user',
  status public.user_status NOT NULL DEFAULT 'pending',
  google_refresh_token TEXT,
  calendar_sync_enabled BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now())
);

-- Daily plans store the versioned ScheduleEnvelope JSON in activities.
-- The JSON contains activities, schedule_blocks, conflicts, warnings,
-- accurate_travel_time status, and event-specific resolved_location snapshots.
CREATE TABLE IF NOT EXISTS public.daily_plans (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  date DATE NOT NULL,
  activities JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  CONSTRAINT daily_plans_user_date_key UNIQUE (user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_plans_date
  ON public.daily_plans(date);

CREATE INDEX IF NOT EXISTS idx_daily_plans_user_id
  ON public.daily_plans(user_id);

-- User reusable saved locations for Accurate Travel Time.
-- Chat-confirmed one-off map pins are stored inside daily_plans.activities JSON,
-- not here, unless the user saves them from Preferences.
CREATE TABLE IF NOT EXISTS public.user_locations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  display_name TEXT,
  address TEXT NOT NULL,
  latitude DOUBLE PRECISION,
  longitude DOUBLE PRECISION,
  source TEXT NOT NULL DEFAULT 'manual',
  confirmed_by_user BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  CONSTRAINT user_locations_user_label_key UNIQUE (user_id, label)
);

CREATE INDEX IF NOT EXISTS idx_user_locations_user_id
  ON public.user_locations(user_id);

-- User-level planning preferences. Per-day overrides still live inside
-- daily_plans.activities JSON so editing one day does not change defaults.
CREATE TABLE IF NOT EXISTS public.user_preferences (
  user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  day_start_time TIME NOT NULL DEFAULT TIME '08:00',
  day_end_time TIME NOT NULL DEFAULT TIME '22:00',
  use_day_boundary_preferences BOOLEAN NOT NULL DEFAULT true,
  default_buffer_minutes INTEGER NOT NULL DEFAULT 5 CHECK (default_buffer_minutes >= 0 AND default_buffer_minutes <= 60),
  default_start_location_label TEXT,
  default_start_location JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  CONSTRAINT user_preferences_day_boundary_check CHECK (day_start_time < day_end_time)
);

ALTER TABLE public.user_preferences
  ADD COLUMN IF NOT EXISTS default_buffer_minutes INTEGER NOT NULL DEFAULT 5
  CHECK (default_buffer_minutes >= 0 AND default_buffer_minutes <= 60);

-- Cross-device recent confirmed locations. This is user history, not the
-- provider result cache below. The app keeps only the newest five per user.
CREATE TABLE IF NOT EXISTS public.user_recent_locations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  location_key TEXT NOT NULL,
  label TEXT,
  display_name TEXT,
  address TEXT,
  category TEXT,
  latitude DOUBLE PRECISION NOT NULL,
  longitude DOUBLE PRECISION NOT NULL,
  source TEXT NOT NULL DEFAULT 'recent',
  last_used_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  CONSTRAINT user_recent_locations_user_key UNIQUE (user_id, location_key)
);

CREATE INDEX IF NOT EXISTS idx_user_recent_locations_user_last_used
  ON public.user_recent_locations(user_id, last_used_at DESC);

-- Shared geocode cache for ORS/Nominatim candidate results.
-- Nullable hints are handled by the unique expression index below.
CREATE TABLE IF NOT EXISTS public.geocode_cache (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  normalized_query TEXT NOT NULL,
  provider TEXT NOT NULL,
  country_hint TEXT,
  category_hint TEXT,
  result_json JSONB NOT NULL,
  hit_count INTEGER NOT NULL DEFAULT 0,
  expires_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now()),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT timezone('utc'::text, now())
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_geocode_cache_unique_nullable
  ON public.geocode_cache (
    normalized_query,
    provider,
    COALESCE(country_hint, ''),
    COALESCE(category_hint, '')
  );

CREATE INDEX IF NOT EXISTS idx_geocode_cache_provider_query
  ON public.geocode_cache(provider, normalized_query);

CREATE INDEX IF NOT EXISTS idx_geocode_cache_expires_at
  ON public.geocode_cache(expires_at);

-- Timestamp triggers
DROP TRIGGER IF EXISTS update_profiles_updated_at ON public.profiles;
CREATE TRIGGER update_profiles_updated_at
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

DROP TRIGGER IF EXISTS update_daily_plans_updated_at ON public.daily_plans;
CREATE TRIGGER update_daily_plans_updated_at
  BEFORE UPDATE ON public.daily_plans
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

DROP TRIGGER IF EXISTS update_user_locations_updated_at ON public.user_locations;
CREATE TRIGGER update_user_locations_updated_at
  BEFORE UPDATE ON public.user_locations
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

DROP TRIGGER IF EXISTS update_user_preferences_updated_at ON public.user_preferences;
CREATE TRIGGER update_user_preferences_updated_at
  BEFORE UPDATE ON public.user_preferences
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

DROP TRIGGER IF EXISTS update_user_recent_locations_updated_at ON public.user_recent_locations;
CREATE TRIGGER update_user_recent_locations_updated_at
  BEFORE UPDATE ON public.user_recent_locations
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

DROP TRIGGER IF EXISTS update_geocode_cache_updated_at ON public.geocode_cache;
CREATE TRIGGER update_geocode_cache_updated_at
  BEFORE UPDATE ON public.geocode_cache
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at_column();

-- Auto-create a profile row when a Supabase auth user signs up.
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
  INSERT INTO public.profiles (id, email, full_name, avatar_url, role, status)
  VALUES (
    NEW.id,
    NEW.email,
    COALESCE(NEW.raw_user_meta_data->>'full_name', NEW.raw_user_meta_data->>'name', 'Unknown User'),
    COALESCE(NEW.raw_user_meta_data->>'avatar_url', NEW.raw_user_meta_data->>'picture', ''),
    CAST(COALESCE(NEW.raw_user_meta_data->>'role', 'user') AS public.user_role),
    CAST('active' AS public.user_status)
  );
  RETURN NEW;
EXCEPTION WHEN OTHERS THEN
  INSERT INTO public.profiles (id, email)
  VALUES (NEW.id, NEW.email)
  ON CONFLICT (id) DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION public.handle_new_user();

-- Row Level Security
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.daily_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_recent_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.geocode_cache ENABLE ROW LEVEL SECURITY;

-- Profiles policies
DROP POLICY IF EXISTS "Users can read their own profile" ON public.profiles;
CREATE POLICY "Users can read their own profile"
  ON public.profiles
  FOR SELECT
  USING (auth.uid() = id);

DROP POLICY IF EXISTS "Users can update their own profile" ON public.profiles;
CREATE POLICY "Users can update their own profile"
  ON public.profiles
  FOR UPDATE
  USING (auth.uid() = id)
  WITH CHECK (auth.uid() = id);

DROP POLICY IF EXISTS "Admins can read all profiles" ON public.profiles;
CREATE POLICY "Admins can read all profiles"
  ON public.profiles
  FOR SELECT
  USING ((auth.jwt() -> 'user_metadata' ->> 'role') = 'admin');

-- Daily plan policies
DROP POLICY IF EXISTS "Users can manage their own plans" ON public.daily_plans;
CREATE POLICY "Users can manage their own plans"
  ON public.daily_plans
  FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Admins can read all plans" ON public.daily_plans;
CREATE POLICY "Admins can read all plans"
  ON public.daily_plans
  FOR SELECT
  USING ((auth.jwt() -> 'user_metadata' ->> 'role') = 'admin');

-- Saved location policies
DROP POLICY IF EXISTS "Users can manage their own locations" ON public.user_locations;
CREATE POLICY "Users can manage their own locations"
  ON public.user_locations
  FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Preference policies
DROP POLICY IF EXISTS "Users can manage their own preferences" ON public.user_preferences;
CREATE POLICY "Users can manage their own preferences"
  ON public.user_preferences
  FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Recent location policies
DROP POLICY IF EXISTS "Users can manage their own recent locations" ON public.user_recent_locations;
CREATE POLICY "Users can manage their own recent locations"
  ON public.user_recent_locations
  FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Geocode cache is backend-managed through the service role.
DROP POLICY IF EXISTS "Service role can manage geocode cache" ON public.geocode_cache;
CREATE POLICY "Service role can manage geocode cache"
  ON public.geocode_cache
  FOR ALL
  USING ((auth.jwt() ->> 'role') = 'service_role')
  WITH CHECK ((auth.jwt() ->> 'role') = 'service_role');
