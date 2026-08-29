"""Supabase sync bridge for the multi-tenant join system.

The Vercel webhook (serverless, no local SQLite) persists user joins into
Supabase tables; this module lets the egx_quant daemon read/merge that state,
and lets the daemon publish each broadcast trade so the webhook can render the
full DM card from anywhere.

Tables (see supabase_setup.sql):
  - trade_signals   : one row per broadcast trade (card fields by trade_id).
  - user_portfolio  : (user_id, trade_id) opt-in registrations.

Every function is crash-guarded and degrades to empty/no-op results when env
credentials are missing or the network fails.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

logger = logging.getLogger("egx_quant.supabase_sync")

load_dotenv()

TRADE_SIGNALS_TABLE = "trade_signals"
USER_PORTFOLIO_TABLE = "user_portfolio"


def _cfg() -> Optional[Tuple[str, str]]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    # Prioritize SERVICE_ROLE_KEY for all REST API headers (apikey + Authorization Bearer)
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    if not url or not key:
        # Explicit environment audit warnings per spec
        if not url:
            logger.warning("[SUPABASE][ENV AUDIT] SUPABASE_URL is missing or empty - Supabase operations will be skipped")
            print("[SUPABASE][ENV AUDIT] SUPABASE_URL is missing or empty")
        if not key:
            logger.warning("[SUPABASE][ENV AUDIT] SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY is missing or empty - Supabase operations will be skipped")
            print("[SUPABASE][ENV AUDIT] SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY is missing or empty")
        return None
    return url, key


def _headers(prefer: str = "return=minimal") -> Dict[str, str]:
    cfg = _cfg()
    assert cfg is not None
    _, key = cfg
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": prefer}


def _log_http_status(context: str, resp: Any) -> None:
    """Log explicit warning for HTTP 4xx/5xx status codes."""
    try:
        code = int(getattr(resp, "status_code", 0) or 0)
        body = str(getattr(resp, "text", "") or "")[:300]
        if 400 <= code < 500:
            logger.warning("[SUPABASE][4xx] %s failed HTTP %s: %s - check SUPABASE_SERVICE_ROLE_KEY / RLS policy", context, code, body)
            print(f"[SUPABASE][4xx] {context} HTTP {code}: {body[:200]}")
        elif code >= 500:
            logger.warning("[SUPABASE][5xx] %s failed HTTP %s: %s", context, code, body)
            print(f"[SUPABASE][5xx] {context} HTTP {code}: {body[:200]}")
        elif code not in (200, 201, 204):
            logger.warning("[SUPABASE] %s unexpected HTTP %s: %s", context, code, body)
    except Exception:
        pass


def publish_trade_signal(payload: Dict[str, Any]) -> bool:
    """Upsert the broadcast trade's card fields keyed by trade_id."""
    cfg = _cfg()
    if cfg is None:
        logger.warning("[SYNC][ENV AUDIT] Supabase not configured - skipping trade_signals publish (check SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")
        return False
    url, _ = cfg
    try:
        resp = requests.post(
            f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}",
            json=payload,
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        ok = resp.status_code in (200, 201, 204)
        if not ok:
            _log_http_status("publish_trade_signal", resp)
        return ok
    except requests.exceptions.RequestException as exc:
        logger.error("[SYNC] publish_trade_signal request error: %s", exc)
        return False


