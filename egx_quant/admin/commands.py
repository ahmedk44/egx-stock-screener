#!/usr/bin/env python3
"""
Admin Control Commands & User Portfolio (/portfolio) System.

User commands (any user - operates on the caller's OWN user_portfolio row):
  /close <TICKER> [REASON]       - Force-close the caller's own active position
  /update <TICKER> [sl=VALUE] [target1=VALUE] [target2=VALUE] - Personal targets/SL

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
except Exception as _exc:
    print(f"[SUPPRESSED] {_exc}")

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
    except Exception as _exc:
        print(f"[SUPPRESSED] {_exc}")
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
    except Exception as _exc:
        print(f"[SUPPRESSED] {_exc}")
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
    except Exception as _exc:
        print(f"[SUPPRESSED] {_exc}")
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
    except Exception as _exc:
        print(f"[SUPPRESSED] {_exc}")
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
        # Allocated capital + unrealized EGP on the REMAINING position
        try:
            alloc_val = float(pos.get("allocation_pct") if pos.get("allocation_pct") is not None else 100.0)
        except Exception:
            alloc_val = 100.0
        try:
            rem_val = float(pos.get("remaining_qty_pct") if pos.get("remaining_qty_pct") is not None else 100.0)
        except Exception:
            rem_val = 100.0
        allocated_egp = pos.get("allocated_egp")
        if allocated_egp:
            lines.append(f"   💰 التخصيص: {alloc_val:.0f}% ({float(allocated_egp):,.2f} EGP) | المتبقي: {rem_val:.0f}%")
            if entry_f and current and entry_f != 0:
                unrealized = float(allocated_egp) * ((float(current) - entry_f) / entry_f) * (rem_val / 100.0)
                sign_u = "+" if unrealized >= 0 else ""
                lines.append(f"   📊 PnL غير محقق (على المتبقي): {sign_u}{unrealized:,.2f} EGP")
        lines.append(f"   📦 الكمية: {pos.get('quantity', '-')} | 📅 {str(pos.get('joined_at', ''))[:10]}")
        lines.append(f"   ✏️ <code>/join {bare} [سعر] [تخصيص%]</code> - تحديث بيانات الدخول")
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
        f"🔴 تم تغيير status إلى <b>CLOSED</b> في user_portfolio للمتابعين.",
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
        except Exception as _exc:
            print(f"[SUPPRESSED] {_exc}")
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
            except Exception as _exc:
                print(f"[SUPPRESSED] {_exc}")
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
    """Close trade: update trade_signals.status=CLOSED and user_portfolio.status=CLOSED (remaining=0). Broadcast + DM."""
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
    # Update user_portfolio: standardize full closes to status='CLOSED' (+ remaining 0)
    # Legacy EXITED fallback only for pre-migration DBs (old check constraint).
    if supabase_url and supabase_key:
        headers = _headers(prefer="return=minimal")
        url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?symbol=eq.{ticker_bare}.CA&status=eq.TRACKING"
        for payload in ({"status": "CLOSED", "remaining_qty_pct": 0}, {"status": "CLOSED"}, {"status": "EXITED"}):
            try:
                resp = requests.patch(url, json=payload, headers=headers, timeout=10)
                if resp.status_code in (200, 204):
                    if payload.get("status") == "EXITED":
                        print("[CLOSE][WARN] legacy DB check-constraint - marked EXITED (run supabase_migration_remaining_qty.sql)")
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
        except Exception as _exc:
            print(f"[SUPPRESSED] {_exc}")
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
            except Exception as _exc:
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
        except Exception as _exc:
            print(f"[SUPPRESSED] {_exc}")
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
        "• <code>/join &lt;TICKER&gt; [PRICE] [ALLOCATION%]</code> - انضم/حدّث صفقة (مثال: /join TEST1 10.20 20%)\n"
        "• <code>/exit &lt;TICKER&gt; [PRICE] [QTY%]</code> - خروج جزئي/كامل (بدون QTY% = خروج كامل من المتبقي)\n"
        "• <code>/stats</code> / <code>/weekly</code> - تقرير أرباح الأسبوع مع ROI\n"
        "• <code>/set_capital &lt;المبلغ&gt;</code> - ضبط رأس مال المحفظة (نقطة البداية)\n"
        "• <code>/add_capital &lt;المبلغ&gt;</code> - إضافة تعزيز سيولة للمحفظة\n"
        "• <code>/close &lt;TICKER&gt; [سبب]</code> - إغلاق صفقتك أنت من محفظتك\n"
        "• <code>/update &lt;TICKER&gt; sl=VALUE target1=VALUE</code> - تخصيص وقف/أهداف صفقتك\n"
        "------------------------------------\n"
        "💡 اضغط زر <b>انضم للصفقة | Track Signal</b> من أي إشارة في القناة العامة لبدء المتابعة.\n"
        "🔒 جميع التحديثات ستصلك في الخاص.\n"
    )

def _fetch_tracking_rows(supabase_url: str, supabase_key: str, user_id: str) -> List[Dict[str, Any]]:
    """Fetch the user's TRACKING positions (symbol, trade_id) for interactive menus. Never raises."""
    try:
        headers = _headers(prefer="return=minimal")
        url = f"{supabase_url}/rest/v1/{USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&status=eq.TRACKING&select=symbol,trade_id,remaining_qty_pct&order=id.desc&limit=20"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict) and r.get("symbol")]
    except Exception as exc:
        print(f"[MENU][WARN] tracking rows fetch failed: {exc}")
    return []


