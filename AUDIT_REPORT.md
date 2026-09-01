# EGX Stock Screener — Comprehensive Diagnostic Audit Report

**Date:** 2026-08-31 | **Scope:** Full repo (api/, egx_quant/, scripts/, vercel.json, .github/, SQL) | **Mode:** Read-only audit, no fixes applied
**Live DB verified:** Yes (read-only Supabase REST schema introspection) | **Prod endpoint:** healthy (/api/scanner → 200)

---

## Executive Summary

The live Supabase schema has **diverged significantly from the repo's code and SQL files**. Three write paths are silently failing in production (exit archiving, trade-close marking, custom entry prices), the Vercel cron layer has no timeout headroom configured (10s default vs 30-60s pipelines), and the scanner cron is not registered in `vercel.json` at all (external cron-job.org dependency). 72 bare `except:` blocks in `api/webhook.py` alone have suppressed the evidence for these failures.

---

## 1. Cron & Endpoint Health

**Architecture:** `vercel.json` registers only 2 crons (`pre_market` 05:30 UTC, `post_market` 12:30 UTC, Sun-Thu). `scanner` (`*/15 07:00-11:30 UTC`) is **not registered** — it depends on external cron-job.org via `scripts/setup_cronjobs.py` (requires `CRONJOB_API_KEY`). All GH Action schedules are DISABLED (workflow_dispatch only) — no double-posting risk.

| ID | Component/File | Root Cause | Severity |
|----|----------------|------------|----------|
| CR-01 | vercel.json:38-47 / api/scanner.py:5 | Scanner cron `*/15 7-11 * * 0-4` is absent from `vercel.json` while the docstring claims it exists. Triggering depends entirely on cron-job.org being configured with `CRONJOB_API_KEY`; if setup was never run, the scanner never fires — primary candidate for "cron not triggering". | **High** |
| CR-02 | vercel.json (no `functions`/`maxDuration` key) | No `maxDuration` configured for any function → Vercel default **10s** (Hobby). `pre_market`/`post_market` run RSS fetch + Gemini AI + Telegram synchronously (commonly 15-60s); `scanner` batch `yf.download` of 8 tickers can exceed 10s. Result: `FUNCTION_INVOCATION_TIMEOUT` → cron executions fail despite healthy code. Primary candidate for "cron jobs failing". | **High** |
| CR-03 | api/pre_market.py:117-121, api/post_market.py:144-157, api/scanner.py:207-220 | All cron endpoints catch pipeline crashes and still return **HTTP 200** (with `ok:false` in the body). Vercel cron sees success; failures are invisible to any monitoring/retry layer — silent failure by design. | Med |
| CR-04 | api/scanner.py:82-89 | Window guard computes `in_window` (07:00-11:30 UTC Sun-Thu) but **never branches on it** — dead variable. The endpoint scans on any ping at any hour; the `[CRON][AUDIT]` log line misleads operators into believing gating exists. | Med |
| CR-05 | vercel.json schedules + api/scanner.py:2 | Hobby plan: crons fire at most once/day (fine for pre/post, impossible for */15 — external mandatory). Additionally, fixed UTC schedules drift vs Cairo DST (UTC+3 summer / UTC+2 winter): 05:30 UTC = 08:30 Cairo in summer but 07:30 in winter. | Med |
| CR-06 | api/scanner.py:182-189 | Batch-scan failure is swallowed and replaced with a synthetic `ok:true` result (`overall_ok` never set false) — real scan failures undetectable from responses. | Med |
| CR-07 | api/pre_market.py:5,15-16 / api/post_market.py:5,19 | Stale docstrings reference `/api/cron/pre_market` paths (pre-flat-structure) — misleading runbooks/curl examples. | Low |
| CR-08 | api/pre_market.py:100-105 | Response `scheduled` computation does not walk back to the last Sun-Thu 05:30 (unlike the audit block) — wrong value on weekend/late pings. | Low |
| CR-09 | api/scanner.py:239-253 | `__main__` direct-run banner claims "Running → SCALPING channel" but the code only downloads data and prints — no publishing. | Low |

---

## 2. Database & Schema Alignment (live schema verified)

**Live tables/columns (introspected):**
- `user_portfolio`: `id, user_id, symbol, trade_id, status, joined_at, remaining_qty_pct`
- `closed_positions`: `id, user_id, symbol, trade_id, entry_price, exit_price, qty_pct, realized_pnl_pct, exit_reason, closed_at`
- `trade_signals`: `id, ticker, strategy_type, entry_price, stop_loss, target_1..3, tqi_score, shariah_status, created_at`
- `sent_alerts`: `id, ticker, strategy, date_sent, entry_price, current_stop_loss, target_1..3, created_at, signal_hash`
- **Missing entirely:** `news_publish_log`, `target_hits` (news idempotency falls back to local JSON)

