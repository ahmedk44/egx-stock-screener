-- cleanup_stale_signals.sql
-- Run this in Supabase SQL Editor to expire stale ACTIVE/TRACKING signals
-- that are blocking new signal generation via the dedup gate.

-- Step 1: Preview what will be expired (run first to verify)
SELECT id, ticker, status, created_at, entry_price
FROM trade_signals
WHERE status IN ('ACTIVE', 'TRACKING', 'OPEN')
  AND created_at < NOW() - INTERVAL '3 days'
ORDER BY created_at DESC;

-- Step 2: Expire stale signals (uncomment and run after preview)
-- NOTE: status-only (trade_signals has no expired_at column).
-- UPDATE trade_signals
-- SET status = 'EXPIRED'
-- WHERE status IN ('ACTIVE', 'TRACKING', 'OPEN')
--   AND created_at < NOW() - INTERVAL '3 days';

-- Step 3: Verify results (run after update)
-- SELECT status, COUNT(*) as count
-- FROM trade_signals
-- GROUP BY status
-- ORDER BY count DESC;
