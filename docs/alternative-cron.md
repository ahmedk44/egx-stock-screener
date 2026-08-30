# Alternative Cron Trigger — Direct Webhooks (Eliminate GitHub Delays)

> **Migration complete:** All scheduled bulletins & scanners now run via Vercel Cron / cron-job.org, not GitHub `schedule` (which caused 4.5h queue delays). GitHub workflows retain `workflow_dispatch` for manual runs only.

## Endpoints (All Accept `CRON_SECRET` via `Authorization: Bearer` OR `?secret=`)

| Job | Vercel Cron | Endpoint | Schedule (UTC) | Local Time |
|-----|-------------|----------|----------------|------------|
| Pre-Market | `30 5 * * 0-4` | `GET /api/cron/pre_market` | 05:30 UTC Sun-Thu | 08:30 Cairo / 09:30 Oman — FIRST |
| Live Scanner | `*/15 7-11 * * 0-4` | `GET /api/cron/scanner` | Every 15m 07:00-11:30 UTC Sun-Thu | 10:00-14:30 Cairo / 11:00-15:30 Oman — Active Session |
| Post-Market | `30 12 * * 0-4` | `GET /api/cron/post_market` | 12:30 UTC Sun-Thu | 15:30 Cairo / 16:30 Oman — FINAL |

## Problem — Post-Market Example (12:30 UTC)

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
"crons": [
  { "path": "/api/cron/pre_market", "schedule": "30 5 * * 0-4" },
  { "path": "/api/cron/scanner", "schedule": "*/15 7-11 * * 0-4" },
  { "path": "/api/cron/post_market", "schedule": "30 12 * * 0-4" }
]
```

*   Vercel's scheduler triggers each `GET https://<your-app>.vercel.app/api/cron/<job>` at its `schedule` on Vercel's edge (not GitHub's queue).
*   Handlers `api/cron/pre_market.py`, `api/cron/scanner.py`, `api/cron/post_market.py` run their pipelines synchronously, re-use the **60-minute stale guard** for post-market (late banner `⚠️ Late Run` appended if `delay>60m` or `past 14:00 UTC`), and return `{ok, delay_minutes, is_stale}`.
*   Auth: set `CRON_SECRET` in Vercel env. Accepts `Authorization: Bearer <CRON_SECRET>` **or** query `?secret=<CRON_SECRET>` / `?cron_secret=` / `?token=` (all three endpoints). Vercel sends `x-vercel-cron: 1` automatically.

Deploy:

```bash
vercel --prod
vercel env add CRON_SECRET  # optional but recommended — then use header OR ?secret=
```

Verify (header or query — both work):

```bash
curl -i https://<app>.vercel.app/api/cron/post_market -H "Authorization: Bearer $CRON_SECRET"
curl -i "https://<app>.vercel.app/api/cron/pre_market?secret=$CRON_SECRET"
curl -i "https://<app>.vercel.app/api/cron/scanner?secret=$CRON_SECRET"
```

If you keep both triggers, the Python **idempotency guard** (`news_publish_log` unique `bulletin_type+publish_date`) ensures only the first writer publishes — the second run exits `Idempotent — Already published today` (no duplicate Telegram).

## Solution B — External Lightweight Ping (cron-job.org / easycron) — Helper Script

**Automated via helper (recommended):**

```bash
# Preview what will be created:
python scripts/setup_cronjobs.py --dry-run --base-url https://egx-stock-screener.vercel.app

# Verify endpoints (header + ?secret=):
CRON_SECRET=xxx python scripts/setup_cronjobs.py --verify-only

# Create 3 jobs on cron-job.org (needs CRONJOB_API_KEY from https://cron-job.org/en/members/settings/):
CRONJOB_API_KEY=yyy CRON_SECRET=xxx python scripts/setup_cronjobs.py --create
```

The helper creates:

*   `EGX Pre-Market 05:30 UTC` → `GET https://.../api/cron/pre_market?secret=xxx` — `30 5 * * 0-4`
*   `EGX Live Scanner */15` → `GET https://.../api/cron/scanner?secret=xxx` — `*/15 7-11 * * 0-4`
*   `EGX Post-Market 12:30 UTC` → `GET https://.../api/cron/post_market?secret=xxx` — `30 12 * * 0-4`

**Manual cron-job.org steps (if not using script):**

1.  Create account → *Create Cronjob*
2.  Titles & URLs (use query-string auth — no custom header needed):
    *   `EGX Pre-Market 05:30 UTC` → `https://<app>.vercel.app/api/cron/pre_market?secret=<CRON_SECRET>` — `30 5 * * 0-4`
    *   `EGX Live Scanner 07:00-11:30` → `https://<app>.vercel.app/api/cron/scanner?secret=<CRON_SECRET>` — `*/15 7-11 * * 0-4`
    *   `EGX Post-Market 12:30 UTC` → `https://<app>.vercel.app/api/cron/post_market?secret=<CRON_SECRET>` — `30 12 * * 0-4`
    *   Alternative: trigger GitHub directly via workflow_dispatch:
        `POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/post_market.yml/dispatches`
        with `Authorization: Bearer <GH_PAT>` and `{"ref":"main"}` — but Vercel endpoint is lighter.
3.  Schedule: set Hours/Minutes/Weekdays as above, Timezone **UTC**, Weekdays `0,1,2,3,4` (Sun-Thu).
4.  Alternatively add header: `Authorization: Bearer <CRON_SECRET>` + plain URL (both work).
5.  Enable `Fail on HTTP != 200` alert so you get email if Telegram fails.
6.  Save → Test Run → expect `{"ok":true,"delay_minutes":0}`.

**easycron / healthchecks.io** — identical URL + `?secret=` query.

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
