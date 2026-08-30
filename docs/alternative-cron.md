# Alternative Cron Trigger — Post-Market Bulletin (12:30 UTC)

## Problem

GitHub Actions `schedule` cron runs on public `ubuntu-latest` runners.

*   `cron` syntax is evaluated in **UTC**
*   GitHub docs state scheduled workflows may be **delayed up to several hours** when runners are under heavy load. The `Created at` (queued) vs `Started at` (runner picked) gap is visible in the Actions run header. Observed: `Created 12:31 UTC → Started 16:48 UTC → Telegram 16:54 UTC` = **~4.5h queue delay** (12:30 UTC task arrived 16:54 UTC / 20:54 Oman).

This is **not** a `crontab` syntax error — `post_market.yml` is clean (`30 12 * * 0-4 → 12:30 UTC = 15:30 Cairo / 16:30 Oman Sun-Thu`). No retry logic or `timeout-minutes` caused the slip; pure public-runner queuing.

How to audit:

1.  GitHub → Actions → *Post-Market News Scan* → click the delayed run (e.g. 2025-08-28)
2.  Top-right header: `Created 12:31 UTC` (when cron enqueued) vs `Started 16:48 UTC` (when VM started). Delta = queue delay.
3.  Job logs first line now prints `Runner UTC now: ...` vs `Scheduled: 12:30 UTC`.

## Solution A — Vercel Cron (Recommended, Zero-Cost, Already Wired)

`vercel.json` now declares:

```json
"crons": [{ "path": "/api/cron/post_market", "schedule": "30 12 * * 0-4" }]
```

*   Vercel's scheduler triggers `GET https://<your-app>.vercel.app/api/cron/post_market` at **12:30 UTC Sun-Thu** on Vercel's edge (not GitHub's queue).
*   Handler `api/cron/post_market.py` runs `egx_quant/news/post_market_summary.py` synchronously, re-uses the **60-minute stale guard** (late banner `⚠️ Late Run` appended if `delay>60m` or `past 14:00 UTC`), and returns `{ok, delay_minutes, is_stale}`.
*   Auth: set `CRON_SECRET` in Vercel env. Vercel sends `x-vercel-cron: 1` automatically; external callers must send `Authorization: Bearer <CRON_SECRET>`.

Deploy:

```bash
vercel --prod
vercel env add CRON_SECRET  # optional but recommended
```

Verify: `curl -i https://<app>.vercel.app/api/cron/post_market -H "Authorization: Bearer $CRON_SECRET"`

If you keep both triggers, the Python **idempotency guard** (`news_publish_log` unique `bulletin_type+publish_date`) ensures only the first writer publishes — the second run exits `Idempotent — Already published today` (no duplicate Telegram).

## Solution B — External Lightweight Ping (cron-job.org / easycron)

If Vercel Cron is not desired, use any external cron that does a single `GET`:

**cron-job.org steps:**

1.  Create account → *Create Cronjob*
2.  Title: `EGX Post-Market 12:30 UTC`
3.  URL: `https://<your-app>.vercel.app/api/cron/post_market`
    *   Alternative: trigger GitHub directly via workflow_dispatch:
        `POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/post_market.yml/dispatches`
        with `Authorization: Bearer <GH_PAT>` and `{"ref":"main"}` — but Vercel endpoint is lighter.
4.  Schedule: `30 12 * * 0-4`  (or use *Every 5 minutes* expression `30 12 * * 0,1,2,3,4`)
5.  Advanced → Headers → Add: `Authorization: Bearer <CRON_SECRET>` (if you set `CRON_SECRET`)
6.  Enable `Fail on HTTP != 200` alert so you get email if Telegram fails.
7.  Save → Test Run → expect `{"ok":true,"delay_minutes":0}`.

**easycron / healthchecks.io** — identical URL + Bearer header.

Cost: free tier (cron-job.org allows 50 jobs at 1-min granularity). No runner queue — request hits Vercel serverless in <100 ms.

## Fallback Behavior in Code

`egx_quant/news/post_market_summary.py` now contains a **Time-Window Check**:

*   `scheduled = 12:30 UTC` (most recent Sun-Thu)
*   `delay = now_utc - scheduled`
*   If `delay > 60m` **or** `now_utc ≥ 14:00 UTC` → `is_stale=True`
*   On stale: `format_late_banner()` prepended to Telegram card (`⏰ تأخر التنفيذ 4h24m — late-run indicator`), `trigger_stale_retry_alert()` logs `[STALE-ALERT]` and optionally DMs admin (`ADMIN_USER_IDS`). If `STRICT_STALE_ABORT=1` env is set, the run exits `2` without Telegram (so external cron can retry without duplicate).

This guards against silently sending a stale session report late at night.

## Recommended Production Setup

*   Keep **both** GitHub cron **and** Vercel cron enabled. Idempotency makes this safe; whichever fires first at 12:30 UTC wins, the other becomes a no-op. Redundancy mitigates GitHub queue spikes.
*   **Or** disable GitHub schedule and rely solely on Vercel Cron / cron-job.org for strict SLA.
*   Monitor: Add a health check that alerts if no `POST_MARKET` row appears in `news_publish_log` by `14:00 UTC` (i.e., 90 min after window).

## Verification After Deploy

```bash
# Local stale simulation (inject fake now):
GITHUB_EVENT_NAME=schedule python -c "from egx_quant.news.post_market_summary import check_execution_window; print(check_execution_window())"
# Should be not stale when run near 12:30 UTC, stale after 14:00 UTC.

# Dry-run with banner:
python -m egx_quant.news.post_market_summary --dry-run
```

Check Telegram: late runs now carry `⚠️ تنبيه تأخر التنفيذ` header — if you see it repeatedly, switch primary trigger to Vercel/cron-job.org.
