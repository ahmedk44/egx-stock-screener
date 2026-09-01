CREATE TABLE IF NOT EXISTS public.sent_alerts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker text NOT NULL,
    strategy text NOT NULL,
    date_sent date NOT NULL,
    entry_price numeric,
    current_stop_loss numeric,
    target_1 numeric,
    target_2 numeric,
    target_3 numeric,
    created_at timestamp with time zone DEFAULT now()
);
ALTER TABLE public.sent_alerts DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.active_positions DISABLE ROW LEVEL SECURITY;

-- Multi-tenant opt-in registry: one row per user per joined trade.
-- Written by the Vercel webhook when a user taps
-- [ 📥 انضم للصفقة | Track Signal ] in any public channel.
CREATE TABLE IF NOT EXISTS public.user_portfolio (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id text NOT NULL,
    trade_id bigint NOT NULL DEFAULT 0,
    symbol text NOT NULL,
    joined_at timestamp with time zone NOT NULL DEFAULT now(),
    snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'TRACKING' CHECK (status IN ('TRACKING', 'EXITED')),
    CONSTRAINT user_portfolio_user_symbol_unique UNIQUE (user_id, symbol)
);
ALTER TABLE public.user_portfolio DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.user_portfolio TO anon;
GRANT ALL ON TABLE public.user_portfolio TO authenticated;
GRANT ALL ON TABLE public.user_portfolio TO service_role;

-- ============================================================
-- closed_positions: archive of all closed/exited trades
-- Written by _archive_closed_position() on every /exit or exit_confirm.
-- Read by /stats and /weekly for realized PnL aggregation.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.closed_positions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id text NOT NULL,
    symbol text NOT NULL,
    trade_id bigint NOT NULL DEFAULT 0,
    entry_price numeric,
    exit_price numeric,
    quantity_percentage integer NOT NULL DEFAULT 100,
    realized_pnl numeric DEFAULT 0,
    realized_pnl_pct numeric DEFAULT 0,
    close_reason text DEFAULT 'Manual Exit',
    closed_at timestamp with time zone DEFAULT now()
);
ALTER TABLE public.closed_positions DISABLE ROW LEVEL SECURITY;
GRANT ALL ON TABLE public.closed_positions TO anon;
GRANT ALL ON TABLE public.closed_positions TO authenticated;
GRANT ALL ON TABLE public.closed_positions TO service_role;

CREATE INDEX IF NOT EXISTS idx_closed_positions_user_closed ON public.closed_positions(user_id, closed_at DESC);
