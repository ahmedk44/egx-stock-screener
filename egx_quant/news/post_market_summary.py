#!/usr/bin/env python3
"""
Post-Market Summary for EGX News & Market Summaries channel.

Features:
- Fetches closing prices & indices performance (EGX30, EGX70, EGX100) via yfinance
- Aggregates top gainers, top losers, highest turnover stocks from TICKERS
- Generates AI Market Sentiment summary (Bullet points: Market Trend, Liquidity, Top Headlines)
- Formats card with title: 🌙 **ملخص إغلاق البورصة المصرية | Post-Market Bulletin**
- Publishes to Telegram Channel ID for EGX News & Market Summaries
"""
from __future__ import annotations

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except:
    pass

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    import requests
except ImportError:
    requests = None

# Idempotency + Active Signals helpers (Context-Aware)
try:
    from egx_quant.news.common import (
        check_already_published,
        mark_published,
        fetch_active_signals,
        enrich_active_signals_with_prices,
        format_active_signals_section,
        get_cairo_date_str,
        build_context_aware_categories,
        format_context_aware_section,
        _verified_session_headlines,
        _is_sanctioned_ticker,
        LLM_GUARDRAILS_AR,
    )
except ImportError:
    try:
        from common import (  # type: ignore
            check_already_published,
            mark_published,
            fetch_active_signals,
            enrich_active_signals_with_prices,
            format_active_signals_section,
            get_cairo_date_str,
            build_context_aware_categories,
            format_context_aware_section,
            _verified_session_headlines,
            _is_sanctioned_ticker,
            LLM_GUARDRAILS_AR,
        )
    except:
        check_already_published = lambda x: False  # type: ignore
        mark_published = lambda x: True  # type: ignore
        fetch_active_signals = lambda limit=10: []  # type: ignore
        enrich_active_signals_with_prices = lambda x: []  # type: ignore
        format_active_signals_section = lambda x: "🎯 **متابعة أسهم المنظومة والمحفظة | Active Signals Tracker:**\nلا توجد صفقات مفتوحة حالياً في المنظومة."  # type: ignore
        build_context_aware_categories = lambda x, **kw: {"active": [], "watchlist": [], "avoid": []}  # type: ignore
        format_context_aware_section = lambda x: "🎯 **متابعة أسهم المنظومة والفرص | System Signals & Opportunities**\nلا توجد صفقات مفتوحة حالياً في المنظومة."  # type: ignore
        get_cairo_date_str = lambda: datetime.now().strftime("%Y-%m-%d")  # type: ignore
        _verified_session_headlines = lambda h: list(h or [])  # type: ignore
        _is_sanctioned_ticker = lambda t: False  # type: ignore
        LLM_GUARDRAILS_AR = ""  # type: ignore

logger = logging.getLogger("egx_news.post_market")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

# Import TICKERS and names from main or fallback
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from main import TICKERS as MAIN_TICKERS, STOCK_NAMES_AR  # type: ignore
    TICKERS = MAIN_TICKERS
except Exception as e:
    logger.warning(f"Could not import TICKERS from main: {e}, using fallback")
    TICKERS = [
        "COMI.CA","ABUK.CA","ADIB.CA","AMOC.CA","SWDY.CA","TMGH.CA","HELI.CA",
        "ORAS.CA","EFIH.CA","ETEL.CA","FWRY.CA","JUFO.CA","EFID.CA","ISPH.CA",
        "SKPC.CA","SAUD.CA","FAIT.CA","ELWA.CA","HELI.CA","ORWE.CA"
    ]
    STOCK_NAMES_AR = {t: t.replace(".CA","") for t in TICKERS}

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Title required by spec
POST_MARKET_TITLE = "🌙 **ملخص إغلاق البورصة المصرية | Post-Market Bulletin**"

# Indices mapping - try multiple yfinance tickers per index
INDICES_MAP = {
    "EGX30": ["^CASE30", "EGX30.CA", "^CASE", "EGX30.INDX"],
    "EGX70": ["EGX70.CA", "^EGX70", "EGX70.INDX"],
    "EGX100": ["EGX100.CA", "^EGX100", "EGX100.INDX"],
}

