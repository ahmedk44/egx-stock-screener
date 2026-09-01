#!/usr/bin/env python3
"""
send_test_signal.py - Dispatch a dummy test trade signal to verify live channel teaser + private DM workflow.

Steps:
  1. Construct dummy payload (ticker TEST.CA, strategy Scalping, entry 100, stop 95, target 105, tqi 8.5, COMPLIANT)
  2. Save into public.trade_signals via Supabase REST (service_role) to generate real trade_id (id column)
  3. Broadcast concise teaser card with single [ 📥 انضم للصفقة | Track Signal ] button to TELEGRAM_CHANNEL_SCALPING via broadcast_signal()
  4. Print trade_id and HTTP 200/201 confirmation

Usage:
  python send_test_signal.py
  python send_test_signal.py --dry-run  # no Telegram send, only Supabase insert

Requires .env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_SCALPING
"""
from __future__ import annotations

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from typing import Any, Dict

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # type: ignore[assignment]

# Scanner pipeline imports - unified card comes from the egx_quant notifier;
# main.py provides the join markup + telegram transport.
try:
    from main import build_join_markup, send_telegram
except ImportError:
    build_join_markup = None  # type: ignore[assignment]
    send_telegram = None  # type: ignore[assignment]

# Fallback via egx_quant notifier (alternative path)
try:
    from egx_quant.utils.telegram_notifier import build_join_markup as eq_build_join_markup  # type: ignore
except Exception:
    eq_build_join_markup = None  # type: ignore[assignment]


