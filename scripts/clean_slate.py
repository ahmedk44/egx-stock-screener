#!/usr/bin/env python3
"""
scripts/clean_slate.py — Clean Slate Utility for Supabase + Telegram Channels

Before launching live production, wipe test data from Supabase and clear
test messages/cards from Telegram channels and private bot chat logs.

Supabase Database Purge:
  - DELETE all rows from test tables respecting FK constraints:
    public.trade_signals, public.user_portfolio, public.sent_alerts,
    public.news_publish_log (news history), public.active_positions,
    public.news_log (alias)
  - Keep schema, FKs, RLS, indexes intact (DELETE not DROP/TRUNCATE via API).
  - Print verification summary row counts post-cleanup (must all show 0).

Telegram History Purge:
  - Attempts bulk delete via Bot API deleteMessage for each channel where
    bot is admin. Handles rate limits and age restrictions, then prints
    manual instructions if API cannot complete bulk deletion.

Safety:
  - Requires --confirm flag to prevent accidental production deletion.

Usage:
  python scripts/clean_slate.py --dry-run          # preview only
  python scripts/clean_slate.py --confirm           # full purge
  python scripts/clean_slate.py --confirm --skip-telegram  # DB only
  python scripts/clean_slate.py --confirm --skip-db        # Telegram only
  python scripts/clean_slate.py --confirm --telegram-limit 50

Env:
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) — required for DB purge
  TELEGRAM_BOT_TOKEN — required for Telegram purge
  TELEGRAM_CHANNEL_SCALPING, SWING, INVESTMENT, NEWS — channels to clean
  TELEGRAM_USER_CHAT_ID / TELEGRAM_CHAT_ID — private bot chat

Exit codes:
  0 = clean slate verified (all counts 0 or skipped)
  1 = partial failure (some counts >0 or API error)
  2 = missing confirm or env
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # type: ignore

# Table purge order respecting FKs: child first, parent last (trade_signals is parent of user_portfolio)
TABLES_ORDERED = [
    "user_portfolio",      # child -> references trade_signals(trade_id)
    "sent_alerts",         # independent
    "news_publish_log",    # news history
    "news_log",            # alias / legacy
    "active_positions",    # legacy tracker
    "trade_signals",       # parent -> LAST
]

# Additional tables that may exist but not required (checked opportunistically)
OPTIONAL_TABLES = []

TELEGRAM_ENV_CHANNELS = [
    ("Scalping", "TELEGRAM_CHANNEL_SCALPING"),
    ("Swing", "TELEGRAM_CHANNEL_SWING"),
    ("Investment", "TELEGRAM_CHANNEL_INVESTMENT"),
    ("News & Bulletins", "TELEGRAM_CHANNEL_NEWS"),
    ("News (alt 1)", "TELEGRAM_NEWS_CHANNEL_ID"),
    ("News (alt 2)", "TELEGRAM_CHAT_ID_NEWS"),
]

PRIVATE_CHAT_ENVS = [
    "TELEGRAM_USER_CHAT_ID",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_CHANNEL_ID",  # fallback
]


def get_supabase_config() -> Optional[Tuple[str, str]]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    if not url or not key:
        return None
    return url, key


def _headers(key: str, prefer: str = "return=minimal") -> Dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def get_row_count(url: str, key: str, table: str) -> Optional[int]:
    """Get row count via Content-Range header (count=exact) with fallback to len(json)."""
    if requests is None:
        return None
    headers = _headers(key, prefer="count=exact")
    # Use HEAD first? Supabase supports GET with Prefer count=exact returning header
    try:
        resp = requests.get(f"{url}/rest/v1/{table}?select=*", headers=headers, timeout=15)
        if resp.status_code in (200, 206):
            cr = resp.headers.get("Content-Range") or resp.headers.get("content-range") or resp.headers.get("content_range") or ""
            if "/" in cr:
                try:
                    count_str = cr.split("/")[-1].strip()
                    if count_str.isdigit():
                        return int(count_str)
                    if count_str == "*":
                        # No count available, fallback to json len
                        pass
                except Exception:
                    pass
            # Fallback: json length (may be paginated but for test data <1000 it's fine)
            try:
                data = resp.json()
                if isinstance(data, list):
                    # If pagination limited, header count is more accurate; but use len if header missing
                    if cr and "/" in cr:
                        # Already parsed, but len may be truncated
                        hdr_count = int(cr.split("/")[-1]) if cr.split("/")[-1].isdigit() else len(data)
                        return hdr_count
                    return len(data)
            except Exception:
                pass
            # If header not present and no json, assume 0
            return 0
        elif resp.status_code == 404 and "PGRST205" in (resp.text or ""):
            print(f"[DB][WARN] Table {table} not found (PGRST205) — DDL not executed, skipping count.")
            return None  # table does not exist
        else:
            # Try alternative count via HEAD
            try:
                head = requests.head(f"{url}/rest/v1/{table}?select=*", headers=headers, timeout=10)
                cr2 = head.headers.get("Content-Range") or head.headers.get("content-range") or ""
                if "/" in cr2 and cr2.split("/")[-1].isdigit():
                    return int(cr2.split("/")[-1])
            except Exception:
                pass
            print(f"[DB][WARN] Count for {table} failed HTTP {resp.status_code}: {(resp.text or '')[:200]}")
            return None
    except Exception as exc:
        print(f"[DB][ERROR] Count request failed for {table}: {exc}")
        return None


def delete_all_rows(url: str, key: str, table: str, dry_run: bool = False) -> Tuple[bool, str]:
    """DELETE all rows from table using REST filters. Returns (success, detail)."""
    if dry_run:
        return True, "dry-run (no delete)"
    if requests is None:
        return False, "requests not installed"
    # Candidate filters that match all rows for different schemas
    candidate_filters = [
        "id=not.is.null",
        "trade_id=not.is.null",
        "user_id=not.is.null",
        "symbol=not.is.null",
        "ticker=not.is.null",
        "ticker_bare=not.is.null",
        "bulletin_type=not.is.null",
        "publish_date=not.is.null",
        "date_sent=not.is.null",
        "strategy=not.is.null",
        "created_at=not.is.null",
        "joined_at=not.is.null",
        "status=not.is.null",
    ]
    # Also try gte filters for numeric identity
    extra_filters = [
        "id=gte.0",
        "id=gt.0",
        "trade_id=gte.0",
    ]
    all_filters = candidate_filters + extra_filters

    last_error = ""
    for filt in all_filters:
        try:
            headers = _headers(key, prefer="return=minimal")
            endpoint = f"{url}/rest/v1/{table}?{filt}"
            # Supabase DELETE requires Prefer return header, but we use minimal
            resp = requests.delete(endpoint, headers=headers, timeout=15)
            if resp.status_code in (200, 202, 204):
                # Success — may have deleted 0 or N rows; verify count afterwards
                print(f"[DB][PURGE] {table} DELETE ?{filt} → HTTP {resp.status_code} (success)")
                return True, f"DELETE ?{filt} → {resp.status_code}"
            elif resp.status_code == 404 and "PGRST205" in (resp.text or ""):
                print(f"[DB][SKIP] {table} not found (PGRST205) — table does not exist, nothing to purge.")
                return True, "table not exists (PGRST205)"
            elif resp.status_code == 400:
                body = (resp.text or "")
                # PGRST204 column not found, or other filter error — try next filter
                if "PGRST204" in body or "column" in body.lower() or "42703" in body:
                    last_error = f"400 PGRST204/column for filter {filt}: {body[:120]}"
                    continue
                # Other 400 like no filter? try next
                if "filter" in body.lower() or "operator" in body.lower():
                    continue
                last_error = f"400 for {filt}: {body[:150]}"
                continue
            elif resp.status_code == 401:
                return False, f"401 Unauthorized — check SUPABASE_SERVICE_ROLE_KEY for {table}"
            else:
                last_error = f"HTTP {resp.status_code} for {filt}: {(resp.text or '')[:150]}"
                continue
        except Exception as exc:
            last_error = f"Exception for {filt}: {exc}"
            continue

    # Final fallback: try RPC-like approach via POST with empty? Not possible.
    # Try deleting via `?select=*` trick? Some PostgREST requires filter; we exhausted.
    return False, f"All delete filters failed for {table}. Last: {last_error}"


def purge_supabase_tables(dry_run: bool = False, verbose: bool = True) -> Dict[str, Optional[int]]:
    """Purge all TABLES_ORDERED and return post-cleanup counts dict."""
    cfg = get_supabase_config()
    if cfg is None:
        print("[DB][FATAL] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing — cannot purge DB")
        print("[DB][HINT] Set them in .env or secrets. Run check_tables.py for diagnostics.")
        return {}
    url, key = cfg
    print(f"[DB] SUPABASE_URL: {url}")
    print(f"[DB] Key prefix: {key[:10]}... len={len(key)}")
    print(f"[DB] Purge order (FK-safe): {', '.join(TABLES_ORDERED)}")
    print(f"[DB] Mode: {'DRY-RUN (no deletes)' if dry_run else 'LIVE PURGE'}")
    print("-" * 70)

    before_counts: Dict[str, Optional[int]] = {}
    after_counts: Dict[str, Optional[int]] = {}

    for table in TABLES_ORDERED:
        cnt_before = get_row_count(url, key, table)
        before_counts[table] = cnt_before
        if cnt_before is None:
            print(f"[DB] {table:20s} count: (table missing or error) — skipping delete")
            after_counts[table] = None
            continue
        print(f"[DB] {table:20s} BEFORE: {cnt_before} rows")
        if cnt_before == 0:
            print(f"[DB] {table:20s} already empty — skipping DELETE")
            after_counts[table] = 0
            continue
        # Perform delete
        success, detail = delete_all_rows(url, key, table, dry_run=dry_run)
        if not success:
            print(f"[DB][FAIL] {table:20s} purge failed: {detail}")
            # Still check count after to see if partial
            after_counts[table] = get_row_count(url, key, table)
            continue
        # Verify after
        # Small delay for eventual consistency
        time.sleep(0.5)
        cnt_after = get_row_count(url, key, table)
        after_counts[table] = cnt_after
        if cnt_after == 0:
            print(f"[DB][PASS] {table:20s} AFTER: 0 rows ✓")
        else:
            print(f"[DB][WARN] {table:20s} AFTER: {cnt_after} rows (expected 0) — detail: {detail}")

    print("-" * 70)
    print("[DB] Verification Summary (post-cleanup row counts must all show 0):")
    all_zero = True
    for table in TABLES_ORDERED:
        cnt = after_counts.get(table)
        if cnt is None:
            print(f"  {table:20s} : (table not found / skipped)")
        elif cnt == 0:
            print(f"  {table:20s} : 0 ✓")
        else:
            print(f"  {table:20s} : {cnt} ✗ (NOT CLEAN)")
            all_zero = False
    if dry_run:
        print("[DB] DRY-RUN complete — no rows deleted (re-run with --confirm to purge).")
    elif all_zero:
        print("[DB][SUCCESS] Clean slate verified — all tables 0 rows. Schema, FKs, RLS, indexes intact (DELETE preserves structure).")
    else:
        print("[DB][FAIL] Some tables still have rows — manual intervention may be needed (see instructions below).")

    # Sequence reset note
    if not dry_run:
        print()
        print("[DB][INFO] Primary key sequences (IDENTITY) are preserved after DELETE (PostgREST DELETE keeps sequences).")
        print("[DB][INFO] To fully reset sequences to 1, run in Supabase SQL Editor (optional, not required for clean slate):")
        for tbl in ["trade_signals", "sent_alerts", "news_publish_log"]:
            print(f"  TRUNCATE public.{tbl} RESTART IDENTITY CASCADE;  -- resets id sequence to 1")
        print("  -- Or for user_portfolio (no identity?): DELETE already sufficient; sequence not applicable.")
        print("  -- Schema, FKs, RLS policies, and indexes remain intact — only rows deleted.")

    return after_counts


def get_telegram_channels() -> List[Tuple[str, str]]:
    """Resolve channel IDs from env, deduped."""
    seen = set()
    result: List[Tuple[str, str]] = []
    for label, env in TELEGRAM_ENV_CHANNELS:
        val = (os.environ.get(env) or "").strip().strip('"').strip("'")
        if val and val not in seen:
            seen.add(val)
            result.append((label, val))
    # Private chats
    for env in PRIVATE_CHAT_ENVS:
        val = (os.environ.get(env) or "").strip().strip('"').strip("'")
        if val and val not in seen:
            seen.add(val)
            result.append((f"Private ({env})", val))
            break  # only first private chat
    return result


def try_telegram_delete(chat_id: str, message_id: int, token: str) -> Tuple[bool, str]:
    """Attempt Bot API deleteMessage. Returns (success, detail)."""
    if requests is None or not token:
        return False, "no token/requests"
    try:
        url = f"https://api.telegram.org/bot{token}/deleteMessage"
        payload = {"chat_id": chat_id, "message_id": message_id}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            try:
                data = resp.json()
                if data.get("ok"):
                    return True, "deleted"
                return False, f"ok=false: {data.get('description','')}"
            except Exception:
                return True, "200 but non-json"
        # Handle rate limit 429
        if resp.status_code == 429:
            try:
                data = resp.json()
                retry_after = data.get("parameters", {}).get("retry_after", 2)
                return False, f"429 rate limited retry_after={retry_after}"
            except Exception:
                return False, f"429: {resp.text[:120]}"
        # Other errors
        body = (resp.text or "")[:300]
        return False, f"HTTP {resp.status_code}: {body}"
    except Exception as exc:
        return False, f"exception: {exc}"


def purge_telegram_history(limit: int = 100, dry_run: bool = False, verbose: bool = True) -> Dict[str, Dict[str, int]]:
    """Attempt bulk delete via Bot API for each channel where bot is admin.

    Strategy: Send dummy message to discover max_message_id, then iterate
    backwards attempting deleteMessage for limit messages. Handles 429 rate
    limits and age restrictions (messages >48h cannot be deleted via bot in some contexts).
    """
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("[TG][SKIP] TELEGRAM_BOT_TOKEN missing — cannot purge Telegram history")
        print("[TG][HINT] Provide token via .env or --token to enable Telegram purge")
        return {}
    if requests is None:
        print("[TG][FATAL] requests not installed — cannot call Telegram Bot API")
        return {}

    channels = get_telegram_channels()
    if not channels:
        print("[TG][WARN] No channel IDs found in env (TELEGRAM_CHANNEL_* / TELEGRAM_USER_CHAT_ID)")
        print("[TG][HINT] Set channel IDs via env; get IDs via https://api.telegram.org/bot<TOKEN>/getUpdates")
        return {}

    print(f"[TG] Found {len(channels)} Telegram target(s):")
    for label, cid in channels:
        print(f"  {label:20s} -> {cid}")

    if dry_run:
        print("[TG] DRY-RUN — no messages will be deleted (use --confirm to execute)")
        return {}

    results: Dict[str, Dict[str, int]] = {}

    for label, chat_id in channels:
        print()
        print(f"[TG][{label}] Processing chat_id={chat_id} limit={limit} ...")
        # Discover max message_id by sending a temporary message
        max_id: Optional[int] = None
        temp_sent = False
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            dummy = {"chat_id": chat_id, "text": "🧹 Clean slate probe — will be deleted immediately", "disable_notification": True}
            resp = requests.post(url, json=dummy, timeout=10)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("ok") and isinstance(data.get("result"), dict):
                        max_id = int(data["result"].get("message_id", 0))
                        if max_id:
                            # Delete the probe message itself
                            del_url = f"https://api.telegram.org/bot{token}/deleteMessage"
                            requests.post(del_url, json={"chat_id": chat_id, "message_id": max_id}, timeout=10)
                            temp_sent = True
                            print(f"[TG][{label}] Probe message_id={max_id} created & deleted — using as upper bound")
                except Exception:
                    pass
            if not max_id:
                # Probe failed (bot not admin, or channel restricts?). Try fallback via getUpdates
                print(f"[TG][{label}] Probe send failed HTTP {resp.status_code}: {(resp.text or '')[:200]}")
                print(f"[TG][{label}] Bot may not be admin in this chat or lacks post permission — will attempt limited delete via message_id guessing starting at 5000")
                max_id = 5000  # guess
        except Exception as exc:
            print(f"[TG][{label}] Probe exception: {exc}")
            max_id = 5000

        if not max_id:
            max_id = 5000

        deleted = 0
        not_found = 0
        forbidden = 0
        rate_limited = 0
        too_old = 0
        other_fail = 0

        # Iterate backwards from max_id-1 down to max(1, max_id-limit)
        start_id = max_id - 1
        end_id = max(1, max_id - limit)
        print(f"[TG][{label}] Attempting deleteMessage for message_ids {end_id}..{start_id} (total {limit})")
        for mid in range(start_id, end_id - 1, -1):
            ok, detail = try_telegram_delete(chat_id, mid, token)
            if ok:
                deleted += 1
                if verbose and deleted % 20 == 0:
                    print(f"[TG][{label}] ... deleted {deleted} so far (mid={mid})")
            else:
                dl = detail.lower()
                if "message to delete not found" in dl or "not found" in dl:
                    not_found += 1
                elif "message can't be deleted" in dl or "can't be deleted" in dl:
                    too_old += 1
                elif "not enough rights" in dl or "need administrator" in dl or "forbidden" in dl:
                    forbidden += 1
                    # If bot not admin, stop early for this chat
                    if forbidden > 5:
                        print(f"[TG][{label}] Bot lacks admin rights — stopping early for this chat")
                        break
                elif "429" in dl or "rate" in dl:
                    rate_limited += 1
                    # Extract retry_after if present
                    import re
                    m = re.search(r"retry_after[^\d]*(\d+)", dl)
                    wait = int(m.group(1)) if m else 2
                    wait = min(wait, 5)
                    if verbose:
                        print(f"[TG][{label}] Rate limited, sleeping {wait}s (mid={mid})")
                    time.sleep(wait)
                else:
                    other_fail += 1
            # Gentle rate limiting
            time.sleep(0.05)
            # Early exit if mostly not_found (channel empty)
            if not_found > limit * 0.8 and deleted == 0:
                # Likely channel has few messages; continue but don't spam
                pass

        results[label] = {
            "deleted": deleted,
            "not_found": not_found,
            "forbidden": forbidden,
            "too_old": too_old,
            "rate_limited": rate_limited,
            "other": other_fail,
            "max_id": max_id,
            "chat_id": chat_id,
        }
        print(f"[TG][{label}] Summary: deleted={deleted}, not_found={not_found}, forbidden={forbidden}, too_old(age>48h)={too_old}, rate_limited={rate_limited}, other={other_fail}")

    print()
    print("-" * 70)
    print("[TG] Telegram Purge Summary:")
    for label, stats in results.items():
        print(f"  {label:20s} ({stats['chat_id']}): deleted={stats['deleted']} / attempted {limit}")

    # Instructions for cases where bulk deletion cannot complete
    any_forbidden = any(v.get("forbidden", 0) > 0 for v in results.values())
    any_too_old = any(v.get("too_old", 0) > 3 for v in results.values())
    any_low_delete = all(v.get("deleted", 0) == 0 and v.get("not_found", 0) > 10 for v in results.values()) if results else False

    if any_forbidden or any_too_old or any_low_delete:
        print()
        print("[TG][IMPORTANT] Bot API limits apply:")
        print("  • Bots can ONLY delete messages they sent as the bot (not user messages).")
        print("  • In groups: messages older than 48 hours cannot be deleted via bot API (Telegram limit).")
        print("  • In channels: bot must be Administrator with 'Delete messages' permission.")
        print("  • Bot API has no getChatHistory — it can only delete by message_id if known.")
        print()
        print("  Manual actions (if Telegram API rate limits / age restrictions prevent bulk deletion):")
        print("  1. Telegram Desktop / Mobile → Open each channel → Channel Settings (⋯) → Manage Channel →")
        print("     → For Private Channels: Use 'Delete Channel' and recreate, or long-press messages → Delete.")
        print("  2. For clean history: In channel, tap channel title → (i) → Delete all messages / Clear History")
        print("     (Requires channel creator permissions; admins may need creator to clear).")
        print("  3. Alternative: In Supabase, channel invite links remain valid after history clear — no need to recreate")
        print("     unless you want fresh invite hash: In channel settings → Invite Links → Revoke & Create New.")
        print("  4. For Private Bot Chat logs: Open chat with @YourBot → Clear History (Telegram app: Delete Chat → Clear History).")
        print("  5. Telethon / Pyrogram bulk-delete (user account, not bot):")
        print("     pip install telethon && use user API to iterate history and delete:")
        print("       from telethon import TelegramClient")
        print("       client = TelegramClient('sess', api_id, api_hash)")
        print("       async for msg in client.iter_messages(channel_id):")
        print("           await client.delete_messages(channel_id, msg.id)")
        print("     Note: Requires Telegram API ID/Hash from https://my.telegram.org and user session, not bot token.")
    else:
        print("[TG][INFO] Bot API purge completed within limits (if any messages remain >48h old, clear manually as above).")

    return results


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Clean Slate Utility — Purge Supabase test rows + Telegram history (requires --confirm)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  python scripts/clean_slate.py --dry-run\n"
        "  python scripts/clean_slate.py --confirm\n"
        "  python scripts/clean_slate.py --confirm --skip-telegram\n"
        "  python scripts/clean_slate.py --confirm --telegram-limit 200\n",
    )
    p.add_argument("--confirm", action="store_true", help="Required flag to actually delete rows/messages (safety guard)")
    p.add_argument("--dry-run", action="store_true", help="Preview only — show counts and what would be deleted, but do not delete")
    p.add_argument("--skip-telegram", action="store_true", help="Skip Telegram purge (DB only)")
    p.add_argument("--skip-db", action="store_true", help="Skip Supabase DB purge (Telegram only)")
    p.add_argument("--telegram-limit", type=int, default=100, help="Max message_ids to attempt per channel (default: 100)")
    p.add_argument("--verbose", action="store_true", help="Verbose logging", default=True)
    p.add_argument("--no-verbose", dest="verbose", action="store_false", help="Quiet mode")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Safety guard
    if args.confirm and args.dry_run:
        print("[FATAL] Cannot use --confirm and --dry-run together. Choose one.")
        return 2
    if not args.confirm and not args.dry_run:
        print("[SAFETY] This script will DELETE all rows from Supabase and attempt to delete Telegram history.")
        print("[SAFETY] Schema, FKs, RLS, indexes will be preserved (DELETE, not DROP).")
        print("[SAFETY] To proceed, re-run with explicit flag:")
        print("  python scripts/clean_slate.py --confirm        # live purge")
        print("  python scripts/clean_slate.py --dry-run        # preview only")
        print()
        print("[SAFETY] Refusing to run without --confirm or --dry-run (safety guard).")
        return 2

    dry_run = args.dry_run or not args.confirm  # if --confirm not given but dry-run, dry_run true
    # If --confirm given, dry_run false
    if args.confirm:
        dry_run = False

    print("=" * 70)
    print("🧹 Clean Slate Utility — Supabase + Telegram Channels")
    print("=" * 70)
    print(f"[SAFETY] Mode: {'DRY-RUN (preview)' if dry_run else 'LIVE PURGE (--confirm)'}")
    print(f"[SAFETY] Telegram limit per channel: {args.telegram_limit}")
    print(f"[SAFETY] Skip DB: {args.skip_db}, Skip Telegram: {args.skip_telegram}")
    print()

    # Env audit
    cfg = get_supabase_config()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    print("[ENV AUDIT] SUPABASE_URL:", "OK" if cfg else "MISSING")
    print("[ENV AUDIT] SUPABASE_SERVICE_ROLE_KEY:", "OK" if cfg else "MISSING")
    print("[ENV AUDIT] TELEGRAM_BOT_TOKEN:", "OK" if token else "MISSING")
    for env in ["TELEGRAM_CHANNEL_SCALPING", "TELEGRAM_CHANNEL_SWING", "TELEGRAM_CHANNEL_INVESTMENT", "TELEGRAM_CHANNEL_NEWS"]:
        present = bool((os.environ.get(env) or "").strip())
        print(f"[ENV AUDIT] {env}: {'OK' if present else 'MISSING'}")
    print()

    db_counts: Dict[str, Optional[int]] = {}
    tg_results: Dict[str, Dict[str, int]] = {}
    db_ok = True
    tg_ok = True

    # Supabase purge
    if not args.skip_db:
        print("▶ Supabase Database Purge")
        print("-" * 70)
        db_counts = purge_supabase_tables(dry_run=dry_run, verbose=args.verbose)
        # Check all counts 0
        for tbl, cnt in db_counts.items():
            if cnt is not None and cnt != 0:
                db_ok = False
        if not db_counts:
            # No config or error
            if not dry_run:
                db_ok = False
    else:
        print("[DB] Skipped (--skip-db)")

    # Telegram purge
    if not args.skip_telegram:
        print()
        print("▶ Telegram History Purge")
        print("-" * 70)
        tg_results = purge_telegram_history(limit=args.telegram_limit, dry_run=dry_run, verbose=args.verbose)
        # Telegram success is best-effort; we don't fail overall if messages too old (manual instructions provided)
        # But if token missing or forbidden, treat as warning not fail
    else:
        print()
        print("[TG] Skipped (--skip-telegram)")

    # Final verification
    print()
    print("=" * 70)
    print("✅ Verification — Clean State Readiness for Next Live Cron Cycle")
    print("=" * 70)
    if not args.skip_db:
        if dry_run:
            print("[VERIFY] DRY-RUN — DB counts not yet zeroed (preview). Re-run with --confirm to purge.")
            # Show before counts
            for tbl, cnt in db_counts.items():
                if cnt is None:
                    print(f"  {tbl:20s} : (table missing)")
                else:
                    print(f"  {tbl:20s} : {cnt} rows (would purge to 0)")
        else:
            print("[VERIFY] Post-cleanup row counts (must all show 0):")
            for tbl in TABLES_ORDERED:
                cnt = db_counts.get(tbl)
                if cnt is None:
                    print(f"  {tbl:20s} : (skipped / table not found)")
                else:
                    status = "✓ 0" if cnt == 0 else f"✗ {cnt} NOT CLEAN"
                    print(f"  {tbl:20s} : {status}")
            if db_ok:
                print("[VERIFY][PASS] DB clean slate verified — all trading/news tables 0 rows.")
                print("[VERIFY][READY] Next live cron cycle will start from clean state (no duplicate test signals).")
            else:
                print("[VERIFY][FAIL] Some tables still have rows — manual cleanup may be required.")
                print("[VERIFY][HINT] Run SQL in Supabase SQL Editor for full truncate:")
                print("  TRUNCATE public.user_portfolio, public.sent_alerts, public.news_publish_log, public.active_positions RESTART IDENTITY CASCADE;")
                print("  TRUNCATE public.trade_signals RESTART IDENTITY CASCADE;  -- parent last due to FK")
    else:
        print("[VERIFY] DB skip — no verification")

    if not args.skip_telegram:
        if dry_run:
            print("[VERIFY] DRY-RUN — Telegram no deletes attempted.")
        else:
            print("[VERIFY] Telegram purge attempted (best-effort via Bot API).")
            print("[VERIFY] If any channel still shows old messages >48h old, follow manual Clear History instructions above.")

    print()
    if dry_run:
        print("[DONE] Preview complete. To execute clean slate: python scripts/clean_slate.py --confirm")
        return 0
    # Exit code: 0 if DB ok (or skipped), 1 if DB not clean
    if not args.skip_db and not db_ok:
        print("[DONE][FAIL] Clean slate incomplete — check DB warnings above.")
        return 1
    print("[DONE][SUCCESS] Clean slate ready for live production cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