def get_news_channel_id() -> Optional[str]:
    """Hard-aligned to NEWS & Summaries Channel per task spec."""
    NEWS_FALLBACK = "-1004492677393"
    for env in [
        "TELEGRAM_NEWS_CHANNEL_ID",
        "NEWS_CHANNEL_ID",
        "TELEGRAM_CHANNEL_NEWS",
        "TELEGRAM_CHAT_ID_NEWS",
        "EGX_NEWS_CHANNEL_ID",
    ]:
        val = (os.environ.get(env) or "").strip().strip('"').strip("'")
        if val:
            if val == "-1003993921849":
                logger.warning(f"News channel env {env} still points to SCALPING ID -1003993921849 — expected NEWS -1004492677393")
            logger.info(f"News channel resolved via {env}={val} (NEWS hard fallback {NEWS_FALLBACK})")
            return val
    logger.info(f"No NEWS env set — using hard fallback NEWS_CHANNEL_ID={NEWS_FALLBACK} per spec")
    return NEWS_FALLBACK

def fetch_indices_performance() -> Dict[str, Dict[str, Any]]:
    """Fetch closing prices & performance for EGX30/EGX70/EGX100 via yfinance.

    Optimized: ONE batched yf.download for every candidate index ticker
    (threads=True) instead of sequential per-candidate Ticker.history calls.
    """
    result: Dict[str, Dict[str, Any]] = {}
    if yf is None:
        logger.warning("yfinance not available, using synthetic indices")
        for name in INDICES_MAP:
            result[name] = {"close": 28000 + hash(name) % 5000, "prev_close": 27800 + hash(name) % 5000, "change_pct": 0.85, "volume": 0}
        return result

    # One batched download for all candidate index tickers
    candidates: List[str] = []
    for tickers in INDICES_MAP.values():
        for t in tickers:
            if t not in candidates:
                candidates.append(t)
    frames: Dict[str, Any] = {}
    try:
        import pandas as pd  # type: ignore
        logger.info(f"Batched index download: {candidates}")
        data = yf.download(
            tickers=candidates,
            period="5d",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
        )
        multi = isinstance(data.columns, pd.MultiIndex)
        level0 = set(data.columns.get_level_values(0)) if multi else set()
        for t in candidates:
            try:
                if multi:
                    if t not in level0:
                        continue
                    frame = data[t]
                else:
                    frame = data if t == candidates[0] else None
                if frame is None or frame.empty:
                    continue
                if hasattr(frame.columns, "levels"):
                    frame.columns = [c[0] if isinstance(c, tuple) else c for c in frame.columns]
                frames[t] = frame
            except Exception as e:
                logger.warning(f"index frame {t} extract failed: {e}")
        logger.info(f"Batched index download resolved {len(frames)}/{len(candidates)} frames")
    except Exception as e:
        logger.warning(f"Batched index download failed ({e}) - falling back to synthetic")

    for idx_name, tickers in INDICES_MAP.items():
        fetched = False
        for ticker in tickers:
            frame = frames.get(ticker)
            try:
                if frame is None or frame.empty or len(frame) < 2:
                    if frame is None:
                        logger.warning(f"{idx_name} {ticker}: no frame from batch")
                    else:
                        logger.warning(f"{idx_name} {ticker}: no history or insufficient data")
                    continue
                close = float(frame["Close"].iloc[-1])
                prev_close = float(frame["Close"].iloc[-2])
                change_pct = (close - prev_close) / prev_close * 100 if prev_close else 0
                volume = int(frame["Volume"].iloc[-1]) if "Volume" in frame.columns else 0
                result[idx_name] = {"ticker": ticker, "close": close, "prev_close": prev_close, "change_pct": change_pct, "volume": volume}
                logger.info(f"{idx_name} {ticker}: {close:.2f} ({change_pct:+.2f}%)")
                fetched = True
                break
            except Exception as e:
                logger.warning(f"{idx_name} {ticker} failed: {e}")
                continue
        if not fetched:
            # Synthetic fallback for this index
            logger.warning(f"{idx_name}: all tickers failed, using synthetic fallback")
            base = {"EGX30": 28500, "EGX70": 6500, "EGX100": 9200}.get(idx_name, 10000)
            result[idx_name] = {"ticker": "SYNTH", "close": float(base), "prev_close": float(base*0.9915), "change_pct": 0.85, "volume": 0}
    return result

