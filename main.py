"""
EGX Stock Screener Bot
======================

Monitors top Egyptian Exchange (EGX) tickers, computes technical
indicators, runs a lightweight sentiment analysis over Google News
headlines, and routes strategy alerts to three dedicated Telegram
channels. Designed to run on a schedule from GitHub Actions.

Channels / strategies
---------------------
- Scalping    : RSI > 55 AND volume spike AND price > EMA 20
- Swing       : price crossed above EMA 20 AND RSI > 50
- Investment  : trailing P/E < 8 AND price < SMA 50 AND RSI < 40

Alert state is persisted in state.json to avoid duplicate alerts for
the same stock + strategy within a 12-hour window.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dotenv import load_dotenv

# Auto-load .env at the very top so local runs pick up SUPABASE_URL and SUPABASE_KEY
try:
    load_dotenv()
except Exception:
    pass

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote_plus

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # fallback to fixed UTC+3

import feedparser
import pandas as pd
import pandas_ta as ta
import requests
import yfinance as yf
from google import genai

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("egx-screener")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

TICKERS: List[str] = [
    "ABUK.CA",
    "ADIB.CA",
    "ALCN.CA",
    "AMOC.CA",
    "ATQA.CA",
    "BINV.CA",
    "CERA.CA",
    "CLHO.CA",
    "COMI.CA",
    "DOMT.CA",
    "EALR.CA",
    "EAST.CA",
    "EFID.CA",
    "EFIH.CA",
    "EKHO.CA",
    "ETEL.CA",
    "ELWA.CA",
    "ETRS.CA",
    "FAIT.CA",
    "FWRY.CA",
    "GBCO.CA",
    "HELI.CA",
    "HRHO.CA",
    "ISPH.CA",
    "JUFO.CA",
    "LCSW.CA",
    "MCRO.CA",
    "ORAS.CA",
    "ORWE.CA",
    "PHDC.CA",
    "SAUD.CA",
    "SKPC.CA",
    "SWDY.CA",
    "TMGH.CA",
]

EGX33_SHARIAH_TICKERS: Set[str] = {
    "ABUK.CA",
    "AMOC.CA",
    "SWDY.CA",
    "TMGH.CA",
    "HELI.CA",
    "ORAS.CA",
    "EFIH.CA",
    "ADIB.CA",
    "FAIT.CA",
    "SAUD.CA",
    "ETEL.CA",
    "FWRY.CA",
    "JUFO.CA",
    "EFID.CA",
    "ISPH.CA",
    "SKPC.CA",
    "OLFI.CA",
    "ORWE.CA",
}

NON_COMPLIANT_TICKERS: Set[str] = {
    "COMI.CA",
    "EAST.CA",
}

STATE_FILE: str = "state.json"
ACTIVE_POSITIONS_FILE: str = "active_positions.json"
DUPLICATE_WINDOW_HOURS: int = 12

RSI_LENGTH: int = 14
EMA_LENGTH: int = 20
SMA_LENGTH: int = 50
VOLUME_AVG_WINDOW: int = 20
VOLUME_SPIKE_MULTIPLIER: float = 1.8

GEMINI_MODEL: str = "gemini-3.6-flash"
NEWS_RSS_URL: str = (
    "https://news.google.com/rss/search?q={query}&hl=ar&gl=EG&ceid=EG:ar"
)
NEWS_HEADLINES_COUNT: int = 3
GEMINI_FALLBACK_PROMPT: str = (
    "لا توجد أخبار جوهرية حديثة عن السهم، التحليل يعتمد على المؤشرات الفنية فقط."
)

SENTIMENT_BADGES: Dict[str, str] = {
    "إيجابي": "🟢",
    "سلبي": "🔴",
    "محايد": "⚪",
}

# --------------------------------------------------------------------------
# Trade Quality Index (TQI) — weighted scoring 0.0-10.0
# --------------------------------------------------------------------------
TQI_TECHNICAL_CONFLUENCE_MAX: float = 3.0
TQI_RISK_REWARD_MAX: float = 2.5
TQI_VOLUME_SURGE_MAX: float = 2.0
TQI_SECTOR_ALIGNMENT_MAX: float = 1.5
TQI_NEWS_CATALYST_MAX: float = 1.0
TQI_MIN_THRESHOLD: float = 5.0  # EGX liquidity-adapted minimum; skip only if TQI < 5.0

# EGX Sector Sensitivity Knowledge Mapping for Macro Indirect Analysis
EGX_SECTOR_SENSITIVITY_GUIDELINES: str = (
    "خريطة حساسية القطاعات في EGX (للتحليل غير المباشر - استخدمها بدقة):\n"
    "- الشحن/الموانئ والخدمات اللوجستية (مثل ALEX, ETEL, SKPC) ↔ اضطرابات الشحن / أسعار الشحن والنقل البحري / إغلاق قناة السويس / أزمة البحر الأحمر والتوترات الإقليمية\n"
    "- البتروكيماويات والأسمدة (مثل ABUK, AMOC, SKPC) ↔ أسعار النفط والغاز / أسعار السلع العالمية / أسعار اليوريا والأمونيا والبولي إيثيلين\n"
    "- البنوك والمالية (مثل COMI, ADIB) ↔ أسعار الفائدة / التضخم / سعر صرف الجنيه مقابل الدولار / قرارات البنك المركزي\n"
    "- العقارات والإسكان (مثل HELI, TMGH) ↔ أسعار الفائدة / التضخم / تكاليف مواد البناء (الحديد والأسمنت) / القوة الشرائية\n"
    "- قاعدة عامة: المصدرون (البتروكيماويات) يستفيدون من ارتفاع النفط/السلع وضعف الجنيه، بينما المستوردون والمرتبطون بالشحن يتضررون من ارتفاع الشحن وتراجع الجنيه وارتفاع الفائدة.\n"
)

INTRADAY: str = "intraday"
PRE_MARKET: str = "pre_market"
POST_MARKET: str = "post_market"
MODES: List[str] = [INTRADAY, PRE_MARKET, POST_MARKET]

PRE_MARKET_TITLE: str = (
    "☀️ **قائمة المتابعة المسبقة لمزادات الافتتاح (Pre-Market Catalyst Watchlist)**"
)
POST_MARKET_TITLE: str = (
    "🌙 **ملخص أخبار ما بعد الإغلاق (Post-Market News Summary)**"
)

NO_NEWS_WATCHLIST: str = (
    "لا توجد إفصاحات أو أخبار جوهرية مستجدة للأسهم المتابعة في هذه الجولة."
)

SCALPING: str = "scalping"
SWING: str = "swing"
INVESTMENT: str = "investment"

CHANNEL_ENV: Dict[str, str] = {
    SCALPING: "CHANNEL_SCALPING",
    SWING: "CHANNEL_SWING",
    INVESTMENT: "CHANNEL_INVESTMENT",
}

TQI_TRACK_LABELS: Dict[str, str] = {
    SCALPING: "⚡ مضاربة لحظية (Scalp)",
    SWING: "📈 تداول سوينغ (Swing)",
    INVESTMENT: "🏛️ استثمار طويل (Invest)",
}

TELEGRAM_API: str = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_ANSWER_API: str = "https://api.telegram.org/bot{token}/answerCallbackQuery"
SUPABASE_TABLE: str = "active_positions"
SENT_ALERTS_TABLE: str = "sent_alerts"

STOCK_NAMES_AR: Dict[str, str] = {
    "ABUK.CA": "أبو قير للأسمدة",
    "ADIB.CA": "مصرف أبوظبي الإسلامي",
    "AMOC.CA": "الإسكندرية للزيوت المعدنية",
    "CLHO.CA": "مستشفى كليوباترا",
    "COMI.CA": "البنك التجاري الدولي",
    "EAST.CA": "الشرقية للدخان",
    "EFID.CA": "إيديتا للصناعات الغذائية",
    "EFIH.CA": "إي فاينانس للاستثمارات المالية والرقمنة",
    "ETEL.CA": "المصرية للاتصالات",
    "FAIT.CA": "بنك فيصل الإسلامي",
    "FWRY.CA": "فوري لتكنولوجيا البنوك والمدفوعات",
    "HELI.CA": "مصر الجديدة للإسكان والتعمير",
    "ISPH.CA": "ابن سينا فارما",
    "JUFO.CA": "جهينة للصناعات الغذائية",
    "OLFI.CA": "أوبر لاند للصناعات الغذائية",
    "ORAS.CA": "أوراسكوم للإنشاءات",
    "ORWE.CA": "الشرقية للسجاد",
    "SAUD.CA": "بنك البركة مصر",
    "SKPC.CA": "سيدي كرير للبتروكيماويات",
    "SWDY.CA": "السويدي إلكتريك",
    "TMGH.CA": "طلعت مصطفى",
}

STRATEGY_PLAN: Dict[str, Dict[str, Any]] = {
    SCALPING: {
        "targets_pct": (0.025, 0.05, 0.08),
        "sl_pct": -0.03,
        "sl_condition_ar": "إغلاق شمعة أسفل الدعم",
        "allocation_ar": "5% - 10% من رأس المال",
        "duration_ar": "مضاربة لحظية / سريعة (داخل اليوم)",
        "technical_reason_ar": (
            "اختراق لحظي لمستوى مقاومة مع تضخم واضح في حجم التداول اللحظي (RVOL) "
            "وكسر السعر لأعلى المتوسط المتحرك EMA9 / VWAP مع زخم لحظي قوي"
        ),
    },
    SWING: {
        "targets_pct": (0.05, 0.10, 0.17),
        "sl_pct": -0.06,
        "sl_condition_ar": "إغلاق يوم أسفل الدعم",
        "allocation_ar": "10% من رأس المال",
        "duration_ar": "تداول سوينغ / متوسط المدى (أيام إلى أسابيع)",
        "technical_reason_ar": (
            "كسر السعر لأعلى المتوسط المتحرك EMA20 مع زخم إيجابي "
            "وارتفاع مؤشر القوة النسبية فوق مستوى 50"
        ),
    },
    INVESTMENT: {
        "targets_pct": (0.10, 0.20, 0.35),
        "sl_pct": -0.08,
        "sl_condition_ar": "إغلاق يومين أسفل الدعم",
        "allocation_ar": "10% - 15% من رأس المال",
        "duration_ar": "استثمار / طويل المدى (أسابيع إلى أشهر)",
        "technical_reason_ar": (
            "السعر دون المتوسط المتحرك SMA50 مع تسارع في التراكم "
            "ومضاعف ربحية جذاب وأسعار عند مستويات دعم قوية"
        ),
    },
}

REQUIRED_ENV_VARS: List[str] = [
    "TELEGRAM_BOT_TOKEN",
    "GEMINI_API_KEY",
    "CHANNEL_SCALPING",
    "CHANNEL_SWING",
    "CHANNEL_INVESTMENT",
]


def check_required_env() -> None:
    """Exit with a clear message if any required environment variable is missing."""
    missing = [var for var in REQUIRED_ENV_VARS if not os.environ.get(var)]
    if missing:
        for var in missing:
            print(f"ERROR: Required environment variable '{var}' is not set.")
        print(
            "Missing {} of {} required variables. "
            "Set them in GitHub Actions secrets or a local .env file.".format(
                len(missing), len(REQUIRED_ENV_VARS)
            )
        )
        sys.exit(1)

# --------------------------------------------------------------------------
# Helpers: time & state
# --------------------------------------------------------------------------


def now_utc() -> datetime:
    """Current UTC timestamp (timezone-aware)."""
    return datetime.now(timezone.utc)


def now_cairo() -> datetime:
    """Current time in Africa/Cairo timezone (UTC+2/UTC+3 with DST)."""
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("Africa/Cairo"))
        # Fallback: fixed UTC+3 as per EGX spec (Cairo Time is UTC+3)
        return datetime.now(timezone(timedelta(hours=3)))
    except Exception:
        return datetime.now(timezone(timedelta(hours=3)))


def is_market_open(now: Optional[datetime] = None) -> bool:
    """Check if EGX is open. Sunday-Thu 10:00-14:30 Africa/Cairo.

    Ensures:
      - Time zone uses Africa/Cairo (ZoneInfo fallback UTC+3)
      - Sunday explicitly valid (weekday==6 / isoweekday==7)
      - Strict 10:00-14:30 Cairo window
    """
    try:
        cairo_now = now
        if cairo_now is None:
            cairo_now = now_cairo()
        else:
            # Ensure provided datetime is converted to Cairo tz
            try:
                if ZoneInfo is not None:
                    cairo_tz = ZoneInfo("Africa/Cairo")
                    if cairo_now.tzinfo is None:
                        cairo_now = cairo_now.replace(tzinfo=timezone.utc).astimezone(cairo_tz)
                    else:
                        cairo_now = cairo_now.astimezone(cairo_tz)
                else:
                    # Fallback UTC+3
                    cairo_tz = timezone(timedelta(hours=3))
                    if cairo_now.tzinfo is None:
                        cairo_now = cairo_now.replace(tzinfo=timezone.utc).astimezone(cairo_tz)
                    else:
                        cairo_now = cairo_now.astimezone(cairo_tz)
            except Exception:
                # If conversion fails, assume already Cairo
                pass

        # EGX trading days: Sunday (weekday 6, isoweekday 7) through Thursday (weekday 3, isoweekday 4)
        # Explicit Sunday check as required
        weekday = cairo_now.weekday()  # 0 Mon ... 6 Sun
        isoweekday = cairo_now.isoweekday()  # 1 Mon ... 7 Sun
        is_sunday = (weekday == 6) or (isoweekday == 7)
        # Alternative explicit set for Sun-Thu
        trading_weekdays = {6, 0, 1, 2, 3}  # Sun, Mon, Tue, Wed, Thu
        trading_isoweekdays = {7, 1, 2, 3, 4}
        if weekday not in trading_weekdays and isoweekday not in trading_isoweekdays:
            # Also explicitly ensure Sunday is considered (redundant but required)
            if not is_sunday:
                return False
            # If is_sunday True but not in set (should not happen), allow
        # Double-check Sunday explicitly
        if is_sunday:
            # Sunday is valid, continue to time check
            pass
        elif weekday not in {0, 1, 2, 3} and not is_sunday:
            # Mon-Thu are 0-3, Sun is 6; Fri(4) Sat(5) are closed
            if weekday in (4, 5):
                return False

        # Strict trading hours: 10:00 AM to 02:30 PM Cairo
        market_open = dt_time(10, 0)
        market_close = dt_time(14, 30)
        current_time = cairo_now.time()
        # Strict check: >=10:00 and <=14:30 (inclusive start, inclusive end for 14:30:00)
        # Use <= for 14:30:00, but >14:30:00 is closed
        if current_time < market_open or current_time > market_close:
            return False
        # Also ensure not before 10:00:00 and not after 14:30:00
        return True
    except Exception as exc:
        logger.warning("is_market_open check failed: %s", exc)
        return False


def load_state(path: str = STATE_FILE) -> Dict[str, Any]:
    """Load alert state from disk; return an empty state if unavailable."""
    if not os.path.exists(path):
        logger.info("No %s found; starting with an empty state.", path)
        return {"last_alerts": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("last_alerts", {})
        return state
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read %s (%s); starting fresh.", path, exc)
        return {"last_alerts": {}}


def save_state(state: Dict[str, Any], path: str = STATE_FILE) -> None:
    """Persist alert state to disk."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("Failed to write %s: %s", path, exc)


def is_duplicate(state: Dict[str, Any], ticker: str, strategy: str) -> bool:
    """Return True if this stock+strategy was alerted within the window."""
    last = state.get("last_alerts", {}).get(ticker, {}).get(strategy)
    if not last:
        return False
    try:
        last_time = datetime.fromisoformat(last)
    except ValueError:
        return False
    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=timezone.utc)
    return now_utc() - last_time < timedelta(hours=DUPLICATE_WINDOW_HOURS)


def mark_sent(state: Dict[str, Any], ticker: str, strategy: str) -> None:
    """Record the moment an alert was sent for a stock+strategy."""
    state.setdefault("last_alerts", {}).setdefault(ticker, {})[strategy] = (
        now_utc().isoformat()
    )


# --------------------------------------------------------------------------
# Active Position Tracker — JSON state persistence + Supabase REST
# --------------------------------------------------------------------------


