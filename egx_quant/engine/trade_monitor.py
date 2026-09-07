#!/usr/bin/env python3
"""
Real-Time Target Hit & Stop-Loss Monitor Engine.

Scans active trade_signals against live market prices, detects target hits,
stop-loss breaches and trailing-stop moves, updates state, and dispatches
alerts as PRIVATE DMs to tracking users only.

Routing policy (per system agreement):
  - Open-trade management alerts (targets / SL / trailing) are PRIVATE per
    user - they NEVER go to public broadcast channels. Public channels carry
    only NEW signal teasers from the scanner.

Idempotency (claim-first, per EVENT - fixes the 15-minute alert loop):
  - public.notified_events.event_key is a UNIQUE claim store:
      SL:{ticker}:{signal_id}              - one SL exit alert per trade, ever
      T{level}:{ticker}:{signal_id}        - one alert per target level per trade
          (the old date-scoped sent_alerts check re-announced T1 every new day)
      TRAIL:{ticker}:{signal_id}:{new_sl}  - one alert per actual stop move
  - SL close PATCHes by PRIMARY KEY `id` (the live schema has no `trade_id`
    column - the old `?trade_id=eq.` filter returned HTTP 400, kept the trade
    ACTIVE forever, and was the root cause of the 15-minute SL loop).
  - Trailing moves persist current_stop_loss BEFORE announcing; if the
    persist fails the alert is suppressed (never a phantom stop move).
  - Suppressed alerts (failed close/persist/delivery) trigger a throttled
    admin alert so signals never die silently.

Rate limiting:
  - DM loops sleep RATE_LIMIT_DELAY_SECONDS between sends (~20 msg/sec) and
    back off on Telegram 429 flood responses (bot cap ~30 msg/sec).

Schedule:
  - Runs every 15 minutes inside the scanner session window
    (Sun-Thu 10:00-14:30 Cairo) - see api/scanner.py / scripts/setup_cronjobs.py.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
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
NOTIFIED_EVENTS_TABLE = "notified_events"

# Telegram rate-limit guard: the bot-wide broadcast cap is ~30 msg/sec.
# 0.05s spacing caps every DM loop at ~20 msg/sec; 429 responses additionally
# back off using the retry_after the API returns.
RATE_LIMIT_DELAY_SECONDS = 0.05


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


def _was_notified(event_key: str) -> bool:
    """True when event_key already exists in notified_events (claim store).

    Fail-open: unreachable Supabase returns False (proceed to notify) with a
    loud log - a missed dedup check is safer than silently dropping alerts.
    """
    cfg = _cfg()
    if requests is None or cfg is None:
        logger.warning("[IDEMPOTENT] No Supabase config - cannot check %s (fail-open)", event_key)
        return False
    url, _ = cfg
    try:
        resp = requests.get(
            f"{url}/rest/v1/{NOTIFIED_EVENTS_TABLE}?event_key=eq.{event_key}&select=id",
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and rows:
                logger.info("[IDEMPOTENT] %s already notified - skip", event_key)
                return True
            return False
        logger.warning("[IDEMPOTENT] check %s failed HTTP %s: %s", event_key, resp.status_code, resp.text[:150])
        return False
    except Exception as e:
        logger.warning("[IDEMPOTENT] check %s exception: %s", event_key, e)
        return False


def _record_event(event_key: str, event_type: str, ticker: str,
                  signal_id: Optional[int] = None, payload: Optional[Dict[str, Any]] = None) -> bool:
    """Insert a claim row. False on duplicate/race (caller must NOT notify)."""
    cfg = _cfg()
    if requests is None or cfg is None:
        return False
    url, _ = cfg
    body = {
        "event_key": event_key,
        "event_type": event_type,
        "ticker": ticker,
        "signal_id": signal_id,
        "payload": payload or {},
    }
    try:
        resp = requests.post(
            f"{url}/rest/v1/{NOTIFIED_EVENTS_TABLE}?on_conflict=event_key",
            json=body,
            headers=_headers(prefer="resolution=ignore-duplicates,return=minimal"),
            timeout=10,
        )
        if resp.status_code in (200, 201, 204):
            logger.info("[EVENT] recorded %s", event_key)
            return True
        if resp.status_code == 409:
            logger.info("[EVENT] %s already recorded (409 race) - skip", event_key)
            return False
        logger.warning("[EVENT] record %s failed HTTP %s: %s", event_key, resp.status_code, resp.text[:150])
        return False
    except Exception as e:
        logger.warning("[EVENT] record %s exception: %s", event_key, e)
        return False


def _unclaim_event(event_key: str) -> None:
    """Remove a claim so a failed delivery can be retried next cycle."""
    cfg = _cfg()
    if requests is None or cfg is None:
        return
    url, _ = cfg
    try:
        requests.delete(
            f"{url}/rest/v1/{NOTIFIED_EVENTS_TABLE}?event_key=eq.{event_key}",
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        logger.info("[EVENT] unclaimed %s (delivery failed - will retry)", event_key)
    except Exception as e:
        logger.warning("[EVENT] unclaim %s exception: %s", event_key, e)


def _event_key(kind: str, ticker: str, signal_id: Optional[int], suffix: str = "") -> str:
    sid = signal_id if signal_id is not None else "NA"
    return f"{kind}:{ticker}:{sid}{suffix}"


def _record_target_hit(ticker: str, target_level: int, target_price: float,
                       current_price: float, signal_id: Optional[int] = None) -> bool:
    """Claim (ticker, target_level) hit. True if newly claimed, False if duplicate.

    Replaces the old date-scoped sent_alerts dedup (which re-announced the same
    target on every new day) with a per-trade notified_events claim.
    """
    key = _event_key(f"T{target_level}", ticker, signal_id)
    if _was_notified(key):
        return False
    return _record_event(key, "TARGET_HIT", ticker, signal_id,
                         {"target_price": target_price, "current_price": current_price})


def _check_sent_alert(ticker: str, target_level: int, target_price: float,
                      signal_id: Optional[int] = None) -> bool:
    """True when (ticker, target_level) already recorded.

    Legacy name kept for verify_trade_monitor compatibility; now backed by
    notified_events (the old body referenced an undefined `payload` variable,
    raised NameError, and was swallowed into always-False).
    """
    key = _event_key(f"T{target_level}", ticker, signal_id)
    return _was_notified(key)


def _admin_chat_ids() -> List[str]:
    """Admin recipients: ADMIN_USER_IDS / ADMIN_TELEGRAM_IDS (comma separated),
    falling back to TELEGRAM_USER_CHAT_ID / TELEGRAM_CHAT_ID."""
    ids: List[str] = []
    raw = (os.environ.get("ADMIN_USER_IDS") or os.environ.get("ADMIN_TELEGRAM_IDS") or "")
    for part in raw.replace(" ", "").split(","):
        p = part.strip()
        if p and p not in ids:
            ids.append(p)
    fallback = (os.environ.get("TELEGRAM_USER_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if fallback and fallback not in ids:
        ids.append(fallback)
    return ids


def _notify_admin(subject: str, detail: str, throttle_key: Optional[str] = None) -> bool:
    """Push a system-failure alert to the admin(s).

    Used whenever a DB write fails and the user-facing alert gets suppressed
    (SL close / trailing persist / DM delivery) so signals never die silently.
    Throttled to ONE alert per key per UTC day via notified_events - a
    persistently failing PATCH must not spam the admin every monitor cycle.
    Always logged at ERROR level regardless of delivery.
    """
    logger.error("[ADMIN-ALERT] %s: %s", subject, detail)
    if throttle_key:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if _was_notified(_event_key("ADMIN", "SYSTEM", None, f":{throttle_key}:{day}")):
            return True
        _record_event(_event_key("ADMIN", "SYSTEM", None, f":{throttle_key}:{day}"),
                      "ADMIN_ALERT", "SYSTEM", None, {"subject": subject})
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning("[ADMIN-ALERT] no TELEGRAM_BOT_TOKEN - logged only")
        return False
    text = (
        "⚠️ <b>[تنبيه النظام] فشل في محرك متابعة الصفقات</b>\n"
        "------------------------------------\n"
        f"📌 <b>{subject}</b>\n"
        f"{detail}\n"
        "------------------------------------\n"
        "⚠️ التنبيه الخاص بهذا الحدث تم كبحه هذه الدورة لتجنب إعادة الإرسال - راجع النظام.\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    sent = False
    ids = _admin_chat_ids()
    for idx, uid in enumerate(ids):
        if idx:
            time.sleep(RATE_LIMIT_DELAY_SECONDS)
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": uid, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code == 200:
                sent = True
            else:
                logger.warning("[ADMIN-ALERT] send to %s failed HTTP %s", uid[:8], resp.status_code)
        except Exception as e:
            logger.warning("[ADMIN-ALERT] send to %s exception: %s", uid[:8], e)
    return sent


def _dm_subscribers(ticker: str, signal_id: Optional[int], card: str,
                    footer: str = "", dry_run: bool = False) -> Tuple[bool, int, int]:
    """DM-only dispatch of a trade-management card to tracking users.

    Policy: open-trade management alerts NEVER go to public channels - the
    public feed carries only new-signal teasers from the scanner.
    Rate-limited: sleeps RATE_LIMIT_DELAY_SECONDS between sends and backs off
    on Telegram 429 (retry_after) to respect the ~30 msg/sec bot cap.
    Returns (ok, delivered, total_subscribers). Zero subscribers is success
    (nothing to do); total>0 with delivered==0 is a failure.
    """
    # UNION of both registries (by trade_id AND by symbol) so no tracking user
    # is ever missed when the two disagree (e.g. legacy joins with trade_id=0).
    subscribers: List[str] = []
    try:
        if signal_id is not None:
            subscribers.extend(list_subscribers(signal_id))
    except Exception as e:
        logger.warning("[DM] list_subscribers failed: %s", e)
    try:
        subscribers.extend(list_subscribers_by_symbol(ticker))
    except Exception as e:
        logger.warning("[DM] list_subscribers_by_symbol failed: %s", e)
    subscribers = sorted({str(u) for u in subscribers if u})
    if not subscribers:
        logger.info("[DM] no tracking users for %s (signal_id=%s) - nothing sent", ticker, signal_id)
        return (True, 0, 0)
    text = f"{card}\n{footer}" if footer else card
    if dry_run:
        logger.info("[DRY-RUN DM] would send to %d subscriber(s) for %s", len(subscribers), ticker)
        for uid in subscribers:
            print(f"[DRY-RUN DM -> {uid[:8]}]\n{text[:400]}")
        return (True, len(subscribers), len(subscribers))
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if requests is None or not token:
        logger.error("[DM] token/requests missing - %d subscriber(s) NOT notified for %s", len(subscribers), ticker)
        return (False, 0, len(subscribers))
    delivered = 0
    for idx, uid in enumerate(subscribers):
        if idx:
            time.sleep(RATE_LIMIT_DELAY_SECONDS)
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": uid, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code == 200:
                delivered += 1
                continue
            if resp.status_code == 429:
                # Flood control: honor retry_after, back off, retry once
                try:
                    retry_after = float(resp.json().get("parameters", {}).get("retry_after", 1.0))
                except Exception:
                    retry_after = 1.0
                retry_after = min(retry_after, 5.0)
                logger.warning("[DM] 429 flood for %s - backing off %.1fs", uid[:8], retry_after)
                time.sleep(retry_after)
                try:
                    retry = requests.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": uid, "text": text, "parse_mode": "HTML"},
                        timeout=10,
                    )
                    if retry.status_code == 200:
                        delivered += 1
                except Exception:
                    pass
                continue
            logger.warning("[DM] send to %s failed HTTP %s: %s", uid[:8], resp.status_code, resp.text[:120])
        except Exception as e:
            logger.warning("[DM] exception sending to %s: %s", uid[:8], e)
    logger.info("[DM] %s: delivered %d/%d", ticker, delivered, len(subscribers))
    return (delivered > 0, delivered, len(subscribers))


def _resolve_signal_id(ticker: str) -> Optional[int]:
    """Latest ACTIVE/TRACKING/OPEN trade_signals.id for ticker (PK for PATCHes)."""
    cfg = _cfg()
    if requests is None or cfg is None:
        return None
    url, _ = cfg
    headers = _headers(prefer="return=minimal")
    for status_q in ("&status=in.(ACTIVE,TRACKING,OPEN)", ""):
        try:
            resp = requests.get(
                f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?ticker=eq.{ticker}{status_q}"
                f"&order=created_at.desc&limit=1&select=id",
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    return rows[0].get("id")
        except Exception as e:
            logger.warning("[RESOLVE] %s failed: %s", ticker, e)
    return None


def _mark_trade_closed(ticker: str, signal_id: Optional[int], reason: str) -> bool:
    """Close the trade_signals row (by PRIMARY KEY `id`) and mirror-close
    user_portfolio rows.

    Root-cause fix: the live schema has NO `trade_id` column (PK is `id`) - the
    previous `?trade_id=eq.` PATCH returned HTTP 400 on every cycle, the trade
    stayed ACTIVE, and the SL alert looped every 15 minutes.
    """
    cfg = _cfg()
    if requests is None or cfg is None:
        logger.warning("No Supabase config - cannot mark trade closed")
        return False
    url, _ = cfg
    headers = _headers(prefer="return=minimal")
    updated = False

    # Resolve the real PK when not provided (PATCH must filter id=eq.)
    if signal_id is None:
        signal_id = _resolve_signal_id(ticker)
        if signal_id is None:
            logger.error("[CLOSED] cannot close %s: no ACTIVE signal row found", ticker)

    if signal_id is not None:
        try:
            resp = requests.patch(
                f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?id=eq.{signal_id}",
                json={"status": "CLOSED", "exit_reason": reason},
                headers=headers, timeout=10,
            )
            if resp.status_code in (200, 204):
                logger.info("[CLOSED] trade_signals id=%s (%s) -> CLOSED (%s)", signal_id, ticker, reason)
                updated = True
            else:
                logger.error("[CLOSED] trade_signals PATCH id=%s failed HTTP %s: %s",
                             signal_id, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("[CLOSED] trade_signals PATCH id=%s exception: %s", signal_id, e)

    # Mirror close on user_portfolio (symbol stored WITH the .CA suffix).
    # EXITED is the legacy check-constraint fallback for pre-005-migration DBs.
    try:
        sym = ticker if ticker.endswith(".CA") else f"{ticker}.CA"
        for status_value in ("CLOSED", "EXITED"):
            try:
                resp = requests.patch(
                    f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?symbol=eq.{sym}&status=eq.TRACKING",
                    json={"status": status_value},
                    headers=headers, timeout=10,
                )
                if resp.status_code in (200, 204):
                    logger.info("[CLOSED] user_portfolio %s -> %s", sym, status_value)
                    break
            except Exception as e:
                logger.warning("[CLOSED] user_portfolio exception: %s", e)
                break
    except Exception as e:
        logger.warning("[CLOSED] user_portfolio exception: %s", e)
    return updated


def _is_sl_closed(ticker: str, signal_id: Optional[int] = None) -> bool:
    """True when the signal row is already CLOSED (SL already processed).
    Prefers the primary key when available; falls back to ticker."""
    cfg = _cfg()
    if requests is None or cfg is None:
        return False
    url, _ = cfg
    q = f"id=eq.{signal_id}" if signal_id is not None else f"ticker=eq.{ticker}"
    try:
        resp = requests.get(
            f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?{q}&status=eq.CLOSED&limit=1&select=id",
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        if resp.status_code == 200:
            rows = resp.json()
            return isinstance(rows, list) and bool(rows)
    except Exception as e:
        logger.debug("[SL-CLOSED] check failed: %s", e)
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
            # SL hit: price at/below the stop (0.2% epsilon for feed rounding).
            # The old *1.02 tolerance treated a price 2% ABOVE the SL as a hit -
            # the phantom trigger behind the repeated CLHO alerts (17.70 vs SL 17.56).
            sl_hit = stop_f is not None and current is not None and current <= stop_f * 1.002
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


def publish_target_alert(ticker: str, target_level: int, target_price: float, current_price: float, entry_price: Optional[float] = None, dry_run: bool = False, trade_id: Optional[int] = None) -> Tuple[bool, bool]:
    """DM-only target-hit dispatch with claim-first idempotency.

    Policy change: trade-management cards NO LONGER broadcast to public
    channels - open-trade monitoring is private per user.
    Returns (dispatched, dm_ok); first element is True whenever the event was
    handled (DM-only by design), False when suppressed/duplicate.
    """
    signal_id = trade_id if trade_id is not None else _resolve_signal_id(ticker)
    key = _event_key(f"T{target_level}", ticker, signal_id)
    if _was_notified(key):
        logger.info(f"[IDEMPOTENT] Target {target_level} hit for {ticker} already sent - skipping")
        return (False, False)
    if not dry_run:
        if not _record_event(key, "TARGET_HIT", ticker, signal_id,
                             {"target_price": target_price, "current_price": current_price}):
            logger.warning(f"[TARGET] claim failed for {key} - suppressed this cycle")
            return (False, False)

    card = format_target_hit_card(ticker, target_level, target_price, current_price, entry_price)

    # Determine actionable suggestion based on target level
    if target_level == 1:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> بيع 50% من الكمية عند T1 وحرك وقف الخسارة إلى نقطة الدخول (Breakeven) لتأمين الأرباح."
    elif target_level == 2:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> بيع 25% إضافية عند T2 وحافظ على وقف متحرك تحت T1."
    elif target_level >= 3:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> جني الأرباح المتبقية أو الإغلاق الكامل - الهدف النهائي تحقق."
    else:
        action_suggestion = "💡 <b>الإجراء المقترح:</b> مراجعة الصفقة وتحديث وقف الخسارة."

    dm_ok, delivered, total = _dm_subscribers(
        ticker, signal_id, card,
        footer=f"{action_suggestion}\n📩 تم إرسال تنبيه الهدف لك في الخاص.",
        dry_run=dry_run,
    )
    if not dry_run and total > 0 and delivered == 0:
        _unclaim_event(key)
        _notify_admin("Target DM delivery failed",
                      f"{key}: 0/{total} delivered - claim released for retry.",
                      throttle_key=f"target-dm:{ticker}")
    return (True, dm_ok)


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

def publish_trailing_sl_alert(ticker: str, new_sl: float, current_price: float, entry_price: Optional[float] = None, dry_run: bool = False, trade_id: Optional[int] = None, stored_sl: Optional[float] = None) -> Tuple[bool, bool]:
    """Persist-then-announce trailing SL update (DM-only).

    Loop fix: the old version computed new_sl but NEVER persisted it to
    trade_signals.current_stop_loss, so the same move re-fired every cycle
    (SUGR 56.35 / SKPC 17.89 loop). Now:
      1. claim TRAIL:{ticker}:{signal_id}:{new_sl}
      2. PATCH current_stop_loss (and stop_loss) - MUST succeed
      3. only then DM subscribers; persist failure = suppress + admin alert
    """
    signal_id = trade_id if trade_id is not None else _resolve_signal_id(ticker)
    key = _event_key("TRAIL", ticker, signal_id, f":{new_sl}")
    if _was_notified(key):
        return (False, False)

    if not dry_run:
        persisted = False
        cfg = _cfg()
        if requests is not None and cfg is not None and signal_id is not None:
            url, _ = cfg
            try:
                resp = requests.patch(
                    f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?id=eq.{signal_id}",
                    json={"current_stop_loss": new_sl, "stop_loss": new_sl},
                    headers=_headers(prefer="return=minimal"),
                    timeout=10,
                )
                persisted = resp.status_code in (200, 204)
                if not persisted:
                    logger.error("[TRAIL] persist %s failed HTTP %s: %s", ticker, resp.status_code, resp.text[:200])
            except Exception as e:
                logger.error("[TRAIL] persist %s exception: %s", ticker, e)
        if not persisted:
            _notify_admin(
                "Trailing SL persist failed",
                f"{ticker}: new_sl={new_sl} NOT persisted to trade_signals - trailing alert suppressed to avoid a repeat loop.",
                throttle_key=f"trail-persist:{ticker}",
            )
            return (False, False)
        _record_event(key, "TRAILING_SL", ticker, signal_id, {"new_sl": new_sl, "previous_stop": stored_sl})

    card = format_trailing_sl_update(ticker, new_sl, current_price, entry_price)
    dm_ok, delivered, total = _dm_subscribers(
        ticker, signal_id, card,
        footer="💡 <b>الإجراء:</b> الوقف المتحرك يحمي أرباحك تلقائياً.",
        dry_run=dry_run,
    )
    if not dry_run and total > 0 and delivered == 0:
        _unclaim_event(key)
        _notify_admin("Trailing DM delivery failed",
                      f"{ticker}: 0/{total} delivered - claim released for retry.",
                      throttle_key=f"trail-dm:{ticker}")
    return (True, dm_ok)

def publish_sl_alert(ticker: str, current_price: float, stop_loss: float, entry_price: Optional[float] = None, dry_run: bool = False, trade_id: Optional[int] = None) -> Tuple[bool, bool]:
    """Close-first SL dispatch (DM-only).

    Loop-fix chain:
      1. claim SL:{ticker}:{signal_id} (never re-announce)
      2. close the trade FIRST via _mark_trade_closed (by PK id)
      3. close write FAILED -> suppress alert + throttled admin alert
         (an un-closed trade would otherwise re-alert every 15 minutes)
      4. success -> DM subscribers only; delivery failure releases the claim
    """
    signal_id = trade_id if trade_id is not None else _resolve_signal_id(ticker)
    key = _event_key("SL", ticker, signal_id)
    if _was_notified(key) or _is_sl_closed(ticker, signal_id):
        if not dry_run:
            # Backfill the claim so legacy-closed trades skip via both guards.
            _record_event(key, "SL_HIT", ticker, signal_id, {"note": "already closed"})
        logger.info(f"[IDEMPOTENT] SL for {ticker} already closed/notified - skipping")
        return (False, False)

    if not dry_run:
        if not _mark_trade_closed(ticker, signal_id, "EXIT_STOP_LOSS"):
            _notify_admin(
                "SL close failed",
                f"{ticker}: trade_signals close PATCH failed - SL alert suppressed to avoid the 15-minute loop.",
                throttle_key=f"sl-close:{ticker}",
            )
            return (False, False)
        _record_event(key, "SL_HIT", ticker, signal_id, {"price": current_price})

    card = format_sl_exit_card(ticker, current_price, stop_loss, entry_price)
    dm_ok, delivered, total = _dm_subscribers(
        ticker, signal_id, card,
        footer="📩 تم إرسال تنبيه وقف الخسارة لك في الخاص.",
        dry_run=dry_run,
    )
    if not dry_run and total > 0 and delivered == 0:
        _unclaim_event(key)
        _notify_admin("SL DM delivery failed",
                      f"{ticker}: 0/{total} delivered - claim released for retry.",
                      throttle_key=f"sl-dm:{ticker}")
    return (True, dm_ok)


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
    """Identify signals where current price has reached a NEW target level.

    Returns list of dicts with keys: ticker, target_level, target_price,
    current_price, entry_price, trade_id.
    Only returns NEW hits - the claim key T{level}:{ticker}:{signal_id} is
    per-trade (not per-day like the old sent_alerts check).
    """
    hits: List[Dict[str, Any]] = []
    for sig in enriched:
        if sig.get("sl_hit"):
            continue  # SL hit takes priority
        ticker = sig["ticker"]
        signal_id = sig.get("trade_id")
        for level in sig.get("targets_hit", []):
            target_price = sig["targets"][level - 1] if level <= len(sig["targets"]) else None
            if target_price is None:
                continue
            if _was_notified(_event_key(f"T{level}", ticker, signal_id)):
                continue
            hits.append({
                "ticker": ticker,
                "target_level": level,
                "target_price": target_price,
                "current_price": sig["current_price"],
                "entry_price": sig["entry_price"],
                "trade_id": signal_id,
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

    Returns list of dicts with keys: ticker, current_price, stop_loss,
    entry_price, trade_id. Filters out already-closed trades (by PK id).
    """
    hits: List[Dict[str, Any]] = []
    for sig in enriched:
        if not sig.get("sl_hit"):
            continue
        if _is_sl_closed(sig["ticker"], sig.get("trade_id")):
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
    """Detect trailing-stop moves: raise SL to breakeven (+5%) or trail to T1 (+10%).

    The comparison base is the PERSISTED current_stop_loss (not the original
    plan SL), so a move is proposed only when the stop genuinely still needs
    raising. The publisher persists the new value BEFORE announcing - this is
    what ended the identical-value-every-15-minutes trailing loop.
    """
    updates: List[Dict[str, Any]] = []
    for sig in enriched:
        try:
            ticker = sig.get("ticker")
            entry = sig.get("entry_price")
            current = sig.get("current_price")
            raw = sig.get("raw") or {}
            # Persisted stop wins; fall back to the plan stop for legacy rows
            stored_sl = raw.get("current_stop_loss") or sig.get("stop_loss")
            if not ticker or entry is None or current is None or stored_sl is None:
                continue
            if current <= stored_sl:
                continue  # SL hit handled separately
            pnl_pct = (current - entry) / entry * 100 if entry else 0
            new_sl = None
            # If up >5% and the stored SL is still below entry, move to breakeven
            if pnl_pct >= 5.0 and stored_sl < entry:
                new_sl = round(entry * 1.005, 2)  # Breakeven + 0.5%
            # If up >10% (and already at/above breakeven), trail to just under T1
            elif pnl_pct >= 10.0:
                targets = sig.get("targets", [])
                if targets:
                    t1 = targets[0]
                    if current >= t1 and stored_sl < t1:
                        new_sl = round(t1 * 0.99, 2)
            if new_sl is not None and new_sl > stored_sl:
                updates.append({
                    "ticker": ticker,
                    "current_price": current,
                    "new_sl": new_sl,
                    "stored_sl": stored_sl,
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
    2. Detect target hits -> claim event, DM-only dispatch.
    3. Detect SL hits -> claim event, close trade FIRST (suppress + admin
       alert when the close write fails), DM-only dispatch.
    4. Detect trailing moves -> persist stop FIRST (suppress + admin alert
       when the persist fails), DM-only dispatch.
    5. Return summary dict.

    Returns:
        dict with keys: signals_scanned, target_hits, sl_hits, target_results,
        sl_results, trailing_results, errors
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
                dispatched, dm_ok = publish_target_alert(
                    ticker=hit["ticker"],
                    target_level=hit["target_level"],
                    target_price=hit["target_price"],
                    current_price=hit["current_price"],
                    entry_price=hit["entry_price"],
                    dry_run=dry_run,
                    trade_id=hit.get("trade_id"),
                )
                result["target_results"].append({
                    "ticker": hit["ticker"],
                    "target_level": hit["target_level"],
                    "channel": "dm_only",
                    "dispatched": dispatched,
                    "dm_ok": dm_ok,
                })
                logger.info(f"Target {hit['target_level']} hit for {hit['ticker']}: dispatched={dispatched} dm={dm_ok}")
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
                dispatched, dm_ok = publish_sl_alert(
                    ticker=hit["ticker"],
                    current_price=hit["current_price"],
                    stop_loss=hit["stop_loss"],
                    entry_price=hit["entry_price"],
                    dry_run=dry_run,
                    trade_id=hit.get("trade_id"),
                )
                result["sl_results"].append({
                    "ticker": hit["ticker"],
                    "channel": "dm_only",
                    "dispatched": dispatched,
                    "dm_ok": dm_ok,
                })
                logger.info(f"SL hit for {hit['ticker']}: dispatched={dispatched} dm={dm_ok}")
            except Exception as e:
                logger.error(f"SL alert failed for {hit['ticker']}: {e}")
                result["errors"].append(f"sl_{hit['ticker']}: {e}")
    except Exception as e:
        logger.error(f"SL hit detection failed: {e}")
        result["errors"].append(f"detect_sl: {e}")

    # Trailing Stop: persist-then-announce, DM-only
    try:
        trailing_updates = check_trailing_stop_updates(enriched)
        result["trailing_updates"] = len(trailing_updates)
        result["trailing_results"] = []
        for upd in trailing_updates:
            try:
                dispatched, dm_ok = publish_trailing_sl_alert(
                    ticker=upd["ticker"],
                    new_sl=upd["new_sl"],
                    current_price=upd["current_price"],
                    entry_price=upd["entry_price"],
                    dry_run=dry_run,
                    trade_id=upd.get("trade_id"),
                    stored_sl=upd.get("stored_sl"),
                )
                result["trailing_results"].append({
                    "ticker": upd["ticker"],
                    "new_sl": upd["new_sl"],
                    "channel": "dm_only",
                    "dispatched": dispatched,
                    "dm_ok": dm_ok,
                })
                logger.info(f"Trailing SL update for {upd['ticker']}: new_sl={upd['new_sl']} dispatched={dispatched} dm={dm_ok}")
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
            lines.append(f"• {r['ticker']} Target {r['target_level']}: dispatched={r['dispatched']} dm={r['dm_ok']} (dm_only)")
    if result.get("sl_results"):
        lines.append("")
        lines.append("🛑 **SL Hits:**")
        for r in result["sl_results"]:
            lines.append(f"• {r['ticker']}: dispatched={r['dispatched']} dm={r['dm_ok']} (dm_only)")
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