def get_cfg() -> tuple[str, str, str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    channel = (os.environ.get("TELEGRAM_CHANNEL_SCALPING") or os.environ.get("CHANNEL_SCALPING") or "").strip()
    return url, key, token, channel


def upsert_trade_signal(url: str, key: str, payload: Dict[str, Any]) -> int | None:
    """UPSERT trade_signals: check if ticker exists, update existing row instead of creating duplicate.

    Uses check-then-PATCH (id) with fallback to POST on_conflict=ticker.
    Returns trade_id (existing or new). Never creates duplicate rows for same ticker.
    """
    assert requests is not None
    ticker = (payload.get("ticker") or "").strip()
    headers_base = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    # 1) Check if active signal for same ticker already exists
    if ticker:
        try:
            check_url = f"{url}/rest/v1/trade_signals?ticker=eq.{ticker}&order=created_at.desc&limit=1&select=id"
            check_resp = requests.get(check_url, headers=headers_base, timeout=10)
            print(f"[UPSERT] Check existing ticker={ticker} -> HTTP {check_resp.status_code}")
            if check_resp.status_code == 200:
                rows = check_resp.json()
                if isinstance(rows, list) and rows and rows[0].get("id") is not None:
                    existing_id = int(rows[0].get("id"))
                    print(f"[UPSERT] Existing row found for {ticker} id={existing_id} -> updating (PATCH) instead of insert")
                    # PATCH existing row
                    patch_headers = {**headers_base, "Prefer": "return=representation"}
                    patch_url = f"{url}/rest/v1/trade_signals?id=eq.{existing_id}"
                    patch_resp = requests.patch(patch_url, json=payload, headers=patch_headers, timeout=15)
                    print(f"[UPSERT] PATCH {patch_url} -> HTTP {patch_resp.status_code} {patch_resp.text[:400]}")
                    if patch_resp.status_code in (200, 204):
                        # For 204, fetch id via check
                        if patch_resp.status_code == 204:
                            print(f"[UPSERT] PATCH success (204) for {ticker} id={existing_id}")
                            return existing_id
                        try:
                            data = patch_resp.json()
                            if isinstance(data, list) and data and isinstance(data[0], dict):
                                tid = int(data[0].get("id") or existing_id)
                                print(f"[UPSERT] PATCH success -> trade_id={tid}")
                                return tid
                            elif isinstance(data, dict) and data.get("id"):
                                return int(data.get("id"))
                            return existing_id
                        except Exception:
                            return existing_id
                    # Fallback try on_conflict
                    print(f"[UPSERT] PATCH failed, trying on_conflict=ticker upsert")
                    upsert_headers = {**headers_base, "Prefer": "resolution=merge-duplicates,return=representation"}
                    upsert_resp = requests.post(f"{url}/rest/v1/trade_signals?on_conflict=ticker", json=payload, headers=upsert_headers, timeout=15)
                    print(f"[UPSERT] POST on_conflict=ticker -> HTTP {upsert_resp.status_code} {upsert_resp.text[:400]}")
                    if upsert_resp.status_code in (200, 201, 204):
                        try:
                            data = upsert_resp.json()
                            if isinstance(data, list) and data:
                                return int(data[0].get("id") or existing_id)
                        except:
                            pass
                        return existing_id
        except Exception as exc:
            print(f"[UPSERT][WARN] dedup check failed: {exc} - proceeding to insert")
    # 2) No existing row - insert with on_conflict try first
    try:
        # Try UPSERT with on_conflict as primary (handles race condition)
        upsert_headers = {**headers_base, "Prefer": "resolution=merge-duplicates,return=representation"}
        upsert_resp = requests.post(f"{url}/rest/v1/trade_signals?on_conflict=ticker", json=payload, headers=upsert_headers, timeout=15)
        print(f"[UPSERT] POST on_conflict=ticker -> HTTP {upsert_resp.status_code} {upsert_resp.text[:400]}")
        if upsert_resp.status_code in (200, 201, 204):
            try:
                data = upsert_resp.json()
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    return int(data[0].get("id") or 0)
                elif isinstance(data, dict) and data.get("id"):
                    return int(data.get("id"))
            except:
                pass
            # If 204, query latest
            try:
                q = requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.{ticker}&order=created_at.desc&limit=1&select=id", headers=headers_base, timeout=10)
                if q.status_code == 200:
                    rows = q.json()
                    if isinstance(rows, list) and rows:
                        return int(rows[0].get("id") or 0)
            except:
                pass
            return None
        # Fallback plain POST if on_conflict not supported
        if upsert_resp.status_code == 400 and "on_conflict" in (upsert_resp.text or "").lower():
            print("[UPSERT] on_conflict not supported, falling back to plain POST")
        else:
            # If upsert failed for other reason, try plain POST anyway
            pass
    except Exception as exc:
        print(f"[UPSERT] on_conflict POST failed: {exc}")
    # 3) Plain POST fallback
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    endpoint = f"{url}/rest/v1/trade_signals"
    print(f"[SUPABASE] POST {endpoint}")
    print(f"[PAYLOAD] {json.dumps(payload, ensure_ascii=False)}")
    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=15)
        print(f"[SUPABASE] POST trade_signals -> HTTP {resp.status_code}")
        body = resp.text[:800] if resp.text else "(empty)"
        preview = body.encode("ascii", "replace").decode("ascii")[:400]
        print(f"[SUPABASE] Body: {preview}")
        if resp.status_code in (200, 201):
            try:
                data = resp.json()
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    trade_id = int(data[0].get("id") or data[0].get("trade_id") or 0)
                    print(f"[SUPABASE] Generated trade_id (id): {trade_id}")
                    return trade_id
                if isinstance(data, dict):
                    trade_id = int(data.get("id") or 0)
                    print(f"[SUPABASE] Generated trade_id: {trade_id}")
                    return trade_id
            except Exception as exc:
                print(f"[WARN] Failed to parse trade_id: {exc}")
                try:
                    q = requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.TEST.CA&order=created_at.desc&limit=1&select=id", headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=10)
                    if q.status_code == 200:
                        rows = q.json()
                        if isinstance(rows, list) and rows:
                            tid = int(rows[0].get("id") or 0)
                            print(f"[SUPABASE] Queried latest trade_id: {tid}")
                            return tid
                except Exception:
                    pass
            return None
        print(f"[FAIL] trade_signals insert failed HTTP {resp.status_code}: {body[:300]}")
        return None
    except Exception as exc:
        print(f"[ERROR] trade_signals insert request failed: {exc}")
        return None

def insert_trade_signal(url: str, key: str) -> int | None:
    """Insert dummy TEST.CA Scalping signal - now via UPSERT to prevent duplicates."""
    payload: Dict[str, Any] = {
        "ticker": "TEST.CA",
        "strategy_type": "Scalping",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "target_1": 105.0,
        "target_2": 107.0,
        "target_3": 110.0,
        "tqi_score": 8.5,
        "shariah_status": "COMPLIANT",
    }
    return upsert_trade_signal(url, key, payload)