| ID | Component/File | Root Cause | Severity |
|----|----------------|------------|----------|
| DB-01 | api/webhook.py `_archive_closed_position` (payload ~1411-1422) | Posts `realized_pnl`, `quantity_percentage`, `close_reason` — live table has **none** of these (it has `qty_pct`, `exit_reason`; no `realized_pnl` at all). Every POST → PGRST204 400 → returns False. **All exit archiving fails silently in production.** Stats source-of-truth is empty. | **High** |
| DB-02 | egx_quant/engine/trade_monitor.py:154-184 / egx_quant/admin/commands.py:363-384 | PATCH `trade_signals` with `{"status": ..., "exit_reason": ...}` — live table has **no `status`/`exit_reason` columns**. Both attempts fail (PGRST204). Signals are never marked CLOSED → `_is_sl_closed` always False → duplicate SL/target alert risk; close state machine broken. | **High** |
| DB-03 | api/webhook.py `_upsert_user_portfolio` / `_apply_portfolio_exit` | `user_portfolio` live lacks `entry_price`, `joined_at_price`, `snapshot` (entry-price migration never applied). Consequences: (a) custom entry prices from `/join` and DM joins silently dropped (PGRST204 fallback); (b) snapshot-PnL persistence never persists; (c) **partial-exit PATCH includes `snapshot` key with no drop-column fallback → whole PATCH fails → `remaining_qty_pct` is NOT decremented on partial exits** (regression in the multi-candidate fallback chain). | **High** |
| DB-04 | egx_quant/admin/commands.py:441-445 (`/update sl=`) | Maps `sl` → `current_stop_loss`; live `trade_signals` has no such column → `/update TICKER sl=X` always fails ("patch-failed"). | Med |
| DB-05 | egx_quant/news/common.py:28-31,117,159 | `news_publish_log` table missing → idempotency falls back to `news_publish_log.json` on Vercel's ephemeral FS → duplicate-bulletin protection ineffective in production (cron retries can double-post). | Med |
| DB-06 | api/webhook.py `_fetch_cumulative_realized_pnl` / commands.py /stats | Selects `realized_pnl` and reads `close_reason` — columns don't exist (PGRST204 on select; exit label always "Manual Exit"). Cumulative PnL line in exit DMs never appears. | Med |
| DB-07 | setup_db.sql / supabase_migration_entry_price.sql / egx_quant/database/db_manager.py:63-71 | Repo SQL documents a schema (`snapshot jsonb`, `quantity_percentage`, status CHECK TRACKING/EXITED/CLOSED) that does not match live DB; no migration file exists for the live `qty_pct`/`exit_reason` closed_positions variant. Schema drift is untracked → recurring silent PGRST204 failures. | Med |
| DB-08 | setup_db.sql:29-31, supabase_setup.sql:7-10 | RLS disabled + `GRANT ALL ... TO anon` on `user_portfolio`/`sent_alerts` — anon (public) key can read/write the multi-tenant portfolio table if the anon key leaks (service_role is used by code, but the grants are over-permissive). | Med |
| DB-09 | — (verification) | PostgreSQL 22007 class: the `/stats` URL-embedded timestamp is fixed (`strftime ...Z`); all remaining `isoformat()` usages are inside JSON bodies (safe). No further 22007 instances found. | OK |

---

## 3. Telegram Bot State Logic

