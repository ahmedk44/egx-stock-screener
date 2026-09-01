-- ============================================================
-- Migration 003: Comprehensive schema fix (capital engine + allocation)
-- Run in Supabase SQL Editor (service_role) once. Idempotent.
--
-- Semantics:
--   user_profile.total_deposits : cumulative cash injected (/set_capital seeds it,
--                                 /add_capital increments it). ROI denominator.
--   user_portfolio.allocation_pct : share of working capital allocated to the trade
--                                 at join. NEVER altered by /exit.
--   user_portfolio.capital_at_join : snapshot of user_profile.capital when the user
--                                 joined - makes allocated capital deterministic.
--   user_portfolio.remaining_qty_pct : remaining portion of the position itself.
--                                 ALWAYS 100 on entry, reduced ONLY by /exit.
-- ============================================================

-- 1) user_profile: cumulative deposits column
ALTER TABLE IF EXISTS public.user_profile
  ADD COLUMN IF NOT EXISTS total_deposits NUMERIC;

-- Backfill: existing profiles treat current capital as deposited so far
UPDATE public.user_profile
SET total_deposits = COALESCE(capital, initial_capital)
WHERE total_deposits IS NULL;

-- 2) user_portfolio: allocation vs remaining disambiguation
ALTER TABLE IF EXISTS public.user_portfolio
  ADD COLUMN IF NOT EXISTS allocation_pct NUMERIC NOT NULL DEFAULT 100;
ALTER TABLE IF EXISTS public.user_portfolio
  ADD COLUMN IF NOT EXISTS capital_at_join NUMERIC;

-- Normalize legacy rows: any NULL allocation counts as fully allocated
UPDATE public.user_portfolio
SET allocation_pct = 100
WHERE allocation_pct IS NULL;

-- 3) Grants (repo convention)
GRANT ALL ON TABLE public.user_profile TO anon;
GRANT ALL ON TABLE public.user_profile TO authenticated;
GRANT ALL ON TABLE public.user_profile TO service_role;
GRANT ALL ON TABLE public.user_portfolio TO anon;
GRANT ALL ON TABLE public.user_portfolio TO authenticated;
GRANT ALL ON TABLE public.user_portfolio TO service_role;

-- Verify
SELECT column_name, data_type, column_default FROM information_schema.columns
WHERE table_schema='public' AND table_name IN ('user_profile','user_portfolio')
ORDER BY table_name, ordinal_position;