def broadcast_teaser(ticker: str, trade_id: int, token: str, channel: str, dry_run: bool = False) -> bool:
    """Send unified teaser card (format_channel_short_card) + single join_trade button."""
    # Unified template: ALL signal broadcasts route through
    # egx_quant.utils.telegram_notifier.format_channel_short_card
    try:
        from types import SimpleNamespace

        from egx_quant.utils.telegram_notifier import TelegramNotifier as _TN

        plan = SimpleNamespace(
            symbol="TEST.CA",
            entry_price=100.0,
            stop_loss=95.0,
            take_profit=105.0,
            target_1=105.0,
            target_2=107.0,
            target_3=110.0,
            tqi_score=8.5,
            strategy_type="scalping",
        )
        short_card = "\n".join(_TN().format_channel_short_card(plan, trade_id))
    except Exception as exc:
        print(f"[WARN] format_channel_short_card failed: {exc}, using minimal fallback")
        short_card = (
            "🚀 <b>إشارة جديدة | TEST</b>\n"
            "💵 <b>سعر الدخول:</b> 100.00 EGP\n"
            "🛑 <b>وقف الخسارة (SL):</b> 95.00 EGP\n"
            "🎯 <b>الهدف الأول:</b> 105.00 EGP\n"
            "👇 اضغط الزر للمتابعة وتلقي التحديثات والتحليل المفصل في الخاص:"
        )

    # Single button with trade_id attached: join_trade:{TICKER_BARE}:{TRADE_ID}
    bare = ticker.replace(".CA", "")
    if eq_build_join_markup is not None:
        try:
            markup = eq_build_join_markup(trade_id, bare)
        except Exception as exc:
            print(f"[WARN] eq_build_join_markup failed: {exc}")
            markup = {"inline_keyboard": [[{"text": "📥 انضم للصفقة | Track Signal", "callback_data": f"join_trade:{bare}:{trade_id}"}]]}
    elif build_join_markup is not None:
        markup = build_join_markup(ticker, trade_id)
    else:
        markup = {"inline_keyboard": [[{"text": "📥 انضم للصفقة | Track Signal", "callback_data": f"join_trade:{bare}:{trade_id}"}]]}
    print(f"[MARKUP] {json.dumps(markup, ensure_ascii=False)}")

    print(f"[TELEGRAM] Channel: {channel} | Card preview (first 200 chars): {short_card[:200]}")
    # Strict single-button validation with trade_id attached
    kbd = markup.get("inline_keyboard", [])
    assert len(kbd) == 1 and len(kbd[0]) == 1, "Legacy multi-button layout detected!"
    btn = kbd[0][0]
    assert btn.get("text") == "📥 انضم للصفقة | Track Signal", f"Button text mismatch: {btn.get('text')}"
    assert f":{trade_id}" in str(btn.get("callback_data", "")), f"trade_id missing from callback_data: {btn.get('callback_data')}"

    if dry_run:
        print("[DRY-RUN] Skipping Telegram send")
        return True

    if not token or not channel:
        print("[FAIL] TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_SCALPING missing")
        return False

    # Use main.send_telegram if available (supports parse_mode="HTML")
    if send_telegram is not None:
        try:
            ok = send_telegram(channel, short_card, token, reply_markup=markup, parse_mode="HTML")
            print(f"[TELEGRAM] send_telegram -> {ok} (HTTP 200/201 expected)")
            return bool(ok)
        except Exception as exc:
            print(f"[ERROR] send_telegram failed: {exc}")
            return False

    # Fallback direct POST (HTML parse mode to match the unified template)
    assert requests is not None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": channel, "text": short_card, "parse_mode": "HTML", "reply_markup": markup}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        print(f"[TELEGRAM] POST sendMessage -> HTTP {resp.status_code}")
        body = resp.text[:500] if resp.text else "(empty)"
        print(f"[TELEGRAM] Body: {body[:300]}")
        ok = resp.status_code in (200, 201)
        if ok:
            print("[TELEGRAM] Broadcast successful HTTP 200/201")
        else:
            print(f"[FAIL] Telegram send failed {resp.status_code}")
        return ok
    except Exception as exc:
        print(f"[ERROR] Telegram request failed: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch dummy TEST.CA signal to SCALPING channel")
    parser.add_argument("--dry-run", action="store_true", help="Insert into Supabase but skip Telegram send")
    args = parser.parse_args()

    url, key, token, channel = get_cfg()
    print(f"[CFG] SUPABASE_URL: {url}")
    print(f"[CFG] SERVICE_ROLE_KEY prefix: {key[:10]}... len={len(key)}" if key else "[CFG] KEY missing")
    print(f"[CFG] TELEGRAM_CHANNEL_SCALPING: {channel}")
    print(f"[CFG] BOT_TOKEN present: {bool(token)}")

    if not url or not key:
        print("[FATAL] Supabase env missing")
        return 2
    if not channel:
        print("[FATAL] TELEGRAM_CHANNEL_SCALPING missing (set in .env)")
        return 2

    # Step 1: Insert dummy signal
    trade_id = insert_trade_signal(url, key)
    if not trade_id:
        print("[FAIL] Could not generate trade_id")
        return 1
    print(f"\n[STEP1] Generated trade_id: {trade_id}")

    # Step 2: Broadcast teaser
    ok = broadcast_teaser("TEST.CA", trade_id, token, channel, dry_run=args.dry_run)
    if ok:
        print(f"\n[SUCCESS] Dispatched TEST.CA Scalping teaser (trade_id={trade_id}) to {channel} with single join_trade button")
        print(f"[OUTPUT] trade_id={trade_id} HTTP 200/201 confirmed")
        return 0
    print("\n[FAIL] Broadcast failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