def _send_symbol_menu(bot_token: str, chat_id: str, title: str, rows: List[Dict[str, Any]], cb_prefix: str) -> bool:
    """Send an inline keyboard with one button per tracked symbol. Returns True when delivered."""
    if not bot_token or not chat_id or not rows:
        return False
    keyboard = {"inline_keyboard": []}
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        bare = sym.replace(".CA", "")
        tid = int(r.get("trade_id") or 0)
        keyboard["inline_keyboard"].append([{"text": f"🔹 {bare}", "callback_data": f"{cb_prefix}:{bare}:{tid}"}])
    try:
        resp = requests.post(
            TELEGRAM_SEND_URL.format(token=bot_token),
            json={"chat_id": chat_id, "text": title, "parse_mode": "HTML", "reply_markup": keyboard},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as exc:
        print(f"[MENU][WARN] menu send failed: {exc}")
        return False


def _handle_exit_command(text: str, from_user: Dict[str, Any], bot_token: str) -> Tuple[bool, str]:
    """Handle /exit <TICKER> [PRICE] [QTY%] - user exit with partial/full support. Never fails silently."""
    try:
        parts = text.strip().split()
        # parts[0] is /exit
        if len(parts) < 2:
            # INTERACTIVE: parameter-less /exit -> symbol picker menu (Telegram Menu button safe)
            try:
                import sys, os
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
                import api.webhook as wh
                supa_url, supa_key = wh._get_supabase_config()
                if not supa_url or not supa_key:
                    return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
                uid = str((from_user or {}).get("id", "")).strip()
                if not uid:
                    return False, "⚠️ تعذر تحديد هويتك"
                rows = _fetch_tracking_rows(supa_url, supa_key, uid)
                if not rows:
                    return False, "لا توجد صفقات نشطة في محفظتك حالياً."
                sent = _send_symbol_menu(bot_token, uid, "📤 اختر الصفقة المطلوب الخروج منها:", rows, "exit_sym")
                if sent:
                    return True, ""
                return False, "⚠️ فشل إرسال قائمة الصفقات - حاول مرة أخرى."
            except Exception as menu_exc:
                print(f"[EXIT][MENU][ERROR] {menu_exc}")
                return False, "📝 الاستخدام: <code>/exit &lt;TICKER&gt; [PRICE] [QTY%]</code>\nمثال: <code>/exit COMI 11.50 50</code>"
        ticker_raw = parts[1].strip().upper()
        if not ticker_raw.endswith(".CA"):
            ticker_raw = f"{ticker_raw}.CA"
        # Parse optional price and qty with STRICT validation (positive numbers required)
        exit_price = None
        qty_pct = 100
        qty_explicit = False
        close_reason = "Manual Exit"
        usage_hint = (
            "📝 الاستخدام: <code>/exit &lt;TICKER&gt; PRICE QTY%</code>\n"
            f"مثال: <code>/exit {ticker_raw.replace('.CA', '')} 10.50 50</code> (بيع 50% بسعر 10.50)\n"
            f"مثال: <code>/exit {ticker_raw.replace('.CA', '')} 10.50</code> (إغلاق كامل بسعر محدد)\n"
            f"مثال: <code>/exit {ticker_raw.replace('.CA', '')}</code> (إغلاق كامل بالسعر الحالي)"
        )
        if len(parts) >= 3:
            raw_price = parts[2].replace("%", "").strip()
            try:
                exit_price = float(raw_price)
            except (ValueError, TypeError):
                return False, f"⚠️ سعر غير صالح: '<code>{parts[2]}</code>' - يجب إدخال رقم موجب.\n{usage_hint}"
            if exit_price <= 0:
                return False, f"⚠️ السعر يجب أن يكون رقماً موجباً (تم إدخال: <code>{parts[2]}</code>).\n{usage_hint}"
        if len(parts) >= 4:
            qty_explicit = True
            raw_qty = parts[3].replace("%", "").strip()
            try:
                qty_pct = int(float(raw_qty))
            except (ValueError, TypeError):
                return False, f"⚠️ نسبة الكمية غير صالحة: '<code>{parts[3]}</code>' - يجب إدخال رقم بين 1 و 100.\n{usage_hint}"
            if qty_pct <= 0 or qty_pct > 100:
                return False, f"⚠️ نسبة الكمية يجب أن تكون بين 1 و 100 (تم إدخال: <code>{parts[3]}</code>).\n{usage_hint}"
        if len(parts) >= 5:
            close_reason = " ".join(parts[4:])
        user_id = str(from_user.get("id", "")).strip()
        if not user_id:
            return False, "⚠️ تعذر تحديد هويتك"
        # Import webhook helpers for archiving (avoid circular import)
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        try:
            import api.webhook as wh
            # Use webhook's helper to archive
            supa_url, supa_key = wh._get_supabase_config()
            if not supa_url or not supa_key:
                return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
            # Fetch entry price
            entry_row = wh._fetch_user_portfolio_entry(supa_url, supa_key, user_id, ticker_raw, 0)
            # Strict validation: closed trades are blocked; QTY% cannot exceed remaining_qty_pct
            row_status, remaining_pct = wh._get_position_exit_state(entry_row)
            if row_status in ("CLOSED", "EXITED"):
                return False, wh.CLOSED_BLOCK_MSG
            # Effective exit quantity (deterministic):
            #   - omitted QTY%  -> 100% of CURRENT remaining (close everything left)
            #   - explicit 100  -> full-exit intent -> exits everything remaining
            #   - explicit 1-99 -> must be <= remaining, else rejected
            if (not qty_explicit) or qty_pct >= 100:
                effective_qty = float(remaining_pct)
            elif qty_pct > remaining_pct:
                return False, f"❌ لا يمكن خروج {qty_pct}% — المتبقي الحالي في الصفقة {remaining_pct:.0f}% فقط"
            else:
                effective_qty = float(qty_pct)
            entry_price = None
            if entry_row:
                entry_price = entry_row.get("entry_price") or entry_row.get("joined_at_price")
                try:
                    entry_price = float(entry_price) if entry_price is not None else None
                except Exception as _exc:
                    entry_price = None
            if entry_price is None:
                sig = wh._fetch_trade_signal(supa_url, supa_key, ticker_raw, 0)
                if sig:
                    entry_price = sig.get("entry_price")
                    try:
                        entry_price = float(entry_price) if entry_price is not None else None
                    except Exception as _exc:
                        entry_price = None
            if exit_price is None:
                # Fetch current market price
                exit_price = wh._get_current_market_price(ticker_raw)
                if exit_price is None:
                    exit_price = entry_price
            if entry_price is None or exit_price is None:
                return False, f"⚠️ تعذر تحديد أسعار {ticker_raw}. استخدم: <code>/exit {ticker_raw.replace('.CA','')} 95.5 50</code>"
            # Archive (PnL weighted by the exited portion of the ORIGINAL position)
            archived = wh._archive_closed_position(supa_url, supa_key, user_id, ticker_raw, 0, entry_price, exit_price, effective_qty, close_reason)
            # Update user_portfolio: full exit -> status='CLOSED' + remaining=0; partial -> reduced remaining, stays TRACKING
            exit_meta = {
                "entry_price": entry_price,
                "exit_price": exit_price,
                "qty_pct": effective_qty,
                "close_reason": close_reason,
                "base_snapshot": (entry_row.get("snapshot") if isinstance(entry_row, dict) else None),
            }
            is_full, new_remaining = wh._apply_portfolio_exit(supa_url, supa_key, user_id, ticker_raw, 0, effective_qty, remaining_pct, exit_meta=exit_meta)
            print(f"[EXIT] {ticker_raw} qty={effective_qty:.0f}% archived={archived} full={is_full} remaining={new_remaining:.0f}%")
            # Build confirmation: full price-gain % + allocated-capital EGP
            try:
                price_gain_pct = (float(exit_price) - float(entry_price)) / float(entry_price) * 100.0 if entry_price else 0.0
            except Exception as _exc:
                price_gain_pct = 0.0
            emoji = "🟢" if price_gain_pct >= 0 else "🔴"
            text_out = (
                f"{emoji} <b>تم تسجيل الخروج</b> {effective_qty:.0f}% من <code>{ticker_raw.replace('.CA','')}</code>\n"
                f"💵 دخول: {float(entry_price):.2f} EGP\n"
                f"💰 خروج: {float(exit_price):.2f} EGP\n"
                f"📊 حركة السعر: {price_gain_pct:+.2f}%\n"
            )
            allocated = wh._get_allocated_capital(entry_row, user_id, supa_url, supa_key)
            if allocated is not None:
                realized_egp = allocated * (price_gain_pct / 100.0) * (effective_qty / 100.0)
                text_out += f"💵 الربح المحقق: {realized_egp:+,.2f} EGP (خروج {effective_qty:.0f}% من المركز)\n"
            text_out += f"📝 السبب: {close_reason}\n"
            text_out += (
                "🔴 تم إغلاق الصفقة بالكامل"
                if is_full
                else f"🟡 خروج جزئي - المتبقي في المحفظة: {new_remaining:.0f}%"
            )
            return True, text_out
        except Exception as e:
            import traceback
            print(f"[JOIN_ERROR] {traceback.format_exc()}")
            return False, f"⚠️ فشل تسجيل الخروج: {str(e)[:150]}"
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ خطأ: {str(e)[:150]}"

def _handle_join_command(ticker_raw: str, custom_price: Optional[float], user_id: str, bot_token: str, custom_alloc: Optional[float] = None) -> Tuple[bool, str]:
    """Handle /join <TICKER> [PRICE] [ALLOCATION%] - join or update a tracked trade.

    Disambiguation (CRITICAL):
      allocation_pct  = share of working capital allocated to this trade. Set at join,
                        updatable via /join, NEVER touched by /exit.
      remaining_qty_pct = remaining portion of the position itself. ALWAYS 100.0 on a
                        fresh join, reduced ONLY by /exit. /join NEVER overwrites it.

    Fresh join: entry_price = provided PRICE or signal price; allocation_pct = provided
    or 100; remaining_qty_pct = 100; status = TRACKING; capital_at_join snapshotted
    from user_profile.capital for deterministic EGP PnL later.
    Already TRACKING: UPDATE entry_price and/or allocation_pct (remaining preserved).
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        user_id_str = str(user_id).strip()
        if not user_id_str:
            return False, "⚠️ تعذر تحديد هويتك"
        bare = ticker_raw.replace(".CA", "")

        def _resolve_price(explicit: Optional[float], row: Optional[Dict[str, Any]]) -> Optional[float]:
            if explicit is not None:
                return float(explicit)
            if row:
                for key in ("entry_price", "joined_at_price"):
                    val = row.get(key)
                    try:
                        if val is not None and str(val).strip() != "":
                            return float(val)
                    except Exception as _exc:
                        continue
            signal = wh._fetch_trade_signal(supa_url, supa_key, ticker_raw, trade_id=0)
            if signal:
                try:
                    if signal.get("entry_price") is not None:
                        return float(signal.get("entry_price"))
                except Exception as _exc:
                    print(f"[SUPPRESSED] {_exc}")
            return None

        def _fetch_capital_at_join() -> Optional[float]:
            try:
                headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json"}
                resp = wh.requests.get(f"{supa_url}/rest/v1/user_profile?user_id=eq.{user_id_str}&select=capital&limit=1", headers=headers, timeout=10)  # type: ignore
                if resp.status_code == 200:
                    rows = resp.json()
                    if isinstance(rows, list) and rows and rows[0].get("capital") is not None:
                        return float(rows[0]["capital"])
            except Exception as cap_exc:
                print(f"[JOIN][WARN] capital_at_join fetch failed: {cap_exc}")
            return None

        # Status-aware check: TRACKING rows get UPDATED (entry_price / allocation_pct only)
        existing_row = wh._fetch_user_portfolio_entry(supa_url, supa_key, user_id_str, ticker_raw, 0)
        existing_status = str((existing_row or {}).get("status") or "").strip().upper()
        if existing_row and existing_status == "TRACKING":
            price = _resolve_price(custom_price, existing_row)
            if price is None:
                return False, f"⚠️ تعذر تحديد سعر الدخول لـ {bare}. حاول تحديد السعر: <code>/join {bare} [سعر]</code>"
            try:
                current_alloc = float(existing_row.get("allocation_pct") if existing_row.get("allocation_pct") is not None else 100.0)
            except Exception as _exc:
                current_alloc = 100.0
            new_alloc = float(custom_alloc) if custom_alloc is not None else current_alloc
            updates: Dict[str, Any] = {"entry_price": float(price)}
            if custom_alloc is not None:
                updates["allocation_pct"] = float(new_alloc)
            headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json", "Prefer": "return=representation"}
            patch_url = f"{supa_url}/rest/v1/{wh.USER_PORTFOLIO_TABLE}?user_id=eq.{user_id_str}&symbol=eq.{wh.normalize_ticker(ticker_raw)}"
            try:
                resp = wh.requests.patch(patch_url, json=updates, headers=headers, timeout=10)  # type: ignore
            except Exception as patch_exc:
                print(f"[JOIN][UPDATE][ERROR] patch failed: {patch_exc}")
                return False, f"⚠️ فشل تحديث بيانات الصفقة: {str(patch_exc)[:120]}"
            if resp.status_code in (200, 204):
                print(f"[JOIN][UPDATE] user={user_id_str} {ticker_raw} -> {updates} (remaining_qty_pct PRESERVED)")
                return True, (
                    f"✅ تم تحديث بيانات الدخول للصفقة {bare}: سعر الدخول {float(price):.2f} EGP | نسبة التخصيص: {new_alloc:.0f}%"
                )
            print(f"[JOIN][UPDATE][WARN] patch rejected {resp.status_code}: {(resp.text or '')[:200]}")
            return False, f"⚠️ فشل تحديث بيانات الصفقة ({resp.status_code})."
        if existing_row:
            # CLOSED / EXITED legacy rows keep the duplicate-guard message
            return False, f"⚠️ أنت تتابع {bare} بالفعل في محفظتك."
        # Fetch trade signal
        signal = wh._fetch_trade_signal(supa_url, supa_key, ticker_raw, trade_id=0)
        if not signal:
            return False, f"⚠️ لم يتم العثور على إشارة لـ {bare}."
        # Determine entry_price: custom if provided, else signal's recommended
        entry_price = _resolve_price(custom_price, None)
        if entry_price is None:
            return False, f"⚠️ تعذر تحديد سعر الدخول لـ {bare}. حاول استخدام سعر مخصص: <code>/join {bare} [سعر]</code>"
        allocation_pct = float(custom_alloc) if custom_alloc is not None else 100.0
        capital_at_join = _fetch_capital_at_join()
        # Build snapshot with the resolved entry_price
        snapshot = {
            "strategy": str(signal.get("strategy") or ""),
            "entry_price": entry_price,
            "current_stop_loss": signal.get("current_stop_loss") or signal.get("stop_loss"),
            "target_1": signal.get("target_1"),
            "target_2": signal.get("target_2"),
            "target_3": signal.get("target_3"),
            "source": "manual_join",
        }
        extra_columns: Dict[str, Any] = {
            "allocation_pct": allocation_pct,
            "remaining_qty_pct": 100.0,
        }
        if capital_at_join is not None:
            extra_columns["capital_at_join"] = capital_at_join
        registered, already = wh._upsert_user_portfolio(
            supabase_url=supa_url,
            supabase_key=supa_key,
            user_id=user_id_str,
            trade_id=0,
            symbol=ticker_raw,
            snapshot=snapshot,
            extra_columns=extra_columns,
        )
        if already:
            return False, f"⚠️ أنت تتابع {bare} بالفعل في محفظتك."
        if not registered:
            return False, f"⚠️ فشل تسجيل {bare} في المحفظة."
        price_label = f"{custom_price:.2f}" if custom_price is not None else f"{entry_price:.2f}"
        is_custom = custom_price is not None
        return True, (
            f"✅ تم تسجيل {bare} في محفظتك بنجاح\n"
            f"💵 سعر الدخول: {price_label} EGP{' (مخصص)' if is_custom else ' (من الإشارة)'}\n"
            f"📊 نسبة التخصيص: {allocation_pct:.0f}% | المتبقي: 100%\n"
            f"📊 راجع المحادثة الخاصة لبطاقة الصفقة الكاملة."
        )
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل تسجيل الدخول: {str(e)[:150]}"

def _handle_set_capital_command(text: str, from_user: Dict[str, Any], bot_token: str) -> Tuple[bool, str]:
    """Handle /set_capital <amount> - ANY user sets their portfolio capital baseline.

    RESET semantics: capital = initial_capital = total_deposits = amount.
    Non-admin command by design - there is intentionally NO admin gate here.
    Use /add_capital for cash top-ups (increments capital + total_deposits only).
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        user_id = str((from_user or {}).get("id", "")).strip()
        if not user_id:
            return False, "⚠️ تعذر تحديد هويتك"
        parts = (text or "").strip().split()
        if len(parts) < 2:
            # INTERACTIVE: parameter-less /set_capital -> ask for amount in next message
            pending_set = False
            try:
                pend_headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
                pend_resp = wh.requests.patch(  # type: ignore
                    f"{supa_url}/rest/v1/user_profile?user_id=eq.{user_id}",
                    json={"pending_action": "set_capital"},
                    headers=pend_headers,
                    timeout=10,
                )
                pending_set = pend_resp.status_code in (200, 204)
            except Exception as pend_exc:
                print(f"[SET_CAPITAL][WARN] pending_action set failed: {pend_exc}")
            hint = "" if pending_set else "\nأو أرسل مباشرة: <code>/set_capital 100000</code>"
            return False, "💬 أرسل المبلغ المطلوب بالجنيه مباشرة." + hint
        raw = parts[1].replace(",", "").replace("EGP", "").replace("ج.م", "").strip()
        try:
            amount = float(raw)
        except (ValueError, TypeError):
            return False, f"⚠️ مبلغ غير صالح: '<code>{parts[1]}</code>' - يجب إدخال رقم موجب.\nمثال: <code>/set_capital 100000</code>"
        if amount <= 0:
            return False, f"⚠️ المبلغ يجب أن يكون رقماً موجباً (تم إدخال: <code>{parts[1]}</code>)."
        headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json", "Prefer": "return=representation"}
        amount_f = round(float(amount), 2)
        payload = {"user_id": user_id, "capital": amount_f, "initial_capital": amount_f, "total_deposits": amount_f}
        try:
            upsert = wh.requests.post(  # type: ignore
                f"{supa_url}/rest/v1/user_profile?on_conflict=user_id",
                json=payload,
                headers={**headers, "Prefer": "resolution=merge-duplicates,return=representation"},
                timeout=10,
            )
            if upsert.status_code not in (200, 201, 204):
                print(f"[SET_CAPITAL][WARN] upsert failed {upsert.status_code}: {(upsert.text or '')[:200]}")
                return False, f"⚠️ فشل حفظ رأس المال ({upsert.status_code})."
        except Exception as e:
            print(f"[SET_CAPITAL][ERROR] write failed: {e}")
            return False, f"⚠️ فشل حفظ رأس المال: {str(e)[:120]}"
        print(f"[SET_CAPITAL] user={user_id} capital=initial=deposits -> {amount_f}")
        return True, f"✅ تم ضبط رأس مال المحفظة: {amount_f:,.2f} EGP"
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل تحديث رأس المال: {str(e)[:150]}"


def _handle_add_capital_command(text: str, from_user: Dict[str, Any], bot_token: str) -> Tuple[bool, str]:
    """Handle /add_capital <amount> - cash top-up / injection (ANY user).

    capital += amount AND total_deposits += amount. initial_capital is UNCHANGED so
    cash injections are never miscounted as trading profits (ROI denominator is
    total_deposits, not capital - initial_capital).
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        user_id = str((from_user or {}).get("id", "")).strip()
        if not user_id:
            return False, "⚠️ تعذر تحديد هويتك"
        parts = (text or "").strip().split()
        if len(parts) < 2:
            # INTERACTIVE: parameter-less /add_capital -> ask for amount in next message
            pending_set = False
            try:
                pend_headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
                pend_resp = wh.requests.patch(  # type: ignore
                    f"{supa_url}/rest/v1/user_profile?user_id=eq.{user_id}",
                    json={"pending_action": "add_capital"},
                    headers=pend_headers,
                    timeout=10,
                )
                pending_set = pend_resp.status_code in (200, 204)
            except Exception as pend_exc:
                print(f"[ADD_CAPITAL][WARN] pending_action set failed: {pend_exc}")
            hint = "" if pending_set else "\nأو أرسل مباشرة: <code>/add_capital 25000</code>"
            return False, "💬 أرسل المبلغ المطلوب بالجنيه مباشرة." + hint
        raw = parts[1].replace(",", "").replace("EGP", "").replace("ج.م", "").strip()
        try:
            amount = float(raw)
        except (ValueError, TypeError):
            return False, f"⚠️ مبلغ غير صالح: '<code>{parts[1]}</code>' - يجب إدخال رقم موجب.\nمثال: <code>/add_capital 25000</code>"
        if amount <= 0:
            return False, f"⚠️ المبلغ يجب أن يكون رقماً موجباً (تم إدخال: <code>{parts[1]}</code>)."
        headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json", "Prefer": "return=representation"}
        try:
            get_url = f"{supa_url}/rest/v1/user_profile?user_id=eq.{user_id}&select=capital,total_deposits&limit=1"
            resp = wh.requests.get(get_url, headers=headers, timeout=10)  # type: ignore
            if resp.status_code == 404 and "PGRST205" in (resp.text or ""):
                return False, "⚠️ جدول user_profile غير موجود - شغل migrations/002_user_profile_capital.sql و 003_comprehensive_schema_fix.sql في Supabase."
            if resp.status_code != 200:
                return False, f"⚠️ فشل جلب بيانات رأس المال: {resp.status_code} {(resp.text or '')[:120]}"
            rows = resp.json()
            existing = rows[0] if isinstance(rows, list) and rows else None
            if existing is None or existing.get("capital") is None:
                return False, "⚠️ لم يتم ضبط رأس المال بعد. استخدم <code>/set_capital &lt;المبلغ&gt;</code> أولاً."
            try:
                current_capital = float(existing.get("capital") or 0.0)
            except Exception as _exc:
                current_capital = 0.0
            try:
                current_deposits = float(existing.get("total_deposits") if existing.get("total_deposits") is not None else current_capital)
            except Exception as _exc:
                current_deposits = current_capital
        except Exception as e:
            print(f"[ADD_CAPITAL][ERROR] fetch failed: {e}")
            return False, f"⚠️ فشل الاتصال: {str(e)[:120]}"
        amount_f = round(float(amount), 2)
        new_capital = round(current_capital + amount_f, 2)
        new_deposits = round(current_deposits + amount_f, 2)
        try:
            patch = wh.requests.patch(  # type: ignore
                f"{supa_url}/rest/v1/user_profile?user_id=eq.{user_id}",
                json={"capital": new_capital, "total_deposits": new_deposits},
                headers=headers,
                timeout=10,
            )
            if patch.status_code not in (200, 204):
                print(f"[ADD_CAPITAL][WARN] patch failed {patch.status_code}: {(patch.text or '')[:200]}")
                return False, f"⚠️ فشل حفظ التعزيز ({patch.status_code})."
        except Exception as e:
            print(f"[ADD_CAPITAL][ERROR] write failed: {e}")
            return False, f"⚠️ فشل حفظ التعزيز: {str(e)[:120]}"
        print(f"[ADD_CAPITAL] user={user_id} capital {current_capital} -> {new_capital} | deposits -> {new_deposits}")
        return True, f"✅ تم إضافة تعزيز سيولة: {amount_f:,.2f} EGP | إجمالي السيولة المودعة: {new_deposits:,.2f} EGP"
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل إضافة التعزيز: {str(e)[:150]}"


def _handle_close_own_position(ticker_raw: str, reason: str, user_id: str, bot_token: str) -> Tuple[bool, str]:
    """Handle /close <TICKER> for ANY user: close the CALLER'S OWN position.

    Scoped strictly to the caller's user_portfolio row (status=CLOSED, remaining=0,
    archived to closed_positions). No admin gate - every user manages their own row.
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        ticker_raw = ticker_raw.strip().upper()
        if not ticker_raw.endswith(".CA"):
            ticker_raw = f"{ticker_raw}.CA"
        bare = ticker_raw.replace(".CA", "")
        entry_row = wh._fetch_user_portfolio_entry(supa_url, supa_key, str(user_id), ticker_raw, 0)
        if not entry_row:
            return False, f"⚠️ لا تتابع {bare} في محفظتك."
        row_status, remaining_pct = wh._get_position_exit_state(entry_row)
        if row_status in ("CLOSED", "EXITED"):
            return False, wh.CLOSED_BLOCK_MSG
        effective_qty = float(remaining_pct)
        trade_id = int(entry_row.get("trade_id") or 0)
        entry_price = None
        for key in ("entry_price", "joined_at_price"):
            val = entry_row.get(key)
            try:
                if val is not None and str(val).strip() != "":
                    entry_price = float(val)
                    break
            except:
                continue
        if entry_price is None:
            sig = wh._fetch_trade_signal(supa_url, supa_key, ticker_raw, trade_id=0)
            if sig and sig.get("entry_price") is not None:
                try:
                    entry_price = float(sig.get("entry_price"))
                except:
                    entry_price = None
        exit_price = wh._get_current_market_price(ticker_raw) or entry_price
        if entry_price is None or exit_price is None:
            return False, f"⚠️ تعذر تحديد أسعار {bare}. استخدم <code>/exit {bare} PRICE</code> للخروج اليدوي."
        archived = wh._archive_closed_position(supa_url, supa_key, str(user_id), ticker_raw, trade_id, entry_price, exit_price, effective_qty, reason[:50])
        exit_meta = {
            "entry_price": entry_price,
            "exit_price": exit_price,
            "qty_pct": effective_qty,
            "close_reason": reason[:50],
            "base_snapshot": (entry_row.get("snapshot") if isinstance(entry_row, dict) else None),
        }
        is_full, new_remaining = wh._apply_portfolio_exit(supa_url, supa_key, str(user_id), ticker_raw, trade_id, effective_qty, remaining_pct, exit_meta=exit_meta)
        print(f"[CLOSE_OWN] user={user_id} {ticker_raw} qty={effective_qty:.0f}% archived={archived} full={is_full}")
        price_gain_pct = (float(exit_price) - float(entry_price)) / float(entry_price) * 100.0 if entry_price else 0.0
        lines = [
            f"🔴 <b>تم إغلاق صفقتك | {bare}</b>",
            f"💵 دخول: {float(entry_price):.2f} EGP | 💰 خروج: {float(exit_price):.2f} EGP",
            f"📊 حركة السعر: {price_gain_pct:+.2f}%",
        ]
        allocated = wh._get_allocated_capital(entry_row, str(user_id), supa_url, supa_key)
        if allocated is not None:
            realized_egp = allocated * (price_gain_pct / 100.0) * (effective_qty / 100.0)
            lines.append(f"💵 الربح المحقق: {realized_egp:+,.2f} EGP")
        lines.append(f"📝 السبب: {reason}")
        return True, "\n".join(lines)
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل إغلاق الصفقة: {str(e)[:150]}"