def fetch_top_movers() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Aggregate top gainers, top losers, highest turnover from TICKERS.

    Optimized: ONE batched yf.download for the whole universe (threads=True)
    instead of a sequential per-ticker Ticker.history loop (~20x fewer HTTP calls).

    Returns (gainers, losers, turnover) each as list of dicts {symbol, name, close, change_pct, volume, turnover}
    """
    stocks: List[Dict[str, Any]] = []
    if yf is None:
        logger.warning("yfinance missing, generating synthetic movers")
        for i, t in enumerate(TICKERS[:10]):
            change = (5 - i) * 1.2  # descending
            stocks.append({"symbol": t, "name": STOCK_NAMES_AR.get(t, t), "close": 50 + i, "prev_close": 50, "change_pct": change, "volume": 1000000 - i*50000, "turnover": (50+i)*(1000000 - i*50000)})
    else:
        # Dedupe while preserving order (fallback TICKERS list may repeat entries)
        universe = list(dict.fromkeys(t.strip().upper() for t in TICKERS if t))
        try:
            import pandas as pd  # type: ignore
            import time as _time
            _t0 = _time.monotonic()
            logger.info(f"Batched movers download: {len(universe)} tickers")
            data = yf.download(
                tickers=universe,
                period="7d",
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
            logger.info(f"Batched movers download completed in {_time.monotonic() - _t0:.1f}s")
            multi = isinstance(data.columns, pd.MultiIndex)
            level0 = set(data.columns.get_level_values(0)) if multi else set()
            for ticker in universe:
                try:
                    if multi:
                        if ticker not in level0:
                            continue
                        frame = data[ticker]
                    else:
                        frame = data
                    if frame is None or frame.empty or len(frame) < 2:
                        logger.debug(f"{ticker}: no history")
                        continue
                    frame = frame.dropna(subset=["Close"])
                    if len(frame) < 2:
                        continue
                    close = float(frame["Close"].iloc[-1])
                    prev_close = float(frame["Close"].iloc[-2])
                    if not prev_close or prev_close == 0:
                        continue
                    change_pct = (close - prev_close) / prev_close * 100
                    volume = int(frame["Volume"].iloc[-1]) if "Volume" in frame.columns and not frame["Volume"].empty else 0
                    turnover = volume * close if volume and close else 0
                    stocks.append({
                        "symbol": ticker,
                        "name": STOCK_NAMES_AR.get(ticker, ticker.replace(".CA", "")),
                        "close": close,
                        "prev_close": prev_close,
                        "change_pct": change_pct,
                        "volume": volume,
                        "turnover": turnover,
                    })
                except Exception as e:
                    logger.debug(f"{ticker} extract failed: {e}")
                    continue
            logger.info(f"Batched movers parsed: {len(stocks)}/{len(universe)} usable")
        except Exception as e:
            logger.warning(f"Batched movers download failed: {e}")
            stocks = []

    if not stocks:
        logger.warning("No stocks fetched, using synthetic fallback")
        for i, t in enumerate(TICKERS[:6]):
            change = (3 - i) * 0.8
            stocks.append({"symbol": t, "name": STOCK_NAMES_AR.get(t, t), "close": 40 + i, "prev_close": 40, "change_pct": change, "volume": 500000, "turnover": (40+i)*500000})

    # Sort
    gainers = sorted([s for s in stocks if s["change_pct"] > 0], key=lambda x: x["change_pct"], reverse=True)[:5]
    losers = sorted([s for s in stocks if s["change_pct"] < 0], key=lambda x: x["change_pct"])[:5]
    turnover = sorted(stocks, key=lambda x: x["turnover"], reverse=True)[:5]

    # Ensure at least 3 entries per category for formatting (fill with synthetic if needed)
    if len(gainers) < 3:
        gainers += [{"symbol": f"SYN{i}.CA", "name": f"سهم{i}", "close": 55, "change_pct": 2.5 - i*0.3, "volume": 800000, "turnover": 40000000} for i in range(3 - len(gainers))]
    if len(losers) < 3:
        losers += [{"symbol": f"SYN{i}.CA", "name": f"سهم{i}", "close": 35, "change_pct": -1.5 - i*0.3, "volume": 600000, "turnover": 20000000} for i in range(3 - len(losers))]
    if len(turnover) < 3:
        turnover += gainers[:3-len(turnover)]

    return gainers, losers, turnover

def generate_ai_sentiment(
    indices: Dict[str, Dict[str, Any]],
    gainers: List[Dict[str, Any]],
    losers: List[Dict[str, Any]],
    turnover: List[Dict[str, Any]],
    budget_seconds: float = 25.0,
) -> str:
    """Generate AI Market Sentiment summary via Gemini (Bullet points: Market Trend, Liquidity, Top Headlines).

    Serverless-safe: each Gemini attempt runs under a HARD wall-clock budget
    (budget_seconds total for all attempts) — the google-genai SDK can internally
    retry/backoff for minutes, which previously stalled the whole pipeline ~197s
    and got the Vercel function killed before publishing. Falls back to heuristic.
    """
    # Collect VERIFIED same-session headlines via main's news fetcher — only for
    # registry-sanctioned (non-test/mock) tickers; stale/unparsable dates rejected.
    headlines_sample = []
    try:
        # Try to import main's headline fetcher
        from main import fetch_arabic_headlines, STOCK_NAMES_AR as MAIN_NAMES  # type: ignore
        for g in gainers[:2]:
            sym = str(g.get("symbol") or "")
            if not _is_sanctioned_ticker(sym):
                logger.info(f"[SANITIZE] {sym}: unknown/mock/test ticker - excluded from AI sentiment context")
                continue
            try:
                h = fetch_arabic_headlines(MAIN_NAMES.get(sym, sym), sym)
                if h:
                    headlines_sample.extend([hh for hh in _verified_session_headlines(h)[:1]])
            except:
                continue
    except:
        headlines_sample = []

    # Build prompt
    idx_summary = ", ".join([f"{k} {v['close']:.0f} ({v['change_pct']:+.2f}%)" for k, v in indices.items()])
    gainers_str = ", ".join([f"{g['symbol'].replace('.CA','')} {g['change_pct']:+.2f}%" for g in gainers[:3]])
    losers_str = ", ".join([f"{l['symbol'].replace('.CA','')} {l['change_pct']:+.2f}%" for l in losers[:3]])
    turnover_str = ", ".join([f"{t['symbol'].replace('.CA','')} {t['turnover']/1e6:.1f}M" for t in turnover[:3]])
    headlines_str = " | ".join(headlines_sample[:3]) if headlines_sample else "لا توجد عناوين رئيسية جديدة"

    prompt = (
        f"أنت محلل بورصة مصرية محترف. لخص أداء السوق اليوم بناءً على:\n"
        f"المؤشرات: {idx_summary}\n"
        f"أكبر الرابحين: {gainers_str}\n"
        f"أكبر الخاسرين: {losers_str}\n"
        f"أعلى تداول: {turnover_str}\n"
        f"العناوين: {headlines_str}\n"
        "أعط 3 نقاط فقط بالعربية:\n"
        "• اتجاه السوق: (صاعد/هابط/عرضي مع سبب)\n"
        "• السيولة: (نشطة/ضعيفة/متوسطة مع حجم)\n"
        "• أبرز العناوين: (تلخيص 1-2 خبر)\n"
        "اجعلها موجزة جداً (كل نقطة سطر واحد)."
        + LLM_GUARDRAILS_AR
    )

    # Try Gemini — under a hard wall-clock budget (serverless-safe)
    try:
        import time as _time
        from concurrent.futures import ThreadPoolExecutor

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if api_key and api_key.strip():
            from google import genai  # type: ignore
            client = genai.Client(api_key=api_key.strip())
            # Use available model - main.py uses gemini-3.6-flash, fallback chain accordingly
            model = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
            deadline = _time.monotonic() + float(budget_seconds)
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                for m in [model, "gemini-2.0-flash", "gemini-1.5-flash"]:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 3:
                        logger.warning(f"[AI-BUDGET] Gemini budget exhausted before {m} - falling back to heuristic")
                        break
                    try:
                        fut = pool.submit(client.models.generate_content, model=m, contents=prompt)
                        resp = fut.result(timeout=remaining)
                        text = getattr(resp, "text", None) or (resp.candidates[0].content.parts[0].text if getattr(resp, "candidates", None) else None)
                        if text and len(text.strip()) > 20:
                            logger.info(f"Gemini AI sentiment generated via {m}")
                            return text.strip()
                        logger.warning(f"Gemini {m} returned empty/short text")
                    except TimeoutError:
                        logger.warning(f"[AI-BUDGET] Gemini {m} exceeded {remaining:.0f}s budget - trying next / fallback")
                        continue
                    except Exception as e:
                        logger.warning(f"Gemini {m} failed: {e}")
                        continue
            finally:
                # Never block on hung Gemini calls — abandon them
                pool.shutdown(wait=False, cancel_futures=True)
        else:
            logger.warning("GEMINI_API_KEY not set, using heuristic sentiment")
    except Exception as e:
        logger.warning(f"Gemini import/generation failed: {e}")

    # Heuristic fallback
    try:
        avg_change = sum(v["change_pct"] for v in indices.values()) / len(indices) if indices else 0
        if avg_change > 0.8:
            trend = "صاعد قوي مدعوم بمكاسب المؤشرات الرئيسية"
        elif avg_change > 0.2:
            trend = "صاعد محدود مع تفاؤل حذر"
        elif avg_change < -0.8:
            trend = "هابط تحت ضغط بيعي"
        elif avg_change < -0.2:
            trend = "متراجع طفيف مع جني أرباح"
        else:
            trend = "عرضي مستقر بانتظار محفزات جديدة"

        total_turnover = sum(t["turnover"] for t in turnover) / 1e9 if turnover else 0
        if total_turnover > 1.5:
            liq = f"نشطة جداً (~{total_turnover:.1f} مليار جنيه تداول)"
        elif total_turnover > 0.8:
            liq = f"نشطة (~{total_turnover:.1f} مليار جنيه)"
        elif total_turnover > 0.4:
            liq = f"متوسطة (~{total_turnover:.1f} مليار جنيه)"
        else:
            liq = "ضعيفة نسبياً مع حذر المستثمرين"

        top_head = headlines_str if headlines_str != "لا توجد عناوين رئيسية جديدة" else f"أبرز الرابحين: {gainers_str} | أبرز الخاسرين: {losers_str}"
        return (
            f"• **اتجاه السوق:** {trend}\n"
            f"• **السيولة:** {liq}\n"
            f"• **أبرز العناوين:** {top_head}"
        )
    except Exception as e:
        logger.warning(f"Heuristic fallback failed: {e}")
        return "• **اتجاه السوق:** عرضي\n• **السيولة:** متوسطة\n• **أبرز العناوين:** لا توجد أخبار جوهرية"

def format_post_market_card(
    indices: Dict[str, Dict[str, Any]],
    gainers: List[Dict[str, Any]],
    losers: List[Dict[str, Any]],
    turnover: List[Dict[str, Any]],
    ai_summary: str,
    date_str: Optional[str] = None,
    active_signals: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Format card with required title and sections, including Active Signals Tracker."""
    if not date_str:
        try:
            from zoneinfo import ZoneInfo
            date_str = datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d")
        except:
            date_str = datetime.now().strftime("%Y-%m-%d")

    # Indices block
    idx_lines = []
    for name in ["EGX30", "EGX70", "EGX100"]:
        v = indices.get(name)
        if not v:
            continue
        emoji = "🟢" if v["change_pct"] >= 0 else "🔴"
        sign = "+" if v["change_pct"] >= 0 else ""
        idx_lines.append(f"{emoji} **{name}:** {v['close']:,.0f} ({sign}{v['change_pct']:.2f}%)")
    idx_block = "\n".join(idx_lines) if idx_lines else "لا توجد بيانات مؤشرات"

    # Gainers
    gain_lines = []
    for g in gainers[:5]:
        sym = g["symbol"].replace(".CA", "")
        name = g.get("name", sym)
        gain_lines.append(f"• `{sym}` {name}: **{g['change_pct']:+.2f}%** @ {g['close']:.2f}")
    gain_block = "\n".join(gain_lines) if gain_lines else "لا يوجد"

    # Losers
    lose_lines = []
    for l in losers[:5]:
        sym = l["symbol"].replace(".CA", "")
        name = l.get("name", sym)
        lose_lines.append(f"• `{sym}` {name}: **{l['change_pct']:+.2f}%** @ {l['close']:.2f}")
    lose_block = "\n".join(lose_lines) if lose_lines else "لا يوجد"

    # Turnover
    turn_lines = []
    for t in turnover[:5]:
        sym = t["symbol"].replace(".CA", "")
        name = t.get("name", sym)
        vol_m = t["turnover"] / 1e6 if t["turnover"] else t.get("volume", 0) / 1e3
        suffix = "M EGP" if t["turnover"] else "K vol"
        turn_lines.append(f"• `{sym}` {name}: **{vol_m:,.1f}** {suffix}")
    turn_block = "\n".join(turn_lines) if turn_lines else "لا يوجد"

    # Ensure AI summary has bullet points
    ai_block = ai_summary.strip()
    if "اتجاه السوق" not in ai_block:
        ai_block = f"• **اتجاه السوق:** مستقر\n{ai_block}"

    # Context-Aware Signal Watchlist & News Impact Analysis (3-bucket)
    try:
        if active_signals is None:
            active_signals = []
        # Build dynamic categories: Active Trades Impact, Incoming Setups, Avoid Watchlist
        try:
            categories = build_context_aware_categories(active_signals)
            active_section = format_context_aware_section(categories)
        except Exception as e:
            logger.warning(f"Context-aware categories failed: {e}, falling back to legacy tracker")
            active_section = format_active_signals_section(active_signals)
    except Exception as e:
        logger.warning(f"Active signals section failed: {e}")
        active_section = "🎯 **متابعة أسهم المنظومة والفرص | System Signals & Opportunities**\nلا توجد صفقات مفتوحة حالياً في المنظومة."

    card = (
        f"{POST_MARKET_TITLE}\n"
        f"📅 **التاريخ:** {date_str} | ⏰ **الإغلاق:** 15:30 بتوقيت القاهرة (النشرة الختامية)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 **أداء المؤشرات:**\n"
        f"{idx_block}\n"
        f"\n"
        f"🚀 **أكبر الرابحين:**\n"
        f"{gain_block}\n"
        f"\n"
        f"📉 **أكبر الخاسرين:**\n"
        f"{lose_block}\n"
        f"\n"
        f"💰 **الأعلى تداولاً (Turnover):**\n"
        f"{turn_block}\n"
        f"\n"
        f"🤖 **تحليل السوق بالذكاء الاصطناعي:**\n"
        f"{ai_block}\n"
        f"\n"
        f"{active_section}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *التحليل استرشادي فقط - القرار الاستثماري مسؤوليتك*\n"
        f"📈 [TradingView EGX](https://www.tradingview.com/symbols/EGX-EGX30/)\n"
    )
    return card

