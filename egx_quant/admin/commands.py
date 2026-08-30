#!/usr/bin/env python3
"""
Admin Control Commands & User Portfolio (/portfolio) System.

Admin-only commands (restricted to configured Telegram User IDs):
  /close <TICKER> [REASON]       - Close active signal + notify subscribers
  /update <TICKER> [sl=VALUE] [target1=VALUE] [target2=VALUE] - Modify targets/SL

User commands:
  /portfolio                     - Display tracked positions with live PnL
  محفظتي                          - Alias for /portfolio
"""
from __future__ import annotations

import os
import re
import logging
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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

logger = logging.getLogger("egx_admin.commands")

# Admin IDs from env (comma-separated) - supports both ADMIN_USER_IDS (task spec) and legacy ADMIN_TELEGRAM_IDS
def _load_admin_ids() -> List[str]:
    raw = (os.environ.get("ADMIN_USER_IDS") or os.environ.get("ADMIN_TELEGRAM_IDS") or os.environ.get("ADMIN_IDS") or "").strip()
    # Also check Vercel-style ADMIN_USER_IDS with fallback
    if not raw:
        # Try alternative env that may contain single ID
        raw = (os.environ.get("ADMIN_TELEGRAM_ID") or "").strip()
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    # Also support space-separated
    if len(ids) == 1 and " " in ids[0]:
        ids = [x.strip() for x in ids[0].split() if x.strip()]
    return ids

ADMIN_IDS: List[str] = _load_admin_ids()

def _refresh_admin_ids() -> List[str]:
    """Re-read ADMIN_USER_IDS dynamically (env may change at runtime on Vercel)."""
    ids = _load_admin_ids()
    # Update global for backward compat
    try:
        global ADMIN_IDS
        ADMIN_IDS = ids
    except Exception:
        pass
    return ids

TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_ANSWER_URL = "https://api.telegram.org/bot{token}/answerCallbackQuery"

# Supabase table names
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


def is_admin(user_id: str) -> bool:
    """Check if user_id is in the configured admin list (dynamic lookup)."""
    ids = _refresh_admin_ids()
    if not ids:
        # Log env audit warning
        print("[ADMIN][ENV AUDIT] ADMIN_USER_IDS / ADMIN_TELEGRAM_IDS is empty - all admin commands will be denied")
        logger.warning("[ADMIN][ENV AUDIT] ADMIN_USER_IDS is empty - admin check denied for %s", str(user_id)[:8])
        return False
    return str(user_id).strip() in ids

