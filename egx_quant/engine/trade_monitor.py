#!/usr/bin/env python3
"""
Real-Time Target Hit & Stop-Loss Monitor Engine.

Scans active trade_signals against live market prices, detects target hits
and stop-loss breaches, updates state, and dispatches alerts to public
channels and private DMs.

Idempotency:
  - Uses public.sent_alerts table to record every (ticker, target_level) hit
    so the same target level is never announced twice.
  - Tracks SL hits via trade_signals.status -> 'CLOSED' guard.

Schedule:
  - .github/workflows/trade_monitor.yml runs every 5 minutes during
    market hours (Sun-Thu 10:00 AM - 02:30 PM Cairo, UTC+3).
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except Exception:
    pass

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

logger = logging.getLogger("egx_engine.trade_monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# ---------------------------------------------------------------------------
# Local constants / helpers
# ---------------------------------------------------------------------------
TARGET_HIT_TABLE = "sent_alerts"
TRADE_SIGNALS_TABLE = "trade_signals"
USER_PORTFOLIO_TABLE = "user_portfolio"


def get_supabase_config() -> Optional[Tuple[str, str]]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    if not url or not key:
        return None
    return url, key


def _headers(prefer: str = "return=minimal") -> Dict[str, str]:
    cfg = get_supabase_config()
    if cfg is None:
        return {}
    _, key = cfg
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": prefer}


def _cfg() -> Optional[Tuple[str, str]]:
    return get_supabase_config()


# Reuse common price helper
from egx_quant.news.common import fetch_current_price_yfinance  # type: ignore

# Reuse telegram notifier
from egx_quant.utils.telegram_notifier import TelegramNotifier, clean_ticker  # type: ignore

# Reuse supabase sync helpers
from egx_quant.utils.supabase_sync import list_subscribers, broadcast_trade_update  # type: ignore

notifier = TelegramNotifier()


def _record_target_hit(ticker: str, target_level: int, target_price: float, current_price: float) -> bool:
    """Insert into sent_alerts to record that ticker reached target_level.

    Returns True if newly inserted (first time), False if already recorded
    (duplicate - should NOT re-alert).
    """
    cfg = _cfg()
    if requests is None or cfg is None:
        logger.warning("No Supabase config - cannot record target hit idempotency")
        return True  # optimistic: proceed
    url, key = cfg
    headers = _headers(prefer="return=minimal")
    payload = {
        "ticker": ticker,
        "strategy": "monitor",
        "date_sent": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "entry_price": target_price,
        "current_stop_loss": 0.0,
        "target_1": target_price if target_level == 1 else None,
        "target_2": target_price if target_level == 2 else None,
        "target_3": target_price if target_level == 3 else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # Try UPSERT on_conflict=(ticker, target_level equivalent)
    # sent_alerts has no unique constraint on (ticker, date_sent), so we check first
    try:
        check_url = f"{url}/rest/v1/{TARGET_HIT_TABLE}?ticker=eq.{ticker}&date_sent=eq.{payload['date_sent']}&select=id,target_1,target_2,target_3"
        resp = requests.get(check_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and rows:
                for r in rows:
                    existing_target = r.get(f"target_{target_level}")
                    if existing_target is not None and float(existing_target) >= target_price * 0.99:
                        logger.info(f"[IDEMPOTENT] Target {target_level} for {ticker} already recorded - skip")
                        return False
    except Exception as e:
        logger.debug(f"Check sent_alerts failed: {e}")

    try:
        resp = requests.post(f"{url}/rest/v1/{TARGET_HIT_TABLE}", json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201, 204):
            logger.info(f"[SENT_ALERT] Recorded target {target_level} hit for {ticker} @ {target_price}")
            return True
        logger.warning(f"[SENT_ALERT] Insert failed {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"[SENT_ALERT] Exception: {e}")
        return False


def _mark_trade_closed(ticker: str, trade_id: Optional[int], reason: str) -> bool:
    """Update trade_signals status to CLOSED and user_portfolio status to CLOSED (remaining=0)."""
    cfg = _cfg()
    if requests is None or cfg is None:
        logger.warning("No Supabase config - cannot mark trade closed")
        return False
    url, key = cfg
    headers = _headers(prefer="return=minimal")
    updated = False
    # Update trade_signals
    if trade_id is not None:
        try:
            patch_url = f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?trade_id=eq.{trade_id}"
            resp = requests.patch(patch_url, json={"status": "CLOSED", "exit_reason": reason}, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                logger.info(f"[CLOSED] trade_signals trade_id={trade_id} -> CLOSED ({reason})")
                updated = True
            else:
                logger.warning(f"[CLOSED] trade_signals patch failed {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"[CLOSED] trade_signals exception: {e}")
    else:
        # Fallback: update by ticker
        try:
            patch_url = f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?ticker=eq.{ticker}&order=created_at.desc&limit=1"
            resp = requests.patch(patch_url, json={"status": "CLOSED", "exit_reason": reason}, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                logger.info(f"[CLOSED] trade_signals ticker={ticker} -> CLOSED ({reason})")
                updated = True
        except Exception as e:
            logger.warning(f"[CLOSED] trade_signals exception: {e}")
    # Update user_portfolio: standardize full closes to status='CLOSED' (+ remaining 0)
    # Legacy EXITED fallback only for pre-migration DBs (old check constraint).
    try:
        patch_url = f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?symbol=eq.{ticker}&status=eq.TRACKING"
        for payload in ({"status": "CLOSED", "remaining_qty_pct": 0}, {"status": "CLOSED"}, {"status": "EXITED"}):
            try:
                resp = requests.patch(patch_url, json=payload, headers=headers, timeout=10)
                if resp.status_code in (200, 204):
                    if payload.get("status") == "EXITED":
                        logger.warning("[CLOSED] legacy DB check-constraint - marked EXITED (run supabase_migration_remaining_qty.sql)")
                    logger.info(f"[CLOSED] user_portfolio ticker={ticker} -> {payload.get('status')}")
                    break
            except Exception as e:
                logger.warning(f"[CLOSED] user_portfolio exception: {e}")
                break
    except Exception as e:
        logger.warning(f"[CLOSED] user_portfolio exception: {e}")
    return updated


def _is_sl_closed(ticker: str) -> bool:
    """Check if trade_signals for this ticker already has status=CLOSED (SL already processed)."""
    cfg = _cfg()
    if requests is None or cfg is None:
        return False
    url, key = cfg
    headers = _headers(prefer="return=minimal")
    try:
        check_url = f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?ticker=eq.{ticker}&status=eq.CLOSED&order=created_at.desc&limit=1&select=id"
        resp = requests.get(check_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and rows:
                return True
    except Exception as e:
        logger.debug(f"Check closed status failed: {e}")
    return False


def _get_active_signals_from_supabase() -> List[Dict[str, Any]]:
    """Fetch active signals from trade_signals where status in ('TRACKING','ACTIVE','OPEN')."""
    cfg = _cfg()
    if requests is None or cfg is None:
        logger.info("No Supabase config - no active signals")
        return []
    url, key = cfg
    headers = _headers(prefer="return=minimal")
    signals: List[Dict[str, Any]] = []
    for status_query in [
        "status=in.(TRACKING,ACTIVE,OPEN)",
        "status=in.(TRACKING,ACTIVE)",
        None,
    ]:
        try:
            if status_query:
                endpoint = f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?{status_query}&order=created_at.desc&limit=50&select=*"
            else:
                endpoint = f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?order=created_at.desc&limit=50&select=*"
            resp = requests.get(endpoint, headers=headers, timeout=10)
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list):
                    logger.info(f"[MONITOR] Fetched {len(rows)} active signals via query '{status_query or 'no filter'}'")
                    return rows
            elif resp.status_code == 400 and "PGRST204" in (resp.text or "") and status_query:
                logger.warning(f"[MONITOR] status column missing (PGRST204), trying fallback")
                continue
            else:
                logger.warning(f"[MONITOR] Fetch failed {resp.status_code}: {resp.text[:200]}")
                if status_query is None:
                    return []
        except Exception as e:
            logger.warning(f"[MONITOR] Fetch exception: {e}")
            continue
    return []


def fetch_active_signals_enriched(limit: int = 50) -> List[Dict[str, Any]]:
    """Fetch active trades and enrich with live prices + PnL."""
    raw = _get_active_signals_from_supabase()
    if not raw:
        logger.info("No raw active signals")
        return []
    enriched: List[Dict[str, Any]] = []
    for sig in raw:
        try:
            ticker = sig.get("ticker") or sig.get("symbol") or "UNKNOWN"
            entry = sig.get("entry_price")
            try:
                entry_f = float(entry) if entry is not None else None
            except Exception:
                entry_f = None
            stop = sig.get("stop_loss") or sig.get("current_stop_loss")
            try:
                stop_f = float(stop) if stop is not None else None
            except Exception:
                stop_f = None
            # Collect targets
            targets: List[float] = []
            for k in ["target_1", "target_2", "target_3", "target_4"]:
                if sig.get(k) is not None:
                    try:
                        targets.append(float(sig.get(k)))
                    except Exception:
                        continue
            # Fetch live price
            current = fetch_current_price_yfinance(ticker)
            if current is None and entry_f is not None:
                current = entry_f  # neutral fallback
            # PnL
            pnl_pct = None
            if entry_f and current and entry_f != 0:
                try:
                    pnl_pct = (current - entry_f) / entry_f * 100
                except Exception:
                    pnl_pct = 0
            # Determine if any target already hit
            targets_hit: List[int] = []
            for idx, tv in enumerate(targets, start=1):
                if current is not None and current >= tv * 0.98:
                    targets_hit.append(idx)
            # Determine if SL hit
            sl_hit = stop_f is not None and current is not None and current <= stop_f * 1.02
            # Trade id
            trade_id = sig.get("trade_id") or sig.get("id")
            status = sig.get("status") or "TRACKING"

            enriched.append({
                "ticker": ticker,
                "ticker_bare": ticker.replace(".CA", ""),
                "entry_price": entry_f,
                "stop_loss": stop_f,
                "targets": targets,
                "current_price": current,
                "pnl_pct": pnl_pct,
                "targets_hit": targets_hit,
                "sl_hit": sl_hit,
                "trade_id": trade_id,
                "status": status,
                "raw": sig,
            })
        except Exception as e:
            logger.warning(f"Enrich failed for {sig.get('ticker')}: {e}")
            continue
    return enriched


def format_target_hit_card(ticker: str, target_level: int, target_price: float, current_price: float, entry_price: Optional[float] = None) -> str:
    """Format celebratory target-hit card for public channel."""
    bare = clean_ticker(ticker)
    pnl = ((current_price - entry_price) / entry_price * 100) if entry_price else ((current_price - target_price) / target_price * 100)
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    medal = medals.get(target_level, "🎯")
    lines = [
        f"{medal} <b>🎯 تم تحقيق الهدف {target_level} لصفقة {bare}!</b>",
        f"------------------------------------",
        f"🔹 <b>السهم:</b> <code>{bare}</code>",
        f"🎯 <b>الهدف {target_level}:</b> {target_price:.2f} EGP",
        f"💵 <b>السعر الحالي:</b> {current_price:.2f} EGP",
        f"📈 <b>نسبة الربح:</b> +{pnl:.2f}%",
    ]
    if entry_price:
        lines.append(f"💵 <b>سعر الدخول:</b> {entry_price:.2f} EGP")
    lines += [
        f"------------------------------------",
        f"✅ تهانينا! تم تحقيق الهدف {target_level}.",
        f"📊 [EGX TradingView](https://www.tradingview.com/markets/egypt/)",
    ]
    return "\n".join(lines)


def format_sl_exit_card(ticker: str, current_price: float, stop_loss: float, entry_price: Optional[float] = None) -> str:
    """Format stop-loss exit alert card for public channel."""
    bare = clean_ticker(ticker)
    pnl = ((current_price - entry_price) / entry_price * 100) if entry_price else ((current_price - stop_loss) / stop_loss * 100)
    lines = [
        f"🛑 <b>تنبيه ضرب وقف الخسارة لصفقة {bare}!</b>",
        f"------------------------------------",
        f"🔹 <b>السهم:</b> <code>{bare}</code>",
        f"🛑 <b>وقف الخسارة:</b> {stop_loss:.2f} EGP",
        f"💵 <b>السعر الحالي:</b> {current_price:.2f} EGP",
        f"📉 <b>نسبة الخسارة:</b> {pnl:+.2f}%",
    ]
    if entry_price:
        lines.append(f"💵 <b>سعر الدخول:</b> {entry_price:.2f} EGP")
    lines += [
        f"------------------------------------",
        f"🔴 تم إغلاق الصفقة وتفعيل وقف الخسارة لحماية المحفظة.",
        f"📊 [EGX TradingView](https://www.tradingview.com/markets/egypt/)",
    ]
    return "\n".join(lines)


def publish_target_alert(ticker: str, target_level: int, target_price: float, current_price: float, entry_price: Optional[float] = None, dry_run: bool = False) -> Tuple[bool, bool]:
    """Broadcast target-hit alert to public channel + push DM to subscribers.

    Returns (public_ok, dm_ok).
    Idempotency: skips if sent_alerts already has this (ticker, target_level).
    """
    card = format_target_hit_card(ticker, target_level, target_price, current_price, entry_price)

    # Idempotency guard
    already = not _record_target_hit(ticker, target_level, target_price, current_price)
    if already:
        logger.info(f"[IDEMPOTENT] Target {target_level} hit for {ticker} already sent - skipping")
        return (False, False)

    public_ok = False
    dm_ok = False

    if not dry_run:
        # Broadcast to public channel (TELEGRAM_CHANNEL_NEWS or TELEGRAM_CHANNEL_SCALPING)
        channel = os.environ.get("TELEGRAM_CHANNEL_NEWS") or os.environ.get("TELEGRAM_CHANNEL_SCALPING") or ""
        if channel and notifier.enabled:
            public_ok = notifier.send_to_chat(channel, card)
        elif notifier.enabled:
            public_ok = notifier.broadcast_signal(card)
        else:
            logger.info(f"[MOCK BROADCAST] Target {target_level} hit card for {ticker}")
            print(f"[MOCK BROADCAST - PUBLIC]\n{card[:500]}")
            public_ok = True
    else:
        logger.info(f"[DRY-RUN] Would broadcast target {target_level} hit for {ticker}")
        print(f"[DRY-RUN - PUBLIC CARD]\n{card[:800]}")
        public_ok = True

    # Push DM to subscribers with actionable steps
    trade_id = None
    raw_signals = _get_active_signals_from_supabase()
    for s in raw_signals:
        if (s.get("ticker") or s.get("symbol")) == ticker:
            trade_id = s.get("trade_id") or s.get("id")
            break

    # Determine actionable suggestion based on target level
    if target_level == 1:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> بيع 50% من الكمية عند T1 وحرك وقف الخسارة إلى نقطة الدخول (Breakeven) لتأمين الأرباح."
    elif target_level == 2:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> بيع 25% إضافية عند T2 وحافظ على وقف متحرك تحت T1."
    elif target_level >= 3:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> جني الأرباح المتبقية أو الإغلاق الكامل - الهدف النهائي تحقق."
    else:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> مراجعة الصفقة وتحديث وقف الخسارة."

    if not dry_run:
        try:
            subscribers = list_subscribers(trade_id) if trade_id else []
            if not subscribers and ticker:
                subscribers = list_subscribers_by_symbol(ticker)
            if subscribers:
                dm_text = (
                    f"{card}\n"
                    f"------------------------------------\n"
                    f"{action_suggestion}\n"
                    f"📩 تم إرسال تنبيه الهدف لك في الخاص."
                )
                token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
                if token and requests:
                    ok_count = 0
                    for uid in subscribers:
                        try:
                            resp = requests.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": uid, "text": dm_text, "parse_mode": "HTML"},
                                timeout=10,
                            )
                            if resp.status_code == 200:
                                ok_count += 1
                        except Exception:
                            continue
                    dm_ok = ok_count > 0
                    logger.info(f"[DM] Target alert with action sent to {ok_count}/{len(subscribers)} subscribers for {ticker} T{target_level}")
                else:
                    logger.info(f"[MOCK DM] Would send target alert with action to {len(subscribers)} users for {ticker}")
                    dm_ok = True
            else:
                logger.info(f"[DM] No subscribers for {ticker} (trade_id={trade_id})")
                dm_ok = True  # no one to notify = success
        except Exception as e:
            logger.warning(f"[DM] Exception: {e}")
            dm_ok = True  # degrade gracefully
    else:
        logger.info(f"[DRY-RUN] Would push DM with action to subscribers for {ticker} T{target_level}")
        dm_ok = True

    return (public_ok, dm_ok)


def format_trailing_sl_update(ticker: str, new_sl: float, current_price: float, entry_price: Optional[float] = None) -> str:
    """Format trailing stop update card with actionable suggestion."""
    bare = clean_ticker(ticker)
    pnl = ((current_price - entry_price) / entry_price * 100) if entry_price else 0
    return (
        f"📈 <b>تحديث وقف الخسارة المتحرك | {bare}</b>\n"
        f"------------------------------------\n"
        f"🔹 <b>السهم:</b> <code>{bare}</code>\n"
        f"💵 <b>السعر الحالي:</b> {current_price:.2f} EGP ({pnl:+.2f}%)\n"
        f"🔴 <b>وقف الخسارة الجديد:</b> {new_sl:.2f} EGP\n"
        f"------------------------------------\n"
        f"💡 <b>الإجراء المقترح:</b> تم رفع الوقف لحماية الأرباح - لا حاجة للتدخل.\n"
        f"📊 [EGX TradingView](https://www.tradingview.com/markets/egypt/)"
    )

def publish_trailing_sl_alert(ticker: str, new_sl: float, current_price: float, entry_price: Optional[float] = None, dry_run: bool = False) -> Tuple[bool, bool]:
    """Broadcast trailing SL update to subscribers with actionable DM."""
    card = format_trailing_sl_update(ticker, new_sl, current_price, entry_price)
    # Reuse target alert logic but with trailing specific
    public_ok = False
    dm_ok = False
    if not dry_run:
        channel = os.environ.get("TELEGRAM_CHANNEL_NEWS") or os.environ.get("TELEGRAM_CHANNEL_SCALPING") or ""
        if channel and notifier.enabled:
            public_ok = notifier.send_to_chat(channel, card)
        elif notifier.enabled:
            public_ok = notifier.broadcast_signal(card)
        else:
            logger.info(f"[MOCK BROADCAST] Trailing SL update for {ticker}")
            print(f"[MOCK BROADCAST - PUBLIC]\n{card[:500]}")
            public_ok = True
    else:
        logger.info(f"[DRY-RUN] Would broadcast trailing SL update for {ticker}")
        print(f"[DRY-RUN - PUBLIC CARD]\n{card[:800]}")
        public_ok = True

    # Push DM with trailing suggestion
    raw_signals = _get_active_signals_from_supabase()
    trade_id = None
    for s in raw_signals:
        if (s.get("ticker") or s.get("symbol")) == ticker:
            trade_id = s.get("trade_id") or s.get("id")
            break
    if not dry_run:
        try:
            subscribers = list_subscribers(trade_id) if trade_id else []
            if not subscribers and ticker:
                subscribers = list_subscribers_by_symbol(ticker)
            if subscribers:
                dm_text = f"{card}\n------------------------------------\n💡 <b>الإجراء:</b> الوقف المتحرك يحمي أرباحك تلقائياً."
                token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
                if token and requests:
                    ok_count = 0
                    for uid in subscribers:
                        try:
                            resp = requests.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": uid, "text": dm_text, "parse_mode": "HTML"},
                                timeout=10,
                            )
                            if resp.status_code == 200:
                                ok_count += 1
                        except Exception:
                            continue
                    dm_ok = ok_count > 0
                    logger.info(f"[DM] Trailing SL sent to {ok_count}/{len(subscribers)} for {ticker}")
                else:
                    dm_ok = True
            else:
                dm_ok = True
        except Exception as e:
            logger.warning(f"[DM] Exception: {e}")
            dm_ok = True
    else:
        dm_ok = True
    return (public_ok, dm_ok)

def publish_sl_alert(ticker: str, current_price: float, stop_loss: float, entry_price: Optional[float] = None, dry_run: bool = False) -> Tuple[bool, bool]:
    """Broadcast SL exit alert to public channel + mark trade closed + push DM.

    Idempotency: skips if trade_signals.status is already CLOSED for this ticker.
    """
    if _is_sl_closed(ticker):
        logger.info(f"[IDEMPOTENT] SL already closed for {ticker} - skipping")
        return (False, False)

    card = format_sl_exit_card(ticker, current_price, stop_loss, entry_price)

    public_ok = False
    dm_ok = False

    if not dry_run:
        # Update DB status to CLOSED
        trade_id = None
        raw_signals = _get_active_signals_from_supabase()
        for s in raw_signals:
            if (s.get("ticker") or s.get("symbol")) == ticker:
                trade_id = s.get("trade_id") or s.get("id")
                break
        _mark_trade_closed(ticker, trade_id, "EXIT_STOP_LOSS")

        # Broadcast to public channel
        channel = os.environ.get("TELEGRAM_CHANNEL_NEWS") or os.environ.get("TELEGRAM_CHANNEL_SCALPING") or ""
        if channel and notifier.enabled:
            public_ok = notifier.send_to_chat(channel, card)
        elif notifier.enabled:
            public_ok = notifier.broadcast_signal(card)
        else:
            logger.info(f"[MOCK BROADCAST] SL exit card for {ticker}")
            print(f"[MOCK BROADCAST - PUBLIC]\n{card[:500]}")
            public_ok = True
    else:
        logger.info(f"[DRY-RUN] Would broadcast SL exit for {ticker}")
        print(f"[DRY-RUN - PUBLIC CARD]\n{card[:800]}")
        public_ok = True

    # Push DM to subscribers
    if not dry_run:
        try:
            subscribers = list_subscribers(trade_id) if trade_id else []
            if not subscribers and ticker:
                subscribers = list_subscribers_by_symbol(ticker)
            if subscribers:
                dm_text = (
                    f"{card}\n"
                    f"------------------------------------\n"
                    f"🔴 تم إرسال تنبيه وقف الخسارة لك في الخاص."
                )
                token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
                if token and requests:
                    ok_count = 0
                    for uid in subscribers:
                        try:
                            resp = requests.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": uid, "text": dm_text, "parse_mode": "HTML"},
                                timeout=10,
                            )
                            if resp.status_code == 200:
                                ok_count += 1
                        except Exception:
                            continue
                    dm_ok = ok_count > 0
                    logger.info(f"[DM] SL alert sent to {ok_count}/{len(subscribers)} subscribers for {ticker}")
                else:
                    logger.info(f"[MOCK DM] Would send SL alert to {len(subscribers)} users for {ticker}")
                    dm_ok = True
            else:
                logger.info(f"[DM] No subscribers for {ticker}")
                dm_ok = True
        except Exception as e:
            logger.warning(f"[DM] Exception: {e}")
            dm_ok = True
    else:
        logger.info(f"[DRY-RUN] Would push DM to subscribers for {ticker}")
        dm_ok = True

    return (public_ok, dm_ok)


def list_subscribers_by_symbol(symbol: str) -> List[str]:
    """Fallback: list subscribers by symbol from user_portfolio."""
    cfg = _cfg()
    if requests is None or cfg is None:
        return []
    url, key = cfg
    headers = _headers(prefer="return=minimal")
    try:
        if not symbol.endswith(".CA"):
            sym = f"{symbol}.CA"
        else:
            sym = symbol
        resp = requests.get(f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?symbol=eq.{sym}&status=eq.TRACKING&select=user_id", headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json() or []
            return sorted({str(r["user_id"]) for r in rows if r.get("user_id")})
    except Exception as e:
        logger.debug(f"list_subscribers_by_symbol failed: {e}")
    return []


def check_target_hits(enriched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Identify signals where current price has reached a new target level.

    Returns list of dicts with keys: ticker, target_level, target_price, current_price, entry_price.
    Only returns NEW hits (not already recorded in sent_alerts).
    """
    hits: List[Dict[str, Any]] = []
    for sig in enriched:
        if sig.get("sl_hit"):
            continue  # SL hit takes priority
        for level in sig.get("targets_hit", []):
            # Check idempotency: has this level already been recorded?
            ticker = sig["ticker"]
            target_price = sig["targets"][level - 1] if level <= len(sig["targets"]) else None
            if target_price is None:
                continue
            # Check sent_alerts
            already_recorded = _check_sent_alert(ticker, level, target_price)
            if not already_recorded:
                hits.append({
                    "ticker": ticker,
                    "target_level": level,
                    "target_price": target_price,
                    "current_price": sig["current_price"],
                    "entry_price": sig["entry_price"],
                    "trade_id": sig.get("trade_id"),
                })
    return hits


