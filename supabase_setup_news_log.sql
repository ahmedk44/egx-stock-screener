-- Supabase setup for idempotent news publishing
-- Table: public.news_publish_log
-- Tracks daily bulletins to prevent duplicate posts to EGX News & Market Summaries channel

-- Initial creation (supports PRE_MARKET, POST_MARKET, WEEKLY)
CREATE TABLE IF NOT EXISTS public.news_publish_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bulletin_type TEXT NOT NULL CHECK (bulletin_type IN ('PRE_MARKET', 'POST_MARKET', 'WEEKLY')),
    publish_date DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(bulletin_type, publish_date)
);

-- Migration: if table already exists with old CHECK (only PRE/POST), update to include WEEKLY
DO $$
BEGIN
    -- Drop old check constraint if exists and recreate with WEEKLY
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname LIKE '%news_publish_log%check%' AND conrelid = 'public.news_publish_log'::regclass
    ) THEN
        -- Find and drop existing check
        EXECUTE (
            SELECT 'ALTER TABLE public.news_publish_log DROP CONSTRAINT ' || quote_ident(conname)
            FROM pg_constraint
            WHERE conrelid = 'public.news_publish_log'::regclass AND contype = 'c'
            LIMIT 1
        );
    END IF;
    -- Add new constraint if not exists
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'news_publish_log_bulletin_type_check' AND conrelid = 'public.news_publish_log'::regclass
    ) THEN
        ALTER TABLE public.news_publish_log
        ADD CONSTRAINT news_publish_log_bulletin_type_check
        CHECK (bulletin_type IN ('PRE_MARKET', 'POST_MARKET', 'WEEKLY'));
    END IF;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Migration for WEEKLY bulletin_type failed: %', SQLERRM;
END $$;

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
