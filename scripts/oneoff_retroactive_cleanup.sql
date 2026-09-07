-- ============================================================================
-- ONE-OFF RETROACTIVE CLEANUP - fix trades stuck by the old 400-bug monitor
--
-- WHY: the old monitor closed trades with a PATCH on `?trade_id=eq.` but the
-- trade_signals PK is `id` (no trade_id column) -> HTTP 400 every cycle ->
-- trades stayed ACTIVE and SL/trailing/target alerts looped every 15 minutes.
--
-- PRE-REQUISITE: run migrations/005_notified_events_and_close_fix.sql FIRST
-- (this script seeds notified_events - it does not exist yet, verified
--  HTTP 404 on 2026-09-07).
--
-- Run ONCE in Supabase SQL Editor, SECTION BY SECTION. Idempotent.
-- Live state verified 2026-09-07 (signal ids from prod):
--   id=7  CLHO.CA entry=18.10 sl=17.56  -> SL breached (close 17.45) -> CLOSE
--   id=10 SUGR.CA entry=56.07 t1=58.87  -> T1 hit (+7.6%)  -> keep ACTIVE, SL -> 56.35 (breakeven)
--   id=5  SKPC.CA entry=17.80 t1=18.69  -> T1 hit (+5.1%)  -> keep ACTIVE, SL -> 17.89 (breakeven)
--   JUFO/EAST/ETEL/SAUD/COMI (ids 9,8,6,4,3) -> genuinely open -> UNTOUCHED
--   TEST1.CA id=1 / TEST2.CA id=2           -> test rows -> DELETE
-- ============================================================================

-- ---------------------------------------------------------------------------
-- (0) REVIEW FIRST - run alone, inspect, then decide
-- ---------------------------------------------------------------------------
SELECT id, ticker, status, entry_price, stop_loss, current_stop_loss,
       target_1, target_2, created_at
FROM trade_signals
WHERE status IN ('ACTIVE', 'TRACKING', 'OPEN')
ORDER BY created_at DESC;

-- ---------------------------------------------------------------------------
-- (1) CLHO: stop-loss breached -> CLOSE the trade + its user_portfolio mirrors
-- ---------------------------------------------------------------------------
UPDATE trade_signals
SET status = 'CLOSED', exit_reason = 'EXIT_STOP_LOSS'
WHERE status IN ('ACTIVE', 'TRACKING', 'OPEN') AND ticker = 'CLHO.CA';

UPDATE user_portfolio
SET status = 'CLOSED'
WHERE status = 'TRACKING' AND symbol = 'CLHO.CA';

-- ---------------------------------------------------------------------------
-- (2) SUGR / SKPC: Target-1 hit -> per system rules (sell 50% + breakeven stop)
--     the trade STAYS OPEN; persist the announced breakeven stop so the
--     trailing detector never re-fires the same move.
-- ---------------------------------------------------------------------------
UPDATE trade_signals
SET current_stop_loss = 56.35, stop_loss = 56.35      -- breakeven 56.07 +0.5%
WHERE status IN ('ACTIVE', 'TRACKING', 'OPEN') AND ticker = 'SUGR.CA';

UPDATE trade_signals
SET current_stop_loss = 17.89, stop_loss = 17.89      -- breakeven 17.80 +0.5%
WHERE status IN ('ACTIVE', 'TRACKING', 'OPEN') AND ticker = 'SKPC.CA';

-- ---------------------------------------------------------------------------
-- (3) SEED notified_events so the FIXED monitor never re-announces these
--     (keys match the code: SL:{ticker}:{id} / T1:{ticker}:{id} / TRAIL:...)
-- ---------------------------------------------------------------------------
INSERT INTO notified_events (event_key, event_type, ticker, signal_id)
SELECT 'SL:' || ticker || ':' || id, 'SL_HIT', ticker, id
FROM trade_signals
WHERE ticker = 'CLHO.CA' AND status = 'CLOSED'
ON CONFLICT (event_key) DO NOTHING;

INSERT INTO notified_events (event_key, event_type, ticker, signal_id)
SELECT 'T1:' || ticker || ':' || id, 'TARGET_HIT', ticker, id
FROM trade_signals
WHERE ticker IN ('SUGR.CA', 'SKPC.CA') AND status IN ('ACTIVE', 'TRACKING', 'OPEN')
ON CONFLICT (event_key) DO NOTHING;

INSERT INTO notified_events (event_key, event_type, ticker, signal_id)
SELECT 'TRAIL:' || ticker || ':' || id || ':56.35', 'TRAILING_SL', ticker, id
FROM trade_signals
WHERE ticker = 'SUGR.CA' AND status IN ('ACTIVE', 'TRACKING', 'OPEN')
ON CONFLICT (event_key) DO NOTHING;

INSERT INTO notified_events (event_key, event_type, ticker, signal_id)
SELECT 'TRAIL:' || ticker || ':' || id || ':17.89', 'TRAILING_SL', ticker, id
FROM trade_signals
WHERE ticker = 'SKPC.CA' AND status IN ('ACTIVE', 'TRACKING', 'OPEN')
ON CONFLICT (event_key) DO NOTHING;

-- ---------------------------------------------------------------------------
-- (4) DELETE test-data rows (they 404 on yfinance and pollute every bulletin
--     and the scanner dedup)
-- ---------------------------------------------------------------------------
DELETE FROM user_portfolio
WHERE symbol IN ('TEST1.CA', 'TEST2.CA', 'TEST1', 'TEST2');

DELETE FROM trade_signals
WHERE ticker IN ('TEST1.CA', 'TEST2.CA', 'TEST1', 'TEST2');

-- ---------------------------------------------------------------------------
-- (5) VERIFY - expected: CLHO CLOSED, SUGR/SKPC ACTIVE with new stops,
--     5 seeded events, TEST rows gone, 7 active signals remaining
-- ---------------------------------------------------------------------------
SELECT id, ticker, status, exit_reason, stop_loss, current_stop_loss
FROM trade_signals
WHERE ticker IN ('CLHO.CA', 'SUGR.CA', 'SKPC.CA', 'TEST1.CA', 'TEST2.CA')
ORDER BY id DESC;

SELECT event_key, event_type FROM notified_events ORDER BY id;

SELECT status, COUNT(*) AS n FROM trade_signals GROUP BY status;
