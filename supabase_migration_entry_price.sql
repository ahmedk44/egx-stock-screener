-- Migration: Add custom entry price tracking to user_portfolio
-- Required for precise P&L calculation per user's actual entry vs signal entry
-- Run in Supabase SQL Editor (service_role) once.

-- Add entry_price and joined_at_price columns (numeric) to user_portfolio if not exists
ALTER TABLE public.user_portfolio
  ADD COLUMN IF NOT EXISTS entry_price numeric,
  ADD COLUMN IF NOT EXISTS joined_at_price numeric;

-- Optional: add check to allow CLOSED status alongside TRACKING/EXITED
-- Existing constraint is CHECK (status IN ('TRACKING','EXITED')) - extend to allow CLOSED
DO $$
BEGIN
  -- Drop existing check if present and recreate with CLOSED
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'user_portfolio_status_check'
      AND conrelid = 'public.user_portfolio'::regclass
  ) THEN
    ALTER TABLE public.user_portfolio DROP CONSTRAINT user_portfolio_status_check;
  END IF;
  ALTER TABLE public.user_portfolio
    ADD CONSTRAINT user_portfolio_status_check
    CHECK (status IN ('TRACKING','EXITED','CLOSED'));
EXCEPTION WHEN OTHERS THEN
  -- Fallback: ignore if constraint recreation fails due to existing data
  RAISE NOTICE 'Could not recreate status check: %', SQLERRM;
END $$;

-- Index for faster lookup by user_id + symbol (already UNIQUE) and trade_id
CREATE INDEX IF NOT EXISTS idx_user_portfolio_trade_id ON public.user_portfolio(trade_id);
CREATE INDEX IF NOT EXISTS idx_user_portfolio_user ON public.user_portfolio(user_id);

-- Verify
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name='user_portfolio' AND table_schema='public'
ORDER BY ordinal_position;