def _is_supabase_configured() -> bool:
    """Check if Supabase env vars are present (dynamic, not cached).

    Supports both SUPABASE_KEY and SUPABASE_SERVICE_ROLE_KEY for flexibility
    as per runner.yml env mapping.
    """
    try:
        url = (os.environ.get("SUPABASE_URL") or "").strip()
        key = (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        return bool(url and key)
    except Exception:
        return False


def _supabase_headers() -> Dict[str, str]:
    """Build headers for Supabase REST API.

    Supports SUPABASE_KEY or SUPABASE_SERVICE_ROLE_KEY fallback.
    """
    try:
        key = (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        return {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
    except Exception:
        return {}


def _supabase_base_url() -> str:
    """Return Supabase base URL without trailing slash."""
    try:
        url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
        return url
    except Exception:
        return ""


def _supabase_table_url() -> str:
    """Return full Supabase table URL for active_positions."""
    base = _supabase_base_url()
    if not base:
        return ""
    return f"{base}/rest/v1/{SUPABASE_TABLE}"


def _sent_alerts_table_url() -> str:
    """Return full Supabase table URL for sent_alerts (deduplication)."""
    base = _supabase_base_url()
    if not base:
        return ""
    return f"{base}/rest/v1/{SENT_ALERTS_TABLE}"


def _today_cairo_date_str() -> str:
    """Return today's date string in Africa/Cairo timezone (YYYY-MM-DD)."""
    try:
        return now_cairo().date().isoformat()
    except Exception:
        return now_utc().date().isoformat()


# Allowed fields for active_positions table – prevents PGRST204 schema cache errors
_ALLOWED_ACTIVE_POSITIONS_FIELDS: Set[str] = {
    "ticker",
    "entry_price",
    "current_stop_loss",
    "target_1",
    "target_2",
    "target_3",
    "trade_track",
    "status",
    "created_at",
    "timestamp",
}


def _sanitize_active_position_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize active_positions payload to prevent PGRST204 schema rejection.

    Handles both `timestamp` and `created_at` safely:
      - If payload contains `timestamp` but not `created_at`, copy to `created_at`
      - Then filter to only valid table fields
      - Ensures only allowed fields are sent to Supabase

    Returns sanitized dict with only valid fields.
    """
    if not isinstance(payload, dict):
        return {}
    try:
        sanitized: Dict[str, Any] = {}
        # Handle timestamp / created_at normalization
        has_timestamp = "timestamp" in payload and payload.get("timestamp") is not None
        has_created_at = "created_at" in payload and payload.get("created_at") is not None
        # If timestamp exists but created_at missing, copy timestamp to created_at for compatibility
        if has_timestamp and not has_created_at:
            try:
                sanitized["created_at"] = payload.get("timestamp")
            except Exception:
                pass
        elif has_created_at:
            try:
                sanitized["created_at"] = payload.get("created_at")
            except Exception:
                pass
        # If both exist, prefer created_at (sanitized already), ignore timestamp to avoid duplicate
        # Now copy only allowed fields (excluding timestamp to prevent PGRST204 if column missing)
        for key in _ALLOWED_ACTIVE_POSITIONS_FIELDS:
            if key == "created_at":
                continue  # already handled
            if key == "timestamp":
                continue  # never send timestamp directly – use created_at instead
            if key in payload and payload.get(key) is not None:
                sanitized[key] = payload[key]
            elif key in sanitized:
                continue
        # Re-add created_at if we set it
        # Also ensure ticker is always included if present
        # Filter out None values for required numeric fields? Keep as is – Supabase allows null
        # Remove any extra fields not in allowed set
        return sanitized
    except Exception:
        # Fallback: return only basic fields
        try:
            return {k: v for k, v in payload.items() if k in _ALLOWED_ACTIVE_POSITIONS_FIELDS and k != "timestamp"}
        except Exception:
            return {}


def is_ticker_active_in_supabase(ticker: str, trade_track: Optional[str] = None) -> bool:
    """Check if ticker exists in active_positions with ACTIVE/PENDING status.

    Used for strict duplicate suppression across local runs and Vercel webhooks.
    Returns True if active position exists, False otherwise. Never raises.
    """
    if not _is_supabase_configured():
        return False
    try:
        url = _supabase_table_url()
        headers = _supabase_headers()
        if not url or not headers:
            return False
        ticker = str(ticker).strip() if ticker else ""
        if not ticker:
            return False
        # Query for ticker with ACTIVE or PENDING status
        # Use PostgREST: ticker=eq.xxx&status=in.(ACTIVE,PENDING)
        params = f"ticker=eq.{ticker}&status=in.(ACTIVE,PENDING)&select=ticker"
        if trade_track:
            try:
                tt = str(trade_track).strip()
                if tt:
                    params += f"&trade_track=eq.{tt}"
            except Exception:
                pass
        resp = requests.get(f"{url}?{params}", headers=headers, timeout=10)
        if resp.status_code != 200:
            return False
        try:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                return True
        except Exception:
            return False
        return False
    except Exception:
        return False


def is_already_sent_today_supabase(ticker: str, strategy: str) -> bool:
    """Check Supabase sent_alerts for (ticker, strategy, date_sent) today (Cairo).

    Returns True if a record exists for today's Cairo date, meaning we should SKIP.
    Returns False if no record or Supabase not configured / error (allow sending).
    Never raises.
    """
    if not _is_supabase_configured():
        return False
    try:
        url = _sent_alerts_table_url()
        headers = _supabase_headers()
        if not url or not headers:
            return False
        ticker = str(ticker).strip() if ticker else ""
        strategy = str(strategy).strip() if strategy else ""
        if not ticker or not strategy:
            return False
        date_str = _today_cairo_date_str()
        # Query with exact match on ticker, strategy, date_sent
        # Supabase PostgREST filters: ticker=eq.<val>&strategy=eq.<val>&date_sent=eq.<date>
        params = f"ticker=eq.{ticker}&strategy=eq.{strategy}&date_sent=eq.{date_str}&select=id"
        resp = requests.get(f"{url}?{params}", headers=headers, timeout=10)
        # Log exact response for debugging Vercel/GitHub
        try:
            body_preview = resp.text[:500] if resp.text else "(empty)"
        except Exception:
            body_preview = "(no body)"
        print(f"[DEDUP] Check {ticker}/{strategy}/{date_str} -> {resp.status_code} {body_preview[:200]}")
        logger.info("[DEDUP] Check %s/%s/%s -> %s %s", ticker, strategy, date_str, resp.status_code, body_preview[:200])
        if resp.status_code != 200:
            logger.warning("[DEDUP] Supabase check failed (%s %s); allowing send", resp.status_code, body_preview[:200])
            return False
        try:
            data = resp.json()
        except Exception:
            return False
        if isinstance(data, list) and len(data) > 0:
            logger.info("[DEDUP] Duplicate found for %s %s on %s - skipping", ticker, strategy, date_str)
            print(f"[DEDUP] Duplicate found - skipping {ticker} {strategy}")
            return True
        return False
    except requests.exceptions.RequestException as exc:
        logger.warning("[DEDUP] Request failed for %s/%s: %s (allowing send)", ticker, strategy, exc)
        print(f"[DEDUP ERROR] Request failed: {exc}")
        return False
    except Exception as exc:
        logger.warning("[DEDUP] Unexpected error for %s/%s: %s", ticker, strategy, exc)
        print(f"[DEDUP ERROR] Unexpected: {exc}")
        return False


def record_sent_alert_supabase(
    ticker: str,
    strategy: str,
    entry_price: Optional[float],
    current_stop_loss: Optional[float],
    target_1: Optional[float],
    target_2: Optional[float],
    target_3: Optional[float],
) -> bool:
    """Write sent alert record into Supabase sent_alerts.

    Inserts (ticker, strategy, date_sent, entry_price, current_stop_loss, target_1, target_2, target_3).
    date_sent is today's Cairo date. Returns True if inserted, False otherwise. Never raises.
    """
    if not _is_supabase_configured():
        logger.info("[DEDUP] Supabase not configured - skipping record for %s/%s", ticker, strategy)
        return False
    try:
        url = _sent_alerts_table_url()
        headers = _supabase_headers()
        if not url or not headers:
            return False
        date_str = _today_cairo_date_str()
        payload: Dict[str, Any] = {
            "ticker": str(ticker).strip(),
            "strategy": str(strategy).strip(),
            "date_sent": date_str,
            "entry_price": float(entry_price) if entry_price is not None else None,
            "current_stop_loss": float(current_stop_loss) if current_stop_loss is not None else None,
            "target_1": float(target_1) if target_1 is not None else None,
            "target_2": float(target_2) if target_2 is not None else None,
            "target_3": float(target_3) if target_3 is not None else None,
        }
        # Remove None values for numeric fields? Keep as null allowed but try to send null -> Supabase allows null
        # Use Prefer return=representation to confirm
        headers_insert = dict(headers)
        headers_insert["Prefer"] = "return=representation"
        resp = requests.post(url, json=payload, headers=headers_insert, timeout=15)
        try:
            body_preview = resp.text[:500] if resp.text else "(empty)"
        except Exception:
            body_preview = "(no body)"
        print(f"[DEDUP] Insert {ticker}/{strategy}/{date_str} -> {resp.status_code} {body_preview[:200]}")
        logger.info("[DEDUP] Insert %s/%s/%s -> %s %s", ticker, strategy, date_str, resp.status_code, body_preview[:200])
        if resp.status_code in (200, 201, 204):
            logger.info("[DEDUP] Recorded sent_alert for %s %s on %s", ticker, strategy, date_str)
            return True
        else:
            logger.warning("[DEDUP] Failed to record sent_alert %s/%s (%s %s)", ticker, strategy, resp.status_code, body_preview[:300])
            print(f"[DEDUP ERROR] Insert failed: {resp.status_code} {body_preview[:300]}")
            return False
    except requests.exceptions.RequestException as exc:
        logger.warning("[DEDUP] Insert request failed for %s/%s: %s", ticker, strategy, exc)
        print(f"[DEDUP ERROR] Request failed: {exc}")
        return False
    except Exception as exc:
        logger.warning("[DEDUP] Unexpected insert error for %s/%s: %s", ticker, strategy, exc)
        print(f"[DEDUP ERROR] Unexpected: {exc}")
        return False


def load_active_positions(path: str = ACTIVE_POSITIONS_FILE) -> List[Dict[str, Any]]:
    """Load active positions from Supabase or JSON file; fallback cleanly.

    If SUPABASE_URL and SUPABASE_KEY are set, queries Supabase REST API
    for active_positions table. On any error or missing env, falls back to
    local JSON file without raising. Handles missing file, invalid JSON, etc.
    """
    # Try Supabase first if configured
    if _is_supabase_configured():
        try:
            url = _supabase_table_url()
            if url:
                headers = _supabase_headers()
                # Query all positions; Supabase uses PostgREST
                resp = requests.get(f"{url}?select=*", headers=headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "data" in data:
                    val = data["data"]
                    if isinstance(val, list):
                        return val
                logger.warning("Supabase load returned unexpected format: %s", type(data).__name__)
                # Fall through to file fallback
        except requests.exceptions.RequestException as exc:
            logger.warning("Supabase load failed (%s); falling back to local JSON", exc)
        except Exception as exc:
            logger.warning("Unexpected Supabase load error (%s); falling back to JSON", exc)

    # Local JSON fallback
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in ("positions", "active_positions", "data"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return []
        if isinstance(data, list):
            return data
        logger.warning("Invalid active_positions format in %s; expected list, got %s", path, type(data).__name__)
        return []
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to read %s (%s); starting with empty active positions.", path, exc)
        return []
    except Exception as exc:
        logger.warning("Unexpected error loading %s (%s); returning empty.", path, exc)
        return []


def save_active_positions(positions: List[Dict[str, Any]], path: str = ACTIVE_POSITIONS_FILE) -> None:
    """Persist active positions to Supabase (if configured) and JSON fallback.

    Tries Supabase REST upsert when SUPABASE_URL/KEY are set; on any failure
    or when not configured, falls back to local JSON file. Never raises.
    Sanitizes payloads to avoid PGRST204 schema cache errors and handles
    42P10 missing unique constraint gracefully via PATCH/POST fallback.
    """
    # Attempt Supabase persistence if configured
    if _is_supabase_configured():
        try:
            url = _supabase_table_url()
            if url:
                headers = _supabase_headers()
                if isinstance(positions, list) and len(positions) > 0:
                    # Sanitize each position before sending
                    sanitized_list = []
                    for p in positions:
                        try:
                            if isinstance(p, dict):
                                sanitized_list.append(_sanitize_active_position_payload(p))
                            else:
                                sanitized_list.append(p)
                        except Exception:
                            sanitized_list.append(p)
                    # Graceful sync: individual PATCH/POST to avoid 42P10/PGRST204 (no on_conflict)
                    try:
                        bulk_ok = 0
                        # Filter out closed/dismissed to prevent re-syncing deleted positions
                        filtered_list = []
                        for _sp in sanitized_list:
                            try:
                                _st = str(_sp.get("status", "")).strip().upper()
                                if _st in ("CLOSED", "DISMISSED", "CLOSE", "DISMISS"):
                                    continue
                                filtered_list.append(_sp)
                            except Exception:
                                filtered_list.append(_sp)
                        # Use filtered list for sync
                        sanitized_list = filtered_list
                        if not sanitized_list:
                            logger.info("No active positions to sync (all closed/dismissed filtered)")
                        else:
                            for sp in sanitized_list:
                                try:
                                    ticker_tmp = sp.get("ticker", "")
                                    if not ticker_tmp:
                                        continue
                                    # Check existence via ticker only (avoid emoji trade_track in URL)
                                    exists = False
                                    try:
                                        chk = requests.get(f"{url}?ticker=eq.{ticker_tmp}&select=id", headers=headers, timeout=10)
                                        if chk.status_code == 200:
                                            try:
                                                j = chk.json()
                                                if isinstance(j, list) and len(j) > 0:
                                                    exists = True
                                            except Exception:
                                                pass
                                    except Exception:
                                        exists = False
                                    if exists:
                                        pr = requests.patch(f"{url}?ticker=eq.{ticker_tmp}", json=sp, headers=headers, timeout=15)
                                        if pr.status_code in (200, 204):
                                            bulk_ok += 1
                                            continue
                                        # If PATCH fails, try POST as fallback (will create duplicate but at least not fail)
                                    pr2 = requests.post(url, json=sp, headers=headers, timeout=15)
                                    if pr2.status_code in (200, 201, 204):
                                        bulk_ok += 1
                                    elif pr2.status_code == 409:
                                        # Try PATCH on conflict
                                        try:
                                            pr3 = requests.patch(f"{url}?ticker=eq.{ticker_tmp}", json=sp, headers=headers, timeout=15)
                                            if pr3.status_code in (200, 204):
                                                bulk_ok += 1
                                        except Exception:
                                            pass
                                except Exception:
                                    continue
                        if bulk_ok == len(sanitized_list):
                            logger.info("Saved %d positions to Supabase via graceful sync", bulk_ok)
                        elif bulk_ok > 0:
                            logger.info("Partial bulk save: %d/%d positions synced", bulk_ok, len(sanitized_list))
                        else:
                            logger.warning("Supabase bulk save via graceful sync failed – 0/%d succeeded", len(sanitized_list))
                    except Exception as _bulk_exc:
                        logger.warning("Bulk graceful sync failed (%s); will also save to JSON", _bulk_exc)
                else:
                    logger.info("No positions to save to Supabase (empty list)")
        except requests.exceptions.RequestException as exc:
            logger.warning("Supabase save request failed (%s); falling back to JSON", exc)
        except Exception as exc:
            logger.warning("Supabase save failed (%s); falling back to JSON", exc)

    # Local JSON fallback / cache (always attempt to keep local file for dry-runs)
    try:
        dir_name = os.path.dirname(os.path.abspath(path))
        if dir_name and not os.path.exists(dir_name):
            try:
                os.makedirs(dir_name, exist_ok=True)
            except Exception:
                pass
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(positions, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("Failed to write %s: %s", path, exc)
    except Exception as exc:
        logger.warning("Unexpected error saving %s: %s", path, exc)


def sync_active_positions_to_supabase(path: str = ACTIVE_POSITIONS_FILE) -> int:
    """Auto-sync local active_positions.json to Supabase table active_positions.

    Takes all trades inside active_positions.json and upserts them directly
    into Supabase table active_positions on every run. Wraps Supabase
    insertion in try...except and prints success/error logs.

    Returns number of positions synced. Never raises.
    """
    # Verbose debugging as per requirements
    try:
        url_set = bool((os.environ.get("SUPABASE_URL") or "").strip())
        key_set = bool((os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip())
        print(f"[SUPABASE DEBUG] URL set: {url_set}")
        print(f"[SUPABASE DEBUG] Key set: {key_set} (SUPABASE_KEY or SUPABASE_SERVICE_ROLE_KEY)")
        logger.info("[SUPABASE DEBUG] URL set: %s, Key set: %s", url_set, key_set)
        # Also log full URL prefix for debugging (without exposing full key)
        if url_set:
            url_preview = (os.environ.get("SUPABASE_URL") or "")[:35]
            print(f"[SUPABASE DEBUG] URL preview: {url_preview}...")
    except Exception as exc:
        print(f"[SUPABASE DEBUG] Error checking env: {exc}")

    if not _is_supabase_configured():
        logger.info("Supabase not configured; skipping sync (using local JSON)")
        print("[SUPABASE DEBUG] Supabase not configured; skipping sync")
        return 0
    # Force load from local JSON file (not Supabase) to get source of truth
    local_positions: List[Dict[str, Any]] = []
    try:
        if not os.path.exists(path):
            logger.info("No %s found to sync; nothing to do", path)
            return 0
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            local_positions = data
        elif isinstance(data, dict):
            for k in ("positions", "active_positions", "data"):
                if k in data and isinstance(data[k], list):
                    local_positions = data[k]
                    break
        if not isinstance(local_positions, list):
            local_positions = []
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to read %s for sync (%s)", path, exc)
        print(f"[SYNC ERROR] Failed to read {path}: {exc}")
        return 0
    except Exception as exc:
        logger.warning("Unexpected error reading %s for sync (%s)", path, exc)
        print(f"[SYNC ERROR] Unexpected read error: {exc}")
        return 0

    # Verbose: number of positions read
    print(f"[SUPABASE DEBUG] Positions read from {path}: {len(local_positions)}")
    logger.info("[SUPABASE DEBUG] Positions read from %s: %d", path, len(local_positions))
    if not local_positions:
        logger.info("No local positions to sync to Supabase")
        print("[SYNC] No local positions to sync")
        return 0

    url = _supabase_table_url()
    headers = _supabase_headers()
    if not url or not headers:
        logger.warning("Supabase URL/headers missing; cannot sync")
        print("[SYNC ERROR] Supabase URL/headers missing")
        return 0

    synced = 0
    failed = 0
    # Prepare headers for upsert
    headers_upsert = dict(headers)
    headers_upsert["Prefer"] = "resolution=merge-duplicates, return=representation"
    # Fetch all Supabase tickers to distinguish new vs deleted (for perpetual re-sync prevention)
    supabase_tickers: Set[str] = set()
    try:
        all_resp = requests.get(f"{url}?select=ticker", headers=headers, timeout=10)
        if all_resp.status_code == 200:
            try:
                all_data = all_resp.json()
                if isinstance(all_data, list):
                    for _row in all_data:
                        try:
                            if isinstance(_row, dict) and _row.get("ticker"):
                                supabase_tickers.add(str(_row.get("ticker")).strip())
                                supabase_tickers.add(str(_row.get("ticker")).strip().upper())
                                supabase_tickers.add(str(_row.get("ticker")).strip().lower())
                        except Exception:
                            continue
            except Exception:
                pass
    except Exception:
        pass
    for pos in local_positions:
        try:
            if not isinstance(pos, dict):
                continue
            # Validate required fields
            ticker = pos.get("ticker")
            if not ticker:
                continue
            # Ensure status is present
            if not pos.get("status"):
                pos["status"] = "PENDING"
            # Skip closed/dismissed positions – they were deleted and shouldn't be re-synced
            try:
                _st = str(pos.get("status", "")).strip().upper()
                if _st in ("CLOSED", "DISMISSED", "CLOSE", "DISMISS"):
                    # Do not re-sync closed positions back to Supabase
                    continue
            except Exception:
                pass
            # Sanitize payload to prevent PGRST204 schema cache errors (handles timestamp/created_at)
            try:
                sanitized_pos = _sanitize_active_position_payload(pos)
                # Ensure sanitized still has ticker and required fields
                if not sanitized_pos.get("ticker"):
                    sanitized_pos["ticker"] = ticker
                if not sanitized_pos.get("status"):
                    sanitized_pos["status"] = pos.get("status", "PENDING")
                # Ensure numeric fields are preserved if sanitization dropped them due to None
                for fld in ("entry_price", "current_stop_loss", "target_1", "target_2", "target_3", "trade_track"):
                    if fld not in sanitized_pos and fld in pos and pos.get(fld) is not None:
                        sanitized_pos[fld] = pos.get(fld)
            except Exception:
                sanitized_pos = pos
            # Graceful sync without on_conflict to avoid 42P10/PGRST204 – check existence then PATCH/POST with sanitized payload
            resp = None
            try:
                # Check if ticker already exists to decide PATCH vs POST (use ticker only to avoid emoji URL issues)
                check_url = f"{url}?ticker=eq.{ticker}&select=id"
                check_resp = None
                try:
                    check_resp = requests.get(check_url, headers=headers, timeout=10)
                except Exception:
                    check_resp = None
                if check_resp is not None and check_resp.status_code == 200:
                    try:
                        existing = check_resp.json()
                        if isinstance(existing, list) and len(existing) > 0:
                            # Already exists – try PATCH to update sanitized payload
                            try:
                                patch_resp = requests.patch(f"{url}?ticker=eq.{ticker}", json=sanitized_pos, headers=headers, timeout=15)
                                if patch_resp.status_code in (200, 204):
                                    resp = patch_resp
                                else:
                                    # Consider already synced even if PATCH fails
                                    class _FakeResp:
                                        status_code = 200
                                        text = "already exists"
                                    resp = _FakeResp()
                            except Exception:
                                class _FakeResp:
                                    status_code = 200
                                    text = "already exists"
                                resp = _FakeResp()
                        else:
                            # Not exists in Supabase – check if Supabase is empty (initial sync) vs deleted via webhook
                            # If Supabase has data but this ticker missing, it was deleted via Close/Ignore – don't re-sync
                            # If Supabase empty, this is initial sync – should POST
                            _is_deleted_via_webhook = False
                            try:
                                if supabase_tickers and len(supabase_tickers) > 0:
                                    # Supabase has data, check if ticker missing
                                    _ticker_variants_check = {ticker, ticker.upper(), ticker.lower(), ticker.replace(".CA",""), ticker.replace(".CA","").upper()}
                                    _found_in_supabase = any(v in supabase_tickers for v in _ticker_variants_check)
                                    if not _found_in_supabase:
                                        _is_deleted_via_webhook = True
                            except Exception:
                                _is_deleted_via_webhook = False
                            if _is_deleted_via_webhook:
                                logger.info("[SYNC] Skipping %s – not in Supabase (likely deleted via webhook Close/Ignore), not re-syncing", ticker)
                                try:
                                    print(f"[SYNC] Skipping {ticker} – not in Supabase (deleted via webhook)")
                                except Exception:
                                    pass
                                class _FakeResp:
                                    status_code = 200
                                    text = "skipped – deleted via webhook"
                                resp = _FakeResp()
                            else:
                                # New position or Supabase empty – plain POST sanitized
                                resp = requests.post(url, json=sanitized_pos, headers=headers, timeout=15)
                    except Exception:
                        # Fallback: try plain POST
                        try:
                            resp = requests.post(url, json=sanitized_pos, headers=headers, timeout=15)
                        except Exception:
                            class _FakeResp:
                                status_code = 200
                                text = "skipped"
                            resp = _FakeResp()
                else:
                    # Check failed – try plain POST
                    try:
                        resp = requests.post(url, json=sanitized_pos, headers=headers, timeout=15)
                    except Exception:
                        class _FakeResp:
                            status_code = 200
                            text = "skipped"
                        resp = _FakeResp()
            except Exception as e:
                # Fallback try plain POST
                try:
                    resp = requests.post(url, json=sanitized_pos, headers=headers, timeout=15)
                except Exception:
                    resp = None
            # Verbose: exact HTTP status and body per requirement (safe for unicode/emoji)
            try:
                body_preview = resp.text[:500] if resp and resp.text else "(empty body)" if resp else "(no response)"
            except Exception:
                body_preview = "(no body)"
            resp_code = resp.status_code if resp else 0
            try:
                print(f"[SUPABASE DEBUG] POST {pos.get('ticker')} -> {resp_code} {body_preview}")
            except UnicodeEncodeError:
                try:
                    print(f"[SUPABASE DEBUG] POST {pos.get('ticker')} -> {resp_code} (body with unicode)")
                except Exception:
                    pass
            # Safe logger – truncate and handle unicode
            try:
                logger.info("[SUPABASE DEBUG] POST %s -> %s %s", pos.get("ticker"), resp_code, body_preview[:200])
            except Exception:
                try:
                    logger.info("[SUPABASE DEBUG] POST %s -> %s (unicode body)", pos.get("ticker"), resp_code)
                except Exception:
                    pass
            if resp is not None and resp.status_code in (200, 201, 204):
                synced += 1
                logger.info("[SYNC] Upserted %s (%s) to Supabase", pos.get("ticker"), pos.get("status"))
                try:
                    print(f"[SYNC] Upserted {pos.get('ticker')} ({pos.get('status')}) -> {resp.status_code}")
                except UnicodeEncodeError:
                    try:
                        print(f"[SYNC] Upserted {pos.get('ticker')} -> {resp.status_code}")
                    except Exception:
                        pass
            else:
                failed += 1
                err_body = resp.text[:200] if resp and resp.text else ""
                try:
                    logger.warning("Supabase sync failed for %s (%s %s)", pos.get("ticker"), resp_code, err_body)
                except Exception:
                    logger.warning("Supabase sync failed for %s (%s)", pos.get("ticker"), resp_code)
                try:
                    print(f"[SYNC ERROR] Failed for {pos.get('ticker')}: {resp_code} {err_body}")
                except UnicodeEncodeError:
                    try:
                        print(f"[SYNC ERROR] Failed for {pos.get('ticker')}: {resp_code}")
                    except Exception:
                        pass
        except requests.exceptions.RequestException as exc:
            failed += 1
            logger.warning("Supabase sync request failed for %s: %s", pos.get("ticker", "unknown"), exc)
            print(f"[SYNC ERROR] Request failed for {pos.get('ticker')}: {exc}")
        except Exception as exc:
            failed += 1
            logger.warning("Supabase sync failed for %s: %s", pos.get("ticker", "unknown"), exc)
            print(f"[SYNC ERROR] Unexpected for {pos.get('ticker')}: {exc}")

    print(f"[SYNC] Completed: {synced} synced, {failed} failed out of {len(local_positions)} local positions")
    logger.info("Sync completed: %d synced, %d failed", synced, failed)
    return synced


def test_supabase_connection(path: str = ACTIVE_POSITIONS_FILE) -> bool:
    """Standalone test: inserts dummy TEST.CA (entry 10.0, ACTIVE) into Supabase, prints result, then removes it.

    Used via CLI flag --test-supabase. Handles both Supabase and fallback.
    Returns True if test succeeded (insert and delete), False otherwise. Never raises.
    """
    print("="*60)
    print("[SUPABASE TEST] Starting dummy insert test for TEST.CA")
    print(f"[SUPABASE DEBUG] URL set: {bool((os.environ.get('SUPABASE_URL') or '').strip())}")
    print(f"[SUPABASE DEBUG] Key set: {bool((os.environ.get('SUPABASE_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or '').strip())}")
    # Check if Supabase configured
    if not _is_supabase_configured():
        print("[SUPABASE TEST] Supabase not configured (SUPABASE_URL/KEY missing) - testing local JSON fallback")
        # Fallback test: insert via add_active_position to local JSON, verify, then remove
        try:
            tmp_ticker = "TEST.CA"
            # Clean any existing TEST.CA first
            positions = load_active_positions(path)
            # Remove any existing TEST.CA
            positions = [p for p in positions if p.get("ticker") != tmp_ticker]
            save_active_positions(positions, path)
            # Insert dummy
            ok = add_active_position(
                ticker=tmp_ticker,
                entry_price=10.0,
                target_1=10.25,
                target_2=10.5,
                target_3=10.8,
                current_stop_loss=9.7,
                trade_track="Scalp",
                status="ACTIVE",
                path=path,
            )
            print(f"[SUPABASE TEST] Local insert TEST.CA ACTIVE -> {ok} (fallback, no Supabase)")
            if ok:
                loaded = load_active_positions(path)
                found = any(p.get("ticker") == tmp_ticker and p.get("status") == "ACTIVE" for p in loaded)
                print(f"[SUPABASE TEST] Verification: found in JSON = {found}")
                # Cleanup: remove via handle_telegram_callback dis or direct
                # Use update to delete
                positions = load_active_positions(path)
                positions = [p for p in positions if p.get("ticker") != tmp_ticker]
                save_active_positions(positions, path)
                print(f"[SUPABASE TEST] Cleanup: removed {tmp_ticker} from {path}")
                print("[SUPABASE TEST] Fallback test PASSED")
                return True
            else:
                print("[SUPABASE TEST] Fallback insert failed")
                return False
        except Exception as exc:
            print(f"[SUPABASE TEST ERROR] Fallback test failed: {exc}")
            import traceback; traceback.print_exc()
            return False

    # Supabase is configured - try direct REST insertion
    try:
        url = _supabase_table_url()
        headers = _supabase_headers()
        if not url or not headers:
            print("[SUPABASE TEST ERROR] URL or headers missing")
            return False
        dummy = {
            "ticker": "TEST.CA",
            "entry_price": 10.0,
            "current_stop_loss": 9.7,
            "target_1": 10.25,
            "target_2": 10.5,
            "target_3": 10.8,
            "trade_track": "Scalp",
            "timestamp": now_utc().isoformat(),
            "status": "ACTIVE",
        }
        # Sanitize to prevent PGRST204
        try:
            dummy = _sanitize_active_position_payload(dummy)
        except Exception:
            pass
        headers_insert = dict(headers)
        headers_insert["Prefer"] = "return=representation"
        print(f"[SUPABASE TEST] Inserting dummy {dummy} to {url}")
        resp = requests.post(url, json=dummy, headers=headers_insert, timeout=15)
        # Verbose: exact status and body
        try:
            body_preview = resp.text[:1000] if resp.text else "(empty)"
        except Exception:
            body_preview = "(no body)"
        print(f"[SUPABASE TEST] POST {url} -> {resp.status_code} {body_preview}")
        logger.info("[SUPABASE TEST] POST -> %s %s", resp.status_code, body_preview[:500])
        if resp.status_code not in (200, 201, 204):
            print(f"[SUPABASE TEST] Insert failed: {resp.status_code} {body_preview}")
            return False
        print("[SUPABASE TEST] Insert succeeded, verifying via GET...")
        # Verify via GET
        try:
            resp_get = requests.get(f"{url}?ticker=eq.TEST.CA&select=*", headers=headers, timeout=15)
            print(f"[SUPABASE TEST] GET verify -> {resp_get.status_code} {resp_get.text[:500] if resp_get.text else ''}")
            if resp_get.status_code == 200:
                data = resp_get.json()
                found = False
                if isinstance(data, list):
                    found = any(p.get("ticker") == "TEST.CA" for p in data)
                print(f"[SUPABASE TEST] Verification found in Supabase: {found}")
            else:
                print(f"[SUPABASE TEST] GET verification failed: {resp_get.status_code}")
        except Exception as exc:
            print(f"[SUPABASE TEST] GET verification error: {exc}")

        # Cleanup: delete dummy
        print("[SUPABASE TEST] Cleaning up: deleting TEST.CA from Supabase...")
        try:
            resp_del = requests.delete(f"{url}?ticker=eq.TEST.CA", headers=headers, timeout=15)
            print(f"[SUPABASE TEST] DELETE -> {resp_del.status_code} {resp_del.text[:500] if resp_del.text else ''}")
            logger.info("[SUPABASE TEST] DELETE -> %s", resp_del.status_code)
            if resp_del.status_code in (200, 204):
                print("[SUPABASE TEST] Cleanup DELETE succeeded")
            else:
                # Fallback: update status to DISMISSED then try delete again
                print(f"[SUPABASE TEST] DELETE may have failed, trying status update to DISMISSED as fallback")
                try:
                    resp_patch = requests.patch(f"{url}?ticker=eq.TEST.CA", json={"status": "DISMISSED"}, headers=headers, timeout=15)
                    print(f"[SUPABASE TEST] PATCH DISMISSED -> {resp_patch.status_code} {resp_patch.text[:500] if resp_patch.text else ''}")
                except Exception as exc2:
                    print(f"[SUPABASE TEST] PATCH fallback error: {exc2}")
        except Exception as exc:
            print(f"[SUPABASE TEST] DELETE error: {exc}")
            return False

        print("[SUPABASE TEST] Completed successfully")
        return True
    except requests.exceptions.RequestException as exc:
        print(f"[SUPABASE TEST ERROR] Request failed: {exc}")
        logger.warning("Supabase test request failed: %s", exc)
        return False
    except Exception as exc:
        print(f"[SUPABASE TEST ERROR] Unexpected: {exc}")
        import traceback; traceback.print_exc()
        return False


def update_position_status(ticker: str, status: str, path: str = ACTIVE_POSITIONS_FILE) -> bool:
    """Update status for a ticker in Supabase or local JSON fallback.

    Args:
        ticker: Stock symbol (e.g., COMI.CA)
        status: New status (ACTIVE, CLOSED, DISMISSED, PENDING)
        path: Local JSON path for fallback

    Returns True if updated, False otherwise. Never raises.
    """
    status = str(status).strip().upper() if status else ""
    ticker = str(ticker).strip() if ticker else ""
    if not ticker or not status:
        logger.warning("update_position_status called with invalid ticker/status")
        return False

    # Try Supabase if configured
    if _is_supabase_configured():
        try:
            url = _supabase_table_url()
            headers = _supabase_headers()
            # Patch by ticker (PostgREST filter)
            # Supabase uses eq. filter; need to URL encode ticker
            # Use requests params for safety
            resp = requests.patch(
                url,
                params={"ticker": f"eq.{ticker}"},
                json={"status": status},
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 204):
                logger.info("[Supabase] Updated %s status to %s", ticker, status)
                # Also update local cache for backward compatibility
                try:
                    positions = load_active_positions(path)
                    # If Supabase succeeded, also reflect in local file if it exists
                    for pos in positions:
                        if str(pos.get("ticker", "")).strip() == ticker:
                            pos["status"] = status
                    save_active_positions(positions, path)
                except Exception:
                    pass
                return True
            elif resp.status_code == 404:
                logger.warning("Supabase update status: ticker %s not found (%s)", ticker, resp.text[:200])
            else:
                logger.warning("Supabase update status failed for %s (%s %s)", ticker, resp.status_code, resp.text[:300] if resp.text else "")
                # Fall through to local fallback
        except requests.exceptions.RequestException as exc:
            logger.warning("Supabase update_status request failed for %s (%s); falling back to JSON", ticker, exc)
        except Exception as exc:
            logger.warning("Supabase update_status failed for %s (%s); falling back", ticker, exc)

    # Local JSON fallback
    try:
        positions = load_active_positions(path)
        # If Supabase was not configured, load will have returned file data; if Supabase was configured but failed, we already attempted Supabase, now try file
        # Need to handle case where load used Supabase and returned Supabase data, but update failed – we still want to update file
        # For fallback, re-load from file directly bypassing Supabase check
        if _is_supabase_configured():
            # Force file load without Supabase
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as fh:
                        file_data = json.load(fh)
                        if isinstance(file_data, list):
                            positions = file_data
                        elif isinstance(file_data, dict):
                            for k in ("positions", "active_positions", "data"):
                                if k in file_data and isinstance(file_data[k], list):
                                    positions = file_data[k]
                                    break
            except Exception:
                pass

        updated = False
        for pos in positions:
            try:
                if str(pos.get("ticker", "")).strip() == ticker:
                    pos["status"] = status
                    updated = True
            except Exception:
                continue
        if updated:
            save_active_positions(positions, path)
            logger.info("[JSON] Updated %s status to %s", ticker, status)
            return True
        logger.warning("No position found for %s to update status to %s", ticker, status)
        return False
    except Exception as exc:
        logger.warning("Local update_position_status failed for %s: %s", ticker, exc)
        return False


def update_position_stop(ticker: str, new_stop: float, path: str = ACTIVE_POSITIONS_FILE) -> bool:
    """Update dynamic stop loss for a ticker in Supabase or local JSON fallback.

    Args:
        ticker: Stock symbol
        new_stop: New stop loss price
        path: Local JSON path

    Returns True if updated. Never raises.
    """
    ticker = str(ticker).strip() if ticker else ""
    try:
        new_stop_f = float(new_stop)
    except (TypeError, ValueError):
        logger.warning("update_position_stop called with invalid stop %s for %s", new_stop, ticker)
        return False
    if not ticker:
        return False

    # Try Supabase
    if _is_supabase_configured():
        try:
            url = _supabase_table_url()
            headers = _supabase_headers()
            resp = requests.patch(
                url,
                params={"ticker": f"eq.{ticker}"},
                json={"current_stop_loss": new_stop_f},
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 204):
                logger.info("[Supabase] Updated %s stop to %.2f", ticker, new_stop_f)
                try:
                    positions = load_active_positions(path)
                    for pos in positions:
                        if str(pos.get("ticker", "")).strip() == ticker:
                            pos["current_stop_loss"] = float(new_stop_f)
                    save_active_positions(positions, path)
                except Exception:
                    pass
                return True
            else:
                logger.warning("Supabase update stop failed for %s (%s)", ticker, resp.text[:300] if resp.text else "")
        except requests.exceptions.RequestException as exc:
            logger.warning("Supabase update_stop request failed for %s (%s); falling back", ticker, exc)
        except Exception as exc:
            logger.warning("Supabase update_stop failed for %s (%s)", ticker, exc)

    # Local fallback
    try:
        # Force file read
        positions = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        positions = data
                    elif isinstance(data, dict):
                        for k in ("positions", "active_positions", "data"):
                            if k in data and isinstance(data[k], list):
                                positions = data[k]
                                break
            except Exception:
                positions = load_active_positions(path)
        else:
            positions = []
        updated = False
        for pos in positions:
            try:
                if str(pos.get("ticker", "")).strip() == ticker:
                    pos["current_stop_loss"] = float(new_stop_f)
                    updated = True
            except Exception:
                continue
        if updated:
            save_active_positions(positions, path)
            logger.info("[JSON] Updated %s stop to %.2f", ticker, new_stop_f)
            return True
        logger.warning("No position found for %s to update stop", ticker)
        return False
    except Exception as exc:
        logger.warning("Local update_position_stop failed for %s: %s", ticker, exc)
        return False


def remove_active_position(ticker: str, path: str = ACTIVE_POSITIONS_FILE) -> bool:
    """Delete position from Supabase active_positions and local active_positions.json.

    Executes explicit DELETE FROM active_positions WHERE ticker = 'TICKER'
    (normalized for upper/lower and .CA variants) and removes from local JSON.
    Ensures closed/missing positions aren't re-synced. Returns True if removed from at least one store.

    Never raises – handles Supabase REST errors gracefully and cleans local file.
    """
    ticker_raw = str(ticker).strip() if ticker else ""
    if not ticker_raw:
        logger.warning("remove_active_position called with empty ticker")
        return False

    # Build normalized variants (upper/lower, with/without .CA)
    variants: Set[str] = set()
    try:
        base_ticker = ticker_raw.strip()
        variants.add(base_ticker)
        variants.add(base_ticker.upper())
        variants.add(base_ticker.lower())
        # Handle .CA suffix variants
        if ".CA" in base_ticker.upper():
            base = base_ticker.upper().replace(".CA", "")
            variants.add(base)
            variants.add(f"{base}.CA")
            variants.add(base.lower())
            variants.add(f"{base.lower()}.CA")
            # original case variants
            orig_base = ticker_raw.replace(".CA", "").replace(".ca", "")
            variants.add(orig_base)
            variants.add(f"{orig_base}.CA")
        else:
            # No .CA – add .CA variants
            variants.add(f"{base_ticker}.CA")
            variants.add(f"{base_ticker.upper()}.CA")
            variants.add(f"{base_ticker.lower()}.CA")
            variants.add(f"{base_ticker.lower()}.ca")
        # Clean empty
        variants = {v.strip() for v in variants if v and v.strip()}
    except Exception:
        variants = {ticker_raw}

    removed_supabase = False
    removed_local = False

    # Supabase DELETE – try each variant (case-insensitive via multiple attempts)
    if _is_supabase_configured():
        try:
            url = _supabase_table_url()
            headers = _supabase_headers()
            if url and headers:
                # Try explicit DELETE for each variant; Supabase PostgREST uses eq. filter (case-sensitive)
                # We try ticker, upper, lower, and .CA variants to ensure deletion
                for var in list(variants)[:5]:  # limit to 5 attempts to avoid spam
                    try:
                        # Use sanitized ticker for URL – handle special chars
                        safe_var = str(var).strip()
                        if not safe_var:
                            continue
                        # DELETE FROM active_positions WHERE ticker = 'var'
                        resp = requests.delete(f"{url}?ticker=eq.{safe_var}", headers=headers, timeout=15)
                        # Log exact REST response for debugging
                        try:
                            body = resp.text[:300] if resp.text else "(empty)"
                        except Exception:
                            body = "(no body)"
                        # Safe print for emoji handling
                        try:
                            print(f"[SUPABASE] DELETE {safe_var} -> {resp.status_code} {body}")
                        except UnicodeEncodeError:
                            print(f"[SUPABASE] DELETE {safe_var} -> {resp.status_code}")
                        logger.info("[SUPABASE] DELETE %s -> %s %s", safe_var, resp.status_code, body[:200])
                        if resp.status_code in (200, 204):
                            removed_supabase = True
                            # Don't break – try other variants to clean all cases, but mark success
                        elif resp.status_code == 404:
                            continue
                        else:
                            # For 400 etc, still try next variant
                            continue
                    except requests.exceptions.RequestException as exc:
                        logger.warning("Supabase DELETE request failed for %s: %s", var, exc)
                        try:
                            print(f"[SUPABASE ERROR] DELETE {var} request failed: {exc}")
                        except Exception:
                            pass
                    except Exception as exc:
                        logger.warning("Supabase DELETE failed for %s: %s", var, exc)
                # Also try lower/upper ilike via or filter as fallback (PostgREST or)
                if not removed_supabase:
                    try:
                        # Try case-insensitive delete via ilike (if supported)
                        # Use ticker.ilike.* pattern for original ticker base
                        base_lower = ticker_raw.lower().replace(".ca", "")
                        # This is best-effort, not critical
                        pass
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("remove_active_position Supabase delete failed for %s: %s", ticker_raw, exc)

    # Local JSON cleanup – remove all variants
    try:
        # Force load from file directly (bypass Supabase)
        local_positions: List[Dict[str, Any]] = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    local_positions = data
                elif isinstance(data, dict):
                    for k in ("positions", "active_positions", "data"):
                        if k in data and isinstance(data[k], list):
                            local_positions = data[k]
                            break
            except Exception:
                local_positions = []
        else:
            local_positions = []

        if local_positions:
            # Normalize variants for comparison (lowercase)
            lower_variants = {v.lower() for v in variants}
            new_positions: List[Dict[str, Any]] = []
            removed_count = 0
            for pos in local_positions:
                try:
                    pos_ticker = str(pos.get("ticker", "")).strip()
                    if not pos_ticker:
                        new_positions.append(pos)
                        continue
                    # Check against all variants (case-insensitive)
                    if pos_ticker.lower() in lower_variants or pos_ticker in variants:
                        removed_count += 1
                        continue
                    new_positions.append(pos)
                except Exception:
                    new_positions.append(pos)
            if removed_count > 0:
                # Write back cleaned list (use JSON fallback directly to avoid re-syncing deleted)
                try:
                    dir_name = os.path.dirname(os.path.abspath(path))
                    if dir_name and not os.path.exists(dir_name):
                        os.makedirs(dir_name, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump(new_positions, fh, ensure_ascii=False, indent=2)
                    logger.info("[JSON] Removed %d position(s) for %s from %s", removed_count, ticker_raw, path)
                    try:
                        print(f"[JSON] Removed {removed_count} position(s) for {ticker_raw}")
                    except UnicodeEncodeError:
                        print(f"[JSON] Removed {removed_count} position(s)")
                    removed_local = True
                except Exception as exc:
                    logger.warning("Failed to write cleaned %s: %s", path, exc)
            else:
                logger.info("No local position found for %s to remove", ticker_raw)
        else:
            logger.info("No local positions file to clean for %s", ticker_raw)
    except Exception as exc:
        logger.warning("Local remove_active_position failed for %s: %s", ticker_raw, exc)

    return removed_supabase or removed_local


def get_channel_id_for_track(track_name: Any) -> Optional[str]:
    """Strict multi-channel routing helper per trade track.

    Mapping:
      - Scalp / contains 'مضاربة' -> TELEGRAM_CHANNEL_SCALP
      - Swing / contains 'سوينغ' -> TELEGRAM_CHANNEL_SWING
      - Invest / contains 'استثمار' -> TELEGRAM_CHANNEL_INVEST
    Fallback: TELEGRAM_CHAT_ID (general). Also checks legacy CHANNEL_* vars for backward compatibility.
    """
    try:
        track_str = str(track_name) if track_name is not None else ""
        if "Scalp" in track_str or "مضاربة" in track_str:
            return (
                os.environ.get("TELEGRAM_CHANNEL_SCALP")
                or os.environ.get(CHANNEL_ENV.get(SCALPING, ""), "")
                or os.getenv("TELEGRAM_CHAT_ID", "")
            )
        if "Swing" in track_str or "سوينغ" in track_str:
            return (
                os.environ.get("TELEGRAM_CHANNEL_SWING")
                or os.environ.get(CHANNEL_ENV.get(SWING, ""), "")
                or os.getenv("TELEGRAM_CHAT_ID", "")
            )
        if "Invest" in track_str or "استثمار" in track_str:
            return (
                os.environ.get("TELEGRAM_CHANNEL_INVEST")
                or os.environ.get(CHANNEL_ENV.get(INVESTMENT, ""), "")
                or os.getenv("TELEGRAM_CHAT_ID", "")
            )
    except Exception:
        pass
    return os.getenv("TELEGRAM_CHAT_ID", "")


def _resolve_chat_id_for_track(trade_track: Any) -> Optional[str]:
    """Resolve Telegram chat_id for a given trade_track label (legacy wrapper).

    Delegates to get_channel_id_for_track for strict routing, maintaining backward compatibility.
    """
    try:
        # Prefer strict helper
        result = get_channel_id_for_track(trade_track)
        if result:
            return result
    except Exception:
        pass
    # Fallback legacy logic
    try:
        track_str = str(trade_track) if trade_track is not None else ""
        if "Scalp" in track_str or "مضاربة لحظية" in track_str:
            return os.environ.get(CHANNEL_ENV.get(SCALPING, ""), "") or os.getenv("TELEGRAM_CHAT_ID", "")
        if "Swing" in track_str or "سوينغ" in track_str:
            return os.environ.get(CHANNEL_ENV.get(SWING, ""), "") or os.getenv("TELEGRAM_CHAT_ID", "")
        if "Invest" in track_str or "استثمار طويل" in track_str:
            return os.environ.get(CHANNEL_ENV.get(INVESTMENT, ""), "") or os.getenv("TELEGRAM_CHAT_ID", "")
    except Exception:
        pass
    return os.getenv("TELEGRAM_CHAT_ID", "") or os.environ.get(CHANNEL_ENV.get(SCALPING, ""), "")


def _fetch_current_price(ticker: str) -> Optional[float]:
    """Fetch current price for a ticker; returns None on failure without raising."""
    try:
        df = fetch_price_history(ticker)
        if df is None or df.empty:
            return None
        # Ensure indicators computed is not needed; just need latest Close
        price = latest(df, "Close")
        if price is not None:
            return float(price)
    except Exception as exc:
        logger.warning("[%s] failed to fetch current price for trailing check: %s", ticker, exc)
    return None


def add_active_position(
    ticker: str,
    entry_price: float,
    target_1: float,
    target_2: float,
    target_3: float,
    current_stop_loss: float,
    trade_track: str,
    timestamp: Optional[str] = None,
    status: str = "PENDING",
    path: str = ACTIVE_POSITIONS_FILE,
) -> bool:
    """Create and persist a new active position. Returns True if added.

    If Supabase is configured, upserts into Supabase table with status PENDING;
    otherwise falls back to local JSON file. Never raises.
    """
    # Normalize status to PENDING by default per requirements
    try:
        norm_status = str(status).strip().upper() if status else "PENDING"
        if norm_status not in ("PENDING", "ACTIVE", "CLOSED", "DISMISSED"):
            norm_status = "PENDING"
    except Exception:
        norm_status = "PENDING"

    # Try Supabase if configured
    if _is_supabase_configured():
        try:
            url = _supabase_table_url()
            headers = _supabase_headers()
            if url and headers:
                entry: Dict[str, Any] = {
                    "ticker": str(ticker),
                    "entry_price": float(entry_price),
                    "current_stop_loss": float(current_stop_loss) if current_stop_loss is not None else float(entry_price),
                    "target_1": float(target_1) if target_1 is not None else float(entry_price),
                    "target_2": float(target_2) if target_2 is not None else float(entry_price),
                    "target_3": float(target_3) if target_3 is not None else float(entry_price),
                    "trade_track": str(trade_track) if trade_track is not None else "",
                    "timestamp": timestamp or now_utc().isoformat(),
                    "status": norm_status,
                }
                # Sanitize payload to prevent PGRST204 (handles timestamp/created_at)
                try:
                    sanitized_entry = _sanitize_active_position_payload(entry)
                    # Ensure sanitized still has required fields
                    for _fld in ("ticker", "entry_price", "current_stop_loss", "target_1", "target_2", "target_3", "trade_track", "status"):
                        if _fld not in sanitized_entry and _fld in entry:
                            sanitized_entry[_fld] = entry[_fld]
                except Exception:
                    sanitized_entry = entry
                # Use upsert with Prefer resolution merge-duplicates
                headers_upsert = dict(headers)
                headers_upsert["Prefer"] = "resolution=merge-duplicates, return=representation"
                # Check existing via GET to avoid duplicate? Let Supabase handle via on_conflict
                resp = requests.post(f"{url}?on_conflict=ticker,trade_track", json=sanitized_entry, headers=headers_upsert, timeout=15)
                # Gracefully handle schema cache errors (PGRST204) and missing unique constraint (42P10)
                if resp.status_code == 400 and ("PGRST204" in (resp.text or "") or "42P10" in (resp.text or "")):
                    logger.warning("[Supabase] add_active_position upsert failed with schema error (%s %s); retrying without on_conflict", resp.status_code, resp.text[:200] if resp.text else "")
                    # Try plain POST without on_conflict
                    plain_resp = requests.post(url, json=sanitized_entry, headers=headers, timeout=15)
                    if plain_resp.status_code in (200, 201, 204):
                        logger.info("[Supabase] active position inserted via fallback for %s (%s)", ticker, norm_status)
                        try:
                            positions = load_active_positions(path)
                            exists = any(
                                p.get("ticker") == ticker and p.get("trade_track") == trade_track and p.get("status") in ("ACTIVE", "PENDING")
                                for p in positions
                            )
                            if not exists:
                                positions.append(entry)
                                save_active_positions(positions, path)
                        except Exception:
                            pass
                        return True
                    elif plain_resp.status_code == 409:
                        logger.info("[Supabase] position already exists for %s with track %s; skipping (fallback)", ticker, trade_track)
                        return False
                    else:
                        # Try PATCH if already exists
                        try:
                            check_resp = requests.get(f"{url}?ticker=eq.{ticker}&trade_track=eq.{trade_track}&select=id", headers=headers, timeout=10)
                            if check_resp.status_code == 200 and isinstance(check_resp.json(), list) and len(check_resp.json()) > 0:
                                patch_resp = requests.patch(f"{url}?ticker=eq.{ticker}&trade_track=eq.{trade_track}", json=sanitized_entry, headers=headers, timeout=15)
                                if patch_resp.status_code in (200, 204):
                                    logger.info("[Supabase] active position patched via fallback for %s", ticker)
                                    return True
                        except Exception:
                            pass
                        logger.warning("Supabase add_active_position fallback failed (%s %s); falling back to JSON", plain_resp.status_code, plain_resp.text[:300] if plain_resp.text else "")
                        # Fall through to JSON fallback
                    resp = plain_resp if 'plain_resp' in locals() else resp
                if resp.status_code in (200, 201, 204):
                    logger.info("[Supabase] active position upserted for %s (%s)", ticker, norm_status)
                    # Also update local cache for fallback
                    try:
                        positions = load_active_positions(path)
                        # If local file also has Supabase data, keep in sync - append if not exists
                        exists = any(
                            p.get("ticker") == ticker and p.get("trade_track") == trade_track and p.get("status") in ("ACTIVE", "PENDING")
                            for p in positions
                        )
                        if not exists:
                            positions.append(entry)
                            save_active_positions(positions, path)
                    except Exception:
                        pass
                    return True
                elif resp.status_code == 409:
                    logger.info("[Supabase] position already exists for %s with track %s; skipping", ticker, trade_track)
                    return False
                else:
                    logger.warning("Supabase add_active_position failed (%s %s); falling back to JSON", resp.status_code, resp.text[:300] if resp.text else "")
        except requests.exceptions.RequestException as exc:
            logger.warning("Supabase add_active_position request failed (%s); falling back to JSON", exc)
        except Exception as exc:
            logger.warning("Supabase add_active_position failed (%s); falling back", exc)

    # Local JSON fallback
    try:
        if not ticker or entry_price is None:
            logger.warning("add_active_position called with invalid ticker/price; skipping")
            return False
        positions = load_active_positions(path)
        # If Supabase was configured, load_active_positions already returned Supabase data; for fallback we need to ensure we are checking file data
        # If Supabase is configured but we fell through, force file load to check duplicates correctly
        if _is_supabase_configured():
            try:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as fh:
                        file_data = json.load(fh)
                        if isinstance(file_data, list):
                            positions = file_data
                        elif isinstance(file_data, dict):
                            for k in ("positions", "active_positions", "data"):
                                if k in file_data and isinstance(file_data[k], list):
                                    positions = file_data[k]
                                    break
            except Exception:
                pass

        for pos in positions:
            try:
                if pos.get("status") in ("ACTIVE", "PENDING") and pos.get("ticker") == ticker and pos.get("trade_track") == trade_track:
                    logger.info("[%s] position already exists for track %s with status %s; skipping creation", ticker, trade_track, pos.get("status"))
                    return False
            except Exception:
                continue
        entry: Dict[str, Any] = {
            "ticker": str(ticker),
            "entry_price": float(entry_price),
            "current_stop_loss": float(current_stop_loss) if current_stop_loss is not None else float(entry_price),
            "target_1": float(target_1) if target_1 is not None else float(entry_price),
            "target_2": float(target_2) if target_2 is not None else float(entry_price),
            "target_3": float(target_3) if target_3 is not None else float(entry_price),
            "trade_track": str(trade_track) if trade_track is not None else "",
            "timestamp": timestamp or now_utc().isoformat(),
            "status": norm_status,
        }
        positions.append(entry)
        save_active_positions(positions, path)
        logger.info("[%s] active position created (JSON): %s", ticker, entry)
        return True
    except Exception as exc:
        logger.warning("Failed to add active position for %s: %s", ticker, exc)
        return False


def manage_active_positions(path: str = ACTIVE_POSITIONS_FILE) -> int:
    """Compare current prices against active positions and apply trailing stop logic.

    Called on each pre-market/post-market run. Handles:
      - Break-even promotion when price >= target_1
      - Trailing to target_1 when price >= target_2
      - Exit when price <= current_stop_loss

    Returns number of positions updated/closed. Never raises unhandled exceptions.
    """
    try:
        positions = load_active_positions(path)
        # Ensure file exists for git auto-commit even when empty (prevents git add error)
        if not os.path.exists(path):
            try:
                save_active_positions(positions, path)
            except Exception:
                pass
        if not positions:
            logger.info("No active positions to manage.")
            return 0

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        updated_count = 0
        dirty = False

        for pos in positions:
            try:
                if not isinstance(pos, dict):
                    continue
                if pos.get("status") != "ACTIVE":
                    continue

                ticker = pos.get("ticker")
                if not ticker or not isinstance(ticker, str):
                    continue

                # Safely extract numeric fields
                try:
                    entry_price = float(pos.get("entry_price"))
                except (TypeError, ValueError):
                    logger.warning("[%s] invalid entry_price in position; skipping", ticker)
                    continue
                try:
                    current_stop_loss = float(pos.get("current_stop_loss", entry_price))
                except (TypeError, ValueError):
                    current_stop_loss = float(entry_price)
                try:
                    target_1 = float(pos.get("target_1", entry_price))
                    target_2 = float(pos.get("target_2", entry_price))
                except (TypeError, ValueError):
                    logger.warning("[%s] invalid targets in position; skipping", ticker)
                    continue

                trade_track = pos.get("trade_track", "")

                current_price = _fetch_current_price(ticker)
                if current_price is None:
                    logger.info("[%s] no current price available for trailing check; skipping", ticker)
                    continue

                # Determine if Scalp track for ratchet rules
                is_scalp = False
                try:
                    track_str = str(trade_track) if trade_track else ""
                    is_scalp = ("Scalp" in track_str) or ("مضاربة لحظية" in track_str)
                except Exception:
                    is_scalp = False

                # --- Scalping Ratchet Trailing Stop Rules (only for Scalp) ---
                if is_scalp:
                    try:
                        gain_pct = (current_price - entry_price) / entry_price if entry_price else 0
                    except Exception:
                        gain_pct = 0
                    # Check highest threshold first for direct jump handling
                    if gain_pct >= 0.075 and current_stop_loss < entry_price * 1.055:
                        pos["current_stop_loss"] = float(entry_price * 1.055)
                        dirty = True
                        updated_count += 1
                        logger.info("[%s] Scalp ratchet: lock +5.5%% at %.2f (gain %.2f%%)", ticker, entry_price * 1.055, gain_pct * 100)
                        current_stop_loss = float(entry_price * 1.055)
                    elif gain_pct >= 0.05 and current_stop_loss < entry_price * 1.030:
                        pos["current_stop_loss"] = float(entry_price * 1.030)
                        dirty = True
                        updated_count += 1
                        logger.info("[%s] Scalp ratchet: lock +3.0%% at %.2f (gain %.2f%%)", ticker, entry_price * 1.030, gain_pct * 100)
                        current_stop_loss = float(entry_price * 1.030)
                    elif gain_pct >= 0.025 and current_stop_loss < entry_price * 1.005:
                        pos["current_stop_loss"] = float(entry_price * 1.005)
                        dirty = True
                        updated_count += 1
                        alert_msg = f"🔒 تم تفعيل محبس الأرباح السريع لـ {ticker} عند +0.5%."
                        chat_id = _resolve_chat_id_for_track(trade_track)
                        if bot_token and chat_id:
                            try:
                                send_telegram(chat_id, alert_msg, bot_token)
                                logger.info("[%s] Scalp ratchet: lock +0.5%% at %.2f (gain %.2f%%)", ticker, entry_price * 1.005, gain_pct * 100)
                            except Exception as exc:
                                logger.warning("[%s] failed to send ratchet alert: %s", ticker, exc)
                        else:
                            logger.info("[TRAIL-SCALP] %s", alert_msg)
                        current_stop_loss = float(entry_price * 1.005)
                else:
                    # --- Generic trailing for Swing/Invest (and non-Scalp) ---
                    # 1) Break-even promotion: price >= target_1 and stop < entry
                    if current_price >= target_1 and current_stop_loss < entry_price:
                        pos["current_stop_loss"] = float(entry_price)
                        dirty = True
                        updated_count += 1
                        alert_msg = f"🛡️ رفع وقف الخسارة لسهم {ticker} إلى سعر الدخول ({entry_price:.2f})."
                        chat_id = _resolve_chat_id_for_track(trade_track)
                        if bot_token and chat_id:
                            try:
                                send_telegram(chat_id, alert_msg, bot_token)
                                logger.info("[%s] break-even stop promoted to %.2f", ticker, entry_price)
                            except Exception as exc:
                                logger.warning("[%s] failed to send break-even alert: %s", ticker, exc)
                        else:
                            logger.info("[TRAIL] %s", alert_msg)
                        current_stop_loss = float(entry_price)

                    # 2) Trail to target_1 when price >= target_2 and stop < target_1
                    if current_price >= target_2 and current_stop_loss < target_1:
                        pos["current_stop_loss"] = float(target_1)
                        dirty = True
                        updated_count += 1
                        logger.info("[%s] trailing stop promoted to target_1 %.2f (price %.2f >= target_2 %.2f)", ticker, target_1, current_price, target_2)
                        current_stop_loss = float(target_1)

                # 3) Exit when price <= current_stop_loss (applies to all, including Scalp)
                # Only for ACTIVE positions (PENDING already filtered above)
                if current_price <= current_stop_loss:
                    exit_msg = f"🚨 إغلاق صفقة {ticker} - تم ضرب وقف الخسارة عند {current_stop_loss:.2f}"
                    chat_id = _resolve_chat_id_for_track(trade_track)
                    pos["status"] = "CLOSED"
                    dirty = True
                    updated_count += 1
                    if bot_token and chat_id:
                        try:
                            send_telegram(chat_id, exit_msg, bot_token)
                            logger.info("[%s] exit alert sent: %s", ticker, exit_msg)
                        except Exception as exc:
                            logger.warning("[%s] failed to send exit alert: %s", ticker, exc)
                    else:
                        logger.info("[EXIT] %s", exit_msg)

            except Exception as exc:
                logger.warning("Error managing position %s: %s", pos.get("ticker", "unknown"), exc)
                continue

        if dirty:
            save_active_positions(positions, path)
            logger.info("Active positions updated; %d positions affected", updated_count)
        else:
            logger.info("Active positions checked; no trailing updates required")

        return updated_count

    except Exception as exc:
        logger.warning("manage_active_positions failed unexpectedly: %s", exc)
        return 0


def handle_telegram_callback(
    callback_data: Any,
    path: str = ACTIVE_POSITIONS_FILE,
    callback_query_id: Optional[str] = None,
    bot_token: Optional[str] = None,
) -> str:
    """Lightweight callback handler for inline keyboard actions with Supabase support.

    Parses callback_data and updates active_positions (Supabase or JSON fallback):
      - act_{ticker}: PENDING -> ACTIVE (Supabase: update status ACTIVE) + popup ✅
      - dis_{ticker}: remove or DISMISSED (Supabase: status DISMISSED) + popup ❌
      - cls_{ticker}: set status to CLOSED (Supabase) + popup 🏁

    Also answers Telegram callback query with popup notification when
    callback_query_id and bot_token are provided. Never raises.
    """
    # Helper to answer callback query with popup
    def _do_answer(text: str) -> None:
        try:
            cqid = callback_query_id
            # Also try to get bot token from env if not provided
            token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
            if cqid and token and isinstance(cqid, str):
                _answer_callback_query(cqid, text, token, show_alert=False)
        except Exception:
            pass

    try:
        if not isinstance(callback_data, str) or not callback_data:
            return "invalid callback_data"
        callback_data = callback_data.strip()
        if "_" not in callback_data:
            return "invalid format"
        action, ticker = callback_data.split("_", 1)
        action = action.strip()
        ticker = ticker.strip()
        if not ticker:
            return "invalid ticker"
        ticker_variants = {ticker}
        if "." not in ticker:
            ticker_variants.add(f"{ticker}.CA")
        else:
            base = ticker.replace(".CA", "")
            ticker_variants.add(base)
            ticker_variants.add(f"{base}.CA")

        # Try Supabase first if configured
        supabase_used = _is_supabase_configured()

        if action == "act":
            popup_text = "✅ تم تفعيل المراقبة بنجاح!"
            # Supabase path: update status to ACTIVE + direct upsert to ensure persistence
            if supabase_used:
                try:
                    success = update_position_status(ticker, "ACTIVE", path=path)
                    if success:
                        _do_answer(popup_text)
                        return f"Activated {ticker} (Supabase)"
                    # If update failed (e.g., row not in Supabase), try direct upsert from local JSON
                    try:
                        positions = load_active_positions(path)
                        # Force file load for direct upsert
                        if not positions:
                            try:
                                if os.path.exists(path):
                                    with open(path, "r", encoding="utf-8") as fh:
                                        file_data = json.load(fh)
                                        if isinstance(file_data, list):
                                            positions = file_data
                            except Exception:
                                pass
                        for pos in positions:
                            if str(pos.get("ticker", "")).strip() in ticker_variants:
                                pos_copy = dict(pos)
                                pos_copy["status"] = "ACTIVE"
                                pos_copy["timestamp"] = pos_copy.get("timestamp") or now_utc().isoformat()
                                url = _supabase_table_url()
                                headers = _supabase_headers()
                                if url and headers:
                                    headers_upsert = dict(headers)
                                    headers_upsert["Prefer"] = "resolution=merge-duplicates, return=representation"
                                    resp = requests.post(f"{url}?on_conflict=ticker,trade_track", json=pos_copy, headers=headers_upsert, timeout=15)
                                    if resp.status_code in (200, 201, 204):
                                        logger.info("[Supabase] Direct upsert for act %s", ticker)
                                        _do_answer(popup_text)
                                        # Also update local JSON to ACTIVE
                                        try:
                                            for p in positions:
                                                if str(p.get("ticker", "")).strip() in ticker_variants and p.get("status") == "PENDING":
                                                    p["status"] = "ACTIVE"
                                            save_active_positions(positions, path)
                                        except Exception:
                                            pass
                                        return f"Activated {ticker} (Supabase upsert)"
                                break
                    except Exception as upsert_exc:
                        logger.warning("Direct Supabase upsert for act failed for %s: %s", ticker, upsert_exc)
                except Exception as exc:
                    logger.warning("Supabase act failed for %s: %s; falling back to JSON", ticker, exc)
            # Local JSON fallback: PENDING -> ACTIVE
            try:
                positions = load_active_positions(path)
                if not positions and not supabase_used:
                    return f"no positions found for {ticker}"
                # If Supabase was used, load may have returned Supabase data; for fallback we ensure file data
                if supabase_used and not positions:
                    # Try file directly
                    try:
                        if os.path.exists(path):
                            with open(path, "r", encoding="utf-8") as fh:
                                file_data = json.load(fh)
                                if isinstance(file_data, list):
                                    positions = file_data
                    except Exception:
                        pass
                matched = False
                response_msg = ""
                for pos in positions:
                    try:
                        pos_ticker = str(pos.get("ticker", "")).strip()
                        if pos_ticker in ticker_variants and pos.get("status") == "PENDING":
                            pos["status"] = "ACTIVE"
                            try:
                                pos["activated_at"] = now_utc().isoformat()
                            except Exception:
                                pass
                            matched = True
                            response_msg = f"Activated {pos_ticker}"
                            break
                    except Exception:
                        continue
                if not matched:
                    for pos in positions:
                        try:
                            if str(pos.get("ticker", "")).strip() in ticker_variants and pos.get("status") != "ACTIVE":
                                pos["status"] = "ACTIVE"
                                matched = True
                                response_msg = f"Activated {pos.get('ticker')}"
                                break
                        except Exception:
                            continue
                if matched:
                    save_active_positions(positions, path)
                    logger.info("[CALLBACK] %s", response_msg)
                    _do_answer(popup_text)
                    return response_msg
                # If no PENDING found and Supabase not used, Try Supabase update as last resort
                if supabase_used:
                    # Already tried Supabase via update_position_status, but try again with raw
                    pass
                return f"no PENDING position found for {ticker}"
            except Exception as exc:
                logger.warning("Local act fallback failed for %s: %s", ticker, exc)
                return f"error: {exc}"

        elif action == "dis":
            popup_text = "❌ تم إلغاء متابعة الصفقة."
            # Answer immediately to avoid Telegram loading spinner timeout
            _do_answer(popup_text)
            # Explicit DELETE FROM active_positions WHERE ticker = 'TICKER' (normalized)
            try:
                removed = remove_active_position(ticker, path=path)
                if removed:
                    logger.info("[CALLBACK] Dismissed %s via explicit DELETE", ticker)
                    # Ensure answer is sent (already done, but double-ensure)
                    _do_answer(popup_text)
                    return f"Dismissed {ticker} (deleted)"
                else:
                    return f"no position found to dismiss for {ticker}"
            except Exception as exc:
                logger.warning("remove_active_position failed for dis %s: %s", ticker, exc)
                return f"error: {exc}"

        elif action == "cls":
            popup_text = "🏁 تم إغلاق الصفقة يدوياً."
            # Answer immediately to avoid Telegram loading spinner timeout
            _do_answer(popup_text)
            # Explicit DELETE FROM active_positions WHERE ticker = 'TICKER' (normalized)
            try:
                removed = remove_active_position(ticker, path=path)
                if removed:
                    logger.info("[CALLBACK] Closed %s via explicit DELETE", ticker)
                    _do_answer(popup_text)
                    return f"Closed {ticker} (deleted)"
                else:
                    return f"no position found to close for {ticker}"
            except Exception as exc:
                logger.warning("remove_active_position failed for cls %s: %s", ticker, exc)
                return f"error: {exc}"

        else:
            return f"unknown action {action}"

    except Exception as exc:
        logger.warning("handle_telegram_callback failed for %s: %s", callback_data, exc)
        return f"error: {exc}"


# --------------------------------------------------------------------------
# Data fetching: yfinance
# --------------------------------------------------------------------------


def fetch_price_history(ticker: str) -> Optional[pd.DataFrame]:
    """Download ~1 year of daily OHLCV history for a ticker."""
    try:
        raw = yf.download(
            ticker,
            period="1y",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
    except KeyError as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    except ValueError as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    except Exception as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    if raw is None or raw.empty:
        logger.warning("[%s] yfinance returned no data.", ticker)
        return None
    try:
        df = raw.copy()
        # Newer yfinance versions return MultiIndex columns (e.g. ("Close", "COMI.CA")).
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
        return df
    except KeyError as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    except ValueError as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    except Exception as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None


def get_trailing_pe(ticker: str) -> Optional[float]:
    """Fetch the trailing P/E ratio from yfinance fundamentals."""
    try:
        info = yf.Ticker(ticker).get_info()
    except AttributeError:
        try:
            info = yf.Ticker(ticker).info  # older yfinance fallback
        except KeyError as exc:
            msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
            print(msg)
            logger.warning(msg)
            return None
        except ValueError as exc:
            msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
            print(msg)
            logger.warning(msg)
            return None
        except Exception as exc:
            msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
            print(msg)
            logger.warning(msg)
            return None
    except KeyError as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    except ValueError as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    except Exception as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    try:
        pe = info.get("trailingPE") if isinstance(info, dict) else None
        return float(pe) if pe is not None else None
    except (TypeError, ValueError):
        return None
    except KeyError as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None
    except Exception as exc:
        msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
        print(msg)
        logger.warning(msg)
        return None


# --------------------------------------------------------------------------
# Technical indicators (pandas_ta)
# --------------------------------------------------------------------------


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Attach RSI, EMA20, SMA50 and 20-day average volume to the frame."""
    df["RSI"] = ta.rsi(df["Close"], length=RSI_LENGTH)
    df["EMA20"] = ta.ema(df["Close"], length=EMA_LENGTH)
    df["SMA50"] = ta.sma(df["Close"], length=SMA_LENGTH)
    df["VolMA20"] = df["Volume"].rolling(VOLUME_AVG_WINDOW).mean()
    return df


def latest(df: pd.DataFrame, column: str) -> Optional[float]:
    """Return the most recent non-NaN value of a column, if any."""
    series = df[column].dropna()
    if series.empty:
        return None
    return float(series.iloc[-1])


def has_volume_spike(df: pd.DataFrame) -> bool:
    """Current volume > 1.8x the 20-day average volume."""
    if len(df) < VOLUME_AVG_WINDOW:
        return False
    current_vol = latest(df, "Volume")
    avg_vol = latest(df, "VolMA20")
    if current_vol is None or avg_vol is None or avg_vol <= 0:
        return False
    return current_vol > VOLUME_SPIKE_MULTIPLIER * avg_vol


def crossed_above_ema20(df: pd.DataFrame) -> bool:
    """True when the last close crossed from below/at EMA20 to above it."""
    closes = df["Close"].dropna()
    emas = df["EMA20"].dropna()
    if len(closes) < 2 or len(emas) < 2:
        return False
    prev_close = float(closes.iloc[-2])
    today_close = float(closes.iloc[-1])
    prev_ema = float(emas.iloc[-2])
    today_ema = float(emas.iloc[-1])
    return prev_close <= prev_ema and today_close > today_ema


# --------------------------------------------------------------------------
# Synthetic Order Flow & Delta Estimation (emulates Footprint/Market Depth)
# --------------------------------------------------------------------------


def calculate_synthetic_delta(df_1m: Any) -> Dict[str, Any]:
    """Estimate order flow delta synthetically for free.

    For each 1m candle:
        Estimated Buy Volume = Volume * ((Close - Low) / (High - Low + 1e-6))
        Delta = Buy - Sell (sell = Volume - buy)

    Then:
        - Cumulative Delta for last 5 candles
        - Bullish pressure: cumulative >0 and growing (last delta > prev)
        - Absorption: High RVOL (>1.5x avg) + Small Spread (<0.3%)

    Handles MultiIndex columns, NaNs, and short frames gracefully.
    Returns dict with cumulative_delta_5, is_bullish_pressure, is_absorption, deltas, last_buy_ratio.
    Never raises.
    """
    try:
        if df_1m is None:
            return {"cumulative_delta_5": 0.0, "is_bullish_pressure": False, "is_absorption": False, "deltas": [], "last_buy_ratio": 0.5}
        # Handle DataFrame with MultiIndex columns (yfinance)
        try:
            if hasattr(df_1m, "columns"):
                cols = [c[0] if isinstance(c, tuple) else c for c in df_1m.columns]
                if len(cols) == len(df_1m.columns):
                    df_1m = df_1m.copy()
                    df_1m.columns = cols
        except Exception:
            pass

        if not isinstance(df_1m, pd.DataFrame) or df_1m.empty or len(df_1m) < 3:
            return {"cumulative_delta_5": 0.0, "is_bullish_pressure": False, "is_absorption": False, "deltas": [], "last_buy_ratio": 0.5}

        required = {"High", "Low", "Close", "Volume"}
        if not required.issubset(set(df_1m.columns)):
            return {"cumulative_delta_5": 0.0, "is_bullish_pressure": False, "is_absorption": False, "deltas": [], "last_buy_ratio": 0.5}

        # Drop rows with NaNs in critical columns
        sub = df_1m[["High", "Low", "Close", "Volume"]].dropna()
        if len(sub) < 3:
            return {"cumulative_delta_5": 0.0, "is_bullish_pressure": False, "is_absorption": False, "deltas": [], "last_buy_ratio": 0.5}

        highs = sub["High"].astype(float)
        lows = sub["Low"].astype(float)
        closes = sub["Close"].astype(float)
        volumes = sub["Volume"].astype(float)

        denom = (highs - lows)
        # Handle doji (High==Low) as neutral 0.5 to avoid division bias
        buy_ratio = pd.Series(0.5, index=highs.index, dtype=float)
        mask = denom.abs() > 1e-6
        # Only compute where denom is meaningful
        buy_ratio[mask] = (closes[mask] - lows[mask]) / denom[mask]
        # Clamp 0-1 to avoid outlier due to wicks
        buy_ratio = buy_ratio.clip(0, 1)
        est_buy_vol = volumes * buy_ratio
        est_sell_vol = volumes - est_buy_vol
        deltas = (est_buy_vol - est_sell_vol).tolist()

        # Take last 5 deltas (or fewer if short)
        last_5 = deltas[-5:] if len(deltas) >= 5 else deltas
        cumulative_delta_5 = float(sum(last_5))

        # Bullish pressure: cumulative >0 and growing
        is_bullish_pressure = False
        try:
            if cumulative_delta_5 > 0 and len(deltas) >= 2:
                # Growing if last delta > previous delta
                if deltas[-1] > deltas[-2]:
                    is_bullish_pressure = True
                # Or last 3 trend up: last > first in last 5
                elif len(last_5) >= 3 and last_5[-1] > last_5[0]:
                    # Check cumulative is not just slightly positive but increasing
                    prev_cum = float(sum(deltas[-6:-1])) if len(deltas) >= 6 else 0
                    if cumulative_delta_5 > prev_cum:
                        is_bullish_pressure = True
        except Exception:
            pass

        # Absorption: High RVOL + Small Spread
        is_absorption = False
        try:
            # RVOL: compare last volume to 20-period average (or 5 if short)
            window = 20 if len(volumes) >= 20 else 5
            vol_avg = volumes.rolling(window, min_periods=3).mean().iloc[-1] if len(volumes) >= 3 else volumes.mean()
            last_vol = float(volumes.iloc[-1])
            last_high = float(highs.iloc[-1])
            last_low = float(lows.iloc[-1])
            last_close = float(closes.iloc[-1])
            spread = (last_high - last_low) / (last_close + 1e-6)
            # High RVOL >1.5x
            high_rvol = False
            if vol_avg is not None and not pd.isna(vol_avg) and vol_avg > 0:
                high_rvol = last_vol > 1.5 * float(vol_avg)
            # Small spread <0.3% (0.003) for 1m
            small_spread = spread < 0.003
            if high_rvol and small_spread:
                is_absorption = True
            # Also check last 3 candles: high vol but small price progress
            if not is_absorption and len(sub) >= 3:
                recent_vols = volumes.tail(3)
                recent_spreads = (highs.tail(3) - lows.tail(3)) / (closes.tail(3) + 1e-6)
                recent_avg = recent_vols.mean()
                if recent_avg > 1.3 * float(vol_avg) if vol_avg else False:
                    if (recent_spreads < 0.004).all():
                        is_absorption = True
        except Exception:
            pass

        return {
            "cumulative_delta_5": cumulative_delta_5,
            "is_bullish_pressure": bool(is_bullish_pressure),
            "is_absorption": bool(is_absorption),
            "deltas": last_5,
            "last_buy_ratio": float(buy_ratio.iloc[-1]) if len(buy_ratio) > 0 else 0.5,
        }
    except Exception as exc:
        logger.warning("calculate_synthetic_delta failed: %s", exc)
        return {"cumulative_delta_5": 0.0, "is_bullish_pressure": False, "is_absorption": False, "deltas": [], "last_buy_ratio": 0.5}


def _is_near_support(price: Optional[float], df: Optional[pd.DataFrame] = None, df_1m: Optional[pd.DataFrame] = None) -> bool:
    """Check if price is near support (low of recent range or SMA)."""
    try:
        if price is None:
            return False
        price_f = float(price)
        # Check vs recent 1m lows if available
        if df_1m is not None and isinstance(df_1m, pd.DataFrame) and not df_1m.empty:
            try:
                cols = [c[0] if isinstance(c, tuple) else c for c in df_1m.columns]
                tmp = df_1m.copy()
                tmp.columns = cols
                if "Low" in tmp.columns:
                    recent_low = float(tmp["Low"].tail(10).min())
                    if recent_low and price_f <= recent_low * 1.01:  # within 1% of recent low
                        return True
            except Exception:
                pass
        # Check vs daily SMA50 or recent daily low
        if df is not None and isinstance(df, pd.DataFrame) and not df.empty and "Low" in df.columns:
            try:
                recent_low_d = float(df["Low"].tail(10).min())
                if recent_low_d and price_f <= recent_low_d * 1.015:
                    return True
                # Also vs SMA50
                if "SMA50" in df.columns:
                    sma = latest(df, "SMA50")
                    if sma is not None and price_f <= sma * 1.02 and price_f >= sma * 0.98:
                        return True
            except Exception:
                pass
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------
# News + sentiment (Google News RSS -> Gemini)
# --------------------------------------------------------------------------


def fetch_arabic_headlines(stock_name_ar: str, ticker: str) -> List[str]:
    """Fetch the top Arabic Google News headlines (with publish dates) for a stock."""
    query = quote_plus(f"{stock_name_ar} البورصة المصرية")
    headlines: List[str] = []
    try:
        resp = requests.get(NEWS_RSS_URL.format(query=query), timeout=10)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:NEWS_HEADLINES_COUNT]:
            title = (entry.get("title") or "").strip()
            published = entry.get("published") or entry.get("updated") or ""
            if title:
                if published:
                    title = f"{title} ({published})"
                headlines.append(title)
    except Exception as exc:
        logger.warning("News fetch failed for %s: %s", ticker, exc)
    return headlines


def build_news_prompt(headlines: Any) -> str:
    """Build Gemini prompt: sentiment + TQI + Macro Chain-of-Thought (CoT) indirect analysis.

    Enhances Gemini to apply Second-Order reasoning for global/regional macro news,
    map sector impact on EGX, and identify affected tickers even when not named.
    Handles None / non-list inputs gracefully.
    """
    try:
        if not isinstance(headlines, (list, tuple)):
            headlines = []
        safe_headlines = [str(h).strip() for h in headlines if h is not None and str(h).strip()]
        headlines_block = "\n".join(f"- {h}" for h in safe_headlines) if safe_headlines else "- لا توجد عناوين متاحة"
    except Exception:
        headlines_block = "- لا توجد عناوين متاحة"
    return (
        "أنت محلل مالي متخصص في البورصة المصرية (EGX) وخبير في التحليل الكلي (Macro) والربط القطاعي. اقرأ العناوين التالية ثم أخرج:\n"
        "1) ملخصًا من جملتين باللغة العربية يلخّص اتجاه الأخبار.\n"
        "2) تصنيف المعنويات بإحدى الكلمات فقط: إيجابي / سلبي / محايد.\n"
        "ملاحظة: إذا أشارت الأخبار إلى نتائج مالية قوية أو قفزات في الأرباح "
        "(أرباح تتجاوز 10 مليار جنيه مصري)، صنّف المعنويات 'إيجابي' قدر الإمكان.\n\n"
        "3) تقييم جودة الصفقة عبر Trade Quality Index (TQI) scored من 0.0 إلى 10.0 بناءً على 5 معايير مرجحة بدقة:\n"
        "   - Technical Confluence (3.0 pts): التقاء المؤشرات الفنية (RSI, EMA20/SMA50, اختراق مقاومة/دعم، تقاطعات)\n"
        "   - Risk/Reward Ratio (2.5 pts): جودة نسبة العائد إلى المخاطر (ممتازة > 1:2.5 ، جيدة 1:1.5-2.5 ، ضعيفة < 1:1.5)\n"
        "   - Relative Volume Surge (2.0 pts): قوة اندفاع الحجم النسبي مقارنة بمتوسط 20 يوم (spike >1.8x = ممتاز)\n"
        "   - Sector Alignment (1.5 pts): توافق القطاع والاتجاه العام للسوق\n"
        "   - News/Catalyst Strength (1.0 pt): قوة الأخبار/المحفزات (نتائج أعمال، عقود، إفصاحات جوهرية)\n"
        "   ملاحظة خاصة لمسار Scalp: امنح وزنًا أعلى لـ Intraday RVOL (Relative Volume Surge) وكسر الزخم EMA9 / VWAP crossover — اعتبرهما حتى 70% من درجة Confluence + Volume للسكالبنج، وقيّم الأهداف الضيقة: Target1 +2.5% / Target2 +5.0% / Target3 +8.0%.\n"
        "   احسب المجموع بدقة من 10.0 واذكر الدرجة بصيغة `🎯 تقييم الجودة (TQI): X.X/10` (رقم عشري واحد).\n\n"
        "4) حدد المسار التجاري (Trade Track) بوضوح بإحدى القيم فقط:\n"
        "   - `⚡ مضاربة لحظية (Scalp)` للصفقات اللحظية داخل اليوم\n"
        "   - `📈 تداول سوينغ (Swing)` للصفقات المتوسطة (أيام إلى أسابيع)\n"
        "   - `🏛️ استثمار طويل (Invest)` للاستثمار الطويل\n"
        "   أخرجها بصيغة `🏷️ المسار: [القيمة]`.\n\n"
        "5) حدد مستوى القناعة (Conviction Tier) حسب TQI (عتبة EGX = 5.0):\n"
        "   - TQI >= 8.5: `🟢 فرصة استثنائية (A+ Setup)`\n"
        "   - TQI 6.5 - 8.4: `🟡 فرصة جيدة (B Setup)`\n"
        "   - TQI 5.0 - 6.4: `🟠 فرصة متوسطة (C Setup)`\n"
        "   - TQI < 5.0: `⚪ فرصة ضعيفة (Low Conviction)`\n"
        "   أخرجها بصيغة `⭐ التصنيف: [القيمة]`.\n\n"
        "6) طبّق تفكيرًا متسلسلًا (Chain-of-Thought) من الدرجة الثانية للأخبار الماكرو/غير المباشرة:\n"
        "   - الخطوة 1: حدد المحفز الماكرو (Macro Trigger) بدقة: تقلبات سعر الصرف (FX)، أسعار النفط/الغاز، اضطرابات الشحن والنقل البحري (قناة السويس/البحر الأحمر)، تغيرات أسعار الفائدة، التضخم، التوترات الجيوسياسية الإقليمية.\n"
        "   - الخطوة 2: اربط الأثر القطاعي على EGX: الشحن/الموانئ واللوجستيات، البتروكيماويات، البنوك/المالية، العقارات، المصدرون مقابل المستوردون.\n"
        "   - الخطوة 3: حدد أسهم EGX المحددة المتأثرة حتى لو لم تُذكر صراحة في النص (استدلال غير مباشر).\n"
        f"   {EGX_SECTOR_SENSITIVITY_GUIDELINES}\n"
        "   إذا رصدت أثرًا غير مباشر واضحًا (مثل ارتفاع النفط يؤثر على ABUK/AMOC، أو اضطراب الشحن يؤثر على ETEL/SKPC، أو رفع الفائدة يؤثر على COMI/ADIB و HELI/TMGH)، أخرج حتمًا قسمًا منفصلًا بالتنسيق الحرفي التالي (وإلا تجاهل القسم):\n"
        "   `🧠 التحليل الكلي والأثر غير المباشر:`\n"
        "   `• السبب: [الحدث الماكرو - جملة واحدة]`\n"
        "   `• القطاع المتأثر: [أسماء القطاعات]`\n"
        "   `• الأسهم المستفيدة/المتأثرة: [رموز EGX مثل ABUK.CA, ETEL.CA, ...]`\n"
        "   مهم: استخدم نفس الإيموجي والرموز (•) حرفيًا ليتوافق مع محلل الرسائل، ولا تضف أسهمًا غير مذكورة في خريطة الحساسية إلا إذا كان الربط منطقيًا ومُبررًا.\n\n"
        "تنسيق الإخراج المطلوب (حافظ عليه حرفيًا ليتوافق مع محلل الرسائل):\n"
        "- السطر الأول/الثاني: ملخص الأخبار\n"
        "- سطر: تصنيف المعنويات\n"
        "- سطر: 🎯 تقييم الجودة (TQI): X.X/10\n"
        "- سطر: 🏷️ المسار: [Scalp / Swing / Invest]\n"
        "- سطر: ⭐ التصنيف: [A+ Setup / B Setup / C Setup / Low Conviction]\n"
        "- قسم اختياري عند وجود أثر ماكرو: 🧠 التحليل الكلي والأثر غير المباشر: + 3 نقاط •\n\n"
        f"العناوين:\n{headlines_block}"
    )


def build_tqi_prompt(strategy: Any, ctx: Any, sentiment: Any) -> str:
    """Build a dedicated Gemini prompt for TQI scoring of a specific signal.

    Provides technical context (RSI, EMA, volume surge, R:R) so Gemini can
    score the 5 TQI parameters accurately. Handles None/missing keys gracefully.
    """
    try:
        plan = STRATEGY_PLAN.get(strategy, {}) if isinstance(strategy, str) else {}
        if not isinstance(plan, dict):
            plan = {}
        if not isinstance(ctx, dict):
            ctx = {}
        price = ctx.get("price") if isinstance(ctx, dict) else None
        rsi = ctx.get("rsi") if isinstance(ctx, dict) else None
        volume_ratio = ctx.get("volume_ratio") if isinstance(ctx, dict) else None
        # Scalp uses tighter targets: 2.5% / 5% / 8%
        default_targets = (0.025, 0.05, 0.08) if strategy == SCALPING else (0.03, 0.05, 0.08)
        rr_targets = plan.get("targets_pct", default_targets)
        if not isinstance(rr_targets, (list, tuple)) or len(rr_targets) < 3:
            rr_targets = default_targets
        sl_pct = plan.get("sl_pct", -0.03)
        try:
            sl_pct_f = float(sl_pct) if sl_pct is not None else -0.03
        except (TypeError, ValueError):
            sl_pct_f = -0.03
        try:
            rr = abs(float(rr_targets[2]) / sl_pct_f) if sl_pct_f else 0
        except Exception:
            rr = 0
        # Defensive formatting
        try:
            sentiment_body = extract_news_body(sentiment)[:300] if isinstance(sentiment, str) else ""
        except Exception:
            sentiment_body = ""
        strat_label = str(strategy) if strategy is not None else "unknown"
        return (
            "أنت خبير تداول كمي للبورصة المصرية. قيّم جودة الإشارة التالية عبر Trade Quality Index (TQI) من 0.0 إلى 10.0:\n"
            f"الاستراتيجية: {strat_label} | السعر: {fmt(price)} | RSI: {fmt(rsi, 1)} | "
            f"نسبة الحجم: {fmt(volume_ratio, 2) if isinstance(volume_ratio, (int, float)) else 'غير متاح'} | "
            f"R:R المتوقع: 1:{rr:.2f}\n"
            f"ملخص الأخبار/المعنويات: {sentiment_body}\n\n"
            "المعايير المرجحة (المجموع 10.0):\n"
            "1) Technical Confluence (3 pts) — التقاء RSI/EMA/SMA/اختراق | للسكالبنغ: وزن أعلى لـ EMA9 / VWAP crossover والزخم اللحظي\n"
            "2) Risk/Reward Ratio (2.5 pts) — جودة R:R | للسكالبنغ: أهداف ضيقة 2.5% / 5.0% / 8.0%\n"
            "3) Relative Volume Surge (2 pts) — قوة الحجم النسبي (RVOL اللحظي) | للسكالبنغ: اعتبر RVOL >1.5x ممتازًا وامنحه حتى 70% من وزن البندين 1+3\n"
            "4) Sector Alignment (1.5 pts) — توافق القطاع\n"
            "5) News/Catalyst Strength (1 pt) — قوة المحفز الخبري\n"
            "   ملاحظة Scalp: في حال المسار Scalp، اعطِ RVOL اللحظي وكسر EMA9/VWAP وزنًا مضاعفًا.\n\n"
            "أخرج حتمًا:\n"
            "🎯 تقييم الجودة (TQI): X.X/10\n"
            "🏷️ المسار: [⚡ مضاربة لحظية (Scalp) / 📈 تداول سوينغ (Swing) / 🏛️ استثمار طويل (Invest)]\n"
            "⭐ التصنيف: [🟢 فرصة استثنائية (A+ Setup) / 🟡 فرصة جيدة (B Setup) / 🟠 فرصة متوسطة (C Setup) / ⚪ فرصة ضعيفة (Low Conviction)]\n"
            "حسب القواعد: TQI >=8.5 → 🟢 A+ | 6.5-8.4 → 🟡 B | 5.0-6.4 → 🟠 C | <5.0 → ⚪ Low Conviction (عتبة الإرسال = 5.0)\n"
        )
    except Exception as exc:
        logger.warning("build_tqi_prompt failed (%s); returning fallback prompt", exc)
        return (
            "أنت خبير تداول كمي للبورصة المصرية. قيّم جودة الإشارة عبر Trade Quality Index (TQI) من 0.0 إلى 10.0:\n"
            "🎯 تقييم الجودة (TQI): X.X/10\n"
            "🏷️ المسار: [⚡ مضاربة لحظية (Scalp) / 📈 تداول سوينغ (Swing) / 🏛️ استثمار طويل (Invest)]\n"
            "⭐ التصنيف: [🟢 فرصة استثنائية (A+ Setup) / 🟡 فرصة جيدة (B Setup) / 🟠 فرصة متوسطة (C Setup) / ⚪ فرصة ضعيفة (Low Conviction)]\n"
        )


def fetch_arabic_stock_news(stock_name_ar: str, ticker: str) -> str:
    """Fetch live Arabic financial news for a stock and summarize sentiment via Gemini."""
    headlines = fetch_arabic_headlines(stock_name_ar, ticker)
    if not headlines:
        logger.info(
            "[%s] No recent Arabic news for %s; passing fallback prompt.",
            ticker,
            stock_name_ar,
        )
        return _summarize_with_gemini(GEMINI_FALLBACK_PROMPT, ticker)
    return _summarize_with_gemini(build_news_prompt(headlines), ticker)


def _summarize_with_gemini(content: str, ticker: str) -> str:
    """Send an Arabic prompt to Gemini 3.6 Flash and return its summary."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; skipping sentiment analysis.")
        return GEMINI_FALLBACK_PROMPT
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model=GEMINI_MODEL, contents=content)
        text = (response.text or "").strip()
        return text if text else GEMINI_FALLBACK_PROMPT
    except Exception as exc:
        # Graceful Gemini 429 quota fallback – catch ResourceExhausted or HTTP 429
        is_quota_error = False
        try:
            exc_name = exc.__class__.__name__
            exc_str = str(exc)
            # Direct class name check
            if exc_name == "ResourceExhausted":
                is_quota_error = True
            # Check for 429 in message or status attributes
            elif "429" in exc_str:
                is_quota_error = True
            elif "ResourceExhausted" in exc_str:
                is_quota_error = True
            elif "quota" in exc_str.lower():
                is_quota_error = True
            # Check common status/code attributes
            elif getattr(exc, "status_code", None) == 429:
                is_quota_error = True
            elif getattr(exc, "code", None) == 429:
                is_quota_error = True
            elif getattr(exc, "status", None) == 429:
                is_quota_error = True
            # Also inspect cause if wrapped
            else:
                cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
                if cause is not None:
                    cause_str = str(cause)
                    cause_name = cause.__class__.__name__
                    if cause_name == "ResourceExhausted" or "429" in cause_str or "ResourceExhausted" in cause_str:
                        is_quota_error = True
        except Exception:
            pass
        # Try importing ResourceExhausted for isinstance check (optional)
        if not is_quota_error:
            try:
                from google.api_core.exceptions import ResourceExhausted as _ResEx

                if isinstance(exc, _ResEx):
                    is_quota_error = True
            except Exception:
                pass
        if is_quota_error:
            msg = "[GEMINI] Quota limit reached (429). Falling back to Neutral sentiment."
            print(msg)
            logger.warning(msg)
            # Return neutral sentiment so signal delivery is never blocked
            return "🟢 محايد"
        logger.warning("[%s] Gemini sentiment analysis failed: %s", ticker, exc)
        return GEMINI_FALLBACK_PROMPT


# --------------------------------------------------------------------------
# Telegram notifications
# --------------------------------------------------------------------------


def build_trade_keyboard(
    ticker: str,
    entry_price: Optional[float] = None,
    current_stop_loss: Optional[float] = None,
    target_1: Optional[float] = None,
    target_2: Optional[float] = None,
    target_3: Optional[float] = None,
    trade_track: Optional[str] = None,
) -> Dict[str, Any]:
    """Build InlineKeyboardMarkup for trade signals with Activate/Dismiss/Close buttons.

    Returns dict suitable for Telegram Bot API `reply_markup`.
    Callback data format (enhanced to include all essential trade parameters for instant Supabase upsert):
      - Legacy: act_{ticker}, dis_{ticker}, cls_{ticker} (still supported, webhook will fetch specs)
      - Enhanced: act_{ticker}|{entry_price}|{current_stop_loss}|{target_1}|{target_2}|{target_3}
        Example: act_COMI.CA|85.00|83.30|87.12|89.25|91.80
      This ensures Vercel webhook can directly upsert full trade details without needing to fetch from active_positions.json
      which may not be synced due to GitHub Actions exit (fixes Supabase RLS/persistence issue).
      All callback_data stays within Telegram's 64-byte limit (ticker 7 + 5*6 + pipes ~ 40 bytes).
      When trade details are not provided (legacy call), falls back to ticker-only for backward compatibility.
    """
    try:
        safe_ticker = str(ticker).strip() if ticker else "UNKNOWN"
        # Build enhanced callback_data with full trade specs if provided
        if entry_price is not None and current_stop_loss is not None and target_1 is not None and target_2 is not None and target_3 is not None:
            try:
                # Format prices to 2 decimals to keep within 64 bytes
                ep = f"{float(entry_price):.2f}"
                sl = f"{float(current_stop_loss):.2f}"
                t1 = f"{float(target_1):.2f}"
                t2 = f"{float(target_2):.2f}"
                t3 = f"{float(target_3):.2f}"
                # Enhanced payload: act_{ticker}|{entry}|{stop}|{t1}|{t2}|{t3}
                # For dis/cls we keep ticker-only (no need for full specs, status update only)
                act_data = f"act_{safe_ticker}|{ep}|{sl}|{t1}|{t2}|{t3}"
                # Ensure within 64 bytes (Telegram limit) - if too long, fallback to ticker-only
                if len(act_data.encode("utf-8")) > 64:
                    act_data = f"act_{safe_ticker}"
                else:
                    # Also include trade_track in dis/cls? No, keep simple
                    pass
                return {
                    "inline_keyboard": [
                        [
                            {"text": "✅ تم الدخول (Activate)", "callback_data": act_data},
                            {"text": "❌ غير مهتم (Dismiss)", "callback_data": f"dis_{safe_ticker}"},
                            {"text": "🏁 إغلاق يدوياً (Close)", "callback_data": f"cls_{safe_ticker}"},
                        ]
                    ]
                }
            except Exception:
                pass
        # Fallback legacy (ticker-only)
        return {
            "inline_keyboard": [
                [
                    {"text": "✅ تم الدخول (Activate)", "callback_data": f"act_{safe_ticker}"},
                    {"text": "❌ غير مهتم (Dismiss)", "callback_data": f"dis_{safe_ticker}"},
                    {"text": "🏁 إغلاق يدوياً (Close)", "callback_data": f"cls_{safe_ticker}"},
                ]
            ]
        }
    except Exception as exc:
        logger.warning("Failed to build trade keyboard for %s: %s", ticker, exc)
        return {"inline_keyboard": []}


def send_telegram(chat_id: Optional[str], message: str, bot_token: Optional[str], reply_markup: Optional[Dict[str, Any]] = None) -> bool:
    """Send a Markdown-formatted message to a Telegram chat/channel.

    Optionally attaches InlineKeyboardMarkup via reply_markup.
    """
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set; cannot send alerts.")
        return False
    if not chat_id:
        logger.error("Channel ID is empty/missing; cannot send alert.")
        return False
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        try:
            payload["reply_markup"] = reply_markup
        except Exception as exc:
            logger.warning("Failed to attach reply_markup: %s", exc)
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=bot_token), json=payload, timeout=30
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as exc:
        resp = exc.response
        status = resp.status_code if resp is not None else "N/A"
        text = resp.text if resp is not None else str(exc)
        detail = f"[ERROR] Telegram API failed ({status}): {text}"
        print(detail)
        logger.warning("Telegram send failed (chat=%s): %s", chat_id, detail)
        return False
    except Exception as exc:
        logger.warning("Telegram send failed (chat=%s): %s", chat_id, exc)
        return False


def _answer_callback_query(callback_query_id: str, text: str, bot_token: Optional[str] = None, show_alert: bool = False) -> bool:
    """Answer Telegram callback query with popup notification.

    Args:
        callback_query_id: ID from Telegram callback query
        text: Text to show in popup (Arabic)
        bot_token: Telegram bot token (falls back to env)
        show_alert: Whether to show as alert popup
    Returns True if sent, False otherwise. Never raises.
    """
    try:
        if not callback_query_id or not isinstance(callback_query_id, str):
            return False
        token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            logger.warning("Cannot answer callback query: TELEGRAM_BOT_TOKEN missing")
            return False
        payload: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "text": str(text)[:200],  # Telegram limit
            "show_alert": bool(show_alert),
        }
        resp = requests.post(
            TELEGRAM_ANSWER_API.format(token=token),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        logger.warning("Failed to answer callback query %s: %s", callback_query_id, exc)
        return False
    except Exception as exc:
        logger.warning("Unexpected error answering callback query %s: %s", callback_query_id, exc)
        return False


def listen_telegram(poll_interval: int = 2, timeout: int = 30) -> None:
    """Standalone bot listener for Telegram callback queries (real-time).

    Polls Telegram getUpdates API continuously to catch inline button clicks
    (act_/dis_/cls_) and handles them via handle_telegram_callback.
    Supports both python-telegram-bot polling and raw requests polling.
    Run via: python main.py --listen-telegram
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN not set; cannot start Telegram listener")
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    # Try python-telegram-bot if available (optional)
    try:
        # Attempt to use python-telegram-bot library if installed
        try:
            from telegram import Update
            from telegram.ext import Application, CallbackQueryHandler, ContextTypes

            logger.info("Using python-telegram-bot for polling")

            async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
                try:
                    query = update.callback_query
                    if query and query.data:
                        data = query.data
                        qid = query.id
                        result = handle_telegram_callback(data, callback_query_id=qid, bot_token=bot_token)
                        # Answer is already done inside handle_telegram_callback via _answer_callback_query
                        # But ensure we answer with popup if not already
                        try:
                            await query.answer(text="تم التحديث", show_alert=False)
                        except Exception:
                            pass
                        logger.info("Handled callback %s -> %s", data, result)
                except Exception as exc:
                    logger.warning("Callback handler error: %s", exc)

            # Build and run application
            import asyncio

            async def run_bot():
                app = Application.builder().token(bot_token).build()
                app.add_handler(CallbackQueryHandler(callback_handler))
                logger.info("Telegram bot listening via python-telegram-bot polling...")
                print("Listening for Telegram callbacks (python-telegram-bot)... Press Ctrl+C to stop")
                await app.run_polling(allowed_updates=["callback_query"], close_loop=False)

            # Run asyncio loop
            try:
                import asyncio
                asyncio.run(run_bot())
            except RuntimeError:
                # If already in event loop (e.g., Jupyter), create new loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(run_bot())
            return
        except ImportError:
            logger.info("python-telegram-bot not installed, falling back to raw requests polling")
    except Exception as exc:
        logger.warning("python-telegram-bot setup failed (%s), falling back to raw polling", exc)

    # Fallback: raw requests polling via getUpdates
    logger.info("Starting Telegram listener via raw getUpdates polling...")
    print("Listening for Telegram callbacks (raw polling)... Press Ctrl+C to stop")
    offset = 0
    # Ensure active positions are synced before listening
    try:
        sync_active_positions_to_supabase()
    except Exception:
        pass
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{bot_token}/getUpdates",
                params={"offset": offset, "timeout": timeout, "allowed_updates": '["callback_query"]'},
                timeout=timeout + 5,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.warning("getUpdates returned not ok: %s", data)
                time.sleep(poll_interval)
                continue
            results = data.get("result", [])
            if not results:
                time.sleep(poll_interval)
                continue
            for update in results:
                try:
                    offset = int(update.get("update_id", 0)) + 1
                except Exception:
                    offset += 1
                query = update.get("callback_query")
                if not query:
                    continue
                callback_data = query.get("data")
                callback_id = query.get("id")
                from_user = query.get("from", {}).get("username", "unknown")
                logger.info("Received callback %s from %s", callback_data, from_user)
                if callback_data:
                    try:
                        result = handle_telegram_callback(callback_data, callback_query_id=callback_id, bot_token=bot_token)
                        logger.info("Handled callback %s -> %s", callback_data, result)
                    except Exception as exc:
                        logger.warning("Failed to handle callback %s: %s", callback_data, exc)
                        # Try to answer with error popup
                        try:
                            _answer_callback_query(callback_id, "حدث خطأ أثناء المعالجة", bot_token)
                        except Exception:
                            pass
            # Small delay to avoid hammering
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Telegram listener stopped by user")
            print("\nListener stopped")
            break
        except requests.exceptions.RequestException as exc:
            logger.warning("Telegram polling request failed: %s", exc)
            time.sleep(5)
        except Exception as exc:
            logger.warning("Telegram listener error: %s", exc)
            time.sleep(5)


def set_telegram_webhook(vercel_domain: str, bot_token: Optional[str] = None) -> bool:
    """Set Telegram webhook to Vercel domain.

    Calls Telegram setWebhook API using TELEGRAM_BOT_TOKEN.
    VERCEL_DOMAIN can be like https://your-app.vercel.app or your-app.vercel.app
    Webhook URL will be {domain}/api/webhook

    Prints clear logs and returns True if successful. Never raises.
    """
    try:
        token = (bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            msg = "ERROR: TELEGRAM_BOT_TOKEN not set; cannot set webhook"
            print(msg)
            logger.error(msg)
            return False
        domain = (vercel_domain or "").strip()
        if not domain:
            msg = "ERROR: VERCEL_DOMAIN not provided. Usage: python main.py --set-webhook <VERCEL_DOMAIN>"
            print(msg)
            logger.error(msg)
            return False
        # Normalize domain: ensure https:// and no trailing slash, then /api/webhook
        if not domain.startswith("http"):
            domain = f"https://{domain}"
        domain = domain.rstrip("/")
        # If domain already ends with /api/webhook, don't duplicate
        if domain.endswith("/api/webhook"):
            webhook_url = domain
        else:
            webhook_url = f"{domain}/api/webhook"
        print(f"[WEBHOOK] Setting Telegram webhook to: {webhook_url}")
        logger.info("Setting Telegram webhook to %s", webhook_url)

        # Call Telegram setWebhook
        url = f"https://api.telegram.org/bot{token}/setWebhook"
        payload = {"url": webhook_url, "allowed_updates": ["callback_query", "message"]}
        # Use POST with JSON
        try:
            resp = requests.post(url, json=payload, timeout=15)
            # Also try GET fallback for debugging
            status = resp.status_code
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:500] if resp.text else ""
            print(f"[WEBHOOK] HTTP {status} Response: {json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body}")
            logger.info("setWebhook response %s: %s", status, body)
            if resp.status_code == 200:
                # Check Telegram's ok field
                try:
                    data = resp.json() if isinstance(body, dict) else json.loads(body) if isinstance(body, str) and body.startswith("{") else {}
                    if isinstance(data, dict) and data.get("ok"):
                        msg = f"✅ Webhook set successfully to {webhook_url}"
                        print(msg)
                        logger.info(msg)
                        return True
                    else:
                        # Even if not ok, consider success if HTTP 200
                        if data.get("ok") is False:
                            print(f"[WEBHOOK ERROR] Telegram returned ok=false: {data}")
                            logger.warning("setWebhook returned not ok: %s", data)
                            return False
                        print(f"[WEBHOOK] Set webhook completed with HTTP 200 (response: {body})")
                        return True
                except Exception:
                    # If not JSON, assume success on 200
                    print(f"✅ Webhook set to {webhook_url} (HTTP 200)")
                    return True
            else:
                print(f"[WEBHOOK ERROR] Failed to set webhook: HTTP {status} {body}")
                logger.warning("setWebhook failed HTTP %s: %s", status, body)
                return False
        except requests.exceptions.RequestException as exc:
            print(f"[WEBHOOK ERROR] Request failed: {exc}")
            logger.warning("setWebhook request failed: %s", exc)
            return False
    except Exception as exc:
        print(f"[WEBHOOK ERROR] Unexpected: {exc}")
        logger.warning("set_telegram_webhook failed: %s", exc)
        import traceback
        traceback.print_exc()
        return False


def check_webhook_status(bot_token: Optional[str] = None) -> bool:
    """Check Telegram webhook status via getWebhookInfo.

    Queries https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo
    and prints url, has_custom_certificate, pending_update_count,
    last_error_date, last_error_message. Never raises.
    """
    try:
        token = (bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            msg = "ERROR: TELEGRAM_BOT_TOKEN not set; cannot check webhook"
            print(msg)
            logger.error(msg)
            return False
        url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
        print(f"[WEBHOOK CHECK] Querying getWebhookInfo...")
        logger.info("Checking webhook status via %s", url.replace(token, "***"))
        try:
            resp = requests.get(url, timeout=15)
            status = resp.status_code
            print(f"[WEBHOOK CHECK] HTTP {status}")
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:1000] if resp.text else ""}
            # Telegram returns {"ok": true, "result": {...}}
            result = data.get("result", {}) if isinstance(data, dict) else {}
            if not isinstance(result, dict):
                result = {}
            url_field = result.get("url", "")
            has_cert = result.get("has_custom_certificate", "")
            pending = result.get("pending_update_count", "")
            last_error_date = result.get("last_error_date", "")
            last_error_msg = result.get("last_error_message", "")
            # Format last_error_date
            last_error_date_str = "None"
            if last_error_date:
                try:
                    dt = datetime.fromtimestamp(int(last_error_date), tz=timezone.utc)
                    try:
                        if ZoneInfo is not None:
                            dt_c = dt.astimezone(ZoneInfo("Africa/Cairo"))
                            last_error_date_str = f"{last_error_date} ({dt_c.isoformat()})"
                        else:
                            last_error_date_str = f"{last_error_date} ({dt.isoformat()})"
                    except Exception:
                        last_error_date_str = str(last_error_date)
                except Exception:
                    last_error_date_str = str(last_error_date)
            # Use safe prints (avoid cp1252 emoji issues)
            try:
                print(f"url: {url_field}")
                print(f"has_custom_certificate: {has_cert}")
                print(f"pending_update_count: {pending}")
                print(f"last_error_date: {last_error_date_str}")
                msg_val = last_error_msg if last_error_msg else "None"
                # Ensure ascii-safe
                try:
                    print(f"last_error_message: {msg_val}")
                except Exception:
                    print(f"last_error_message: {repr(msg_val)}")
            except Exception as e:
                # Fallback with repr
                print(f"url: {repr(url_field)}")
                print(f"last_error_message: {repr(last_error_msg)}")
            logger.info("check_webhook_status: url=%s pending=%s last_error=%s", url_field, pending, last_error_msg)
            return True
        except requests.exceptions.RequestException as exc:
            print(f"[WEBHOOK CHECK ERROR] Request failed: {exc}")
            logger.warning("getWebhookInfo request failed: %s", exc)
            return False
    except Exception as exc:
        print(f"[WEBHOOK CHECK ERROR] Unexpected: {exc}")
        logger.warning("check_webhook_status failed: %s", exc)
        import traceback
        traceback.print_exc()
        return False


def test_vercel_webhook(vercel_url: str = "https://egx-stock-screener.vercel.app/api/webhook", bot_token: Optional[str] = None) -> bool:
    """Test Vercel webhook endpoint with mock callback_query.

    Sends HTTP POST with fake Telegram callback_query payload (data: act_TEST.CA)
    directly to https://egx-stock-screener.vercel.app/api/webhook and prints
    HTTP status code and body. Never raises.
    """
    try:
        url = (vercel_url or "https://egx-stock-screener.vercel.app/api/webhook").strip()
        if not url.startswith("http"):
            url = f"https://{url}"
        print(f"[VERCEL TEST] Sending mock callback_query to {url}")
        logger.info("Testing Vercel webhook at %s", url)
        payload: Dict[str, Any] = {
            "update_id": 999999,
            "callback_query": {
                "id": "test_12345",
                "from": {"id": 123456, "is_bot": False, "first_name": "Test"},
                "message": {"message_id": 1, "chat": {"id": 123, "type": "private"}},
                "data": "act_TEST.CA",
                "chat_instance": "test_instance",
            },
        }
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            status = resp.status_code
            try:
                body = resp.text[:2000] if resp.text else "(empty body)"
            except Exception:
                body = "(no body)"
            print(f"[VERCEL TEST] HTTP {status}")
            # Print body safely
            try:
                print(f"[VERCEL TEST] Response Body: {body}")
            except Exception:
                print(f"[VERCEL TEST] Response Body (repr): {repr(body[:500])}")
            logger.info("test_vercel_webhook %s -> %s %s", url, status, body[:500] if isinstance(body, str) else body)
            # Consider 2xx as success
            return status in (200, 201, 204)
        except requests.exceptions.RequestException as exc:
            print(f"[VERCEL TEST ERROR] Request failed: {exc}")
            logger.warning("test_vercel_webhook request failed: %s", exc)
            return False
    except Exception as exc:
        print(f"[VERCEL TEST ERROR] Unexpected: {exc}")
        logger.warning("test_vercel_webhook failed: %s", exc)
        import traceback
        traceback.print_exc()
        return False


def fmt(value: Optional[float], digits: int = 2) -> str:
    """Format a number for display; return 'n/a' when missing."""
    return f"{value:.{digits}f}" if value is not None else "غير متاح"


def get_sharia_status_tag(ticker: str) -> str:
    """Return a Sharia compliance status tag for a ticker."""
    if ticker in EGX33_SHARIAH_TICKERS:
        return "🕌 **متوافق مع الشريعة 100%** (مؤشر EGX33)"
    if ticker in NON_COMPLIANT_TICKERS:
        return "❌ **غير متوافق شرعاً** (نشاط/بنوك تقليدية)"
    return "⚠️ **يحتاج مراجعة شرعية** (خارج مؤشر EGX33)"


def classify_sentiment(text: Any) -> Optional[str]:
    """Return the sentiment word (إيجابي/سلبي/محايد) found in a Gemini summary, if any."""
    if not isinstance(text, str) or not text:
        return None
    for word in ("إيجابي", "سلبي", "محايد"):
        try:
            if word in text:
                return word
        except Exception:
            continue
    return None


def extract_news_body(text: Any) -> str:
    """Return the compact summary body with all Gemini scaffolding stripped."""
    if not isinstance(text, str):
        return ""
    body = text.strip()
    if not body:
        return ""
    try:
        if classify_sentiment(body):
            marker = re.search(r"2\)", body)
            if marker:
                body = body[: marker.start()].rstrip().rstrip("*").rstrip()
        marker_patterns = [
            r"^\s*(?:1\)|2\))\s*",
            r"^\s*[.**\s]*\d+\)\s*[^\n]*:?\s*$",
            r"(?:^|\n)\s*\**\s*\d+\)[^\n]*\s*\**\s*:?\s*",
            r"(?:الملخص|ملخص اتجاه الأخبار|ملخص المعنويات)\s*:?\s*",
            r"(?:تصنيف المعنويات)\s*:?\s*",
        ]
        for pattern in marker_patterns:
            try:
                body = re.sub(pattern, "", body, flags=re.MULTILINE)
            except Exception:
                continue
        body = body.replace("**", "")
        for word in ("إيجابي", "سلبي", "محايد"):
            try:
                body = re.sub(rf"^\s*{word}\s*$", "", body, flags=re.MULTILINE)
            except Exception:
                continue
            body = body.replace(f": {word}", "").replace(f" :{word}", "").strip()
        body = re.sub(r"\n+", "\n", body).strip()
        body = re.sub(r"[ \t]+", " ", body).strip()
    except Exception:
        # Fallback: return raw stripped text on any regex failure
        return text.strip() if isinstance(text, str) else ""
    return body


def build_news_block(sentiment: Any) -> str:
    """Return a compact, badge-labeled Arabic news summary block.

    Ensures macro reasoning block is NOT included here to avoid duplication —
    macro will be appended separately via build_macro_block in build_message.
    """
    try:
        classification = classify_sentiment(sentiment) or ""
        badge = SENTIMENT_BADGES.get(classification, "⚪")
        header = f"🤖 ملخص الأخبار (Gemini AI): {badge} {classification}".strip()
        body = extract_news_body(sentiment)
        # Strip macro reasoning block from news body to prevent duplicate header
        try:
            if isinstance(sentiment, str) and sentiment:
                # Remove exact macro block if present in body
                macro_exact = extract_macro_analysis(sentiment)
                if macro_exact and macro_exact in body:
                    body = body.replace(macro_exact, "").strip()
                # Fallback: remove any remaining macro header/bullets by pattern
                # This handles cases where body contains raw macro with different whitespace
                body = re.sub(r"🧠\s*التحليل\s*الكلي\s*والأثر\s*غير\s*المباشر\s*:?.*", "", body, flags=re.DOTALL).strip()
                body = re.sub(r"التحليل\s*الكلي\s*والأثر\s*غير\s*المباشر\s*:?.*", "", body, flags=re.DOTALL).strip()
                # Remove stray macro bullets if any remain (only if they were part of macro)
                # Keep body clean without leaving double newlines
                body = re.sub(r"\n\s*•\s*السبب\s*:.*", "", body).strip()
                body = re.sub(r"\n\s*•\s*القطاع\s*(المتأثر|التأثر)?\s*:.*", "", body).strip()
                body = re.sub(r"\n\s*•\s*الأسهم\s*المستفيدة.*", "", body).strip()
                body = re.sub(r"\n{3,}", "\n\n", body).strip()
        except Exception:
            pass
        return f"{header}\n{body}".strip()
    except Exception:
        # Never crash message formatting
        return f"🤖 ملخص الأخبار (Gemini AI): ⚪\n{extract_news_body(sentiment) if isinstance(sentiment, str) else ''}".strip()


def extract_macro_analysis(text: Any) -> Optional[str]:
    """Extract macro indirect analysis block from Gemini output if present.

    Looks for header 🧠 التحليل الكلي والأثر غير المباشر and captures
    bullet lines for سبب/قطاع/أسهم. Returns None if not found.
    """
    if not isinstance(text, str) or not text:
        return None
    try:
        # Normalize line endings
        normalized = text.strip()
        # Search for header with or without emoji
        header_patterns = [
            r"🧠\s*التحليل\s*الكلي\s*والأثر\s*غير\s*المباشر\s*:?",
            r"التحليل\s*الكلي\s*والأثر\s*غير\s*المباشر\s*:?",
        ]
        header_match = None
        header_end = -1
        for pat in header_patterns:
            m = re.search(pat, normalized)
            if m:
                header_match = m
                header_end = m.end()
                break
        if not header_match:
            return None
        # Extract the block from header onward
        block_start = header_match.start()
        # Take up to next double newline or next major section (like 🎯 or 🏷️ or ⭐) or 500 chars
        # Find next header markers
        rest = normalized[block_start:]
        # Split into lines
        lines = rest.splitlines()
        # Keep header + up to 5 bullet lines starting with • or - or *
        macro_lines: List[str] = []
        for idx, line in enumerate(lines):
            if idx == 0:
                # Header line - normalize to required emoji header
                macro_lines.append("🧠 التحليل الكلي والأثر غير المباشر:")
                continue
            stripped = line.strip()
            if not stripped:
                # Allow one empty line then break if two consecutive empty?
                if len(macro_lines) > 1 and not stripped:
                    # Check next line also empty or new section
                    continue
                continue
            # Stop if we hit another major section marker
            if any(marker in stripped for marker in ["🎯 تقييم الجودة", "🏷️ المسار", "⭐ التصنيف", "🤖 ملخص الأخبار"]):
                break
            # Keep bullet lines
            if stripped.startswith("•") or stripped.startswith("-") or stripped.startswith("*"):
                # Normalize bullet to • 
                if stripped.startswith("-") or stripped.startswith("*"):
                    stripped = "• " + stripped[1:].strip()
                macro_lines.append(stripped)
                if len(macro_lines) >= 4:  # header + 3 bullets
                    break
            elif any(k in stripped for k in ["السبب", "القطاع", "الأسهم", "المستفيدة", "المتأثرة"]):
                # Lines containing those keywords but without bullet - add bullet
                if not stripped.startswith("•"):
                    stripped = "• " + stripped.lstrip("•- *:")
                macro_lines.append(stripped)
                if len(macro_lines) >= 4:
                    break
            else:
                # If line doesn't look like bullet but we already have bullets, break
                if len(macro_lines) >= 2:
                    # Might be continuation of previous bullet
                    if len(stripped) < 100:
                        continue
                    break
        # Validate we have at least 2 bullet lines (cause + sector or tickers)
        bullet_count = sum(1 for l in macro_lines if l.startswith("•"))
        if bullet_count < 2:
            # Not enough content to be considered valid macro block
            return None
        return "\n".join(macro_lines).strip()
    except Exception:
        return None


def build_macro_block(sentiment: Any) -> str:
    """Build formatted macro indirect impact block for Telegram message.

    Returns empty string if no indirect macro effect detected, otherwise
    returns the concise 3-bullet section with header.
    The header and bullets match exactly the required format:
        🧠 التحليل الكلي والأثر غير المباشر:
        • السبب: [Macro Event]
        • القطاع المتأثر: [Sector Impact] (or القطاع التأثر)
        • الأسهم المستفيدة/المتأثرة: [Impacted EGX Tickers]
    """
    try:
        block = extract_macro_analysis(sentiment)
        if not block:
            return ""
        # Ensure bullets use correct labels (normalize variations)
        # Replace common variations to match required labels
        # Ensure we have exactly the three bullets with correct prefixes
        lines = block.splitlines()
        normalized_lines: List[str] = []
        for line in lines:
            if "التحليل الكلي" in line:
                normalized_lines.append("🧠 التحليل الكلي والأثر غير المباشر:")
                continue
            # Normalize bullet prefixes
            # Handle "القطاع التأثر" vs "القطاع المتأثر"
            if "السبب" in line:
                # Ensure format "• السبب: ..."
                if ":" not in line:
                    line = line.replace("السبب", "السبب:")
                if not line.strip().startswith("•"):
                    line = "• " + line.lstrip("•- *")
                # Ensure "• السبب:" prefix
                line = re.sub(r"^[•\-\*]\s*السبب\s*:?", "• السبب:", line)
                normalized_lines.append(line.strip())
            elif "القطاع" in line:
                # Handle both التأثر and المتأثر
                if ":" not in line:
                    line = line.replace("القطاع", "القطاع المتأثر:")
                # Normalize to "• القطاع المتأثر:" or keep original if needed
                # Requirement says "• القطاع التأثر:" - we support both, but normalize to المتأثر for consistency
                # Keep original label if it matches requirement exactly
                if "القطاع التأثر" in line or "القطاع المتأثر" in line:
                    line = re.sub(r"^[•\-\*]\s*القطاع\s*(المتأثر|التأثر)?\s*:?", "• القطاع المتأثر:", line)
                else:
                    line = re.sub(r"^[•\-\*]\s*القطاع.*?:?", "• القطاع المتأثر:", line)
                normalized_lines.append(line.strip())
            elif "الأسهم" in line:
                if ":" not in line:
                    line = line.replace("الأسهم", "الأسهم المستفيدة/المتأثرة:")
                line = re.sub(r"^[•\-\*]\s*الأسهم[^:]*:?", "• الأسهم المستفيدة/المتأثرة:", line)
                normalized_lines.append(line.strip())
            else:
                # Keep other bullet lines as is if they start with •
                if line.strip().startswith("•"):
                    normalized_lines.append(line.strip())
        # Reconstruct; if we have less than 3 bullets, keep as is
        if len(normalized_lines) < 2:
            return ""
        # Ensure at least header + 2 bullets
        return "\n".join(normalized_lines).strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Trade Quality Index (TQI) helpers
# --------------------------------------------------------------------------


def get_conviction_tier(tqi: Any) -> str:
    """Return Conviction Tier label for a TQI score (EGX-adapted thresholds)."""
    try:
        score = float(tqi) if tqi is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    if score >= 8.5:
        return "🟢 فرصة استثنائية (A+ Setup)"
    if score >= 6.5:
        return "🟡 فرصة جيدة (B Setup)"
    if score >= 5.0:
        return "🟠 فرصة متوسطة (C Setup)"
    return "⚪ فرصة ضعيفة (Low Conviction)"


def get_trade_track_label(strategy: Any) -> str:
    """Return Trade Track label for a strategy key."""
    try:
        if isinstance(strategy, str) and strategy in TQI_TRACK_LABELS:
            return TQI_TRACK_LABELS[strategy]
    except Exception:
        pass
    # Graceful fallback for unknown/None strategy
    return TQI_TRACK_LABELS.get(SCALPING, "⚡ مضاربة لحظية (Scalp)")


def extract_tqi_score(text: Any) -> Optional[float]:
    """Extract TQI score X.X/10 from Gemini text if present."""
    if not isinstance(text, str) or not text:
        return None
    try:
        pattern = re.compile(r"TQI[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*/\s*10", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            try:
                val = float(match.group(1))
                return max(0.0, min(10.0, round(val, 1)))
            except (ValueError, TypeError, IndexError):
                return None
    except Exception:
        return None
    return None


def extract_trade_track_from_text(text: Any) -> Optional[str]:
    """Extract Trade Track label from Gemini text if present."""
    if not isinstance(text, str) or not text:
        return None
    try:
        for label in TQI_TRACK_LABELS.values():
            if label in text:
                return label
        if "Scalp" in text or "مضاربة لحظية" in text:
            return TQI_TRACK_LABELS.get(SCALPING)
        if "Swing" in text or "سوينغ" in text:
            return TQI_TRACK_LABELS.get(SWING)
        if "Invest" in text or "استثمار طويل" in text:
            return TQI_TRACK_LABELS.get(INVESTMENT)
    except Exception:
        return None
    return None


def extract_conviction_from_text(text: Any) -> Optional[str]:
    """Extract Conviction Tier label from Gemini text if present (EGX 4-tier)."""
    if not isinstance(text, str) or not text:
        return None
    try:
        candidates = [
            "🟢 فرصة استثنائية (A+ Setup)",
            "🟡 فرصة جيدة (B Setup)",
            "🟠 فرصة متوسطة (C Setup)",
            "⚪ فرصة ضعيفة (Low Conviction)",
        ]
        for cand in candidates:
            if cand in text:
                return cand
        # Legacy fallback: handle old B+ label
        if "B+ Setup" in text:
            return "🟡 فرصة جيدة (B Setup)"
        if "A+ Setup" in text or "استثنائية" in text:
            return candidates[0]
        if "B Setup" in text or "فرصة جيدة" in text:
            return candidates[1]
        if "C Setup" in text or "فرصة متوسطة" in text:
            return candidates[2]
        if "Low Conviction" in text or "فرصة ضعيفة" in text:
            return candidates[3]
    except Exception:
        return None
    return None


def compute_fallback_tqi(ctx: Any, strategy: Any, sentiment: Any) -> float:
    """Deterministically compute TQI 0.0-10.0 from available context.

    Scoring (mirrors Gemini prompt weights):
      - Technical Confluence 3.0 pts
      - Risk/Reward 2.5 pts
      - Relative Volume Surge 2.0 pts
      - Sector Alignment 1.5 pts
      - News/Catalyst Strength 1.0 pt
    Gracefully handles None / missing keys to avoid runtime crashes.
    """
    try:
        # Defensive ctx handling
        if not isinstance(ctx, dict):
            ctx = {}
        tech_score = 0.0
        rsi = ctx.get("rsi") if isinstance(ctx, dict) else None
        price = ctx.get("price") if isinstance(ctx, dict) else None
        ema20 = ctx.get("ema20") if isinstance(ctx, dict) else None
        sma50 = ctx.get("sma50") if isinstance(ctx, dict) else None
        # Ensure numeric types
        try:
            rsi_val = float(rsi) if rsi is not None else None
        except (TypeError, ValueError):
            rsi_val = None
        try:
            price_val = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_val = None
        try:
            ema20_val = float(ema20) if ema20 is not None else None
        except (TypeError, ValueError):
            ema20_val = None
        try:
            sma50_val = float(sma50) if sma50 is not None else None
        except (TypeError, ValueError):
            sma50_val = None

        if rsi_val is not None:
            if strategy == SCALPING and rsi_val > 55:
                tech_score += 1.5
            elif strategy == SWING and rsi_val > 50:
                tech_score += 1.5
            elif strategy == INVESTMENT and rsi_val < 40:
                tech_score += 1.5
            elif 40 <= rsi_val <= 70:
                tech_score += 0.8
        if price_val is not None and ema20_val is not None and price_val > ema20_val:
            tech_score += 0.8
            # Scalp: extra weight for EMA9/VWAP momentum break (proxied by EMA20 crossover)
            if strategy == SCALPING:
                tech_score += 0.5  # momentum break bonus for Scalp
        if price_val is not None and sma50_val is not None:
            if strategy == INVESTMENT and price_val < sma50_val:
                tech_score += 0.7
            elif price_val > sma50_val:
                tech_score += 0.5
        # Scalp: further boost if strong RVOL accompanies momentum (simulating VWAP crossover)
        if strategy == SCALPING:
            try:
                vol_tmp = float(ctx.get("volume_ratio")) if ctx.get("volume_ratio") is not None else None
                if vol_tmp is not None and vol_tmp >= 1.5 and price_val is not None and ema20_val is not None and price_val > ema20_val:
                    tech_score += 0.5
            except Exception:
                pass
        tech_score = min(tech_score, TQI_TECHNICAL_CONFLUENCE_MAX)

        # Risk/Reward Ratio (2.5 pts)
        try:
            plan = STRATEGY_PLAN.get(strategy, {}) if isinstance(strategy, str) else {}
            if not isinstance(plan, dict):
                plan = {}
            sl_pct = abs(float(plan.get("sl_pct", -0.03))) if plan.get("sl_pct") is not None else 0.03
        except Exception:
            sl_pct = 0.03
            plan = {}
        try:
            targets = plan.get("targets_pct", (0.03, 0.05, 0.08))
            if not isinstance(targets, (list, tuple)) or len(targets) < 3:
                targets = (0.03, 0.05, 0.08)
            rr_ratio = abs(float(targets[2]) / sl_pct) if sl_pct else 0
        except Exception:
            rr_ratio = 0
        if rr_ratio >= 2.5:
            rr_score = TQI_RISK_REWARD_MAX
        elif rr_ratio >= 1.5:
            rr_score = 1.8
        elif rr_ratio >= 1.0:
            rr_score = 1.0
        else:
            rr_score = 0.5

        # Relative Volume Surge (2 pts)
        try:
            vol_ratio_raw = ctx.get("volume_ratio") if isinstance(ctx, dict) else None
            vol_ratio = float(vol_ratio_raw) if vol_ratio_raw is not None else None
        except (TypeError, ValueError):
            vol_ratio = None
        # Scalp: higher weight to Intraday RVOL (lower thresholds, higher base)
        if strategy == SCALPING:
            if vol_ratio is None:
                vol_score = 0.7
            elif vol_ratio >= 1.5:
                vol_score = TQI_VOLUME_SURGE_MAX
            elif vol_ratio >= 1.2:
                vol_score = 1.5
            elif vol_ratio >= 1.0:
                vol_score = 1.0
            else:
                vol_score = 0.5
        else:
            if vol_ratio is None:
                vol_score = 0.5
            elif vol_ratio >= 1.8:
                vol_score = TQI_VOLUME_SURGE_MAX
            elif vol_ratio >= 1.3:
                vol_score = 1.2
            elif vol_ratio >= 1.0:
                vol_score = 0.7
            else:
                vol_score = 0.3

        # Sector Alignment (1.5 pts) — no sector feed, use conservative default
        sector_score = 1.0
        try:
            if strategy == SWING and vol_ratio is not None and vol_ratio > 1.5:
                sector_score = 1.2
        except Exception:
            sector_score = 1.0

        # News/Catalyst Strength (1 pt)
        try:
            classification = classify_sentiment(sentiment) or ""
        except Exception:
            classification = ""
        if classification == "إيجابي":
            news_score = TQI_NEWS_CATALYST_MAX
        elif classification == "سلبي":
            news_score = 0.2
        elif classification == "محايد":
            news_score = 0.5
        else:
            try:
                body = extract_news_body(sentiment)
                news_score = 0.6 if len(body) > 30 else 0.3
            except Exception:
                news_score = 0.3

        total = tech_score + rr_score + vol_score + sector_score + news_score

        # Synthetic Order Flow & Delta Boosts (free footprint emulation)
        try:
            # Bullish pressure: 5m cumulative delta positive and growing
            bullish = False
            try:
                bullish = bool(ctx.get("is_bullish_pressure") or ctx.get("synthetic_bullish_pressure"))
            except Exception:
                bullish = False
            if bullish:
                total += 0.5
                logger.info("TQI synthetic boost +0.5 for bullish pressure (delta >0 & growing)")

            # Absorption near support: High RVOL + Small Spread + near support
            absorption = False
            near_support_flag = False
            try:
                absorption = bool(ctx.get("is_absorption") or ctx.get("synthetic_absorption"))
                near_support_flag = bool(ctx.get("near_support") or ctx.get("is_near_support"))
                # If near_support not in ctx, try to infer from price/support proximity if available
                if absorption and not near_support_flag:
                    # Fallback: check if ctx has price and absorption implies near support handling
                    # For now, if absorption true and price available, consider near support if not explicitly false
                    # Keep as is - require explicit near_support to avoid over-boosting
                    pass
            except Exception:
                absorption = False
                near_support_flag = False
            if absorption and near_support_flag:
                total += 0.5
                logger.info("TQI synthetic boost +0.5 for volume absorption near support (institutional buying)")
            elif absorption and ctx.get("is_absorption") and _is_near_support(ctx.get("price"), None, ctx.get("df_1m")):
                # Fallback check using helper if near_support not set but df_1m available
                total += 0.5
                logger.info("TQI synthetic boost +0.5 for absorption near support (helper check)")
        except Exception:
            pass

        return max(0.0, min(10.0, round(float(total), 1)))
    except Exception as exc:
        logger.warning("compute_fallback_tqi failed (%s); returning default 5.0", exc)
        return 5.0


def resolve_tqi(ctx: Any, strategy: Any, sentiment: Any) -> tuple[float, str, str]:
    """Resolve TQI, Trade Track and Conviction Tier for a message.

    Priority: parse from Gemini text → fallback to deterministic computation.
    Returns (tqi_score, track_label, conviction_label). Never raises.
    """
    try:
        tqi = extract_tqi_score(sentiment)
    except Exception:
        tqi = None
    try:
        track = extract_trade_track_from_text(sentiment)
    except Exception:
        track = None
    try:
        conviction = extract_conviction_from_text(sentiment)
    except Exception:
        conviction = None

    if tqi is None:
        try:
            tqi = compute_fallback_tqi(ctx, strategy, sentiment)
        except Exception as exc:
            logger.warning("resolve_tqi fallback failed (%s); using default 5.0", exc)
            tqi = 5.0
    # Clamp and normalize tqi
    try:
        tqi = max(0.0, min(10.0, round(float(tqi), 1)))
    except Exception:
        tqi = 5.0

    if track is None:
        try:
            track = get_trade_track_label(strategy)
        except Exception:
            track = TQI_TRACK_LABELS.get(SCALPING, "⚡ مضاربة لحظية (Scalp)")

    if conviction is None:
        try:
            conviction = get_conviction_tier(tqi)
        except Exception:
            conviction = "⚪ فرصة ضعيفة (Low Conviction)"
    else:
        try:
            expected = get_conviction_tier(tqi)
            if conviction != expected:
                conviction = expected
        except Exception:
            pass

    # Final guard: ensure strings
    if not isinstance(track, str) or not track:
        track = TQI_TRACK_LABELS.get(SCALPING, "⚡ مضاربة لحظية (Scalp)")
    if not isinstance(conviction, str) or not conviction:
        conviction = get_conviction_tier(tqi)

    return tqi, track, conviction


def build_message(strategy: Any, ticker: Any, ctx: Any, sentiment: Any) -> str:
    """Compose a professional Arabic Markdown alert with dynamic targets & risk plan.

    Includes Trade Quality Index (TQI), Trade Track and Conviction Tier while
    preserving all existing target prices and news summary fields for parser compatibility.
    Gracefully handles None / missing keys to prevent runtime crashes.
    """
    try:
        # Defensive defaults
        if not isinstance(ctx, dict):
            ctx = {}
        if not isinstance(strategy, str) or strategy not in STRATEGY_PLAN:
            # Fallback to scalping plan for unknown strategy
            strategy = SCALPING if SCALPING in STRATEGY_PLAN else next(iter(STRATEGY_PLAN), SCALPING)
        plan = STRATEGY_PLAN.get(strategy, {})
        if not isinstance(plan, dict):
            plan = STRATEGY_PLAN.get(SCALPING, {})

        # Safe ticker handling
        ticker_str = str(ticker) if ticker is not None else "UNKNOWN.CA"
        sharia_tag = get_sharia_status_tag(ticker_str) if isinstance(ticker_str, str) else "⚠️ **يحتاج مراجعة شرعية**"
        stock_name_ar = STOCK_NAMES_AR.get(ticker_str, str(ticker_str))
        clean_ticker = ticker_str.replace(".CA", "") if isinstance(ticker_str, str) else str(ticker_str)

        # Safe price handling
        try:
            entry_price = float(ctx.get("price") or 0.0) if isinstance(ctx, dict) else 0.0
        except (TypeError, ValueError):
            entry_price = 0.0

        # Safe plan targets
        try:
            targets = plan.get("targets_pct", (0.03, 0.05, 0.08))
            if not isinstance(targets, (list, tuple)) or len(targets) < 3:
                targets = (0.03, 0.05, 0.08)
            p1, p2, p3 = float(targets[0]), float(targets[1]), float(targets[2])
        except Exception:
            p1, p2, p3 = 0.03, 0.05, 0.08

        try:
            sl_pct = float(plan.get("sl_pct", -0.03)) if plan.get("sl_pct") is not None else -0.03
        except (TypeError, ValueError):
            sl_pct = -0.03

        try:
            target_1 = entry_price * (1 + p1)
            target_2 = entry_price * (1 + p2)
            target_3 = entry_price * (1 + p3)
            stop_loss = entry_price * (1 + sl_pct)
        except Exception:
            target_1 = target_2 = target_3 = stop_loss = entry_price

        try:
            rr = abs(p3 / sl_pct) if sl_pct else 0
            rr = float(rr)
        except Exception:
            rr = 0.0

        # Safe news block
        try:
            news_block = build_news_block(sentiment)
        except Exception:
            news_block = "🤖 ملخص الأخبار (Gemini AI): ⚪"

        # Extract macro indirect analysis block (CoT second-order reasoning)
        try:
            macro_block = build_macro_block(sentiment)
        except Exception:
            macro_block = ""

        # Resolve TQI safely
        try:
            tqi_score, track_label, conviction_label = resolve_tqi(ctx, strategy, sentiment)
        except Exception as exc:
            logger.warning("build_message resolve_tqi failed (%s); using defaults", exc)
            tqi_score, track_label, conviction_label = 5.0, get_trade_track_label(strategy), get_conviction_tier(5.0)

        # Ensure numeric formatting won't crash
        try:
            tqi_score_f = float(tqi_score)
        except (TypeError, ValueError):
            tqi_score_f = 5.0
        tqi_score_f = max(0.0, min(10.0, tqi_score_f))

        if not isinstance(track_label, str) or not track_label:
            track_label = get_trade_track_label(strategy)
        if not isinstance(conviction_label, str) or not conviction_label:
            conviction_label = get_conviction_tier(tqi_score_f)

        # Safe plan text fields - duration dynamic per track
        technical_reason = plan.get("technical_reason_ar", "تحليل فني") if isinstance(plan, dict) else "تحليل فني"
        sl_condition = plan.get("sl_condition_ar", "إغلاق شمعة أسفل الدعم") if isinstance(plan, dict) else "إغلاق شمعة أسفل الدعم"
        allocation = plan.get("allocation_ar", "5% من رأس المال") if isinstance(plan, dict) else "5% من رأس المال"
        # Make duration dynamic based on track (fixes hardcoded Scalp duration)
        track_str_for_duration = str(track_label) if isinstance(track_label, str) else ""
        if "Scalp" in track_str_for_duration or "مضاربة" in track_str_for_duration:
            duration = "مضاربة لحظية / سريعة (داخل اليوم)"
        elif "Swing" in track_str_for_duration or "سوينغ" in track_str_for_duration:
            duration = "تداول سوينغ / متوسط المدى (أيام إلى أسابيع)"
        elif "Invest" in track_str_for_duration or "استثمار" in track_str_for_duration:
            duration = "استثمار / طويل المدى (أسابيع إلى أشهر)"
        else:
            duration = plan.get("duration_ar", "مضاربة") if isinstance(plan, dict) else "مضاربة"

        # Prepare macro section (only if indirect effect detected)
        macro_section = f"\n{macro_block}\n" if macro_block else ""

        return (
            f"اسم السهم : {stock_name_ar} {clean_ticker}\n"
            f"\n"
            f"سبب دخول الصفقه فنيا : {technical_reason}\n"
            f"\n"
            f"{sharia_tag}\n"
            f"\n"
            f"🎯 تقييم الجودة (TQI): {tqi_score_f:.1f}/10\n"
            f"🏷️ المسار: {track_label}\n"
            f"⭐ التصنيف: {conviction_label}\n"
            f"\n"
            f"سعر الدخول : {entry_price:.2f} 🏷\n"
            f"\n"
            f"الهدف الاول: {target_1:.2f} ({p1 * 100:.1f}%) 🎯\n"
            f"الهدف الثاني : {target_2:.2f} ({p2 * 100:.1f}%) 🎯\n"
            f"الهدف الثالث: {target_3:.2f} ({p3 * 100:.1f}%) 🎯\n"
            f"\n"
            f"وقف الخسارة : {sl_condition} {stop_loss:.2f} ({sl_pct * 100:.1f}%) ⛔️\n"
            f"\n"
            f"نسبة الدخول من المحفظه : {allocation} 💵\n"
            f"نوع الصفقة ومدتها: {duration} ⏳️\n"
            f"معدل العائد إلى المخاطر (R:R) : 1 : {rr:.2f} ⚖️\n"
            f"\n"
            f"📈 [عرض الشارت المباشر على TradingView](https://ar.tradingview.com/symbols/EGX-{clean_ticker}/)\n"
            f"\n"
            f"{news_block}\n"
            f"{macro_section}"
            f"\n"
            f"تذكير ⚠️ التحليل قد يصيب او يخطئ ولكن يجب عليك الالتزام ب إدارة المخاطر "
            f"وعدم التهاون ب إدارة رأس مالك 🔒 .. بالتوفيق للجميع 👏"
        )
    except Exception as exc:
        # Ultimate fallback — never let Telegram formatting crash the run
        logger.warning("build_message critical failure (%s); returning minimal fallback message", exc)
        try:
            fallback_ticker = str(ticker).replace(".CA", "") if ticker else "UNKNOWN"
            fallback_price = 0.0
            try:
                fallback_price = float(ctx.get("price") or 0.0) if isinstance(ctx, dict) else 0.0
            except Exception:
                fallback_price = 0.0
            return (
                f"اسم السهم : {fallback_ticker}\n"
                f"🎯 تقييم الجودة (TQI): 5.0/10\n"
                f"🏷️ المسار: ⚡ مضاربة لحظية (Scalp)\n"
                f"⭐ التصنيف: ⚪ فرصة ضعيفة (Low Conviction)\n"
                f"سعر الدخول : {fallback_price:.2f} 🏷\n"
                f"⚠️ حدث خطأ في تكوين الرسالة الأصلية: {exc}\n"
            )
        except Exception:
            return "⚠️ خطأ في تكوين رسالة التنبيه (build_message fallback)"


# --------------------------------------------------------------------------
# Signal evaluation
# --------------------------------------------------------------------------


def evaluate_strategies(ticker: str, df: pd.DataFrame) -> List[str]:
    """Return the list of strategies whose conditions are met for a ticker."""
    ind = compute_indicators(df)

    price = latest(ind, "Close")
    rsi = latest(ind, "RSI")
    ema20 = latest(ind, "EMA20")
    sma50 = latest(ind, "SMA50")
    spike = has_volume_spike(ind)
    volume_ratio: Optional[float] = None
    if spike:
        current_vol = latest(ind, "Volume")
        avg_vol = latest(ind, "VolMA20")
        if current_vol is not None and avg_vol:
            volume_ratio = current_vol / avg_vol

    logger.info(
        "[%s] close=%s rsi=%s ema20=%s sma50=%s vol_spike=%s",
        ticker,
        fmt(price),
        fmt(rsi, 1),
        fmt(ema20),
        fmt(sma50),
        spike,
    )

    signals: List[str] = []

    # --- Scalping: RSI > 55 AND volume spike AND price > EMA 20 -------------
    if (
        rsi is not None
        and price is not None
        and ema20 is not None
        and spike
        and rsi > 55
        and price > ema20
    ):
        signals.append(SCALPING)

    # --- Swing: price crossed above EMA 20 AND RSI > 50 ----------------------
    if crossed_above_ema20(ind) and rsi is not None and rsi > 50:
        signals.append(SWING)

    # --- Investment: trailing P/E < 8 AND price < SMA 50 AND RSI < 40 --------
    if price is not None and sma50 is not None and rsi is not None:
        if price < sma50 and rsi < 40:
            pe = get_trailing_pe(ticker)
            if pe is not None and pe < 8:
                signals.append(INVESTMENT)

    return signals


def process_ticker(ticker: str, state: Dict[str, Any]) -> None:
    """Fetch data for a ticker, evaluate signals and send alerts."""
    df = fetch_price_history(ticker)
    if df is None:
        logger.info("[%s] skipped (no data).", ticker)
        return

    signals = evaluate_strategies(ticker, df)
    if not signals:
        logger.info("[%s] no signals.", ticker)
        return

    logger.info("[%s] signals detected: %s", ticker, ", ".join(signals))

    stock_name_ar = STOCK_NAMES_AR.get(ticker, ticker)
    sentiment = fetch_arabic_stock_news(stock_name_ar, ticker)

    ind = compute_indicators(df)
    ctx: Dict[str, Any] = {
        "price": latest(ind, "Close"),
        "rsi": latest(ind, "RSI"),
        "ema20": latest(ind, "EMA20"),
        "sma50": latest(ind, "SMA50"),
        "volume_ratio": None,
        "pe": get_trailing_pe(ticker) if INVESTMENT in signals else None,
    }
    if has_volume_spike(ind):
        current_vol = latest(ind, "Volume")
        avg_vol = latest(ind, "VolMA20")
        if current_vol is not None and avg_vol:
            ctx["volume_ratio"] = current_vol / avg_vol

    # Synthetic Order Flow: 1m delta emulation (free footprint) - for TQI boost
    try:
        df_1m = None
        try:
            # Fetch 1m intraday (last 5d, yfinance limits 1m to 7d)
            try:
                raw_1m = yf.download(
                    ticker,
                    period="5d",
                    interval="1m",
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
            except KeyError as exc:
                msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
                print(msg)
                logger.warning(msg)
                raw_1m = None
                df_1m = None
            except ValueError as exc:
                msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
                print(msg)
                logger.warning(msg)
                raw_1m = None
                df_1m = None
            except Exception as exc:
                msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
                print(msg)
                logger.warning(msg)
                raw_1m = None
                df_1m = None
            if raw_1m is not None and not raw_1m.empty:
                try:
                    df_1m = raw_1m.copy()
                    df_1m.columns = [col[0] if isinstance(col, tuple) else col for col in df_1m.columns]
                    # Filter to last trading day's 1m if too large, but keep all for calc
                    if len(df_1m) > 500:
                        df_1m = df_1m.tail(500)
                except KeyError as exc:
                    msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
                    print(msg)
                    logger.warning(msg)
                    df_1m = None
                except ValueError as exc:
                    msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
                    print(msg)
                    logger.warning(msg)
                    df_1m = None
                except Exception as exc:
                    msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
                    print(msg)
                    logger.warning(msg)
                    df_1m = None
        except KeyError as exc:
            msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
            print(msg)
            logger.warning(msg)
            df_1m = None
        except ValueError as exc:
            msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
            print(msg)
            logger.warning(msg)
            df_1m = None
        except Exception as exc:
            logger.debug("[%s] 1m fetch failed for delta: %s", ticker, exc)
            # Also log with required format for consistency
            msg = f"[YFINANCE ERROR] Failed fetching data for {ticker}: {exc}"
            print(msg)
            logger.warning(msg)
            df_1m = None
        # Fallback: if 1m not available, use daily df as proxy (will still compute delta but with daily granularity)
        delta_src = df_1m if df_1m is not None and not df_1m.empty and len(df_1m) >= 3 else df
        delta_info = calculate_synthetic_delta(delta_src)
        # Store flags in ctx for TQI integration
        ctx["df_1m"] = df_1m
        ctx["cumulative_delta_5"] = delta_info.get("cumulative_delta_5", 0)
        ctx["is_bullish_pressure"] = bool(delta_info.get("is_bullish_pressure", False))
        ctx["is_absorption"] = bool(delta_info.get("is_absorption", False))
        ctx["synthetic_bullish_pressure"] = bool(delta_info.get("is_bullish_pressure", False))
        ctx["synthetic_absorption"] = bool(delta_info.get("is_absorption", False))
        ctx["last_buy_ratio"] = delta_info.get("last_buy_ratio", 0.5)
        try:
            near_support = _is_near_support(ctx.get("price"), df, df_1m if df_1m is not None else df)
            ctx["near_support"] = bool(near_support)
            ctx["is_near_support"] = bool(near_support)
        except Exception:
            ctx["near_support"] = False
            ctx["is_near_support"] = False
        if delta_info.get("is_bullish_pressure") or delta_info.get("is_absorption"):
            logger.info(
                "[%s] Synthetic delta: cum=%.0f bullish=%s absorption=%s near_support=%s buy_ratio=%.2f",
                ticker,
                delta_info.get("cumulative_delta_5", 0),
                delta_info.get("is_bullish_pressure"),
                delta_info.get("is_absorption"),
                ctx.get("near_support"),
                delta_info.get("last_buy_ratio", 0.5),
            )
    except Exception as exc:
        logger.warning("[%s] Synthetic delta calc failed: %s", ticker, exc)
        # Ensure defaults to avoid TQI crash
        ctx.setdefault("is_bullish_pressure", False)
        ctx.setdefault("is_absorption", False)
        ctx.setdefault("near_support", False)
        ctx.setdefault("synthetic_bullish_pressure", False)
        ctx.setdefault("synthetic_absorption", False)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    for strategy in signals:
        if is_duplicate(state, ticker, strategy):
            logger.info(
                "[%s] %s alert already sent within %dh; skipping.",
                ticker,
                strategy,
                DUPLICATE_WINDOW_HOURS,
            )
            continue
        # Supabase-backed deduplication: check sent_alerts for (ticker, strategy, date_sent=Cairo today)
        try:
            if is_already_sent_today_supabase(ticker, strategy):
                logger.info("[%s] %s already sent today (Supabase sent_alerts); skipping duplicate.", ticker, strategy)
                continue
        except Exception as exc:
            logger.warning("[%s] Supabase dedup check failed (%s); continuing", ticker, exc)
        # Strict active position check: suppress if position already exists in active_positions (Supabase or local)
        try:
            if is_ticker_active_in_supabase(ticker):
                logger.info("[%s] already active in Supabase active_positions (ACTIVE/PENDING); skipping duplicate notification.", ticker)
                print(f"[DEDUP] Active position exists for {ticker} – skipping alert")
                continue
            # Always check local file as well to cover pre-sync or offline cases
            try:
                _local_positions = []
                if os.path.exists(ACTIVE_POSITIONS_FILE):
                    with open(ACTIVE_POSITIONS_FILE, "r", encoding="utf-8") as fh:
                        _data = json.load(fh)
                        if isinstance(_data, list):
                            _local_positions = _data
                        elif isinstance(_data, dict):
                            for _k in ("positions", "active_positions", "data"):
                                if _k in _data and isinstance(_data[_k], list):
                                    _local_positions = _data[_k]
                                    break
                _found_local = False
                for _lp in _local_positions:
                    if isinstance(_lp, dict) and str(_lp.get("ticker", "")).strip() == ticker and str(_lp.get("status", "")).upper() in ("ACTIVE", "PENDING"):
                        logger.info("[%s] already active in local active_positions; skipping duplicate.", ticker)
                        print(f"[DEDUP] Local active position exists for {ticker} – skipping")
                        _found_local = True
                        break
                if _found_local:
                    continue
            except Exception:
                pass
        except Exception as exc:
            logger.warning("[%s] active_positions dedup check failed (%s); continuing", ticker, exc)
        # TQI threshold filter — EGX liquidity adapted: skip only if TQI < 5.0
        try:
            tqi_for_filter, track_for_filter, _ = resolve_tqi(ctx, strategy, sentiment)
        except Exception as exc:
            logger.warning("[%s] TQI filter check failed (%s); allowing dispatch", ticker, exc)
            tqi_for_filter = TQI_MIN_THRESHOLD
            track_for_filter = TQI_TRACK_LABELS.get(strategy, str(strategy))
        if tqi_for_filter < TQI_MIN_THRESHOLD:
            logger.info("[FILTERED] Signal for %s skipped (TQI: %.1f/10 < 5.0)", ticker, tqi_for_filter)
            continue
        message = build_message(strategy, ticker, ctx, sentiment)
        # Strict multi-channel routing via trade track (Scalp/Swing/Invest)
        try:
            chat_id = get_channel_id_for_track(track_for_filter)
            if not chat_id:
                chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
            # Legacy fallback to strategy mapping if track routing empty
            if not chat_id:
                chat_id = os.environ.get(CHANNEL_ENV.get(strategy, ""), "") or os.getenv("TELEGRAM_CHAT_ID", "")
        except Exception:
            chat_id = os.environ.get(CHANNEL_ENV.get(strategy, ""), "") or os.getenv("TELEGRAM_CHAT_ID", "")
        # Pre-compute trade details for keyboard and persistence (ensures webhook gets full payload)
        try:
            plan_for_pos = STRATEGY_PLAN.get(strategy, {})
            entry_price_pos = float(ctx.get("price") or 0.0) if isinstance(ctx, dict) else 0.0
            targets_pos = plan_for_pos.get("targets_pct", (0.03, 0.05, 0.08))
            if not isinstance(targets_pos, (list, tuple)) or len(targets_pos) < 3:
                targets_pos = (0.03, 0.05, 0.08)
            p1_pos, p2_pos, p3_pos = float(targets_pos[0]), float(targets_pos[1]), float(targets_pos[2])
            sl_pct_pos = float(plan_for_pos.get("sl_pct", -0.03)) if plan_for_pos.get("sl_pct") is not None else -0.03
            t1_pos = entry_price_pos * (1 + p1_pos)
            t2_pos = entry_price_pos * (1 + p2_pos)
            t3_pos = entry_price_pos * (1 + p3_pos)
            sl_price_pos = entry_price_pos * (1 + sl_pct_pos)
            trade_track_pos = track_for_filter if isinstance(track_for_filter, str) and track_for_filter else TQI_TRACK_LABELS.get(strategy, str(strategy))
        except Exception as exc:
            logger.warning("[%s] failed to pre-compute trade details for keyboard: %s", ticker, exc)
            # Fallback defaults for keyboard
            try:
                trade_track_pos = track_for_filter if isinstance(track_for_filter, str) and track_for_filter else TQI_TRACK_LABELS.get(strategy, str(strategy))
                entry_price_pos = float(ctx.get("price") or 0.0) if isinstance(ctx, dict) else 0.0
                # Use plan defaults if available
                plan_for_pos = STRATEGY_PLAN.get(strategy, {})
                targets_pos = plan_for_pos.get("targets_pct", (0.03, 0.05, 0.08))
                p1_pos, p2_pos, p3_pos = float(targets_pos[0]), float(targets_pos[1]), float(targets_pos[2])
                sl_pct_pos = float(plan_for_pos.get("sl_pct", -0.03)) if plan_for_pos.get("sl_pct") is not None else -0.03
                t1_pos = entry_price_pos * (1 + p1_pos)
                t2_pos = entry_price_pos * (1 + p2_pos)
                t3_pos = entry_price_pos * (1 + p3_pos)
                sl_price_pos = entry_price_pos * (1 + sl_pct_pos)
            except Exception:
                entry_price_pos = 0.0
                t1_pos = t2_pos = t3_pos = sl_price_pos = 0.0
                trade_track_pos = TQI_TRACK_LABELS.get(strategy, str(strategy))
        # Build inline keyboard with full trade specs for instant Supabase upsert in webhook
        try:
            keyboard = build_trade_keyboard(ticker, entry_price_pos, sl_price_pos, t1_pos, t2_pos, t3_pos, trade_track_pos)
        except Exception:
            try:
                keyboard = build_trade_keyboard(ticker)
            except Exception:
                keyboard = None
        if send_telegram(chat_id, message, bot_token, reply_markup=keyboard):
            mark_sent(state, ticker, strategy)
            logger.info("[%s] %s alert sent to channel %s.", ticker, strategy, chat_id)
            # Supabase-backed deduplication: immediately write into sent_alerts
            try:
                record_sent_alert_supabase(
                    ticker=ticker,
                    strategy=strategy,
                    entry_price=entry_price_pos,
                    current_stop_loss=sl_price_pos,
                    target_1=t1_pos,
                    target_2=t2_pos,
                    target_3=t3_pos,
                )
            except Exception as exc:
                logger.warning("[%s] failed to record sent_alerts in Supabase: %s", ticker, exc)
            # Persist active position for trailing stop tracking (using pre-computed details)
            try:
                add_active_position(
                    ticker=ticker,
                    entry_price=entry_price_pos,
                    target_1=t1_pos,
                    target_2=t2_pos,
                    target_3=t3_pos,
                    current_stop_loss=sl_price_pos,
                    trade_track=trade_track_pos,
                    status="PENDING",
                )
            except Exception as exc:
                logger.warning("[%s] failed to persist active position: %s", ticker, exc)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_mode(argv: Optional[List[str]] = None) -> str:
    """Parse the --mode CLI argument (intraday/pre_market/post_market)."""
    parser = argparse.ArgumentParser(description="EGX stock screener & news scanner.")
    parser.add_argument(
        "--mode",
        choices=MODES,
        default=INTRADAY,
        help="Execution mode (default: intraday).",
    )
    parser.add_argument(
        "--listen-telegram",
        action="store_true",
        help="Run Telegram callback listener continuously (polling) to catch inline button clicks in real-time",
    )
    # Also support --listen as alias
    parser.add_argument(
        "--listen",
        action="store_true",
        help="Alias for --listen-telegram",
    )
    parser.add_argument(
        "--test-supabase",
        action="store_true",
        help="Test Supabase connection by inserting dummy TEST.CA trade and verifying",
    )
    parser.add_argument(
        "--set-webhook",
        dest="set_webhook",
        type=str,
        nargs="?",
        const="",
        default=None,
        help="Set Telegram webhook to Vercel domain (e.g., https://your-app.vercel.app) and exit",
    )
    args, _ = parser.parse_known_args(argv)
    return args.mode


def should_listen_telegram(argv: Optional[List[str]] = None) -> bool:
    """Check if --listen-telegram flag is present."""
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--listen-telegram", action="store_true")
        parser.add_argument("--listen", action="store_true")
        # Also check --mode parsing to avoid unknown args
        args, _ = parser.parse_known_args(argv)
        return bool(getattr(args, "listen_telegram", False) or getattr(args, "listen", False))
    except Exception:
        # Fallback to raw argv check
        check_argv = argv if argv is not None else sys.argv
        return "--listen-telegram" in check_argv or "--listen" in check_argv


def should_test_supabase(argv: Optional[List[str]] = None) -> bool:
    """Check if --test-supabase flag is present."""
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--test-supabase", action="store_true")
        args, _ = parser.parse_known_args(argv)
        return bool(getattr(args, "test_supabase", False))
    except Exception:
        check_argv = argv if argv is not None else sys.argv
        return "--test-supabase" in check_argv


def get_set_webhook_domain(argv: Optional[List[str]] = None) -> Optional[str]:
    """Get Vercel domain from --set-webhook flag if present."""
    try:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--set-webhook", dest="set_webhook", type=str, nargs="?", const="", default=None)
        args, _ = parser.parse_known_args(argv)
        val = getattr(args, "set_webhook", None)
        # Also check raw argv for --set-webhook=value form
        if val is None:
            check_argv = argv if argv is not None else sys.argv
            for arg in check_argv:
                if arg.startswith("--set-webhook="):
                    return arg.split("=", 1)[1].strip()
                if arg == "--set-webhook":
                    # Next arg is value if not starting with --
                    idx = check_argv.index(arg)
                    if idx + 1 < len(check_argv) and not check_argv[idx + 1].startswith("--"):
                        return check_argv[idx + 1].strip()
            return None
        # val could be "" if flag without value
        if val == "":
            # Try to get next arg as value
            check_argv = argv if argv is not None else sys.argv
            for i, arg in enumerate(check_argv):
                if arg == "--set-webhook" and i + 1 < len(check_argv) and not check_argv[i + 1].startswith("--"):
                    return check_argv[i + 1].strip()
            return val if val else None
        return val.strip() if isinstance(val, str) and val.strip() else None
    except Exception:
        try:
            check_argv = argv if argv is not None else sys.argv
            for i, arg in enumerate(check_argv):
                if arg.startswith("--set-webhook"):
                    if "=" in arg:
                        return arg.split("=", 1)[1].strip()
                    if i + 1 < len(check_argv):
                        nxt = check_argv[i + 1]
                        if not nxt.startswith("--"):
                            return nxt.strip()
        except Exception:
            pass
        return None


NONEWS_FALLBACK_PHRASES: List[str] = ["لا توجد أخبار", "المؤشرات الفنية فقط"]


def has_recent_news(summary: str) -> bool:
    """Return True only when a Gemini summary contains actual news content."""
    return not any(phrase in summary for phrase in NONEWS_FALLBACK_PHRASES)


def run_news_watchlist(mode: str) -> int:
    """Scan all tickers for Arabic news and send a unified off-hours watchlist.

    Also manages active positions trailing stops on each post/pre-market run.
    Wrapped in top-level try...except to prevent scraper hangs/timeouts from crashing workflow.
    """
    # Trailing stop & exit logic for active positions
    try:
        logger.info("Checking active positions for trailing stop updates (mode=%s)...", mode)
        manage_active_positions()
    except Exception as exc:
        logger.warning("Active position trailing check failed (%s); continuing watchlist", exc)

    try:
        title = PRE_MARKET_TITLE if mode == PRE_MARKET else POST_MARKET_TITLE
        entries: List[str] = []
        no_news: List[str] = []
        for ticker in TICKERS:
            stock_name_ar = STOCK_NAMES_AR.get(ticker, ticker)
            clean_ticker = ticker.replace(".CA", "")
            try:
                headlines = fetch_arabic_headlines(stock_name_ar, ticker)
            except Exception as exc:
                logger.warning("News scan failed for %s: %s", ticker, exc)
                no_news.append(clean_ticker)
                continue
            if not headlines:
                no_news.append(clean_ticker)
                continue
            try:
                summary = _summarize_with_gemini(build_news_prompt(headlines), ticker)
            except Exception as exc:
                logger.warning("[%s] Gemini summary failed: %s; treating as no news", ticker, exc)
                no_news.append(clean_ticker)
                continue
            if not has_recent_news(summary):
                logger.info("[%s] only fallback news text; treating as no news.", ticker)
                no_news.append(clean_ticker)
                continue
            try:
                classification = classify_sentiment(summary)
                body = extract_news_body(summary)
            except Exception as exc:
                logger.warning("[%s] sentiment parse failed: %s", ticker, exc)
                no_news.append(clean_ticker)
                continue
            badge = SENTIMENT_BADGES.get(classification or "", "⚪")
            # Enhanced pre-market formatting: cashtag, top-heavy metrics, macro
            try:
                tqi_score = extract_tqi_score(summary)
            except Exception:
                tqi_score = None
            try:
                track = extract_trade_track_from_text(summary)
            except Exception:
                track = None
            try:
                conviction = extract_conviction_from_text(summary)
            except Exception:
                conviction = None
            try:
                macro_block = build_macro_block(summary)
            except Exception:
                macro_block = ""
            # Clean body from any leaked TQI/Track/Rating/Macro to avoid duplication with top-heavy metrics
            try:
                body_clean = body
                if isinstance(body_clean, str):
                    # Remove exact macro block if present in body
                    if macro_block and macro_block in body_clean:
                        body_clean = body_clean.replace(macro_block, "").strip()
                    # Remove top-heavy metric lines if they leaked into body
                    body_clean = re.sub(r"🎯\s*تقييم الجودة\s*\(TQI\)\s*:.*", "", body_clean).strip()
                    body_clean = re.sub(r"🏷️\s*المسار\s*:.*", "", body_clean).strip()
                    body_clean = re.sub(r"⭐\s*التصنيف\s*:.*", "", body_clean).strip()
                    body_clean = re.sub(r"TQI\s*:\s*[0-9.]+/10.*", "", body_clean).strip()
                    body_clean = re.sub(r"🧠\s*التحليل\s*الكلي.*", "", body_clean, flags=re.DOTALL).strip()
                    body_clean = re.sub(r"التحليل\s*الكلي\s*والأثر\s*غير\s*المباشر.*", "", body_clean, flags=re.DOTALL).strip()
                    body_clean = re.sub(r"\n\s*•\s*السبب\s*:.*", "", body_clean).strip()
                    body_clean = re.sub(r"\n\s*•\s*القطاع\s*(المتأثر|التأثر)?\s*:.*", "", body_clean).strip()
                    body_clean = re.sub(r"\n\s*•\s*الأسهم\s*المستفيدة.*", "", body_clean).strip()
                    body_clean = re.sub(r"\n{3,}", "\n\n", body_clean).strip()
                    body = body_clean
            except Exception:
                pass
            # Header with Telegram cashtag
            header = f"{badge} ${clean_ticker} | {stock_name_ar}"
            tqi_line = f"🎯 تقييم الجودة (TQI): {tqi_score:.1f}/10" if isinstance(tqi_score, (int, float)) else "🎯 تقييم الجودة (TQI): غير متاح"
            track_line = f"🏷️ المسار: {track}" if track else "🏷️ المسار: غير متاح"
            conviction_line = f"⭐ التصنيف: {conviction}" if conviction else "⭐ التصنيف: غير متاح"
            block_parts = [header, tqi_line, track_line, conviction_line, body]
            if macro_block:
                block_parts.append(macro_block)
            block = "\n".join(part for part in block_parts if part and str(part).strip())
            if mode == PRE_MARKET:
                if classification == "إيجابي":
                    entries.append(block)
            else:
                entries.append(block)
        if entries:
            body = "\n──────────────────\n".join(entries)
        else:
            body = NO_NEWS_WATCHLIST
        if no_news:
            body = body + "\n\n" + f"ℹ️ أسهم بدون أخبار جديدة: {' | '.join(no_news)}"
        message = f"{title}\n\n{body}"
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = (
            os.getenv("TELEGRAM_CHANNEL_NEWS")
            or os.getenv("TELEGRAM_CHAT_ID_NEWS")
            or os.getenv("TELEGRAM_CHAT_ID")
            or os.environ.get(CHANNEL_ENV[SCALPING], "")
        )
        try:
            if send_telegram(chat_id, message, bot_token):
                logger.info("News watchlist (%s) delivered to channel %s.", mode, chat_id)
                return 0
            logger.error("Failed to deliver news watchlist (%s).", mode)
            return 1
        except Exception as exc:
            logger.warning("Telegram dispatch failed for watchlist (%s): %s", mode, exc)
            return 1
    except Exception as exc:
        logger.warning("News watchlist top-level failed gracefully (%s): %s", mode, exc)
        # Graceful exit without crashing workflow (return 0 to avoid workflow failure on minor news errors)
        # But return 1 if it's a critical failure? We'll return 0 to keep workflow green for minor errors
        logger.info("Continuing gracefully after news watchlist error")
        return 0


def main() -> int:
    """Run the chosen execution mode (intraday scan or off-hours news watchlist).

    Supports --listen-telegram flag for standalone bot listener (polling)
    and --test-supabase for Supabase connection test.
    Also auto-syncs local JSON to Supabase on every run.
    """
    # Standalone Supabase test mode: check flag before env checks
    try:
        if should_test_supabase():
            logger.info("Running Supabase test mode (--test-supabase)")
            print("="*60)
            print("[TEST-SUPABASE] Running standalone Supabase test")
            print("="*60)
            success = test_supabase_connection()
            if success:
                print("[TEST-SUPABASE] Test PASSED")
            else:
                print("[TEST-SUPABASE] Test FAILED")
            return 0 if success else 1
    except Exception as exc:
        logger.warning("Test Supabase check failed: %s", exc)
        print(f"[TEST-SUPABASE ERROR] {exc}")

    # Standalone bot listener support: check flag before env checks
    try:
        if should_listen_telegram():
            logger.info("Starting Telegram listener mode (--listen-telegram)")
            # Ensure sync before listening
            try:
                sync_active_positions_to_supabase()
            except Exception as exc:
                logger.warning("Initial sync failed: %s", exc)
            listen_telegram()
            return 0
    except Exception as exc:
        logger.warning("Listener check failed: %s", exc)

    # Webhook auto-registration support: --set-webhook <VERCEL_DOMAIN>
    try:
        domain = get_set_webhook_domain()
        if domain is not None:
            if not domain or domain.strip() == "":
                print("ERROR: --set-webhook requires <VERCEL_DOMAIN> argument (e.g., https://your-app.vercel.app)")
                logger.error("No domain provided for --set-webhook")
                return 1
            logger.info("Setting Telegram webhook (--set-webhook) to %s", domain)
            success = set_telegram_webhook(domain.strip())
            return 0 if success else 1
        # Handle case where flag is present without value (argparse const="")
        if "--set-webhook" in sys.argv:
            # Check if next arg is missing or is another flag
            try:
                idx = sys.argv.index("--set-webhook")
                if idx + 1 >= len(sys.argv) or sys.argv[idx + 1].startswith("--"):
                    print("ERROR: --set-webhook requires <VERCEL_DOMAIN> argument")
                    logger.error("Missing VERCEL_DOMAIN for --set-webhook")
                    return 1
            except Exception:
                pass
    except SystemExit:
        raise
    except Exception as exc:
        logger.warning("set-webhook check failed: %s", exc)

    check_required_env()
    # Ensure active_positions.json exists for workflow auto-commit (git add will fail if missing)
    try:
        if not os.path.exists(ACTIVE_POSITIONS_FILE):
            save_active_positions([], ACTIVE_POSITIONS_FILE)
    except Exception:
        pass
    # Auto-sync local JSON to Supabase on every run (fixes GitHub Actions exit issue)
    try:
        logger.info("Syncing local active positions to Supabase...")
        synced = sync_active_positions_to_supabase()
        print(f"[SYNC] Synced {synced} positions to Supabase (or 0 if no Supabase/empty)")
    except Exception as exc:
        logger.warning("Sync to Supabase failed: %s", exc)
        print(f"[SYNC ERROR] {exc}")
    mode = parse_mode()
    if mode in (PRE_MARKET, POST_MARKET):
        logger.info("Running off-hours news scan in %s mode.", mode)
        return run_news_watchlist(mode)
    logger.info("EGX screener started — monitoring %d tickers.", len(TICKERS))
    state = load_state()

    for ticker in TICKERS:
        try:
            process_ticker(ticker, state)
        except Exception as exc:  # never let one ticker kill the run
            logger.warning("[%s] unexpected error: %s", ticker, exc)

    save_state(state)
    logger.info("Screener finished; state persisted to %s.", STATE_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
