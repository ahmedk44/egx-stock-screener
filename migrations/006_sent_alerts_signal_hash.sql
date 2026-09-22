-- ============================================================
-- 006: sent_alerts smart-dedup fingerprint (signal_hash)
--
-- Context: main.py writes `signal_hash` on every sent_alert insert and
-- queries it for exact-duplicate suppression (is_exact_duplicate_signal).
-- Without this column the insert fails (logged, graceful) and dedup silently
-- degrades to the coarse ticker/strategy/date check — scalps in particular
-- lose their per-setup resignal behavior.
--
-- Run ONCE in Supabase SQL Editor. Idempotent (IF NOT EXISTS).
-- ============================================================

ALTER TABLE IF EXISTS public.sent_alerts
    ADD COLUMN IF NOT EXISTS signal_hash TEXT;

CREATE INDEX IF NOT EXISTS idx_sent_alerts_signal_hash_date
    ON public.sent_alerts(signal_hash, date_sent);