def get_trade_signal(trade_id: int) -> Optional[Dict[str, Any]]:
    cfg = _cfg()
    if cfg is None:
        logger.warning("[SYNC][ENV AUDIT] Supabase not configured - get_trade_signal skipped")
        return None
    url, _ = cfg
    try:
        resp = requests.get(
            f"{url}/rest/v1/{TRADE_SIGNALS_TABLE}?trade_id=eq.{int(trade_id)}&limit=1&select=*",
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        if resp.status_code == 200:
            rows = resp.json()
            if isinstance(rows, list) and rows:
                return dict(rows[0])
        else:
            _log_http_status("get_trade_signal", resp)
        return None
    except requests.exceptions.RequestException as exc:
        logger.error("[SYNC] get_trade_signal error: %s", exc)
        return None


def save_user_join(user_id: str, trade_id: int, symbol: str, ticker_bare: str, tqi: float) -> Tuple[bool, bool]:
    """Persist an opt-in. Returns (saved_new_row_or_confirmed, already_joined).
    
    Uses upsert with on_conflict=user_id,symbol (and fallback to plain insert)
    to prevent crash on duplicate button clicks. Logs explicit 4xx/5xx warnings.
    """
    cfg = _cfg()
    if cfg is None:
        logger.warning("[SYNC][ENV AUDIT] Supabase not configured - skipping user_portfolio save (check SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")
        return False, False
    url, _ = cfg
    # Idempotent upsert path: on_conflict covers UNIQUE(user_id, symbol) constraint
    # (see setup_db.sql). Handles duplicate clicks gracefully via 409/merge-duplicates.
    payload = {
        "user_id": str(user_id),
        "trade_id": int(trade_id),
        "symbol": symbol,
        "ticker_bare": ticker_bare,
        "tqi": float(tqi),
        "joined_at": datetime.now(timezone.utc).isoformat(),
    }
    # Preferred: upsert with merge-duplicates
    try:
        upsert_headers = _headers(prefer="resolution=merge-duplicates,return=minimal")
        resp = requests.post(
            f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?on_conflict=user_id,symbol",
            json=payload,  # type: ignore[arg-type]
            headers=upsert_headers,
            timeout=10,
        )
        if resp.status_code in (200, 201, 204):
            logger.info("[SYNC] user %s joined trade #%s (upsert)", user_id, trade_id)
            return True, False
        if resp.status_code == 409:
            logger.info("[SYNC] user %s already joined trade #%s (409 conflict)", user_id, trade_id)
            return True, True
        _log_http_status("save_user_join upsert", resp)
    except requests.exceptions.RequestException as exc:
        logger.warning("[SYNC] save_user_join upsert request error: %s", exc)
    # Fallback: plain insert - 409 means already joined
    try:
        plain_headers = _headers(prefer="return=minimal")
        post_resp = requests.post(
            f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}",
            json=payload,  # type: ignore[arg-type]
            headers=plain_headers,
            timeout=10,
        )
        if post_resp.status_code in (200, 201, 204):
            logger.info("[SYNC] user %s joined trade #%s (insert)", user_id, trade_id)
            return True, False
        if post_resp.status_code == 409:
            logger.info("[SYNC] user %s already joined trade #%s (409 insert)", user_id, trade_id)
            return True, True
        _log_http_status("save_user_join insert", post_resp)
        return False, False
    except requests.exceptions.RequestException as exc:
        logger.error("[SYNC] save_user_join request error: %s", exc)
        return False, False


