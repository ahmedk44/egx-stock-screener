ALTER TABLE public.active_positions DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.active_positions TO anon;
GRANT ALL ON TABLE public.active_positions TO authenticated;
GRANT ALL ON TABLE public.active_positions TO service_role;

-- Multi-tenant opt-in registry (see setup_db.sql for full DDL).
ALTER TABLE IF EXISTS public.user_portfolio DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.user_portfolio TO anon;
GRANT ALL ON TABLE public.user_portfolio TO authenticated;
GRANT ALL ON TABLE public.user_portfolio TO service_role;

-- ============================================================
-- Phase 4.2: Interactive multi-tenant join system
-- ============================================================
CREATE TABLE IF NOT EXISTS trade_signals (
    trade_id       BIGINT PRIMARY KEY,
    symbol         TEXT NOT NULL,
    ticker_bare    TEXT NOT NULL,
    entry_price    DOUBLE PRECISION NOT NULL,
    stop_loss      DOUBLE PRECISION NOT NULL,
    target_1       DOUBLE PRECISION,
    target_2       DOUBLE PRECISION,
    target_3       DOUBLE PRECISION,
    tqi            DOUBLE PRECISION DEFAULT 5.0,
    quantity       INTEGER,
    allocated_cost DOUBLE PRECISION,
    risk_amount    DOUBLE PRECISION,
    shariah_status TEXT DEFAULT 'COMPLIANT',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_portfolio (
    user_id     TEXT NOT NULL,
    trade_id    BIGINT NOT NULL REFERENCES trade_signals(trade_id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    ticker_bare TEXT,
    tqi         DOUBLE PRECISION,
    joined_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, trade_id)
);

GRANT ALL ON TABLE public.trade_signals TO service_role;
GRANT ALL ON TABLE public.user_portfolio TO service_role;