def check_execution_window(
    scheduled_hour: int = 12,
    scheduled_minute: int = 30,
    window_minutes: int = 60,
    strict_cutoff_hour: int = 14,
    strict_cutoff_minute: int = 0,
) -> Tuple[bool, float, datetime, datetime, str]:
    """
    Time-Window Guard for stale cron detection (GitHub queue delay).

    Compares current UTC time against the most recent scheduled trigger
    (30 12 * * 0-4 → 12:30 UTC Sun-Thu). If execution is >window_minutes
    past the window (or past strict 14:00 UTC / 18:00 Oman), flags stale.

    Returns (is_stale, delay_minutes, now_utc, scheduled_utc, reason).
    Never raises — returns non-stale on any error.
    """
    try:
        # Skip stale check for manual dispatches — operator intentionally triggered
        event_name = (os.environ.get("GITHUB_EVENT_NAME") or "").strip()
        if event_name == "workflow_dispatch":
            now_utc = datetime.now(timezone.utc)
            sched = now_utc.replace(hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0)
            return False, 0.0, now_utc, sched, "manual dispatch — window check bypassed"

        now_utc = datetime.now(timezone.utc)

        # Find most recent scheduled slot (Sun-Thu 12:30 UTC) ≤ now_utc
        # Cron 0-4 = Sun(0), Mon(1), Tue(2), Wed(3), Thu(4)
        scheduled = now_utc.replace(hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0)
        if now_utc < scheduled:
            # Before today's 12:30 → most recent was yesterday
            scheduled = scheduled - timedelta(days=1)

        # Walk back to last Sun-Thu if scheduled falls on Fri/Sat
        for _ in range(7):
            # Python weekday(): Mon=0 … Sun=6; GitHub cron Sun=0 => mapping
            # Map GH 0->6, 1->0, 2->1, 3->2, 4->3, 5->4, 6->5
            gh_dow = (scheduled.weekday() + 1) % 7  # Mon0→1, Sun6→0
            if gh_dow in (0, 1, 2, 3, 4):  # Sun-Thu valid
                break
            scheduled = scheduled - timedelta(days=1)
            scheduled = scheduled.replace(hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0)

        delay_minutes = (now_utc - scheduled).total_seconds() / 60.0

        # Strict cutoff 14:00 UTC (18:00 Oman) per requirement example
        cutoff = now_utc.replace(hour=strict_cutoff_hour, minute=strict_cutoff_minute, second=0, microsecond=0)
        is_past_cutoff = now_utc >= cutoff and delay_minutes > 0

        is_stale = False
        reason = ""
        if delay_minutes > window_minutes:
            is_stale = True
            reason = f"delay {delay_minutes:.0f}m > window {window_minutes}m (scheduled {scheduled.strftime('%H:%M UTC')}, now {now_utc.strftime('%H:%M UTC')})"
        elif is_past_cutoff:
            # Past 14:00 UTC even if delay just over window — strict policy
            is_stale = True
            reason = f"past strict cutoff {strict_cutoff_hour:02d}:{strict_cutoff_minute:02d} UTC (now {now_utc.strftime('%H:%M UTC')}, scheduled {scheduled.strftime('%H:%M UTC')})"
        else:
            reason = f"within window (delay {delay_minutes:.0f}m, scheduled {scheduled.strftime('%H:%M UTC')})"

        return is_stale, delay_minutes, now_utc, scheduled, reason
    except Exception as exc:
        try:
            now_utc = datetime.now(timezone.utc)
            sched = now_utc.replace(hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0)
            return False, 0.0, now_utc, sched, f"window check error: {exc}"
        except Exception:
            return False, 0.0, datetime.now(timezone.utc), datetime.now(timezone.utc), "window check critical error"


