-- ============================================================
-- Migration 004: Interactive command state (pending amount step)
-- Lets parameter-less /set_capital and /add_capital ask the user
-- for the amount and consume it from their next plain message.
-- Run in Supabase SQL Editor (service_role) once. Idempotent.
-- ============================================================

ALTER TABLE IF EXISTS public.user_profile
  ADD COLUMN IF NOT EXISTS pending_action TEXT;

GRANT ALL ON TABLE public.user_profile TO anon;
GRANT ALL ON TABLE public.user_profile TO authenticated;
GRANT ALL ON TABLE public.user_profile TO service_role;

-- Verify
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='user_profile' AND table_schema='public'
ORDER BY ordinal_position;
