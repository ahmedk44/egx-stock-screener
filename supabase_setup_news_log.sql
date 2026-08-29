-- Supabase setup for idempotent news publishing
-- Table: public.news_publish_log
-- Tracks daily bulletins to prevent duplicate posts to EGX News & Market Summaries channel

CREATE TABLE IF NOT EXISTS public.news_publish_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bulletin_type TEXT NOT NULL CHECK (bulletin_type IN ('PRE_MARKET', 'POST_MARKET')),
    publish_date DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(bulletin_type, publish_date)
);

-- Disable RLS for service_role access (consistent with other tables)
ALTER TABLE public.news_publish_log DISABLE ROW LEVEL SECURITY;

-- Grant permissions
GRANT ALL ON TABLE public.news_publish_log TO anon;
GRANT ALL ON TABLE public.news_publish_log TO authenticated;
GRANT ALL ON TABLE public.news_publish_log TO service_role;

-- Index for fast lookup
CREATE INDEX IF NOT EXISTS idx_news_publish_log_type_date ON public.news_publish_log (bulletin_type, publish_date);

-- Verification query
-- SELECT * FROM public.news_publish_log ORDER BY publish_date DESC, bulletin_type;
