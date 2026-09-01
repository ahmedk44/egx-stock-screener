-- Migration: Track remaining position percentage for strict partial-exit validation
-- Required for:
--   * /exit <TICKER> <PRICE> <QTY%> rejection when QTY% > remaining_qty_pct
--   * Automatic status='CLOSED' + remaining_qty_pct=0 on full exits
--   * Blocking /exit on already-closed trades
-- Run in Supabase SQL Editor (service_role) once.

-- Add remaining_qty_pct column (percent of ORIGINAL position still open, default 100)
ALTER TABLE public.user_portfolio
  ADD COLUMN IF NOT EXISTS remaining_qty_pct numeric NOT NULL DEFAULT 100;

-- Ensure status CHECK allows 'CLOSED' alongside legacy 'TRACKING'/'EXITED'
DO $$
BEGIN
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
  RAISE NOTICE 'Could not recreate status check: %', SQLERRM;
END $$;

-- Standardize legacy fully-closed rows: EXITED -> CLOSED, remaining -> 0
UPDATE public.user_portfolio
SET status = 'CLOSED', remaining_qty_pct = 0
WHERE status = 'EXITED';

GRANT ALL ON TABLE public.user_portfolio TO anon;
GRANT ALL ON TABLE public.user_portfolio TO authenticated;
GRANT ALL ON TABLE public.user_portfolio TO service_role;

-- Verify
SELECT column_name, data_type, column_default FROM information_schema.columns
WHERE table_name='user_portfolio' AND table_schema='public'
ORDER BY ordinal_position;
