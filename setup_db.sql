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