| ID | Component/File | Root Cause | Severity |
|----|----------------|------------|----------|
| TG-01 | egx_quant/admin/commands.py:639-640, 671-672 (`_handle_join_command`) | `/join` on an already-TRACKING ticker returns `⚠️ أنت تتابع ... بالفعل` — the requested UPDATE flow (overwrite `entry_price` + `remaining_qty_pct` + Arabic confirmation "✅ تم تحديث بيانات الدخول...") is **not implemented** (pending task from prior session). Additionally the `/join` dispatcher (commands.py:924-938) parses only `[PRICE]` — no `QTY` argument exists. | **High** |
| TG-02 | api/webhook.py:693-734 (`_check_portfolio_exists`) | Existence check matches **any** status (incl. CLOSED/EXITED). After a trade is closed, both `/join` and the channel join button permanently refuse re-joining that ticker ("already tracking"). No status filter, no re-track path. | Med-High |
| TG-03 | api/webhook.py `handle_exit_confirm` (archive + confirm ~1735-1765) | Exit confirmation DM `✅ تم تسجيل الخروج` is sent unconditionally — even when `_archive_closed_position` returned False (DB-01) or the portfolio PATCH failed (DB-03). User-visible success + silent data loss. | Med-High |
| TG-04 | egx_quant/admin/commands.py `/exit` + webhook exit path | Same silent-failure exposure via shared helpers: partial-exit failures don't change the "🟡 خروج جزئي" success message (see DB-03). | Med |
| TG-05 | commands.py `_handle_weekly_stats` | `متوسط PnL` = Σ weighted-pct ÷ exit-rows — a 3-step scale-out counts as 3 rows, so the "average" is per-exit not per-position (may mislead). Also best/worst labels use `close_reason` (missing col → always "Manual Exit", see DB-06). | Low |
| TG-06 | api/webhook.py `handle_exit_confirm` (~1504 + ~1760) | `_answer_callback` invoked twice per exit (initial "⏳" + final "✅") — Telegram silently rejects the second answer; log noise only. | Low |
| TG-07 | api/webhook.py:1722 (`quantity_pct_to_egp`) | Parameter shadows Python builtin `exit` — style hazard, works today. | Low |
| TG-08 | egx_quant/engine/trade_monitor.py:93-130 (`_record_target_hit`) | Idempotency heuristic: same-day row with stored `target_N ≥ price×0.99` suppresses the alert — distinct signals with near-identical targets can be wrongly suppressed; sent_alerts columns are semantically abused (`entry_price` stores target price). | Low-Med |

---

## 4. Log & Exception Audit

| ID | Component/File | Root Cause | Severity |
|----|----------------|------------|----------|
| EX-01 | api/webhook.py (**72** bare `except:`), main.py (9), commands.py (7); ~**135** `except→pass` patterns across core files | Suppressed exceptions hid the PGRST204 schema mismatches (DB-01..DB-04) — failures surfaced only as missing data, never as errors. Broadest observability debt in the repo. | **High** |
| EX-02 | 49 of 134 `requests.*` calls lack `timeout` (main.py ×10, trade_monitor ×3, supabase_sync ×3, commands.py ×2, callback_handler, post_market, etc.) | Any hanging upstream (Supabase/Telegram) stalls the request inside a 10s serverless budget → contributes to FUNCTION_INVOCATION_TIMEOUT. | Med |
| EX-03 | api/*.py catch-alls | Crash → 200 + `ok:false` body; no external healthcheck/alert hook on failure (duplicates CR-03 from the logging angle). | Med |
| EX-04 | api/post_market.py:30-33 | `logger = None` fallback is dead code — `logging.getLogger` never returns None; the except is pointless but harmless. | Low |
| EX-05 | api/webhook.py, api/*.py | print()-based logging interleaved without request/trace IDs — cron failure forensics require full Vercel log drain. | Low |

---

## Why "in-session cron jobs are failing or not triggering" — Ranked Root Causes

1. **CR-02** — no `maxDuration`; pipelines exceed the 10s default → Vercel kills the invocation (check Vercel dashboard → Deployments → Functions for `FUNCTION_INVOCATION_TIMEOUT`).
2. **CR-01** — scanner has **no** Vercel cron entry; it fires only if cron-job.org was provisioned (`CRONJOB_API_KEY`).
3. **CR-03/EX-03** — endpoints always return 200, so failures never register as cron failures anywhere.
4. **CR-05** — Hobby once-per-day cron ceiling; DST drift changes effective Cairo fire times by 1h in winter.

## Top 5 Fix Priorities (when fixes are scheduled)

1. **DB-01** — align `_archive_closed_position` payload to live columns (`qty_pct`, `exit_reason`; drop `realized_pnl` or add the column) — restores stats data.
2. **DB-03** — add migration for `entry_price`/`joined_at_price`/`snapshot` (or strip them from payloads); add drop-column fallback for the partial-exit PATCH so `remaining_qty_pct` decrements again.
3. **TG-01** — implement the `/join TICKER PRICE QTY` UPDATE-on-tracking flow (+ parser for QTY).
4. **DB-02** — add `status`/`exit_reason` columns to `trade_signals` (or reroute close-state to a new table) so SL/target dedup works.
5. **CR-02** — set `maxDuration` (e.g. 60s) for `api/*` functions in `vercel.json` and add scanner to `vercel.json` crons or verify cron-job.org provisioning.
