#!/usr/bin/env python3
"""
scripts/cleanup_stale_signals.py — Expire stale ACTIVE/TRACKING trade_signals in Supabase.

Resolves the dedup gate that blocks new signal generation when old signals
remain in ACTIVE/TRACKING status for weeks without being closed.

Usage:
  python scripts/cleanup_stale_signals.py                    # expire signals older than 7 days
  python scripts/cleanup_stale_signals.py --days 3           # expire signals older than 3 days
  python scripts/cleanup_stale_signals.py --dry-run          # preview only, no DB writes
  python scripts/cleanup_stale_signals.py --all              # expire ALL active signals (dangerous)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

try:
    import requests
except ImportError:
    print("[ERROR] requests not installed: pip install requests")
    sys.exit(1)


def get_supabase_config():
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        print("[ERROR] SUPABASE_URL and SUPABASE_KEY/SUPABASE_SERVICE_ROLE_KEY must be set")
        sys.exit(1)
    return url, key


def fetch_active_signals(url: str, key: str, older_than_days: int, fetch_all: bool = False):
    """Fetch ACTIVE/TRACKING signals older than N days."""
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    
    if fetch_all:
        filter_param = "status=in.(ACTIVE,TRACKING,OPEN)"
    else:
        from urllib.parse import quote as _quote
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        # The '+' in '+00:00' must be percent-encoded, else PostgREST reads a
        # space and rejects the timestamp (HTTP 400 code 22007).
        filter_param = f"status=in.(ACTIVE,TRACKING,OPEN)&created_at=lt.{_quote(cutoff, safe='')}"
    
    endpoint = f"{url}/rest/v1/trade_signals?{filter_param}&select=id,ticker,status,created_at,entry_price"
    resp = requests.get(endpoint, headers=headers, timeout=15)
    
    if resp.status_code != 200:
        print(f"[ERROR] Failed to fetch signals: HTTP {resp.status_code} — {resp.text[:300]}")
        return []
    
    signals = resp.json()
    if not isinstance(signals, list):
        return []
    return signals


def expire_signals(url: str, key: str, signal_ids: list, dry_run: bool = False):
    """Set status=EXPIRED for given signal IDs."""
    if not signal_ids:
        print("[INFO] No signals to expire")
        return 0
    
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    
    expired_count = 0
    for sig_id in signal_ids:
        if dry_run:
            print(f"  [DRY-RUN] Would expire signal id={sig_id}")
            expired_count += 1
            continue
        
        endpoint = f"{url}/rest/v1/trade_signals?id=eq.{sig_id}"
        # Status-only payload: trade_signals has no expired_at column
        # (verified live) - status is what the dedup gate keys on.
        resp = requests.patch(
            endpoint,
            headers=headers,
            json={"status": "EXPIRED"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            expired_count += 1
        else:
            print(f"  [WARN] Failed to expire id={sig_id}: HTTP {resp.status_code} — {resp.text[:100]}")
    
    return expired_count


def main():
    parser = argparse.ArgumentParser(description="Expire stale ACTIVE/TRACKING trade_signals")
    parser.add_argument("--days", type=int, default=7, help="Expire signals older than N days (default: 7)")
    parser.add_argument("--all", action="store_true", dest="expire_all", help="Expire ALL active signals")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no DB writes")
    args = parser.parse_args()
    
    url, key = get_supabase_config()
    print(f"[CLEANUP] Supabase: {url}")
    print(f"[CLEANUP] Mode: {'ALL active signals' if args.expire_all else f'signals older than {args.days} days'}")
    print(f"[CLEANUP] Dry run: {args.dry_run}")
    
    signals = fetch_active_signals(url, key, args.days, fetch_all=args.expire_all)
    print(f"[CLEANUP] Found {len(signals)} active signals to expire")
    
    if signals:
        print()
        for sig in signals[:20]:
            created = sig.get("created_at", "?")[:19]
            print(f"  {sig.get('ticker', '?'):10s} | {sig.get('status', '?'):10s} | entry={sig.get('entry_price', '?')} | created={created}")
        if len(signals) > 20:
            print(f"  ... and {len(signals) - 20} more")
        print()
    
    signal_ids = [s["id"] for s in signals if s.get("id")]
    expired = expire_signals(url, key, signal_ids, dry_run=args.dry_run)
    print(f"[CLEANUP] {'Would expire' if args.dry_run else 'Expired'} {expired} signals")


if __name__ == "__main__":
    main()
