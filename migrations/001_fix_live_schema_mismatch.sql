-- ============================================================
-- Migration 001: Fix live Supabase schema mismatch
-- Syncs the live Supabase schema with the Python codebase.
-- Run in Supabase SQL Editor (service_role) once.
--
-- STATUS: EXECUTED & VERIFIED on live Supabase (2026-08-31).
-- Live schema after execution (introspected via PostgREST):
--   user_portfolio   : + entry_price, joined_at_price, snapshot
--   closed_positions : qty_pct, exit_reason, realized_pnl_pct, closed_at
--                      (NO realized_pnl column - code must not write it)
--   trade_signals    : + status, exit_reason, current_stop_loss
--   news_publish_log : table created (bulletin_type, publish_date, news_hash, ...)
-- ============================================================

-- ============================================================
-- 1. user_portfolio: Add missing columns
--   entry_price, joined_at_price, snapshot
-- ============================================================
ALTER TABLE IF EXISTS public.user_portfolio ADD COLUMN IF NOT EXISTS entry_price NUMERIC;
ALTER TABLE IF EXISTS public.user_portfolio ADD COLUMN IF NOT EXISTS joined_at_price NUMERIC;
ALTER TABLE IF EXISTS public.user_portfolio ADD COLUMN IF NOT EXISTS snapshot JSONB DEFAULT '{}'::jsonb;

-- Extend status CHECK to include 'CLOSED' (required by /exit flow)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'user_portfolio_status_check'
          AND conrelid = 'public.user_portfolio'::regclass
    ) THEN
        ALTER TABLE public.user_portfolio DROP CONSTRAINT user_portfolio_status_check;
        ALTER TABLE public.user_portfolio
            ADD CONSTRAINT user_portfolio_status_check
            CHECK (status IN ('TRACKING', 'EXITED', 'CLOSED'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_user_portfolio_trade_id ON public.user_portfolio(trade_id);
CREATE INDEX IF NOT EXISTS idx_user_portfolio_user ON public.user_portfolio(user_id);

GRANT ALL ON TABLE public.user_portfolio TO anon;
GRANT ALL ON TABLE public.user_portfolio TO authenticated;
GRANT ALL ON TABLE public.user_portfolio TO service_role;

-- ============================================================
-- 2. closed_positions: Align column names & types
--   quantity_percentage -> qty_pct NUMERIC
--   close_reason -> exit_reason TEXT
--   realized_pnl_pct NUMERIC, closed_at TIMESTAMPTZ
-- ============================================================
DO $$
BEGIN
    -- Rename quantity_percentage -> qty_pct
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'closed_positions'
          AND column_name = 'quantity_percentage'
    ) THEN
        ALTER TABLE public.closed_positions RENAME COLUMN quantity_percentage TO qty_pct;
        ALTER TABLE public.closed_positions ALTER COLUMN qty_pct TYPE NUMERIC USING qty_pct::NUMERIC;
    END IF;

    -- Rename close_reason -> exit_reason
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'closed_positions'
          AND column_name = 'close_reason'
    ) THEN
        ALTER TABLE public.closed_positions RENAME COLUMN close_reason TO exit_reason;
        ALTER TABLE public.closed_positions ALTER COLUMN exit_reason TYPE TEXT USING exit_reason::TEXT;
    END IF;
END $$;

-- Ensure realized_pnl_pct exists (NUMERIC)
ALTER TABLE IF EXISTS public.closed_positions ADD COLUMN IF NOT EXISTS realized_pnl_pct NUMERIC;

-- Ensure closed_at exists (TIMESTAMPTZ)
ALTER TABLE IF EXISTS public.closed_positions ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE IF EXISTS public.closed_positions DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.closed_positions TO anon;
GRANT ALL ON TABLE public.closed_positions TO authenticated;
GRANT ALL ON TABLE public.closed_positions TO service_role;

CREATE INDEX IF NOT EXISTS idx_closed_positions_user_closed ON public.closed_positions(user_id, closed_at DESC);

-- ============================================================
-- 3. trade_signals: Add missing status tracking columns
--   status, exit_reason, current_stop_loss
-- ============================================================
ALTER TABLE IF EXISTS public.trade_signals ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'ACTIVE';
ALTER TABLE IF EXISTS public.trade_signals ADD COLUMN IF NOT EXISTS exit_reason TEXT;
ALTER TABLE IF EXISTS public.trade_signals ADD COLUMN IF NOT EXISTS current_stop_loss NUMERIC;

GRANT ALL ON TABLE public.trade_signals TO service_role;

-- ============================================================
-- 4. news_publish_log: Create table if not exists, add missing columns
-- ============================================================
CREATE TABLE IF NOT EXISTS public.news_publish_log (
    id SERIAL PRIMARY KEY,
    bulletin_type TEXT NOT NULL,
    publish_date DATE NOT NULL,
    news_hash TEXT UNIQUE,
    published_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE IF EXISTS public.news_publish_log ADD COLUMN IF NOT EXISTS news_hash TEXT UNIQUE;
ALTER TABLE IF EXISTS public.news_publish_log ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_news_publish_log_type_date ON public.news_publish_log (bulletin_type, publish_date);

ALTER TABLE IF EXISTS public.news_publish_log DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.news_publish_log TO anon;
GRANT ALL ON TABLE public.news_publish_log TO authenticated;
GRANT ALL ON TABLE public.news_publish_log TO service_role;