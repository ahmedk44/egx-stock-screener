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
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote_plus

import feedparser
import pandas as pd
import pandas_ta as ta
import requests
import yfinance as yf
from dotenv import load_dotenv
from google import genai

try:
    load_dotenv()
except Exception:
    pass

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
    "AMOC.CA",
    "COMI.CA",
    "EAST.CA",
    "EFID.CA",
    "EFIH.CA",
    "ETEL.CA",
    "FAIT.CA",
    "FWRY.CA",
    "HELI.CA",
    "ISPH.CA",
    "JUFO.CA",
    "OLFI.CA",
    "ORAS.CA",
    "ORWE.CA",
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
        "targets_pct": (0.03, 0.05, 0.08),
        "sl_pct": -0.03,
        "sl_condition_ar": "إغلاق شمعة أسفل الدعم",
        "allocation_ar": "5% - 10% من رأس المال",
        "duration_ar": "مضاربة لحظية / سريعة (داخل اليوم)",
        "technical_reason_ar": (
            "اختراق لحظي لمستوى مقاومة مع تضخم واضح في حجم التداول "
            "وكسر السعر لأعلى المتوسط المتحرك EMA20"
        ),
    },
    SWING: {
        "targets_pct": (0.05, 0.10, 0.17),
        "sl_pct": -0.06,
        "sl_condition_ar": "إغلاق يوم أسفل الدعم",
        "allocation_ar": "10% من رأس المال",
        "duration_ar": "مضاربة متوسطة المدى (أيام إلى أسابيع)",
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
        "duration_ar": "استثمار قصير إلى متوسط المدى (شهور)",
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
    except Exception as exc:
        logger.warning("[%s] yfinance download failed: %s", ticker, exc)
        return None
    if raw is None or raw.empty:
        logger.warning("[%s] yfinance returned no data.", ticker)
        return None
    df = raw.copy()
    # Newer yfinance versions return MultiIndex columns (e.g. ("Close", "COMI.CA")).
    df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
    return df


def get_trailing_pe(ticker: str) -> Optional[float]:
    """Fetch the trailing P/E ratio from yfinance fundamentals."""
    try:
        info = yf.Ticker(ticker).get_info()
    except AttributeError:
        try:
            info = yf.Ticker(ticker).info  # older yfinance fallback
        except Exception as exc:
            logger.warning("[%s] failed to fetch fundamentals: %s", ticker, exc)
            return None
    except Exception as exc:
        logger.warning("[%s] failed to fetch fundamentals: %s", ticker, exc)
        return None
    try:
        pe = info.get("trailingPE") if isinstance(info, dict) else None
        return float(pe) if pe is not None else None
    except (TypeError, ValueError):
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
# News + sentiment (Google News RSS -> Gemini)
# --------------------------------------------------------------------------


def fetch_arabic_headlines(stock_name_ar: str, ticker: str) -> List[str]:
    """Fetch the top Arabic Google News headlines (with publish dates) for a stock."""
    query = quote_plus(f"{stock_name_ar} البورصة المصرية")
    headlines: List[str] = []
    try:
        resp = requests.get(NEWS_RSS_URL.format(query=query), timeout=15)
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


def build_news_prompt(headlines: List[str]) -> str:
    """Build Gemini prompt: sentiment + Trade Quality Index (TQI) evaluation.

    Instructs Gemini to evaluate each stock signal using TQI 0.0-10.0 across
    5 weighted parameters, assign Trade Track and Conviction Tier, and emit
    machine-parsable lines for Telegram formatting.
    """
    headlines_block = "\n".join(f"- {h}" for h in headlines)
    return (
        "أنت محلل مالي متخصص في البورصة المصرية (EGX). اقرأ العناوين التالية ثم أخرج:\n"
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
        "   احسب المجموع بدقة من 10.0 واذكر الدرجة بصيغة `🎯 تقييم الجودة (TQI): X.X/10` (رقم عشري واحد).\n\n"
        "4) حدد المسار التجاري (Trade Track) بوضوح بإحدى القيم فقط:\n"
        "   - `⚡ مضاربة لحظية (Scalp)` للصفقات اللحظية داخل اليوم\n"
        "   - `📈 تداول سوينغ (Swing)` للصفقات المتوسطة (أيام إلى أسابيع)\n"
        "   - `🏛️ استثمار طويل (Invest)` للاستثمار الطويل\n"
        "   أخرجها بصيغة `🏷️ المسار: [القيمة]`.\n\n"
        "5) حدد مستوى القناعة (Conviction Tier) حسب TQI:\n"
        "   - TQI >= 9.0: `🟢 فرصة استثنائية (A+ Setup)`\n"
        "   - TQI 7.5 - 8.9: `🟡 فرصة جيدة (B+ Setup)`\n"
        "   - TQI < 7.5: `⚪ فرصة ضعيفة (Low Conviction)`\n"
        "   أخرجها بصيغة `⭐ التصنيف: [القيمة]`.\n\n"
        "تنسيق الإخراج المطلوب (حافظ عليه حرفيًا ليتوافق مع محلل الرسائل):\n"
        "- السطر الأول/الثاني: ملخص الأخبار\n"
        "- سطر: تصنيف المعنويات\n"
        "- سطر: 🎯 تقييم الجودة (TQI): X.X/10\n"
        "- سطر: 🏷️ المسار: [Scalp / Swing / Invest]\n"
        "- سطر: ⭐ التصنيف: [A+ Setup / B+ Setup / Low Conviction]\n\n"
        f"العناوين:\n{headlines_block}"
    )


def build_tqi_prompt(strategy: str, ctx: Dict[str, Any], sentiment: str) -> str:
    """Build a dedicated Gemini prompt for TQI scoring of a specific signal.

    Provides technical context (RSI, EMA, volume surge, R:R) so Gemini can
    score the 5 TQI parameters accurately.
    """
    plan = STRATEGY_PLAN.get(strategy, {})
    price = ctx.get("price")
    rsi = ctx.get("rsi")
    volume_ratio = ctx.get("volume_ratio")
    rr_targets = plan.get("targets_pct", (0.03, 0.05, 0.08))
    sl_pct = plan.get("sl_pct", -0.03)
    rr = abs(rr_targets[2] / sl_pct) if sl_pct else 0
    return (
        "أنت خبير تداول كمي للبورصة المصرية. قيّم جودة الإشارة التالية عبر Trade Quality Index (TQI) من 0.0 إلى 10.0:\n"
        f"الاستراتيجية: {strategy} | السعر: {fmt(price)} | RSI: {fmt(rsi, 1)} | "
        f"نسبة الحجم: {fmt(volume_ratio, 2) if volume_ratio else 'غير متاح'} | "
        f"R:R المتوقع: 1:{rr:.2f}\n"
        f"ملخص الأخبار/المعنويات: {extract_news_body(sentiment)[:300]}\n\n"
        "المعايير المرجحة (المجموع 10.0):\n"
        "1) Technical Confluence (3 pts) — التقاء RSI/EMA/SMA/اختراق\n"
        "2) Risk/Reward Ratio (2.5 pts) — جودة R:R\n"
        "3) Relative Volume Surge (2 pts) — قوة الحجم النسبي\n"
        "4) Sector Alignment (1.5 pts) — توافق القطاع\n"
        "5) News/Catalyst Strength (1 pt) — قوة المحفز الخبري\n\n"
        "أخرج حتمًا:\n"
        "🎯 تقييم الجودة (TQI): X.X/10\n"
        "🏷️ المسار: [⚡ مضاربة لحظية (Scalp) / 📈 تداول سوينغ (Swing) / 🏛️ استثمار طويل (Invest)]\n"
        "⭐ التصنيف: [🟢 فرصة استثنائية (A+ Setup) / 🟡 فرصة جيدة (B+ Setup) / ⚪ فرصة ضعيفة (Low Conviction)]\n"
        "حسب القواعد: TQI >=9.0 → 🟢 A+ | 7.5-8.9 → 🟡 B+ | <7.5 → ⚪ Low Conviction\n"
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
        logger.warning("[%s] Gemini sentiment analysis failed: %s", ticker, exc)
        return GEMINI_FALLBACK_PROMPT


# --------------------------------------------------------------------------
# Telegram notifications
# --------------------------------------------------------------------------


def send_telegram(chat_id: Optional[str], message: str, bot_token: Optional[str]) -> bool:
    """Send a Markdown-formatted message to a Telegram chat/channel."""
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


def classify_sentiment(text: str) -> Optional[str]:
    """Return the sentiment word (إيجابي/سلبي/محايد) found in a Gemini summary, if any."""
    for word in ("إيجابي", "سلبي", "محايد"):
        if word in text:
            return word
    return None


def extract_news_body(text: str) -> str:
    """Return the compact summary body with all Gemini scaffolding stripped."""
    body = text.strip()
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
        body = re.sub(pattern, "", body, flags=re.MULTILINE)
    body = body.replace("**", "")
    for word in ("إيجابي", "سلبي", "محايد"):
        body = re.sub(rf"^\s*{word}\s*$", "", body, flags=re.MULTILINE)
        body = body.replace(f": {word}", "").replace(f" :{word}", "").strip()
    body = re.sub(r"\n+", "\n", body).strip()
    body = re.sub(r"[ \t]+", " ", body).strip()
    return body


def build_news_block(sentiment: str) -> str:
    """Return a compact, badge-labeled Arabic news summary block."""
    classification = classify_sentiment(sentiment) or ""
    badge = SENTIMENT_BADGES.get(classification, "⚪")
    header = f"🤖 ملخص الأخبار (Gemini AI): {badge} {classification}".strip()
    body = extract_news_body(sentiment)
    return f"{header}\n{body}".strip()


# --------------------------------------------------------------------------
# Trade Quality Index (TQI) helpers
# --------------------------------------------------------------------------


def get_conviction_tier(tqi: float) -> str:
    """Return Conviction Tier label for a TQI score."""
    if tqi >= 9.0:
        return "🟢 فرصة استثنائية (A+ Setup)"
    if tqi >= 7.5:
        return "🟡 فرصة جيدة (B+ Setup)"
    return "⚪ فرصة ضعيفة (Low Conviction)"


def get_trade_track_label(strategy: str) -> str:
    """Return Trade Track label for a strategy key."""
    return TQI_TRACK_LABELS.get(strategy, "⚪ فرصة ضعيفة (Low Conviction)")


def extract_tqi_score(text: str) -> Optional[float]:
    """Extract TQI score X.X/10 from Gemini text if present."""
    if not text:
        return None
    # Match patterns like "TQI: 8.5/10" or "تقييم الجودة (TQI): 8.5/10"
    pattern = re.compile(r"TQI[^0-9]*([0-9]+(?:\.[0-9]+)?)\s*/\s*10", re.IGNORECASE)
    match = pattern.search(text)
    if match:
        try:
            val = float(match.group(1))
            # Clamp to 0.0-10.0
            return max(0.0, min(10.0, round(val, 1)))
        except ValueError:
            return None
    return None


def extract_trade_track_from_text(text: str) -> Optional[str]:
    """Extract Trade Track label from Gemini text if present."""
    if not text:
        return None
    for label in TQI_TRACK_LABELS.values():
        if label in text:
            return label
    # Fallback: detect keywords
    if "Scalp" in text or "مضاربة لحظية" in text:
        return TQI_TRACK_LABELS[SCALPING]
    if "Swing" in text or "سوينغ" in text:
        return TQI_TRACK_LABELS[SWING]
    if "Invest" in text or "استثمار طويل" in text:
        return TQI_TRACK_LABELS[INVESTMENT]
    return None


def extract_conviction_from_text(text: str) -> Optional[str]:
    """Extract Conviction Tier label from Gemini text if present."""
    if not text:
        return None
    candidates = [
        "🟢 فرصة استثنائية (A+ Setup)",
        "🟡 فرصة جيدة (B+ Setup)",
        "⚪ فرصة ضعيفة (Low Conviction)",
    ]
    for cand in candidates:
        if cand in text:
            return cand
    # Fallback keyword match
    if "A+ Setup" in text or "استثنائية" in text:
        return candidates[0]
    if "B+ Setup" in text or "فرصة جيدة" in text:
        return candidates[1]
    if "Low Conviction" in text or "فرصة ضعيفة" in text:
        return candidates[2]
    return None


def compute_fallback_tqi(ctx: Dict[str, Any], strategy: str, sentiment: str) -> float:
    """Deterministically compute TQI 0.0-10.0 from available context.

    Scoring (mirrors Gemini prompt weights):
      - Technical Confluence 3.0 pts
      - Risk/Reward 2.5 pts
      - Relative Volume Surge 2.0 pts
      - Sector Alignment 1.5 pts
      - News/Catalyst Strength 1.0 pt
    """
    # Technical Confluence (3 pts)
    tech_score = 0.0
    rsi = ctx.get("rsi")
    price = ctx.get("price")
    ema20 = ctx.get("ema20")
    sma50 = ctx.get("sma50")
    if rsi is not None:
        if strategy == SCALPING and rsi > 55:
            tech_score += 1.5
        elif strategy == SWING and rsi > 50:
            tech_score += 1.5
        elif strategy == INVESTMENT and rsi < 40:
            tech_score += 1.5
        elif 40 <= rsi <= 70:
            tech_score += 0.8
    if price is not None and ema20 is not None and price > ema20:
        tech_score += 0.8
    if price is not None and sma50 is not None:
        if strategy == INVESTMENT and price < sma50:
            tech_score += 0.7
        elif price > sma50:
            tech_score += 0.5
    tech_score = min(tech_score, TQI_TECHNICAL_CONFLUENCE_MAX)

    # Risk/Reward Ratio (2.5 pts)
    plan = STRATEGY_PLAN.get(strategy, {})
    sl_pct = abs(plan.get("sl_pct", 0.03))
    targets = plan.get("targets_pct", (0.03, 0.05, 0.08))
    rr_ratio = abs(targets[2] / sl_pct) if sl_pct else 0
    if rr_ratio >= 2.5:
        rr_score = TQI_RISK_REWARD_MAX
    elif rr_ratio >= 1.5:
        rr_score = 1.8
    elif rr_ratio >= 1.0:
        rr_score = 1.0
    else:
        rr_score = 0.5

    # Relative Volume Surge (2 pts)
    vol_ratio = ctx.get("volume_ratio")
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

    # Sector Alignment (1.5 pts) — no sector feed, use conservative default with slight bump for known liquid tickers
    sector_score = 1.0
    if strategy == SWING and vol_ratio and vol_ratio > 1.5:
        sector_score = 1.2

    # News/Catalyst Strength (1 pt)
    classification = classify_sentiment(sentiment) or ""
    if classification == "إيجابي":
        news_score = TQI_NEWS_CATALYST_MAX
    elif classification == "سلبي":
        news_score = 0.2
    elif classification == "محايد":
        news_score = 0.5
    else:
        # Fallback: check headlines presence via sentiment length
        body = extract_news_body(sentiment)
        news_score = 0.6 if len(body) > 30 else 0.3

    total = tech_score + rr_score + vol_score + sector_score + news_score
    return max(0.0, min(10.0, round(total, 1)))


def resolve_tqi(ctx: Dict[str, Any], strategy: str, sentiment: str) -> tuple[float, str, str]:
    """Resolve TQI, Trade Track and Conviction Tier for a message.

    Priority: parse from Gemini text → fallback to deterministic computation.
    Returns (tqi_score, track_label, conviction_label).
    """
    tqi = extract_tqi_score(sentiment)
    track = extract_trade_track_from_text(sentiment)
    conviction = extract_conviction_from_text(sentiment)

    if tqi is None:
        tqi = compute_fallback_tqi(ctx, strategy, sentiment)

    if track is None:
        track = get_trade_track_label(strategy)

    if conviction is None:
        conviction = get_conviction_tier(tqi)
    else:
        # Ensure conviction matches tqi if Gemini provided inconsistent tier
        expected = get_conviction_tier(tqi)
        # Keep Gemini conviction but prefer deterministic if mismatch is large
        # For consistency, trust expected tier when tqi far from tier threshold
        if conviction != expected:
            # Re-derive to keep parser deterministic; Gemini tier is preserved only if tqi close to boundary
            conviction = expected

    return tqi, track, conviction


def build_message(strategy: str, ticker: str, ctx: Dict[str, Any], sentiment: str) -> str:
    """Compose a professional Arabic Markdown alert with dynamic targets & risk plan.

    Includes Trade Quality Index (TQI), Trade Track and Conviction Tier while
    preserving all existing target prices and news summary fields for parser compatibility.
    """
    plan = STRATEGY_PLAN[strategy]
    sharia_tag = get_sharia_status_tag(ticker)
    stock_name_ar = STOCK_NAMES_AR.get(ticker, ticker)
    clean_ticker = ticker.replace(".CA", "")
    entry_price = float(ctx.get("price") or 0.0)
    p1, p2, p3 = plan["targets_pct"]
    sl_pct = plan["sl_pct"]
    target_1 = entry_price * (1 + p1)
    target_2 = entry_price * (1 + p2)
    target_3 = entry_price * (1 + p3)
    stop_loss = entry_price * (1 + sl_pct)
    rr = abs(p3 / sl_pct)
    news_block = build_news_block(sentiment)
    # Resolve Trade Quality Index (TQI) — parsed from Gemini or fallback computed
    tqi_score, track_label, conviction_label = resolve_tqi(ctx, strategy, sentiment)
    return (
        f"اسم السهم : {stock_name_ar} {clean_ticker}\n"
        f"\n"
        f"سبب دخول الصفقه فنيا : {plan['technical_reason_ar']}\n"
        f"\n"
        f"{sharia_tag}\n"
        f"\n"
        f"🎯 تقييم الجودة (TQI): {tqi_score:.1f}/10\n"
        f"🏷️ المسار: {track_label}\n"
        f"⭐ التصنيف: {conviction_label}\n"
        f"\n"
        f"سعر الدخول : {entry_price:.2f} 🏷\n"
        f"\n"
        f"الهدف الاول: {target_1:.2f} ({p1 * 100:.1f}%) 🎯\n"
        f"الهدف الثاني : {target_2:.2f} ({p2 * 100:.1f}%) 🎯\n"
        f"الهدف الثالث: {target_3:.2f} ({p3 * 100:.1f}%) 🎯\n"
        f"\n"
        f"وقف الخسارة : {plan['sl_condition_ar']} {stop_loss:.2f} ({sl_pct * 100:.1f}%) ⛔️\n"
        f"\n"
        f"نسبة الدخول من المحفظه : {plan['allocation_ar']} 💵\n"
        f"نوع الصفقة و مدتها : {plan['duration_ar']} ⏳️\n"
        f"معدل العائد إلى المخاطر (R:R) : 1 : {rr:.2f} ⚖️\n"
        f"\n"
        f"📈 [عرض الشارت المباشر على TradingView](https://ar.tradingview.com/symbols/EGX-{clean_ticker}/)\n"
        f"\n"
        f"{news_block}\n"
        f"\n"
        f"تذكير ⚠️ التحليل قد يصيب او يخطئ ولكن يجب عليك الالتزام ب إدارة المخاطر "
        f"وعدم التهاون ب إدارة رأس مالك 🔒 .. بالتوفيق للجميع 👏"
    )


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
        message = build_message(strategy, ticker, ctx, sentiment)
        chat_id = os.environ.get(CHANNEL_ENV[strategy]) or os.getenv("TELEGRAM_CHAT_ID", "")
        if send_telegram(chat_id, message, bot_token):
            mark_sent(state, ticker, strategy)
            logger.info("[%s] %s alert sent to channel %s.", ticker, strategy, chat_id)


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
    args, _ = parser.parse_known_args(argv)
    return args.mode


NONEWS_FALLBACK_PHRASES: List[str] = ["لا توجد أخبار", "المؤشرات الفنية فقط"]


def has_recent_news(summary: str) -> bool:
    """Return True only when a Gemini summary contains actual news content."""
    return not any(phrase in summary for phrase in NONEWS_FALLBACK_PHRASES)


def run_news_watchlist(mode: str) -> int:
    """Scan all tickers for Arabic news and send a unified off-hours watchlist."""
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
        summary = _summarize_with_gemini(build_news_prompt(headlines), ticker)
        if not has_recent_news(summary):
            logger.info("[%s] only fallback news text; treating as no news.", ticker)
            no_news.append(clean_ticker)
            continue
        classification = classify_sentiment(summary)
        body = extract_news_body(summary)
        badge = SENTIMENT_BADGES.get(classification or "", "⚪")
        if mode == PRE_MARKET:
            if classification == "إيجابي":
                entries.append(f"🟢 {stock_name_ar} ({clean_ticker}): {body}")
        else:
            entries.append(f"{badge} {stock_name_ar} ({clean_ticker}): {body}")
    if entries:
        body = "\n".join(entries)
    else:
        body = NO_NEWS_WATCHLIST
    if no_news:
        body = body + "\n\n" + f"ℹ️ أسهم بدون أخبار جديدة: {' | '.join(no_news)}"
    message = f"{title}\n\n{body}"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID_NEWS")
        or os.getenv("TELEGRAM_CHAT_ID")
        or os.environ.get(CHANNEL_ENV[SCALPING], "")
    )
    if send_telegram(chat_id, message, bot_token):
        logger.info("News watchlist (%s) delivered to channel %s.", mode, chat_id)
        return 0
    logger.error("Failed to deliver news watchlist (%s).", mode)
    return 1


def main() -> int:
    """Run the chosen execution mode (intraday scan or off-hours news watchlist)."""
    check_required_env()
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