def _handle_update_own_position(ticker_raw: str, params: Dict[str, Any], user_id: str, bot_token: str) -> Tuple[bool, str]:
    """Handle /update <TICKER> sl=.. target1=.. for ANY user.

    Stores PERSONAL overrides (custom_stop_loss / custom_target_N) in the caller's
    own user_portfolio row snapshot - surfaced in the 📊 حالة الصفقة card.
    No admin gate - every user customizes their own row.
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        ticker_raw = ticker_raw.strip().upper()
        if not ticker_raw.endswith(".CA"):
            ticker_raw = f"{ticker_raw}.CA"
        bare = ticker_raw.replace(".CA", "")
        valid_keys = {"sl", "target1", "target2", "target3"}
        overrides: Dict[str, Any] = {}
        for key, val in (params or {}).items():
            if key not in valid_keys:
                continue
            try:
                fval = float(val)
            except (ValueError, TypeError):
                return False, f"⚠️ قيمة غير صالحة لـ {key}: '<code>{val}</code>' - يجب إدخال رقم موجب."
            if fval <= 0:
                return False, f"⚠️ قيمة {key} يجب أن تكون رقماً موجباً (تم إدخال: <code>{val}</code>)."
            if key == "sl":
                overrides["custom_stop_loss"] = fval
            else:
                overrides[f"custom_target_{key.replace('target', '')}"] = fval
        if not overrides:
            return False, "⚠️ لا توجد قيم لتحديثها. استخدم <code>sl=</code> أو <code>target1=</code>"
        entry_row = wh._fetch_user_portfolio_entry(supa_url, supa_key, str(user_id), ticker_raw, 0)
        if not entry_row:
            return False, f"⚠️ لا تتابع {bare} في محفظتك - استخدم <code>/join {bare}</code> أولاً."
        row_status = str(entry_row.get("status") or "").strip().upper()
        if row_status in ("CLOSED", "EXITED"):
            return False, wh.CLOSED_BLOCK_MSG
        snap = entry_row.get("snapshot")
        if isinstance(snap, str):
            try:
                snap = json.loads(snap)
            except Exception as snap_exc:
                print(f"[UPDATE_OWN][WARN] snapshot parse failed: {snap_exc}")
                snap = {}
        if not isinstance(snap, dict):
            snap = {}
        merged = {**snap, **overrides}
        headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json", "Prefer": "return=representation"}
        patch_url = f"{supa_url}/rest/v1/{wh.USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&symbol=eq.{wh.normalize_ticker(ticker_raw)}"
        try:
            resp = wh.requests.patch(patch_url, json={"snapshot": merged}, headers=headers, timeout=10)  # type: ignore
        except Exception as patch_exc:
            print(f"[UPDATE_OWN][ERROR] patch failed: {patch_exc}")
            return False, f"⚠️ فشل تحديث الصفقة: {str(patch_exc)[:120]}"
        if resp.status_code not in (200, 204):
            print(f"[UPDATE_OWN][WARN] patch rejected {resp.status_code}: {(resp.text or '')[:200]}")
            return False, f"⚠️ فشل تحديث الصفقة ({resp.status_code})."
        applied = []
        if "custom_stop_loss" in overrides:
            applied.append(f"وقف الخسارة {float(overrides['custom_stop_loss']):.2f}")
        for i in (1, 2, 3):
            k = f"custom_target_{i}"
            if k in overrides:
                applied.append(f"الهدف {i}: {float(overrides[k]):.2f}")
        print(f"[UPDATE_OWN] user={user_id} {ticker_raw} -> {list(overrides.keys())}")
        return True, f"✅ تم تحديث بيانات صفقتك {bare}: " + " | ".join(applied)
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل إضافة التعزيز: {str(e)[:150]}"


def _set_pending_action(user_id: str, action: str) -> bool:
    """Persist the interactive pending-action flag (migration 004). Degrades gracefully."""
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False
        headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
        resp = wh.requests.patch(  # type: ignore
            f"{supa_url}/rest/v1/user_profile?user_id=eq.{user_id}",
            json={"pending_action": action},
            headers=headers,
            timeout=10,
        )
        return resp.status_code in (200, 204)
    except Exception as exc:
        print(f"[PENDING][WARN] set pending_action failed: {exc}")
        return False


def _handle_join_menu(user_id: str, bot_token: str) -> Tuple[bool, str]:
    """Parameter-less /join - list ACTIVE signals as inline join buttons.

    Telegram Menu-button safe: sends the picker message itself and returns (True, "")
    so the caller does not double-send.
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        headers = _headers(prefer="return=minimal")
        # status column added by migration 001; legacy rows may have NULL status
        url = f"{supa_url}/rest/v1/{TRADE_SIGNALS_TABLE}?or=(status.eq.ACTIVE,status.is.null)&select=id,ticker&order=created_at.desc&limit=10"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            rows = resp.json() if resp.status_code == 200 else []
        except Exception as fetch_exc:
            print(f"[JOIN_MENU][WARN] signals fetch failed: {fetch_exc}")
            rows = []
        if not isinstance(rows, list):
            rows = []
        rows = [r for r in rows if isinstance(r, dict) and r.get("ticker") and r.get("id") is not None]
        if not rows:
            return False, "⚠️ لا توجد إشارات نشطة حالياً - تابع القناة العامة لإشارات جديدة."
        keyboard = {"inline_keyboard": []}
        for r in rows:
            ticker = str(r.get("ticker") or "").strip().upper()
            bare = ticker.replace(".CA", "")
            sid = int(r.get("id") or 0)
            keyboard["inline_keyboard"].append([{"text": f"🟢 انضم | {bare}", "callback_data": f"join_trade:{bare}:{sid}"}])
        resp_dm = requests.post(
            TELEGRAM_SEND_URL.format(token=bot_token),
            json={"chat_id": str(user_id), "text": "🟢 <b>الإشارات النشطة</b> - اختر صفقة للانضمام:", "parse_mode": "HTML", "reply_markup": keyboard},
            timeout=10,
        )
        if resp_dm.status_code == 200:
            return True, ""
        return False, "⚠️ فشل إرسال قائمة الإشارات."
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل جلب الإشارات: {str(e)[:150]}"