def list_subscribers(trade_id: int) -> List[str]:
    cfg = _cfg()
    if cfg is None:
        logger.warning("[SYNC][ENV AUDIT] Supabase not configured - list_subscribers skipped")
        return []
    url, _ = cfg
    try:
        resp = requests.get(
            f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?trade_id=eq.{int(trade_id)}&select=user_id",
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        if resp.status_code != 200:
            _log_http_status("list_subscribers", resp)
            return []
        rows = resp.json() or []
        return sorted({str(r["user_id"]) for r in rows if r.get("user_id")})
    except requests.exceptions.RequestException as exc:
        logger.error("[SYNC] list_subscribers error: %s", exc)
        return []


def user_trade_ids(user_id: str) -> List[int]:
    cfg = _cfg()
    if cfg is None:
        logger.warning("[SYNC][ENV AUDIT] Supabase not configured - user_trade_ids skipped")
        return []
    url, _ = cfg
    try:
        resp = requests.get(
            f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?user_id=eq.{user_id}&select=trade_id",
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        if resp.status_code != 200:
            _log_http_status("user_trade_ids", resp)
            return []
        rows = resp.json() or []
        return [int(r["trade_id"]) for r in rows if r.get("trade_id") is not None]
    except (requests.exceptions.RequestException, ValueError, TypeError) as exc:
        logger.error("[SYNC] user_trade_ids error: %s", exc)
        return []


def portfolio_users() -> List[str]:
    cfg = _cfg()
    if cfg is None:
        logger.warning("[SYNC][ENV AUDIT] Supabase not configured - portfolio_users skipped")
        return []
    url, _ = cfg
    try:
        resp = requests.get(
            f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?select=user_id",
            headers=_headers(prefer="return=minimal"),
            timeout=10,
        )
        if resp.status_code != 200:
            _log_http_status("portfolio_users", resp)
            return []
        rows = resp.json() or []
        return sorted({str(r["user_id"]) for r in rows if r.get("user_id")})
    except requests.exceptions.RequestException as exc:
        logger.error("[SYNC] portfolio_users error: %s", exc)
        return []


def broadcast_trade_update(trade_id: int, symbol: str, update_text: str) -> int:
    """Push live update (Trailing SL / Target hit) to all users tracking trade_id.

    Queries user_portfolio for trade_id and sends Telegram DM to each tracking user.
    Used by scanner daemon after broadcasting channel update. Returns delivered count. Never raises.
    """
    cfg = _cfg()
    if cfg is None:
        logger.warning("[SYNC][ENV AUDIT] Supabase not configured - broadcast_trade_update skipped")
        return 0
    if not trade_id:
        logger.warning("[SYNC] broadcast_trade_update missing trade_id")
        return 0
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        logger.warning("[SYNC] TELEGRAM_BOT_TOKEN missing - cannot push updates")
        return 0
    url, _ = cfg
    # Resolve subscribers (prefer trade_id, fallback to symbol)
    subscribers = list_subscribers(int(trade_id))
    if not subscribers and symbol:
        try:
            # Fallback by symbol (normalize variants)
            import egx_quant.utils.supabase_sync as _self  # avoid circular
            # Direct query by symbol
            headers = _headers(prefer="return=minimal")
            # Try normalized symbol
            sym_norm = str(symbol).strip().upper()
            if not sym_norm.endswith(".CA"):
                sym_norm = f"{sym_norm}.CA"
            resp = requests.get(
                f"{url}/rest/v1/{USER_PORTFOLIO_TABLE}?symbol=eq.{sym_norm}&status=eq.TRACKING&select=user_id",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                rows = resp.json() or []
                subscribers = sorted({str(r["user_id"]) for r in rows if r.get("user_id")})
        except Exception:
            pass
    if not subscribers:
        logger.info("[SYNC] broadcast_trade_update no subscribers for trade_id=%s symbol=%s", trade_id, symbol)
        return 0
    delivered = 0
    for uid in subscribers:
        try:
            tg_url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": uid, "text": update_text, "parse_mode": "HTML"}
            r = requests.post(tg_url, json=payload, timeout=10)
            if r.status_code == 200:
                delivered += 1
                logger.info("[SYNC][PUSH] Delivered update to %s for trade %s", uid, trade_id)
            else:
                _log_http_status(f"broadcast to {uid}", r)
        except Exception as exc:
            logger.warning("[SYNC] broadcast to %s failed: %s", uid, exc)
    logger.info("[SYNC] broadcast_trade_update trade_id=%s delivered %d/%d", trade_id, delivered, len(subscribers))
    return delivered


def notify_trailing_sl_update(trade_id: int, symbol: str, new_sl: float, current_price: float) -> int:
    """Convenience: format trailing SL update and push to subscribers."""
    try:
        text = (
            f"📈 <b>تحديث وقف الخسارة المتحرك</b>\n"
            f"------------------------------------\n"
            f"🔹 <b>السهم:</b> {symbol}\n"
            f"💵 <b>السعر الحالي:</b> {float(current_price):.2f} EGP\n"
            f"🔴 <b>وقف الخسارة الجديد:</b> {float(new_sl):.2f} EGP\n"
            f"------------------------------------\n"
            f"ℹ️ تم رفع وقف الخسارة لحماية الأرباح."
        )
        return broadcast_trade_update(int(trade_id), str(symbol), text)
    except Exception as exc:
        logger.warning("[SYNC] notify_trailing_sl_update failed: %s", exc)
        return 0


def notify_target_hit(trade_id: int, symbol: str, target_level: int, target_price: float, current_price: float) -> int:
    """Convenience: format target-hit update and push to subscribers."""
    try:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        medal = medals.get(int(target_level), "🎯")
        text = (
            f"{medal} <b>تحقق الهدف {target_level}</b>\n"
            f"------------------------------------\n"
            f"🔹 <b>السهم:</b> {symbol}\n"
            f"🎯 <b>الهدف {target_level}:</b> {float(target_price):.2f} EGP\n"
            f"💵 <b>السعر الحالي:</b> {float(current_price):.2f} EGP\n"
            f"------------------------------------\n"
            f"✅ تهانينا! تم تحقيق الهدف."
        )
        return broadcast_trade_update(int(trade_id), str(symbol), text)
    except Exception as exc:
        logger.warning("[SYNC] notify_target_hit failed: %s", exc)
        return 0
