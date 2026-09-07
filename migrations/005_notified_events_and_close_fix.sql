-- ============================================================
-- 005: notified_events idempotency store + user_portfolio status fix
--
-- Context: the trade monitor's SL-close PATCH filtered on a non-existent
-- `trade_id` column (live schema PK is `id`), so HTTP 400 kept trades stuck
-- ACTIVE and re-announced SL / trailing / target alerts every cycle.
-- The old target dedup was date-scoped (sent_alerts.date_sent), so the same
-- target re-fired on every new day.
--
-- This migration adds:
--   1. notified_events - universal claim-first idempotency store
--      (event_key UNIQUE -> INSERT conflicts = already notified)
--   2. user_portfolio status constraint that allows 'CLOSED'
--
-- Run ONCE in Supabase SQL Editor. Idempotent (IF NOT EXISTS / IF EXISTS).
-- ============================================================

-- (1) Idempotency / claim store ------------------------------------------------
CREATE TABLE IF NOT EXISTS public.notified_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_key text NOT NULL UNIQUE,
    event_type text NOT NULL,            -- SL_HIT | TARGET_HIT | TRAILING_SL | ADMIN_ALERT
    ticker text NOT NULL,
    signal_id bigint,
    payload jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now()
);
ALTER TABLE public.notified_events DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.notified_events TO service_role;
GRANT ALL ON TABLE public.notified_events TO anon;
GRANT ALL ON TABLE public.notified_events TO authenticated;
CREATE INDEX IF NOT EXISTS idx_notified_events_ticker_type
    ON public.notified_events(ticker, event_type);

-- (2) Allow 'CLOSED' on user_portfolio (legacy constraint: TRACKING/EXITED) ----
ALTER TABLE public.user_portfolio DROP CONSTRAINT IF EXISTS user_portfolio_status_check;
ALTER TABLE public.user_portfolio
    ADD CONSTRAINT user_portfolio_status_check
    CHECK (status IN ('TRACKING', 'EXITED', 'CLOSED'));

-- Verify:
-- SELECT * FROM notified_events LIMIT 5;
-- SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--  WHERE conrelid = 'public.user_portfolio'::regclass AND conname = 'user_portfolio_status_check';