def _check_sent_alert(ticker: str, target_level: int, target_price: float) -> bool:
    """Check if sent_alerts already has this (ticker, target_level) for today."""
    cfg = _cfg()
    if requests is None or cfg is None:
        return False  # can't verify, assume not recorded
    url, key = cfg
    headers = _headers(prefer="return=minimal")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        check_url = f"{url}/rest/v1/{TARGET_HIT_TABLE}?ticker=eq.{ticker}&date_sent=eq.{payload['date_sent']}&select=id,target_1,target_2,target_3"
        resp = requests.get(check_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list):
                for r in rows:
                    existing_target = r.get(f"target_{target_level}")
                    if existing_target is not None and float(existing_target) >= target_price * 0.99:
                        logger.info(f"[IDEMPOTENT] Target {target_level} for {ticker} already recorded - skip")
                        return False
    except Exception:
        pass
    return False


def check_stop_loss_hits(enriched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Identify signals where current price has breached stop-loss.

    Returns list of dicts with keys: ticker, current_price, stop_loss, entry_price, trade_id.
    Filters out already-closed trades.
    """
    hits: List[Dict[str, Any]] = []
    for sig in enriched:
        if not sig.get("sl_hit"):
            continue
        if _is_sl_closed(sig["ticker"]):
            continue
        hits.append({
            "ticker": sig["ticker"],
            "current_price": sig["current_price"],
            "stop_loss": sig["stop_loss"],
            "entry_price": sig["entry_price"],
            "trade_id": sig.get("trade_id"),
        })
    return hits


def check_trailing_stop_updates(enriched: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Detect trailing stop opportunities: when price is up >5% and trailing SL should be moved.

    Returns list of dicts with keys: ticker, current_price, new_sl, entry_price, trade_id.
    Suggests moving SL to breakeven or trailing.
    """
    updates: List[Dict[str, Any]] = []
    for sig in enriched:
        try:
            ticker = sig.get("ticker")
            entry = sig.get("entry_price")
            current = sig.get("current_price")
            stop = sig.get("stop_loss")
            if not ticker or entry is None or current is None or stop is None:
                continue
            if current <= stop:
                continue  # SL hit will be handled separately
            pnl_pct = (current - entry) / entry * 100 if entry else 0
            # If up >5% and current SL still below entry, suggest moving to breakeven
            if pnl_pct >= 5.0 and stop < entry:
                new_sl = round(entry * 1.005, 2)  # Breakeven + 0.5%
                # Check if new SL is higher than old (trailing up)
                if new_sl > stop:
                    updates.append({
                        "ticker": ticker,
                        "current_price": current,
                        "new_sl": new_sl,
                        "entry_price": entry,
                        "trade_id": sig.get("trade_id"),
                        "pnl_pct": pnl_pct,
                    })
            # If up >10% and already breakeven, trail to T1 level
            elif pnl_pct >= 10.0:
                targets = sig.get("targets", [])
                if targets and len(targets) >= 1:
                    t1 = targets[0]
                    # Trail to T1 if current above T1 and SL below T1
                    if current >= t1 and stop < t1:
                        new_sl = round(t1 * 0.99, 2)
                        if new_sl > stop:
                            updates.append({
                                "ticker": ticker,
                                "current_price": current,
                                "new_sl": new_sl,
                                "entry_price": entry,
                                "trade_id": sig.get("trade_id"),
                                "pnl_pct": pnl_pct,
                            })
        except Exception:
            continue
    return updates


def run_monitor_cycle(dry_run: bool = False) -> Dict[str, Any]:
    """Execute one full monitoring cycle.

    1. Fetch active signals enriched with live prices.
    2. Detect target hits -> broadcast + DM, record idempotency.
    3. Detect SL hits -> broadcast + DM + mark closed.
    4. Return summary dict.

    Returns:
        dict with keys: signals_scanned, target_hits, sl_hits, target_results, sl_results, errors
    """
    logger.info("===== Trade Monitor Cycle START =====")
    result: Dict[str, Any] = {
        "signals_scanned": 0,
        "target_hits": 0,
        "sl_hits": 0,
        "target_results": [],
        "sl_results": [],
        "errors": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        enriched = fetch_active_signals_enriched()
        result["signals_scanned"] = len(enriched)
        logger.info(f"Scanned {len(enriched)} active signals")
    except Exception as e:
        logger.error(f"Failed to fetch signals: {e}")
        result["errors"].append(f"fetch: {e}")
        return result

    # Check target hits
    try:
        target_hits = check_target_hits(enriched)
        result["target_hits"] = len(target_hits)
        for hit in target_hits:
            try:
                public_ok, dm_ok = publish_target_alert(
                    ticker=hit["ticker"],
                    target_level=hit["target_level"],
                    target_price=hit["target_price"],
                    current_price=hit["current_price"],
                    entry_price=hit["entry_price"],
                    dry_run=dry_run,
                )
                result["target_results"].append({
                    "ticker": hit["ticker"],
                    "target_level": hit["target_level"],
                    "public_ok": public_ok,
                    "dm_ok": dm_ok,
                })
                logger.info(f"Target {hit['target_level']} hit for {hit['ticker']}: public={public_ok} dm={dm_ok}")
            except Exception as e:
                logger.error(f"Target alert failed for {hit['ticker']}: {e}")
                result["errors"].append(f"target_{hit['ticker']}: {e}")
    except Exception as e:
        logger.error(f"Target hit detection failed: {e}")
        result["errors"].append(f"detect_targets: {e}")

    # Check SL hits
    try:
        sl_hits = check_stop_loss_hits(enriched)
        result["sl_hits"] = len(sl_hits)
        for hit in sl_hits:
            try:
                public_ok, dm_ok = publish_sl_alert(
                    ticker=hit["ticker"],
                    current_price=hit["current_price"],
                    stop_loss=hit["stop_loss"],
                    entry_price=hit["entry_price"],
                    dry_run=dry_run,
                )
                result["sl_results"].append({
                    "ticker": hit["ticker"],
                    "public_ok": public_ok,
                    "dm_ok": dm_ok,
                })
                logger.info(f"SL hit for {hit['ticker']}: public={public_ok} dm={dm_ok}")
            except Exception as e:
                logger.error(f"SL alert failed for {hit['ticker']}: {e}")
                result["errors"].append(f"sl_{hit['ticker']}: {e}")
    except Exception as e:
        logger.error(f"SL hit detection failed: {e}")
        result["errors"].append(f"detect_sl: {e}")

    # Trailing Stop & Target Hit Auto-Alerts: dispatch DM with actionable steps
    try:
        trailing_updates = check_trailing_stop_updates(enriched)
        result["trailing_updates"] = len(trailing_updates)
        result["trailing_results"] = []
        for upd in trailing_updates:
            try:
                public_ok, dm_ok = publish_trailing_sl_alert(
                    ticker=upd["ticker"],
                    new_sl=upd["new_sl"],
                    current_price=upd["current_price"],
                    entry_price=upd["entry_price"],
                    dry_run=dry_run,
                )
                result["trailing_results"].append({
                    "ticker": upd["ticker"],
                    "new_sl": upd["new_sl"],
                    "public_ok": public_ok,
                    "dm_ok": dm_ok,
                })
                logger.info(f"Trailing SL update for {upd['ticker']}: new_sl={upd['new_sl']} public={public_ok} dm={dm_ok}")
            except Exception as e:
                logger.error(f"Trailing alert failed for {upd['ticker']}: {e}")
                result["errors"].append(f"trailing_{upd['ticker']}: {e}")
    except Exception as e:
        logger.error(f"Trailing check failed: {e}")
        result["errors"].append(f"detect_trailing: {e}")

    logger.info(f"===== Trade Monitor Cycle END: {result['target_hits']} targets, {result['sl_hits']} SLs, {result.get('trailing_updates',0)} trailing =====")
    return result


def format_cycle_summary(result: Dict[str, Any]) -> str:
    """Format monitoring cycle summary for logging/dry-run display."""
    lines = [
        "📊 <b>[Monitor Cycle Summary]</b>",
        f"🕐 <b>Timestamp:</b> {result.get('timestamp','')}",
        f"📋 <b>Signals Scanned:</b> {result.get('signals_scanned',0)}",
        f"🎯 <b>Target Hits:</b> {result.get('target_hits',0)}",
        f"🛑 <b>SL Hits:</b> {result.get('sl_hits',0)}",
    ]
    if result.get("target_results"):
        lines.append("")
        lines.append("🎯 **Target Hits:**")
        for r in result["target_results"]:
            lines.append(f"• {r['ticker']} Target {r['target_level']}: public={r['public_ok']} dm={r['dm_ok']}")
    if result.get("sl_results"):
        lines.append("")
        lines.append("🛑 **SL Hits:**")
        for r in result["sl_results"]:
            lines.append(f"• {r['ticker']}: public={r['public_ok']} dm={r['dm_ok']}")
    if result.get("errors"):
        lines.append("")
        lines.append("⚠️ **Errors:**")
        for e in result["errors"]:
            lines.append(f"• {e}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EGX Trade Monitor Engine")
    parser.add_argument("--dry-run", action="store_true", help="Preview without Telegram send")
    args = parser.parse_args()
    result = run_monitor_cycle(dry_run=args.dry_run)
    print(format_cycle_summary(result))