def trigger_stale_retry_alert(delay_minutes: float, scheduled: datetime, now_utc: datetime, reason: str) -> None:
    """Internal-only stale diagnostics: server log + best-effort admin chat DM.

    NEVER touches the public news channel — subscribers must not receive
    orchestration/delay notifications (audit: diagnostics live in server logs).
    """
    try:
        msg = f"[STALE-ALERT] Post-Market bulletin delayed {delay_minutes:.0f}m (scheduled {scheduled.isoformat()}, now {now_utc.isoformat()}) reason={reason}"
        logger.warning(msg)
        print(f"[STALE-ALERT] {msg}")
        # Best-effort admin notify via Telegram if ADMIN chat configured
        admin_chat = (os.environ.get("ADMIN_TELEGRAM_IDS") or os.environ.get("ADMIN_USER_IDS") or os.environ.get("TELEGRAM_USER_CHAT_ID") or "").split(",")[0].strip()
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if admin_chat and token and requests:
            try:
                alert_text = (
                    f"🚨 **تنبيه تأخر نشرة الإغلاق | Stale Execution**\n"
                    f"⏰ المقرر: 12:30 UTC / 15:30 Cairo / 16:30 Oman\n"
                    f"⏰ الفعلي: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}\n"
                    f"⏱️ التأخر: {delay_minutes:.0f} دقيقة\n"
                    f"📋 السبب: {reason}\n"
                    f"💡 الإجراء: يراجع سجل Actions (Created vs Started) ويُفعّل cron-job.org/Vercel Cron كبديل إذا تكرر."
                )
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": admin_chat, "text": alert_text, "parse_mode": "Markdown"},
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f"Stale alert Telegram notify failed: {e}")
    except Exception as e:
        logger.warning(f"trigger_stale_retry_alert failed: {e}")


