#!/usr/bin/env python3
"""
scripts/send_mock_signal.py — Publish ONE mock trade signal card to the Signals Channel.

Flow (mirrors the live scanner/scheduler broadcast path):
  1. Upsert the mock row into Supabase public.trade_signals -> real trade_id
     (required so join_trade -> private DM -> interactive exit flow resolves).
  2. Render the canonical channel teaser card via TelegramNotifier.
  3. Broadcast card + single [ 📥 انضم للصفقة | Track Signal ] button STRICTLY
     to TELEGRAM_CHANNEL_ID. Never touches news/status channels or private DMs.

Usage:
  python scripts/send_mock_signal.py
  python scripts/send_mock_signal.py --ticker TEST2.CA --entry 10.50 --sl 9.80 --t1 11.50
  python scripts/send_mock_signal.py --dry-run

Requires .env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # type: ignore[assignment]

from egx_quant.database.models import RiskPlan
from egx_quant.utils.telegram_notifier import (
    TelegramNotifier,
    build_join_markup,
    clean_ticker,
)


def supabase_upsert_signal(url: str, key: str, payload: Dict[str, Any]) -> Optional[int]:
    """Check-then-PATCH dedup on ticker, else POST. Returns trade_signals.id."""
    assert requests is not None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    ticker = (payload.get("ticker") or "").strip()
    if ticker:
        try:
            check = requests.get(
                f"{url}/rest/v1/trade_signals?ticker=eq.{ticker}&order=created_at.desc&limit=1&select=id",
                headers=headers,
                timeout=10,
            )
            if check.status_code == 200:
                rows = check.json()
                if isinstance(rows, list) and rows and rows[0].get("id") is not None:
                    existing = int(rows[0]["id"])
                    resp = requests.patch(
                        f"{url}/rest/v1/trade_signals?id=eq.{existing}",
                        json=payload,
                        headers={**headers, "Prefer": "return=representation"},
                        timeout=15,
                    )
                    print(f"[UPSERT] Existing {ticker} id={existing} PATCH -> HTTP {resp.status_code}")
                    if resp.status_code in (200, 204):
                        return existing
        except Exception as exc:
            print(f"[UPSERT][WARN] dedup check failed: {exc} - proceeding to insert")
    resp = requests.post(
        f"{url}/rest/v1/trade_signals",
        json=payload,
        headers={**headers, "Prefer": "return=representation"},
        timeout=15,
    )
    print(f"[SUPABASE] POST trade_signals -> HTTP {resp.status_code}")
    if resp.status_code in (200, 201):
        try:
            data = resp.json()
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return int(data[0].get("id") or 0) or None
            if isinstance(data, dict) and data.get("id"):
                return int(data["id"])
        except Exception as exc:
            print(f"[SUPABASE][WARN] id parse failed: {exc}")
    print(f"[SUPABASE][FAIL] {resp.status_code}: {resp.text[:300]}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Send ONE mock signal card to TELEGRAM_CHANNEL_ID")
    parser.add_argument("--ticker", default="TEST2.CA", help="Mock ticker (default TEST2.CA)")
    parser.add_argument("--entry", type=float, default=10.50, help="Entry price (default 10.50)")
    parser.add_argument("--sl", type=float, default=9.80, help="Stop loss (default 9.80)")
    parser.add_argument("--t1", type=float, default=11.50, help="Target 1 (default 11.50)")
    parser.add_argument("--t2", type=float, default=None, help="Target 2 (optional)")
    parser.add_argument("--t3", type=float, default=None, help="Target 3 (optional)")
    parser.add_argument("--tqi", type=float, default=7.5, help="TQI score 0-10 (default 7.5)")
    parser.add_argument("--strategy", default="Swing", help="Strategy track (default Swing)")
    parser.add_argument("--dry-run", action="store_true", help="Render card but skip Telegram send")
    args = parser.parse_args()

    ticker = args.ticker.strip().upper()
    if not ticker.endswith(".CA"):
        ticker = f"{ticker}.CA"

    print("=" * 70)
    print(f"Mock Signal Dispatcher — {ticker} | entry={args.entry:.2f} SL={args.sl:.2f} T1={args.t1:.2f}")
    print("=" * 70)

    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()

    trade_id = 0
    if url and key and requests is not None:
        payload = {
            "ticker": ticker,
            "strategy_type": args.strategy,
            "entry_price": args.entry,
            "stop_loss": args.sl,
            "target_1": args.t1,
            "tqi_score": args.tqi,
            "shariah_status": "COMPLIANT",
        }
        if args.t2 is not None:
            payload["target_2"] = args.t2
        if args.t3 is not None:
            payload["target_3"] = args.t3
        trade_id = supabase_upsert_signal(url, key, payload) or 0
        print(f"[STEP1] trade_id={trade_id}")
    else:
        print("[STEP1][WARN] Supabase env missing - broadcasting with trade_id=0 (webhook falls back to latest-by-ticker)")

    plan = RiskPlan(
        symbol=ticker,
        entry_price=args.entry,
        stop_loss=args.sl,
        take_profit=args.t1,
        target_1=args.t1,
        target_2=args.t2,
        target_3=args.t3,
        tqi_score=args.tqi,
        approved=True,
    )
    notifier = TelegramNotifier()
    card = notifier.format_channel_broadcast(plan, trade_id)
    markup = build_join_markup(trade_id, clean_ticker(ticker))

    print("[CARD]")
    print(card)
    print(f"[MARKUP] {json.dumps(markup, ensure_ascii=False)}")
    print(f"[TARGET] TELEGRAM_CHANNEL_ID={(os.environ.get('TELEGRAM_CHANNEL_ID') or '').strip()}")

    if args.dry_run:
        print("[DRY-RUN] Skipping Telegram send")
        return 0

    ok = notifier.broadcast_signal(card, markup)
    if ok:
        print(f"[SUCCESS] Mock signal published to Signals Channel (trade_id={trade_id})")
        return 0
    print("[FAIL] broadcast_signal returned False - check TELEGRAM_BOT_TOKEN / bot admin rights on channel")
    return 1


if __name__ == "__main__":
    sys.exit(main())
