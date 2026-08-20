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

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
    "COMI.CA",
    "ABUK.CA",
    "HELI.CA",
    "EAST.CA",
    "SWDY.CA",
    "EFIN.CA",
    "HRHO.CA",
    "TMGH.CA",
    "AMOC.CA",
    "ORAS.CA",
]

STATE_FILE: str = "state.json"
DUPLICATE_WINDOW_HOURS: int = 12

RSI_LENGTH: int = 14
EMA_LENGTH: int = 20
SMA_LENGTH: int = 50
VOLUME_AVG_WINDOW: int = 20
VOLUME_SPIKE_MULTIPLIER: float = 1.8

GEMINI_MODEL: str = "gemini-3.6-flash"
NEWS_RSS_URL: str = (
    "https://news.google.com/rss/search?q={ticker}+بورصة+مصر&hl=ar&gl=EG&ceid=EG:ar"
)
NEWS_HEADLINES_COUNT: int = 3

SCALPING: str = "scalping"
SWING: str = "swing"
INVESTMENT: str = "investment"

CHANNEL_ENV: Dict[str, str] = {
    SCALPING: "CHANNEL_SCALPING",
    SWING: "CHANNEL_SWING",
    INVESTMENT: "CHANNEL_INVESTMENT",
}

TELEGRAM_API: str = "https://api.telegram.org/bot{token}/sendMessage"

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


def fetch_news_headlines(ticker: str) -> List[str]:
    """Fetch the top Arabic headlines for a ticker from Google News RSS."""
    try:
        feed = feedparser.parse(NEWS_RSS_URL.format(ticker=ticker))
        headlines: List[str] = []
        for entry in feed.entries[:NEWS_HEADLINES_COUNT]:
            title = (entry.get("title") or "").strip()
            if title:
                headlines.append(title)
        return headlines
    except Exception as exc:
        logger.warning("[%s] news fetch failed: %s", ticker, exc)
        return []


def analyze_sentiment(headlines: List[str]) -> str:
    """Ask Gemini for a 2-sentence Arabic sentiment summary."""
    if not headlines:
        return "لا توجد أخبار كافية لتحليل المعنويات."
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set; skipping sentiment analysis.")
        return "تحليل المعنويات غير متاح حاليًا."
    try:
        client = genai.Client(api_key=api_key)
        headlines_block = "\n".join(f"- {h}" for h in headlines)
        prompt = (
            "اقرأ العناوين التالية الخاصة بأسهم البورصة المصرية، ثم أخرج:\n"
            "1) ملخصًا من جملتين باللغة العربية يلخّص اتجاه الأخبار.\n"
            "2) تصنيف المعنويات بإحدى الكلمات فقط: إيجابي / سلبي / محايد.\n\n"
            f"العناوين:\n{headlines_block}"
        )
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = (response.text or "").strip()
        return text if text else "تحليل المعنويات غير متاح حاليًا."
    except Exception as exc:
        logger.warning("Gemini sentiment analysis failed: %s", exc)
        return "تحليل المعنويات غير متاح حاليًا."


# --------------------------------------------------------------------------
# Telegram notifications
# --------------------------------------------------------------------------


def send_telegram(chat_id: str, message: str, bot_token: str) -> bool:
    """Send a Markdown-formatted message to a Telegram chat/channel."""
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set; cannot send alerts.")
        return False
    if not chat_id:
        logger.error("Channel ID is empty; cannot send alert.")
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
    except Exception as exc:
        logger.warning("Telegram send failed (chat=%s): %s", chat_id, exc)
        return False


def fmt(value: Optional[float], digits: int = 2) -> str:
    """Format a number for display; return 'n/a' when missing."""
    return f"{value:.{digits}f}" if value is not None else "غير متاح"


def build_message(strategy: str, ticker: str, ctx: Dict[str, Any], sentiment: str) -> str:
    """Compose an Arabic Markdown alert for a strategy."""
    price = ctx.get("price")
    rsi = ctx.get("rsi")
    ema20 = ctx.get("ema20")
    sma50 = ctx.get("sma50")
    volume_ratio = ctx.get("volume_ratio")
    pe = ctx.get("pe")

    if strategy == SCALPING:
        return (
            f"*إشارة سكالبنج | {ticker}*\n"
            f"السعر: {fmt(price)} ج.م\n"
            f"RSI(14): {fmt(rsi, 1)}\n"
            f"EMA20: {fmt(ema20)} ج.م\n"
            f"الحجم: تضخم بنسبة {fmt(volume_ratio, 1)}x عن متوسط 20 يوم\n"
            f"المعنويات: {sentiment}"
        )
    if strategy == SWING:
        return (
            f"*إشارة سوينغ | {ticker}*\n"
            "السعر اخترق EMA20 لأعلى\n"
            f"السعر: {fmt(price)} ج.م\n"
            f"RSI(14): {fmt(rsi, 1)}\n"
            f"المعنويات: {sentiment}"
        )
    if strategy == INVESTMENT:
        return (
            f"*إشارة استثمار | {ticker}*\n"
            f"المضاعف الربحي (P/E): {fmt(pe, 1)}\n"
            f"السعر: {fmt(price)} ج.م (أقل من SMA50 = {fmt(sma50)} ج.م)\n"
            f"RSI(14): {fmt(rsi, 1)}\n"
            f"المعنويات: {sentiment}"
        )
    raise ValueError(f"Unknown strategy: {strategy}")


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

    sentiment = analyze_sentiment(fetch_news_headlines(ticker))

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
        chat_id = os.environ.get(CHANNEL_ENV[strategy], "")
        if send_telegram(chat_id, message, bot_token):
            mark_sent(state, ticker, strategy)
            logger.info("[%s] %s alert sent to channel %s.", ticker, strategy, chat_id)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    """Run the full screening pass over every configured ticker."""
    check_required_env()
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