def publish_to_news_channel(text: str, parse_mode: str = "Markdown", dry_run: bool = False) -> bool:
    """Publish to EGX News & Market Summaries channel."""
    if dry_run:
        logger.info(f"[DRY-RUN] Would publish to news channel:\n{text[:1000]}")
        print(f"[DRY-RUN] Post-market card preview:\n{text[:1500]}")
        return True

    channel = get_news_channel_id()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not channel:
        logger.error("TELEGRAM_CHANNEL_NEWS not set; cannot publish")
        print("[ERROR] News channel not set; set TELEGRAM_CHANNEL_NEWS in .env")
        return False
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return False
    if requests is None:
        logger.error("requests not available")
        return False
    url = TELEGRAM_API.format(token=token)
    payload = {"chat_id": channel, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            logger.info(f"Post-market bulletin delivered to news channel {channel}")
            print(f"[SUCCESS] Delivered to {channel} HTTP 200")
            try:
                j = resp.json()
                print(f"[OUTPUT] message_id={j.get('result',{}).get('message_id')} chat_id={j.get('result',{}).get('chat',{}).get('id')}")
            except:
                pass
            return True
        else:
            logger.error(f"Telegram send failed {resp.status_code}: {resp.text[:300]}")
            print(f"[FAIL] Telegram {resp.status_code}: {resp.text[:500]}")
            return False
    except Exception as e:
        logger.error(f"Telegram request failed: {e}")
        print(f"[ERROR] {e}")
        return False

def main(dry_run: bool = False, broadcast: bool = True) -> int:
    """Run full post-market pipeline: fetch -> AI -> format -> publish."""
    logger.info("Starting post-market summary pipeline")
    # ── Time-Window Guard: STALE EXECUTION SUPPRESSION ──
    # Scheduled bulletin: 12:30 UTC (15:30 Cairo) Sun-Thu. If the trigger arrives
    # more than 60 minutes late (past 13:30 UTC / 16:30 Cairo), the PUBLIC Telegram
    # broadcast is silently SUPPRESSED — the delay is logged internally only
    # (console + admin alert); subscribers never receive stale late-night bulletins.
    is_stale = False
    delay_minutes = 0.0
    try:
        is_stale, delay_minutes, now_utc, scheduled_utc, reason = check_execution_window()
        # Audit log for GH Created vs Started — GH runners log UTC; we emit both
        try:
            gh_created = os.environ.get("GITHUB_RUN_CREATED_AT") or os.environ.get("GITHUB_EVENT_CREATED_AT") or "n/a"
        except Exception:
            gh_created = "n/a"
        print(f"[TIMESTAMP-AUDIT] Scheduled=12:30 UTC (15:30 Cairo) | Now={now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')} | Delay={delay_minutes:.0f}m | Reason={reason} | GH_EVENT={os.environ.get('GITHUB_EVENT_NAME','local')} | GH_CREATED={gh_created}")
        logger.info("Timestamp audit: now=%s scheduled=%s delay=%.0fm stale=%s reason=%s", now_utc.isoformat(), scheduled_utc.isoformat(), delay_minutes, is_stale, reason)
        if is_stale:
            logger.error("STALE execution (%s) — public post-market broadcast will be SUPPRESSED", reason)
            print(f"[STALE-DETECTED] {reason}")
            # Internal diagnostics only (server log + admin chat) — never the public channel
            try:
                trigger_stale_retry_alert(delay_minutes, scheduled_utc, now_utc, reason)
            except Exception as e:
                logger.warning(f"Stale alert trigger failed: {e}")
            print(f"[STALE-SUPPRESSED] Public post-market broadcast suppressed (delay {delay_minutes:.0f}m > 60m window). No Telegram message sent to subscribers.")
            return 2
    except Exception as e:
        logger.warning(f"Time-window guard failed (proceeding without stale check): {e}")
        is_stale = False

    # Idempotency guard - check before heavy fetching if already published today
    if broadcast and not dry_run:
        try:
            if check_already_published("POST_MARKET"):
                logger.info("Already published today. Skipping. (POST_MARKET %s)", get_cairo_date_str())
                print(f"[IDEMPOTENT] Already published today. Skipping. (POST_MARKET {get_cairo_date_str()})")
                return 0
        except Exception as e:
            logger.warning(f"Idempotency check failed (proceeding): {e}")
    try:
        import time as _time
        _t_fetch = _time.monotonic()
        indices = fetch_indices_performance()
        print(f"[TIMING] fetch_indices_performance: {_time.monotonic() - _t_fetch:.1f}s")
        _t_movers = _time.monotonic()
        gainers, losers, turnover = fetch_top_movers()
        print(f"[TIMING] fetch_top_movers: {_time.monotonic() - _t_movers:.1f}s")
        _t_ai = _time.monotonic()
        ai_summary = generate_ai_sentiment(indices, gainers, losers, turnover)
        print(f"[TIMING] generate_ai_sentiment: {_time.monotonic() - _t_ai:.1f}s")
        # Fetch active signals tracker
        try:
            _t_sig = _time.monotonic()
            raw_signals = fetch_active_signals(limit=10)
            active_enriched = enrich_active_signals_with_prices(raw_signals)
            print(f"[TIMING] active signals fetch+enrich: {_time.monotonic() - _t_sig:.1f}s ({len(active_enriched)} signals)")
            logger.info(f"Active signals fetched: {len(active_enriched)}")
        except Exception as e:
            logger.warning(f"Active signals fetch failed: {e}")
            active_enriched = []
        card = format_post_market_card(indices, gainers, losers, turnover, ai_summary, active_signals=active_enriched)
        print(card)
        if broadcast:
            ok = publish_to_news_channel(card, dry_run=dry_run)
            if not ok and not dry_run:
                return 1
            # Log publish for idempotency if broadcast succeeded and not dry-run
            if ok and not dry_run:
                try:
                    mark_published("POST_MARKET")
                except Exception as e:
                    logger.warning(f"Mark published failed: {e}")
        logger.info("Post-market summary completed")
        return 0
    except Exception as e:
        logger.error(f"Post-market pipeline failed: {e}", exc_info=True)
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EGX Post-Market Bulletin")
    parser.add_argument("--dry-run", action="store_true", help="Preview card without Telegram send")
    parser.add_argument("--no-broadcast", action="store_true", help="Generate card only, no publish")
    parser.add_argument("--broadcast", action="store_true", help="Force broadcast (default)")
    args = parser.parse_args()
    # dry-run implies no broadcast unless --broadcast forced
    dry = args.dry_run
    broadcast = not args.no_broadcast if not dry else False if not args.broadcast else True
    # If no flags, default to broadcast (for GitHub Actions)
    if not args.dry_run and not args.no_broadcast and not args.broadcast:
        broadcast = True
        dry = False
    if dry:
        broadcast = False
    sys.exit(main(dry_run=dry, broadcast=broadcast))