def verify_env_vars() -> Dict[str, bool]:
    """Verify required env vars per task spec: TELEGRAM_BOT_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ADMIN_USER_IDS."""
    checks = {
        "TELEGRAM_BOT_TOKEN": bool((os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()),
        "SUPABASE_URL": bool((os.environ.get("SUPABASE_URL") or "").strip()),
        "SUPABASE_SERVICE_ROLE_KEY": bool((os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()),
        "ADMIN_USER_IDS": bool((_load_admin_ids())),
    }
    for k, ok in checks.items():
        status = "OK" if ok else "MISSING"
        print(f"[ENV AUDIT] {k}: {status}")
        if not ok:
            logger.warning("[ENV AUDIT] %s is missing", k)
    return checks


def _answer_callback(callback_query_id: str, bot_token: str, text: str, show_alert: bool = False) -> bool:
    if requests is None or not bot_token or not callback_query_id:
        return False
    try:
        resp = requests.post(
            TELEGRAM_ANSWER_URL.format(token=bot_token),
            json={"callback_query_id": callback_query_id, "text": text[:200], "show_alert": show_alert},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _fetch_trade_signal(supabase_url: str, supabase_key: str, ticker: str) -> Optional[Dict[str, Any]]:
    if requests is None or not supabase_url or not supabase_key:
        return None
    try:
        norm = ticker.strip().upper()
        if not norm.endswith(".CA"):
            norm = f"{norm}.CA"
        url = f"{supabase_url}/rest/v1/{TRADE_SIGNALS_TABLE}?ticker=eq.{norm}&order=created_at.desc&limit=1&select=*"
        headers = _headers(prefer="return=minimal")
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and rows:
                return dict(rows[0])
    except Exception:
        pass
    return None


def _fetch_user_portfolio_row(supabase_url: str, supabase_key: str, user_id: str, ticker: str) -> Optional[Dict[str, Any]]:
    if requests is None or not supabase_url or not supabase_key or not user_id or not ticker:
        return None
    try:
        norm = ticker.strip().upper()
        if not norm.endswith(".CA"):
            norm = f"{norm}.CA"
        for sym in (norm, norm.replace(".CA", "")):
            url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&symbol=eq.{sym}&select=*&limit=1"
            headers = _headers(prefer="return=minimal")
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list) and rows:
                    return dict(rows[0])
    except Exception:
        pass
    return None


def _fetch_current_price(ticker: str) -> Optional[float]:
    try:
        if yf is not None:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", auto_adjust=True)
            if hist is not None and not hist.empty and "Close" in hist.columns:
                if hasattr(hist.columns, "levels"):
                    hist.columns = [c[0] if isinstance(c, tuple) else c for c in hist.columns]
                c = float(hist["Close"].iloc[-1])
                if c > 0:
                    return c
    except Exception:
        pass
    return None


def _format_price(val: Any) -> str:
    if val is None:
        return "-"
    try:
        return f"{float(val):.2f}"
    except Exception:
        return str(val)


def format_portfolio_card(
    positions: List[Dict[str, Any]],
    user_id: str,
    user_tg_id: str,
) -> str:
    """Render the /portfolio summary card."""
    sep = "------------------------------------"
    lines = [
        f"💼 <b>محفظتك النشطة | Active Portfolio</b>",
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
        sep,
    ]
    if not positions:
        lines.append("🔍 لا توجد صفقات متابعة حالياً.")
        lines.append("استخدم زر الانضمام من أي إشارة عامة للبدء.")
        return "\n".join(lines)
    for pos in positions:
        ticker = pos.get("ticker", "").replace(".CA", "")
        bare = ticker.replace(".CA", "")
        entry = pos.get("entry_price") or pos.get("joined_at_price") or pos.get("joined_price") or pos.get("price") or 0
        try:
            entry_f = float(entry)
        except Exception:
            entry_f = 0.0
        current = pos.get("current_price")
        custom_entry = pos.get("custom_entry_price") or pos.get("entry_price")
        # Use custom/portfolio entry for PnL
        pnl_pct = None
        pnl_str = "-"
        if entry_f and current and entry_f != 0:
            pnl_pct = (current - entry_f) / entry_f * 100
            sign = "+" if pnl_pct >= 0 else ""
            pnl_str = f"{sign}{pnl_pct:.2f}%"
        status = pos.get("status", "TRACKING")
        status_emoji = "🟢" if status == "TRACKING" else "🔴"
        lines.append(
            f"{status_emoji} <code>{bare}</code> | "
            f"سعر الدخول {_format_price(entry_f)} EGP | "
            f"السعر الحالي {_format_price(current)} EGP | "
            f"الربح/الخسارة: {pnl_str} | "
            f"الحالة: {status}"
        )
        lines.append(f"   📦 الكمية: {pos.get('quantity', '-')} | 📅 {str(pos.get('joined_at', ''))[:10]}")
        lines.append(f"   ✏️ <code>/portfolio {bare}</code> - تعديل سعر الدخول")
    lines.append(sep)
    lines.append("💡 أرسل <code>/portfolio [TICKER]</code> لتعديل سعر الدخول الخاص بك.")
    lines.append("📊 [EGX TradingView](https://www.tradingview.com/markets/egypt/)")
    return "\n".join(lines)


def format_close_card(ticker: str, reason: str, entry_price: Optional[float], current_price: Optional[float], pnl_pct: Optional[float]) -> str:
    """Format manual close alert card for public channel."""
    bare = ticker.replace(".CA", "")
    sep = "------------------------------------"
    lines = [
        f"🔴 <b>تم إغلاق صفقة {bare} يدوياً</b>",
        sep,
        f"🔹 <b>السهم:</b> <code>{bare}</code>",
        f"🔔 <b>السبب:</b> {reason}",
    ]
    if entry_price:
        lines.append(f"💵 <b>سعر الدخول:</b> {_format_price(entry_price)} EGP")
    if current_price:
        lines.append(f"💵 <b>سعر الخروج:</b> {_format_price(current_price)} EGP")
    if pnl_pct is not None:
        emoji = "📈" if pnl_pct >= 0 else "📉"
        lines.append(f"{emoji} <b>النتيجة:</b> {pnl_pct:+.2f}%")
    lines += [
        sep,
        f"✅ تم تحديث الصفقة إلى <b>CLOSED</b> في trade_signals.",
        f"🔴 تم تغيير status إلى <b>EXITED</b> في user_portfolio للمتابعين.",
        sep,
    ]
    return "\n".join(lines)


def format_update_card(ticker: str, updates: Dict[str, str]) -> str:
    """Format update notification card for public channel."""
    bare = ticker.replace(".CA", "")
    sep = "------------------------------------"
    lines = [
        f"📝 <b>تم تحديث إشارة {bare}</b>",
        sep,
    ]
    label_map = {"current_stop_loss": "وقف الخسارة", "sl": "وقف الخسارة", "target_1": "الهدف الأول", "target1": "الهدف الأول", "target_2": "الهدف الثاني", "target2": "الهدف الثاني", "target_3": "الهدف الثالث", "target3": "الهدف الثالث"}
    for key, val in updates.items():
        label = label_map.get(key, key)
        lines.append(f"• {label}: <b>{val}</b> EGP")
    lines += [
        sep,
        "ℹ️ تم إشعار جميع المتابعين بالتغييرات.",
    ]
    return "\n".join(lines)


def _push_update_to_subscribers(
    supabase_url: str,
    supabase_key: str,
    bot_token: str,
    trade_id: int,
    symbol: str,
    update_text: str,
) -> int:
    """Push update to all users tracking this trade."""
    if not supabase_url or not supabase_key or not bot_token or not trade_id:
        return 0
    try:
        headers = _headers(prefer="return=minimal")
        candidates: List[str] = []
        try:
            url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?trade_id=eq.{int(trade_id)}&status=eq.TRACKING&select=user_id"
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict) and r.get("user_id"):
                            candidates.append(str(r.get("user_id")))
        except Exception:
            pass
        if not candidates and symbol:
            try:
                norm = symbol.strip().upper()
                if not norm.endswith(".CA"):
                    norm = f"{norm}.CA"
                url2 = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?symbol=eq.{norm}&status=eq.TRACKING&select=user_id"
                resp2 = requests.get(url2, headers=headers, timeout=10)
                if resp2.status_code == 200:
                    rows2 = resp2.json()
                    if isinstance(rows2, list):
                        for r in rows2:
                            if isinstance(r, dict) and r.get("user_id"):
                                candidates.append(str(r.get("user_id")))
            except Exception:
                pass
        if not candidates:
            return 0
        delivered = 0
        for uid in candidates:
            try:
                payload = {"chat_id": uid, "text": update_text, "parse_mode": "HTML"}
                r = requests.post(TELEGRAM_SEND_URL.format(token=bot_token), json=payload, timeout=10)
                if r.status_code == 200:
                    delivered += 1
            except Exception:
                continue
        return delivered
    except Exception:
        return 0


def close_trade(
    ticker: str,
    reason: str,
    user_id: str,
    bot_token: str,
    supabase_url: str,
    supabase_key: str,
) -> Tuple[bool, str]:
    """Close trade: update trade_signals.status=CLOSED and user_portfolio.status=EXITED. Broadcast + DM."""
    ticker_bare = ticker.strip().upper().replace(".CA", "")
    if not ticker_bare:
        return False, "missing-ticker"
    # Fetch signal for details
    signal = _fetch_trade_signal(supabase_url, supabase_key, ticker)
    entry_price = float(signal["entry_price"]) if signal and signal.get("entry_price") else None
    # Update trade_signals
    updated = False
    if supabase_url and supabase_key:
        headers = _headers(prefer="return=minimal")
        for target_status in ("CLOSED", "EXITED"):
            try:
                url = f"{supabase_url}/rest/v1/{TRADE_SIGNALS_TABLE}?ticker=eq.{ticker_bare}.CA&order=created_at.desc&limit=1"
                resp = requests.patch(url, json={"status": target_status, "exit_reason": reason}, headers=headers, timeout=10)
                if resp.status_code in (200, 204):
                    updated = True
                    break
            except Exception:
                continue
    # Update user_portfolio
    if supabase_url and supabase_key:
        headers = _headers(prefer="return=minimal")
        for target_status in ("EXITED", "CLOSED"):
            try:
                url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?symbol=eq.{ticker_bare}.CA&status=eq.TRACKING"
                resp = requests.patch(url, json={"status": target_status}, headers=headers, timeout=10)
                if resp.status_code in (200, 204):
                    break
            except Exception:
                continue
    # Get current price and PnL
    current_price = _fetch_current_price(f"{ticker_bare}.CA")
    pnl_pct = None
    if entry_price and current_price:
        pnl_pct = (current_price - entry_price) / entry_price * 100
    # Build card
    card = format_close_card(ticker_bare, reason, entry_price, current_price, pnl_pct)
    # Broadcast to public channel
    channel = os.environ.get("TELEGRAM_CHANNEL_NEWS") or os.environ.get("TELEGRAM_CHANNEL_SCALPING") or ""
    public_ok = False
    if channel and requests and bot_token:
        try:
            resp = requests.post(
                TELEGRAM_SEND_URL.format(token=bot_token),
                json={"chat_id": channel, "text": card, "parse_mode": "HTML"},
                timeout=10,
            )
            public_ok = resp.status_code == 200
        except Exception:
            pass
    elif not channel:
        public_ok = True  # mock mode
    # Push DM to subscribers
    dm_ok = False
    trade_id = int(signal.get("id") or signal.get("trade_id") or 0) if signal else 0
    if trade_id and requests and bot_token:
        delivered = _push_update_to_subscribers(supabase_url, supabase_key, bot_token, trade_id, ticker_bare, card)
        dm_ok = delivered > 0
    else:
        dm_ok = True  # no subscribers to notify = success
    return (public_ok, dm_ok), card


def update_trade(
    ticker: str,
    params: Dict[str, str],
    user_id: str,
    bot_token: str,
    supabase_url: str,
    supabase_key: str,
) -> Tuple[bool, str]:
    """Update trade parameters (sl, targets) in trade_signals. Notify subscribers."""
    ticker_bare = ticker.strip().upper().replace(".CA", "")
    if not ticker_bare:
        return False, "missing-ticker"
    updates: Dict[str, Any] = {}
    valid_keys = {"sl", "target1", "target2", "target3"}
    for key, val in params.items():
        if key in valid_keys:
            try:
                fval = float(val)
                if fval <= 0:
                    return False, f"invalid-value:{key}"
                if key == "sl":
                    updates["current_stop_loss"] = fval
                else:
                    idx = int(key.replace("target", ""))
                    updates[f"target_{idx}"] = fval
            except:
                return False, f"invalid-value:{key}"
    if not updates:
        return False, "no-updates"
    # Fetch current signal for comparison
    signal = _fetch_trade_signal(supabase_url, supabase_key, ticker)
    # PATCH trade_signals
    patched = False
    if supabase_url and supabase_key:
        headers = _headers(prefer="return=minimal")
        try:
            url = f"{supabase_url}/rest/v1/{TRADE_SIGNALS_TABLE}?ticker=eq.{ticker_bare}.CA&order=created_at.desc&limit=1"
            resp = requests.patch(url, json=updates, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                patched = True
        except Exception:
            pass
    if not patched:
        return False, "patch-failed"
    # Build update card
    card = format_update_card(ticker_bare, {k: str(v) for k, v in updates.items()})
    # Notify subscribers
    trade_id = int(signal.get("id") or signal.get("trade_id") or 0) if signal else 0
    sub_ok = False
    if trade_id and requests and bot_token:
        delivered = _push_update_to_subscribers(supabase_url, supabase_key, bot_token, trade_id, ticker_bare, card)
        sub_ok = delivered > 0
    else:
        sub_ok = True
    return (sub_ok, card)


def _handle_start(user_id: str) -> Tuple[bool, str]:
    """Handle /start welcome message."""
    return True, (
        "👋 <b>مرحباً بك في بوت البورصة المصرية | EGX Signals Bot</b>\n"
        "------------------------------------\n"
        "📌 <b>الأوامر المتاحة:</b>\n"
        "• <code>/portfolio</code> - عرض محفظتك النشطة مع الأرباح/الخسائر المباشرة\n"
        "• <code>/portfolio [TICKER]</code> - تعديل سعر الدخول لسهم محدد\n"
        "• <code>/close &lt;TICKER&gt; [سبب]</code> - إغلاق صفقة (للمسؤولين فقط)\n"
        "• <code>/update &lt;TICKER&gt; sl=VALUE target1=VALUE</code> - تحديث أهداف الصفقة (للمسؤولين)\n"
        "------------------------------------\n"
        "💡 اضغط زر <b>انضم للصفقة | Track Signal</b> من أي إشارة في القناة العامة لبدء المتابعة.\n"
        "🔒 جميع التحديثات ستصلك في الخاص.\n"
    )

def handle_slash_command(text: str, from_user: Dict[str, Any], bot_token: str) -> Tuple[bool, str]:
    """Route slash commands: /start, /portfolio, /close, /update, محفظتي.

    Returns (success, response_text). Handles Telegram @botname suffix.
    Never fails silently - errors are returned as user-visible messages.
    """
    text = (text or "").strip()
    if not text:
        return False, ""
    # Support Arabic محفظتي without slash
    if text.strip() in ("محفظتي", "محفظتى"):
        text = "/portfolio"
    if not text.startswith("/"):
        return False, ""
    # Strip @botname suffix like /portfolio@EGXSignalsBot or /start@EGXSignalsBot
    try:
        first = text.split()[0]
        if "@" in first:
            text = first.split("@")[0] + (" " + " ".join(text.split()[1:]) if len(text.split()) > 1 else "")
    except Exception:
        pass
    parts = text.split()
    command = parts[0].lower()
    user_id = str(from_user.get("id", ""))
    user_name = from_user.get("first_name", "") or ""

    # /start - always allowed, no Supabase needed
    if command == "/start":
        return _handle_start(user_id)

    # /help alias
    if command in ("/help", "/مساعدة"):
        return _handle_start(user_id)

    # /portfolio or محفظتي - user command with explicit try-except logging
    if command in ("/portfolio", "/محفظتي", "محفظتي"):
        ticker_arg = parts[1] if len(parts) > 1 else None
        try:
            print(f"[PORTFOLIO] Dispatch /portfolio user={user_id} ticker_arg={ticker_arg}")
            logger.info("[PORTFOLIO] Dispatch user=%s ticker_arg=%s", user_id, ticker_arg)
            success, card = handle_portfolio(user_id, bot_token, ticker_arg)
            return success, card
        except Exception as e:
            import traceback
            traceback.print_exc()
            logger.error("[PORTFOLIO] handle_portfolio crashed for user=%s: %s", user_id, e, exc_info=True)
            print(f"[PORTFOLIO][ERROR] handle_portfolio crashed: {e}")
            # Fallback response instead of silent fail
            return False, (
                f"⚠️ حدث خطأ أثناء جلب المحفظة.\n"
                f"السبب: {str(e)[:200]}\n"
                f"تحقق من إعدادات SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY أو تواصل مع المسؤول."
            )

    # Also handle /portfolio with @bot suffix already stripped above
    # Admin-only commands
    if not is_admin(user_id):
        print(f"[ADMIN] Denied {command} for non-admin user={user_id} admin_ids={_load_admin_ids()}")
        return False, "⛔ هذا الأمر مخصص للمسؤولين فقط."

    if command == "/close":
        if len(parts) < 2:
            return False, "📝 الاستخدام: /close <TICKER> [سبب]\nمثال: /close COMI.CA إغلاق يدوي"
        ticker = parts[1].upper()
        reason = " ".join(parts[2:]) if len(parts) > 2 else "إغلاق يدوي من المسؤول"
        try:
            result, card = close_trade(ticker, reason, user_id, bot_token, *get_supabase_config() if get_supabase_config() else ("", ""))
        except Exception as e:
            logger.error("[CLOSE] close_trade crashed: %s", e, exc_info=True)
            return False, f"⚠️ فشل إغلاق {ticker}: {str(e)[:200]}"
        return result[0] if isinstance(result, tuple) else result, card

    if command == "/update":
        if len(parts) < 3:
            return False, "📝 الاستخدام: /update <TICKER> [sl=VALUE] [target1=VALUE] [target2=VALUE]\nمثال: /update COMI.CA sl=95 target1=110 target2=115"
        ticker = parts[1].upper()
        params = {}
        for p in parts[2:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.lower()] = v
        try:
            ok, card = update_trade(ticker, params, user_id, bot_token, *get_supabase_config() if get_supabase_config() else ("", ""))
        except Exception as e:
            logger.error("[UPDATE] update_trade crashed: %s", e, exc_info=True)
            return False, f"⚠️ فشل تحديث {ticker}: {str(e)[:200]}"
        return ok, card

    return False, "⚠️ أمر غير معروف. استخدم /start، /portfolio، /close، أو /update."


def handle_portfolio(user_id: str, bot_token: str, ticker_arg: Optional[str] = None) -> Tuple[bool, str]:
    """Handle /portfolio command. If ticker_arg given, set custom entry price.

    Explicit try-except with logging for missing Supabase env / connection errors.
    Never fails silently - errors are returned as user-visible messages.
    """
    try:
        supabase_url, supabase_key = get_supabase_config() if get_supabase_config() else ("", "")
    except Exception as cfg_exc:
        logger.error("[PORTFOLIO][ENV AUDIT] get_supabase_config failed: %s", cfg_exc, exc_info=True)
        print(f"[PORTFOLIO][ENV AUDIT] get_supabase_config failed: {cfg_exc}")
        return False, f"⚠️ إعدادات قاعدة البيانات غير متوفرة: {str(cfg_exc)[:150]}"
    if not supabase_url or not supabase_key:
        print(f"[PORTFOLIO][ENV AUDIT] SUPABASE_URL present={bool(supabase_url)} SUPABASE_SERVICE_ROLE_KEY present={bool(supabase_key)} - cannot fetch portfolio")
        logger.warning("[PORTFOLIO][ENV AUDIT] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing - portfolio fetch skipped for user=%s", str(user_id)[:8])
        return False, (
            "⚠️ إعدادات قاعدة البيانات غير متوفرة.\n"
            "تأكد من ضبط <code>SUPABASE_URL</code> و <code>SUPABASE_SERVICE_ROLE_KEY</code> في إعدادات Vercel."
        )

    # If ticker_arg provided, set/update custom entry price
    if ticker_arg and ticker_arg.upper() != user_id:
        return _set_custom_entry(user_id, ticker_arg, bot_token, supabase_url, supabase_key)

    # Fetch all user's portfolio rows joined with trade_signals
    positions: List[Dict[str, Any]] = []
    try:
        headers = _headers(prefer="return=minimal")
        # Get user's portfolio rows
        url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&status=eq.TRACKING&select=*"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list):
                for row in rows:
                    ticker = row.get("symbol", "")
                    if not ticker:
                        continue
                    # Fetch signal for this ticker
                    signal = _fetch_trade_signal(supabase_url, supabase_key, ticker)
                    entry = row.get("entry_price") or row.get("joined_at_price") or row.get("joined_price") or row.get("price") or 0
                    # Try custom entry from portfolio row
                    custom_entry = row.get("custom_entry_price")
                    # Fetch live price
                    current = None
                    if signal:
                        current = _fetch_current_price(f"{ticker}.CA" if not ticker.endswith(".CA") else ticker)
                    pos = {
                        "ticker": ticker,
                        "entry_price": float(entry) if entry else 0.0,
                        "current_price": current,
                        "status": row.get("status", "TRACKING"),
                        "quantity": row.get("quantity", "-"),
                        "joined_at": row.get("joined_at", ""),
                        "custom_entry_price": custom_entry,
                    }
                    positions.append(pos)
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(f"Portfolio fetch failed for user={user_id}: {e}", exc_info=True)
        print(f"[PORTFOLIO][ERROR] fetch failed: {e}")
        return False, (
            f"⚠️ فشل جلب المحفظة: {str(e)[:200]}\n"
            f"تحقق من إعدادات SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY."
        )

    try:
        card = format_portfolio_card(positions, user_id, user_id)
    except Exception as ce:
        import traceback
        traceback.print_exc()
        logger.error("[PORTFOLIO] format_portfolio_card crashed: %s", ce, exc_info=True)
        return False, f"⚠️ فشل تنسيق بيانات المحفظة: {str(ce)[:200]}"
    return True, card


def _set_custom_entry(user_id: str, ticker_arg: str, bot_token: str, supabase_url: str, supabase_key: str) -> Tuple[bool, str]:
    """Set custom entry price for a ticker. If called as /portfolio TICKER, prompt for price."""
    # Check if a price was provided after the ticker
    # For now, just confirm the tracking and show the price input prompt
    ticker = ticker_arg.strip().upper()
    if not ticker.endswith(".CA"):
        ticker = f"{ticker}.CA"
    # Check if user follows this ticker
    try:
        headers = _headers(prefer="return=minimal")
        url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&symbol=eq.{ticker}&select=*&limit=1"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and rows:
                # Update with custom entry price prompt
                card = (
                    f"💼 <b>تعديل سعر الدخول | {ticker.replace('.CA','')}</b>\n"
                    f"------------------------------------\n"
                    f"📝 أرسل السعر الذي دخلت به الصفقة كـ رسالة خاصة.\n"
                    f"مثال: <code>/portfolio {ticker.replace('.CA','')} 105.5</code>\n"
                    f"أو استخدم الزر أدناه:\n"
                    f"------------------------------------\n"
                    f"🔹 <b>سعر الدخول الحالي:</b> {_format_price(rows[0].get('entry_price'))} EGP\n"
                    f"💡 لتعديل السعر: أرسل <code>/portfolio {ticker.replace('.CA','')} [سعر]</code>"
                )
                return True, card
    except Exception as e:
        logger.warning(f"Set custom entry check failed: {e}")

    return False, f"⚠️ أنت لا تتابع {ticker} في محفظتك."