def _handle_close_menu(user_id: str, bot_token: str) -> Tuple[bool, str]:
    """Parameter-less /close - one-click force-close menu for the user's OWN positions."""
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        rows = _fetch_tracking_rows(supa_url, supa_key, str(user_id))
        if not rows:
            return False, "لا توجد صفقات نشطة في محفظتك حالياً."
        sent = _send_symbol_menu(bot_token, str(user_id), "🔴 اختر الصفقة المطلوب إغلاقها بالكامل:", rows, "close_own")
        if sent:
            return True, ""
        return False, "⚠️ فشل إرسال قائمة الصفقات - حاول مرة أخرى."
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل جلب الصفقات: {str(e)[:150]}"


def _handle_weekly_stats(user_id: str, bot_token: str) -> Tuple[bool, str]:
    """Handle /stats or /weekly - aggregate realized PnL STRICTLY from closed_positions.

    closed_positions stores the actual entry_price, exit_price, quantity_percentage and
    realized PnL per exit execution (one row per partial/full scale-out), so it is the
    source of truth for performance stats.

    Per-row PnL resolution chain:
      1. Recompute weighted PnL from actual prices: (exit - entry) / entry * 100 * qty%
         (identical formula to the archiver, always correctly weighted by qty_pct).
      2. Stored realized_pnl_pct / realized_pnl (rows lacking raw prices).
      3. trade_signals fallback: entry price joined by ticker when the archive row is
         missing entry_price - never defaults a priceable row to 0.00%.
    """
    try:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        import api.webhook as wh
        supa_url, supa_key = wh._get_supabase_config()
        if not supa_url or not supa_key:
            return False, "⚠️ إعدادات قاعدة البيانات غير متوفرة."
        # Calculate week start (Monday 00:00 UTC)
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        # Find Monday of current week
        monday = now - timedelta(days=now.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        # Strict Supabase ISO timestamp: 'YYYY-MM-DDT00:00:00Z'.
        # NOTE: never embed .isoformat() output in PostgREST URLs - the '+00:00' plus sign
        # decodes as a space -> PG error 22007. strftime with 'Z' suffix is URL-safe.
        monday_iso = monday.strftime("%Y-%m-%dT00:00:00Z")
        headers = {"apikey": supa_key, "Authorization": f"Bearer {supa_key}", "Content-Type": "application/json"}

        def _f(val: Any) -> Optional[float]:
            try:
                if val is None or str(val).strip() == "":
                    return None
                return float(val)
            except Exception as _exc:
                return None

        # SOURCE OF TRUTH: closed_positions rows for the current week
        url = f"{supa_url}/rest/v1/{wh.CLOSED_POSITIONS_TABLE}?user_id=eq.{user_id}&closed_at=gte.{monday_iso}&select=*&order=closed_at.desc&limit=50"
        try:
            resp = wh.requests.get(url, headers=headers, timeout=10)  # type: ignore
            if resp.status_code == 404 and "PGRST205" in (resp.text or ""):
                return False, "⚠️ جدول closed_positions غير موجود - شغل SQL: CREATE TABLE closed_positions ( ... )"
            if resp.status_code != 200:
                return False, f"⚠️ فشل جلب البيانات: {resp.status_code} {(resp.text or '')[:150]}"
            rows = resp.json()
            if not isinstance(rows, list):
                rows = []
        except Exception as e:
            import traceback
            print(f"[JOIN_ERROR] {traceback.format_exc()}")
            return False, f"⚠️ فشل الاتصال: {str(e)[:150]}"

        # First pass: resolve PnL from actual prices where possible, collect symbols
        # whose entry_price is missing (need trade_signals fallback join).
        pending: List[Dict[str, Any]] = []
        needs_entry: set = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            sym = str(r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            entry_px = _f(r.get("entry_price"))
            exit_px = _f(r.get("exit_price"))
            # Live schema (migration 001): qty_pct / exit_reason; legacy fallbacks kept
            qty_pct = _f(r.get("qty_pct"))
            if qty_pct is None:
                qty_pct = _f(r.get("quantity_percentage"))
            qty_pct = qty_pct if qty_pct is not None and qty_pct > 0 else 100.0
            stored_pct = _f(r.get("realized_pnl_pct"))
            stored_egp = _f(r.get("realized_pnl"))
            pnl_pct: Optional[float] = None
            pnl_egp: Optional[float] = None
            if entry_px is not None and entry_px != 0 and exit_px is not None:
                # Actual recorded prices per exit execution, weighted by qty_pct
                pnl_pct = (exit_px - entry_px) / entry_px * 100.0 * (qty_pct / 100.0)
                pnl_egp = (exit_px - entry_px) * (qty_pct / 100.0)
            elif stored_pct is not None or stored_egp is not None:
                pnl_pct = stored_pct if stored_pct is not None else 0.0
                pnl_egp = stored_egp if stored_egp is not None else 0.0
            if (pnl_pct is None or pnl_pct == 0.0) and exit_px is not None and entry_px is None:
                # Priceable row missing entry price -> trade_signals fallback join
                needs_entry.add(sym)
            pending.append({
                "symbol": sym,
                "trade_id": r.get("trade_id"),
                "exit_px": exit_px,
                "qty_pct": qty_pct,
                "stored_pct": stored_pct,
                "stored_egp": stored_egp,
                "pnl_pct": pnl_pct,
                "pnl_egp": pnl_egp,
                "eff_entry": entry_px,
                "close_reason": str(r.get("exit_reason") or r.get("close_reason") or "Manual Exit"),
            })

        # Fallback join: actual entry prices from trade_signals (latest per ticker)
        if needs_entry:
            try:
                tickers = ",".join(sorted(needs_entry))
                ts_url = f"{supa_url}/rest/v1/{TRADE_SIGNALS_TABLE}?ticker=in.({tickers})&select=ticker,entry_price&order=created_at.desc&limit=200"
                ts_resp = wh.requests.get(ts_url, headers=headers, timeout=10)  # type: ignore
                signal_entry: Dict[str, float] = {}
                if ts_resp.status_code == 200:
                    ts_rows = ts_resp.json()
                    if isinstance(ts_rows, list):
                        for tr in ts_rows:
                            try:
                                t = str(tr.get("ticker") or "").strip().upper()
                                ep = _f(tr.get("entry_price"))
                                if t and ep is not None and t not in signal_entry:
                                    signal_entry[t] = ep
                            except Exception as _exc:
                                continue
                for p in pending:
                    if p["pnl_pct"] is None or p["pnl_pct"] == 0.0:
                        eff_entry = signal_entry.get(p["symbol"])
                        if eff_entry and p["exit_px"] is not None:
                            p["eff_entry"] = eff_entry
                            p["pnl_pct"] = (p["exit_px"] - eff_entry) / eff_entry * 100.0 * (p["qty_pct"] / 100.0)
                            p["pnl_egp"] = (p["exit_px"] - eff_entry) * (p["qty_pct"] / 100.0)
            except Exception as ts_exc:
                print(f"[STATS][WARN] trade_signals fallback join failed: {ts_exc}")

        # Allocation context: allocated capital per trade for EGP PnL
        #   allocated = capital_at_join (or user_profile.capital) * allocation_pct / 100
        portfolio_ctx: Dict[Any, Dict[str, Any]] = {}
        profile_capital: Optional[float] = None
        total_deposits: Optional[float] = None
        try:
            up_url = f"{supa_url}/rest/v1/user_profile?user_id=eq.{user_id}&select=capital,total_deposits&limit=1"
            up_resp = wh.requests.get(up_url, headers=headers, timeout=10)  # type: ignore
            if up_resp.status_code == 200:
                up_rows = up_resp.json()
                if isinstance(up_rows, list) and up_rows:
                    profile_capital = _f(up_rows[0].get("capital"))
                    total_deposits = _f(up_rows[0].get("total_deposits"))
        except Exception as prof_exc:
            print(f"[STATS][WARN] user_profile fetch failed: {prof_exc}")
        try:
            po_url = f"{supa_url}/rest/v1/{wh.USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&select=symbol,trade_id,capital_at_join,allocation_pct&limit=200"
            po_resp = wh.requests.get(po_url, headers=headers, timeout=10)  # type: ignore
            if po_resp.status_code == 200:
                po_rows = po_resp.json()
                if isinstance(po_rows, list):
                    for pr in po_rows:
                        try:
                            sym = str(pr.get("symbol") or "").strip().upper()
                            tid = pr.get("trade_id")
                            cap_join = _f(pr.get("capital_at_join"))
                            alloc = _f(pr.get("allocation_pct"))
                            if cap_join is None:
                                cap_join = profile_capital
                            if alloc is None:
                                alloc = 100.0
                            if cap_join is not None:
                                ctx = {"allocated": cap_join * (alloc / 100.0)}
                                if tid:
                                    portfolio_ctx[("tid", int(tid))] = ctx
                                if sym:
                                    portfolio_ctx[("sym", sym)] = ctx
                        except Exception as _exc:
                            continue
        except Exception as po_exc:
            print(f"[STATS][WARN] user_portfolio context fetch failed: {po_exc}")
        # Professional EGP PnL: allocated * ((exit - entry) / entry) * (qty_pct / 100)
        for p in pending:
            ctx = portfolio_ctx.get(("tid", p.get("trade_id"))) if p.get("trade_id") else None
            if ctx is None:
                ctx = portfolio_ctx.get(("sym", p["symbol"]))
            if ctx and p["exit_px"] is not None and p.get("eff_entry"):
                try:
                    p["pnl_egp"] = round(ctx["allocated"] * ((p["exit_px"] - p["eff_entry"]) / p["eff_entry"]) * (p["qty_pct"] / 100.0), 2)
                except Exception as egp_exc:
                    print(f"[STATS][WARN] allocated EGP calc failed for {p['symbol']}: {egp_exc}")

        # Trade-level aggregation: partial exits of the SAME trade collapse into ONE
        # closed trade (keyed by trade_id, fallback symbol when trade_id=0).
        groups: Dict[Any, Dict[str, Any]] = {}
        exits_count = 0
        for p in pending:
            egp = p["pnl_egp"] if p["pnl_egp"] is not None else (p["stored_egp"] if p["stored_egp"] is not None else 0.0)
            pct = p["pnl_pct"] if p["pnl_pct"] is not None else (p["stored_pct"] if p["stored_pct"] is not None else 0.0)
            tid = p.get("trade_id")
            key = ("t", int(tid)) if tid else ("s", p["symbol"])
            g = groups.get(key)
            if g is None:
                g = {"symbol": p["symbol"], "pct": 0.0, "egp": 0.0, "reason": p["close_reason"], "exits": 0}
                groups[key] = g
            g["pct"] += float(pct)
            g["egp"] += float(egp)
            g["exits"] += 1
            exits_count += 1

        if not groups:
            return True, (
                f"📊 <b>تقرير الأسبوع</b> ({monday.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔍 لا توجد صفقات مغلقة هذا الأسبوع.\n"
                f"استخدم زر الخروج لتسجيل أول صفقة."
            )
        total = len(groups)
        wins = 0
        total_pnl_pct = 0.0
        total_pnl_egp = 0.0
        best = None
        worst = None
        best_pnl = -999999.0
        worst_pnl = 999999.0
        for g in groups.values():
            pnl_pct = float(g["pct"])
            total_pnl_pct += pnl_pct
            total_pnl_egp += float(g["egp"])
            if pnl_pct > 0:
                wins += 1
            if pnl_pct > best_pnl:
                best_pnl = pnl_pct
                best = g
            if pnl_pct < worst_pnl:
                worst_pnl = pnl_pct
                worst = g
        win_rate = (wins / total * 100.0) if total else 0.0
        avg_pnl = (total_pnl_pct / total) if total else 0.0
        sep = "━━━━━━━━━━━━━━━━━━━━"
        lines = [
            f"📊 <b>تقرير الأسبوع</b> ({monday.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')})\n",
            sep,
            f"📋 إجمالي الصفقات المغلقة: <b>{total}</b> ({exits_count} عملية خروج)",
            f"🏆 Win Rate: <b>{win_rate:.1f}%</b> ({wins}/{total})",
            f"💰 إجمالي PnL: <b>{total_pnl_egp:+.2f} EGP</b> (<b>{total_pnl_pct:+.2f}%</b>)",
            f"📈 متوسط PnL: <b>{avg_pnl:+.2f}%</b>",
        ]
        # Total Portfolio ROI = ALL-TIME cumulative realized EGP / total_deposits.
        # NEVER (capital - initial_capital) - cash injections are not trading profits.
        try:
            all_url = f"{supa_url}/rest/v1/{wh.CLOSED_POSITIONS_TABLE}?user_id=eq.{user_id}&select=symbol,trade_id,entry_price,exit_price,qty_pct&order=closed_at.desc&limit=500"
            all_resp = wh.requests.get(all_url, headers=headers, timeout=10)  # type: ignore
            if all_resp.status_code == 200:
                all_rows = all_resp.json()
                if isinstance(all_rows, list):
                    cumulative_egp = 0.0
                    for ar in all_rows:
                        try:
                            a_sym = str(ar.get("symbol") or "").strip().upper()
                            a_tid = ar.get("trade_id")
                            a_entry = _f(ar.get("entry_price"))
                            a_exit = _f(ar.get("exit_price"))
                            a_qty = _f(ar.get("qty_pct"))
                            a_qty = a_qty if a_qty is not None and a_qty > 0 else 100.0
                            ctx = portfolio_ctx.get(("tid", a_tid)) if a_tid else None
                            if ctx is None:
                                ctx = portfolio_ctx.get(("sym", a_sym))
                            if ctx and a_entry and a_exit and a_entry != 0:
                                cumulative_egp += ctx["allocated"] * ((a_exit - a_entry) / a_entry) * (a_qty / 100.0)
                        except Exception as _exc:
                            continue
                    if total_deposits and total_deposits > 0:
                        total_roi = cumulative_egp / total_deposits * 100.0
                        lines.append(f"🏦 إجمالي السيولة المودعة: <b>{total_deposits:,.2f} EGP</b>")
                        lines.append(f"📊 إجمالي المحقق (كلي): <b>{cumulative_egp:+.2f} EGP</b>")
                        lines.append(f"🎯 العائد الإجمالي (ROI): <b>{total_roi:+.2f}%</b>")
        except Exception as roi_exc:
            print(f"[STATS][WARN] ROI computation failed: {roi_exc}")
        if best:
            lines.append(f"🥇 أفضل صفقة: <code>{str(best['symbol']).replace('.CA','')}</code> {float(best['pct']):+.2f}% ({best['reason']})")
        if worst:
            lines.append(f"🔻 أسوأ صفقة: <code>{str(worst['symbol']).replace('.CA','')}</code> {float(worst['pct']):+.2f}% ({worst['reason']})")
        lines += [sep, "💡 استخدم <code>/exit TICKER PRICE QTY%</code> لتسجيل خروج جديد"]
        return True, "\n".join(lines)
    except Exception as e:
        import traceback
        print(f"[JOIN_ERROR] {traceback.format_exc()}")
        return False, f"⚠️ فشل تقرير الأسبوع: {str(e)[:150]}"

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
    except Exception as _exc:
        print(f"[SUPPRESSED] {_exc}")
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

    # /join <TICKER> [PRICE] [QTY%] - join or update an already-tracked position
    if command == "/join":
        if len(parts) < 2:
            # INTERACTIVE: parameter-less /join -> active signals picker (Telegram Menu button safe)
            return _handle_join_menu(user_id, bot_token)
        ticker_raw = parts[1].strip().upper()
        if not ticker_raw.endswith(".CA"):
            ticker_raw = f"{ticker_raw}.CA"
        custom_price = None
        if len(parts) >= 3:
            try:
                custom_price = float(parts[2])
                if custom_price <= 0:
                    return False, f"⚠️ السعر يجب أن يكون رقماً موجباً (تم إدخال: <code>{parts[2]}</code>)."
            except (ValueError, TypeError):
                return False, f"⚠️ سعر غير صالح: '<code>{parts[2]}</code>' - يجب إدخال رقم موجب."
        custom_alloc = None
        if len(parts) >= 4:
            try:
                custom_alloc = float(parts[3].replace("%", ""))
            except (ValueError, TypeError):
                return False, f"⚠️ نسبة التخصيص غير صالحة: '<code>{parts[3]}</code>' - يجب إدخال رقم بين 1 و 100."
            if custom_alloc <= 0 or custom_alloc > 100:
                return False, f"⚠️ نسبة التخصيص يجب أن تكون بين 1 و 100 (تم إدخال: <code>{parts[3]}</code>)."
        return _handle_join_command(ticker_raw, custom_price, user_id, bot_token, custom_alloc=custom_alloc)

    # /exit - user exit with partial/full support (not admin-only)
    if command == "/exit":
        # /exit <TICKER> [PRICE] [QTY%] - e.g., /exit COMI 95 50, /exit COMI.CA 100
        return _handle_exit_command(text, from_user, bot_token)

    # /stats and /weekly - weekly PnL from closed_positions
    if command in ("/stats", "/weekly", "/احصائيات", "/تقرير"):
        try:
            print(f"[WEEKLY] Dispatch /weekly user={user_id}")
            success, card = _handle_weekly_stats(user_id, bot_token)
            return success, card
        except Exception as e:
            import traceback
            print(f"[JOIN_ERROR] {traceback.format_exc()}")
            return False, f"⚠️ فشل تقرير الأسبوع: {str(e)[:150]}"

    # /set_capital - ANY user sets their own portfolio capital (non-admin, no admin gate)
    if command == "/set_capital":
        try:
            print(f"[SET_CAPITAL] Dispatch user={user_id}")
            return _handle_set_capital_command(text, from_user, bot_token)
        except Exception as e:
            import traceback
            print(f"[JOIN_ERROR] {traceback.format_exc()}")
            return False, f"⚠️ فشل تحديث رأس المال: {str(e)[:150]}"

    # /add_capital - ANY user tops up cash (capital + total_deposits; initial untouched)
    if command == "/add_capital":
        try:
            print(f"[ADD_CAPITAL] Dispatch user={user_id}")
            return _handle_add_capital_command(text, from_user, bot_token)
        except Exception as e:
            import traceback
            print(f"[JOIN_ERROR] {traceback.format_exc()}")
            return False, f"⚠️ فشل إضافة التعزيز: {str(e)[:150]}"

    # /close - ANY user force-closes their OWN tracked position (user_portfolio row).
    if command == "/close":
        if len(parts) < 2:
            # INTERACTIVE: parameter-less /close -> one-click close menu for own positions
            return _handle_close_menu(user_id, bot_token)
        ticker = parts[1].upper()
        reason = " ".join(parts[2:]) if len(parts) > 2 else "إغلاق يدوي"
        return _handle_close_own_position(ticker, reason, user_id, bot_token)

    if command == "/update":
        if len(parts) < 3:
            return False, "📝 الاستخدام: /update <TICKER> [sl=VALUE] [target1=VALUE] [target2=VALUE]\nمثال: /update COMI.CA sl=95 target1=110"
        ticker = parts[1].upper()
        params = {}
        for p in parts[2:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.lower()] = v
        return _handle_update_own_position(ticker, params, user_id, bot_token)

    # Admin-only commands (everything below)
    if not is_admin(user_id):
        print(f"[ADMIN] Denied {command} for non-admin user={user_id} admin_ids={_load_admin_ids()}")
        return False, "⛔ هذا الأمر مخصص للمسؤولين فقط."

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
    profile_capital: Optional[float] = None
    try:
        headers = _headers(prefer="return=minimal")
        # Working capital for allocated-EGP math (best-effort; card degrades gracefully)
        try:
            up_url = f"{supabase_url}/rest/v1/user_profile?user_id=eq.{user_id}&select=capital&limit=1"
            up_resp = requests.get(up_url, headers=headers, timeout=10)
            if up_resp.status_code == 200:
                up_rows = up_resp.json()
                if isinstance(up_rows, list) and up_rows and up_rows[0].get("capital") is not None:
                    profile_capital = float(up_rows[0]["capital"])
        except Exception as cap_exc:
            print(f"[PORTFOLIO][WARN] user_profile capital fetch failed: {cap_exc}")
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
                    try:
                        alloc_val = float(row.get("allocation_pct") if row.get("allocation_pct") is not None else 100.0)
                    except Exception as _exc:
                        alloc_val = 100.0
                    cap_join = row.get("capital_at_join")
                    try:
                        cap_join_f = float(cap_join) if cap_join is not None else profile_capital
                    except Exception as _exc:
                        cap_join_f = profile_capital
                    try:
                        rem_val = float(row.get("remaining_qty_pct") if row.get("remaining_qty_pct") is not None else 100.0)
                    except Exception as _exc:
                        rem_val = 100.0
                    allocated_egp = round(cap_join_f * (alloc_val / 100.0), 2) if cap_join_f else None
                    pos = {
                        "ticker": ticker,
                        "entry_price": float(entry) if entry else 0.0,
                        "current_price": current,
                        "status": row.get("status", "TRACKING"),
                        "quantity": row.get("quantity", "-"),
                        "joined_at": row.get("joined_at", ""),
                        "custom_entry_price": custom_entry,
                        "allocation_pct": alloc_val,
                        "remaining_qty_pct": rem_val,
                        "allocated_egp": allocated_egp,
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