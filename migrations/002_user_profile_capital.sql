-- ============================================================
-- Migration 002: Per-user profile capital (/set_capital)
-- Stores each user's individual portfolio capital.
-- Run in Supabase SQL Editor (service_role) once.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.user_profile (
    user_id TEXT PRIMARY KEY,
    capital NUMERIC,
    initial_capital NUMERIC,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE IF EXISTS public.user_profile DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.user_profile TO anon;
GRANT ALL ON TABLE public.user_profile TO authenticated;
GRANT ALL ON TABLE public.user_profile TO service_role;

-- Verify
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='user_profile' AND table_schema='public'
ORDER BY ordinal_position;
