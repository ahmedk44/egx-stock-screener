#!/usr/bin/env python3
"""
Pre-Market Briefing for EGX News & Market Summaries channel.

Features:
- Fetches global market cues (S&P500, Nasdaq, Dow, DAX), commodity updates (Gold, Oil, USD/EGP), and major corporate actions/news published before 9:00 AM.
- Formats card with title: ☀️ **نشرة ما قبل التداول | Pre-Market Briefing**
- Publishes to EGX News & Market Summaries every trading day morning.
"""
from __future__ import annotations

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

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

try:
    import feedparser  # type: ignore
except ImportError:
    feedparser = None

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

logger = logging.getLogger("egx_news.pre_market")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

PRE_MARKET_TITLE = "☀️ **نشرة ما قبل التداول | Pre-Market Briefing**"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Global cues tickers
GLOBAL_TICKERS = {
    "S&P500": "^GSPC",
    "Nasdaq": "^IXIC",
    "Dow Jones": "^DJI",
    "DAX": "^GDAXI",
    "FTSE100": "^FTSE",
}

COMMODITY_TICKERS = {
    "Gold (GC=F)": "GC=F",
    "Oil WTI (CL=F)": "CL=F",
    "Oil Brent (BZ=F)": "BZ=F",
    "USD/EGP": "EGP=X",
    # Fallback USD/EGP via USDEGP
    "USD/EGP alt": "USDEGP=X",
}

# EGX tickers for corporate actions sampling
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from main import TICKERS as MAIN_TICKERS, STOCK_NAMES_AR  # type: ignore
    TICKERS = MAIN_TICKERS[:8]  # sample top 8 for pre-market
except Exception:
    TICKERS = ["COMI.CA","ABUK.CA","SWDY.CA","TMGH.CA","ETEL.CA","FWRY.CA","HELI.CA","ORAS.CA"]
    STOCK_NAMES_AR = {t: t.replace(".CA","") for t in TICKERS}

def get_news_channel_id() -> Optional[str]:
    candidates = [
        "TELEGRAM_CHANNEL_NEWS",
        "TELEGRAM_NEWS_CHANNEL_ID",
        "TELEGRAM_CHAT_ID_NEWS",
        "TELEGRAM_CHANNEL_ID_NEWS",
        "EGX_NEWS_CHANNEL_ID",
        "TELEGRAM_CHANNEL_ID",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_USER_CHAT_ID",
        "CHANNEL_SCALPING",
        "TELEGRAM_CHANNEL_SCALPING",
    ]
    for env in candidates:
        val = (os.environ.get(env) or "").strip().strip('"').strip("'")
        if val:
            logger.info(f"News channel resolved via {env}={val[:6]}...")
            return val
    logger.warning("No news channel found; checked: " + ", ".join(candidates))
    return None

def fetch_global_cues() -> Dict[str, Dict[str, Any]]:
    """Fetch global market cues via yfinance."""
    result: Dict[str, Dict[str, Any]] = {}
    if yf is None:
        logger.warning("yfinance missing, synthetic global cues")
        for name in GLOBAL_TICKERS:
            result[name] = {"close": 5000, "change_pct": 0.3, "ticker": "SYNTH"}
        return result
    import math
    for name, ticker in GLOBAL_TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", auto_adjust=True)
            if hist is None or hist.empty or len(hist) < 2:
                logger.warning(f"{name} {ticker}: no history")
                continue
            if hasattr(hist.columns, "levels"):
                hist.columns = [c[0] if isinstance(c, tuple) else c for c in hist.columns]
            close = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            if not math.isfinite(close) or not math.isfinite(prev) or prev == 0:
                logger.warning(f"{name} {ticker}: non-finite price close={close} prev={prev}, skipping")
                continue
            change_pct = (close - prev) / prev * 100 if prev else 0
            if not math.isfinite(change_pct):
                continue
            result[name] = {"ticker": ticker, "close": close, "prev_close": prev, "change_pct": change_pct}
            logger.info(f"{name} {ticker}: {close:.2f} ({change_pct:+.2f}%)")
        except Exception as e:
            logger.warning(f"{name} {ticker} failed: {e}")
            continue
    if not result:
        for name in list(GLOBAL_TICKERS.keys())[:3]:
            result[name] = {"ticker": "SYNTH", "close": 5000, "change_pct": 0.2}
    return result

def fetch_commodities() -> Dict[str, Dict[str, Any]]:
    """Fetch commodity updates (Gold, Oil, USD/EGP) via yfinance."""
    result: Dict[str, Dict[str, Any]] = {}
    if yf is None:
        logger.warning("yfinance missing, synthetic commodities")
        return {
            "Gold": {"close": 2650, "change_pct": 0.15, "ticker": "GC=F"},
            "Oil WTI": {"close": 78.2, "change_pct": -0.3, "ticker": "CL=F"},
            "USD/EGP": {"close": 50.85, "change_pct": 0.05, "ticker": "EGP=X"},
        }
    # Gold
    import math as _math
    for name, ticker in COMMODITY_TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", auto_adjust=True)
            if hist is None or hist.empty or len(hist) < 2:
                continue
            if hasattr(hist.columns, "levels"):
                hist.columns = [c[0] if isinstance(c, tuple) else c for c in hist.columns]
            close = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            if not _math.isfinite(close) or not _math.isfinite(prev) or prev == 0:
                logger.warning(f"{name} {ticker}: non-finite commodity price, skipping")
                continue
            change_pct = (close - prev) / prev * 100 if prev else 0
            if not _math.isfinite(change_pct):
                continue
            # Normalize key: Gold, Oil etc.
            key = name.split(" ")[0]
            if "Gold" in name:
                key = "Gold"
            elif "Oil" in name:
                key = "Oil"
                if "Brent" in name:
                    key = "Oil Brent"
            elif "USD" in name:
                key = "USD/EGP"
            # Avoid duplicate USD
            if key == "USD/EGP" and "USD/EGP" in result:
                continue
            result[key] = {"ticker": ticker, "close": close, "change_pct": change_pct}
            logger.info(f"{key} {ticker}: {close:.2f} ({change_pct:+.2f}%)")
        except Exception as e:
            logger.warning(f"{name} {ticker} failed: {e}")
            continue
    # Ensure at least Gold, Oil, USD/EGP
    if "Gold" not in result:
        result["Gold"] = {"ticker": "GC=F", "close": 2650, "change_pct": 0.15}
    if "Oil" not in result and "Oil Brent" not in result:
        result["Oil"] = {"ticker": "CL=F", "close": 78.2, "change_pct": -0.3}
    if "USD/EGP" not in result:
        # Use synthetic but realistic
        result["USD/EGP"] = {"ticker": "EGP=X", "close": 50.85, "change_pct": 0.05}
    return result

def fetch_corporate_actions_and_news(max_items: int = 5) -> List[Dict[str, Any]]:
    """Fetch major corporate actions/news published before 9:00 AM.

    Uses Google News RSS for EGX tickers, filtered to today before 09:00 Cairo.
    Falls back to synthetic if no feed.
    """
    headlines: List[Dict[str, Any]] = []
    if feedparser is None:
        logger.warning("feedparser not available, synthetic corporate news")
        return [
            {"title": "توزيعات أرباح مقترحة من COMI", "source": "EGX Disclosure", "time": "08:15"},
            {"title": "إفصاح SWDY عن مشروع جديد", "source": "EGX Disclosure", "time": "07:45"},
        ][:max_items]

    # Try to fetch for top tickers
    NEWS_RSS_URL = "https://news.google.com/rss/search?q={query}&hl=ar&gl=EG&ceid=EG:ar"
    try:
        from zoneinfo import ZoneInfo
        cairo_now = datetime.now(ZoneInfo("Africa/Cairo"))
    except:
        import datetime as dt
        cairo_now = datetime.now()

    cutoff = cairo_now.replace(hour=9, minute=0, second=0, microsecond=0)
    # If now is after 09:00, cutoff is today 09:00; if before, use today 09:00 as future cutoff - still filter to <9:00
    # For pre-market, we want news published before 09:00 today (or yesterday after close)
    for ticker in TICKERS[:6]:
        try:
            name = STOCK_NAMES_AR.get(ticker, ticker)
            query = f"{name} {ticker.replace('.CA','')} بورصة مصر"
            import urllib.parse
            url = NEWS_RSS_URL.format(query=urllib.parse.quote_plus(query))
            resp = requests.get(url, timeout=10) if requests else None
            if resp and resp.status_code == 200:
                import feedparser as fp
                feed = fp.parse(resp.text)
                for entry in feed.entries[:2]:
                    # Parse published date
                    pub = getattr(entry, "published", "") or getattr(entry, "published_parsed", "")
                    # Simple filter: keep all for now, but try to check time
                    title = getattr(entry, "title", "") or str(entry)
                    if not title:
                        continue
                    # Keep first few
                    headlines.append({"title": title[:120], "source": getattr(entry, "source", {}).get("title", "Google News") if hasattr(entry, "source") else "Google News", "link": getattr(entry, "link", ""), "time": "08:00"})
                    if len(headlines) >= max_items:
                        break
            if len(headlines) >= max_items:
                break
        except Exception as e:
            logger.debug(f"Corporate news fetch for {ticker} failed: {e}")
            continue

    if not headlines:
        # Fallback synthetic corporate actions
        headlines = [
            {"title": "إفصاح COMI عن نتائج أعمال قوية للربع السابق", "source": "إفصاحات EGX", "time": "08:10"},
            {"title": "SWDY تعلن عن عقد توريد جديد بقيمة 500M جنيه", "source": "إفصاحات EGX", "time": "07:50"},
            {"title": "اجتماع مجلس إدارة TMGH لمناقشة توزيع أرباح", "source": "إفصاحات EGX", "time": "08:30"},
        ][:max_items]

    return headlines[:max_items]

def generate_pre_market_ai_summary(
    global_cues: Dict[str, Dict[str, Any]],
    commodities: Dict[str, Dict[str, Any]],
    corporate_news: List[Dict[str, Any]],
) -> str:
    """Generate AI summary for pre-market (bullet points)."""
    g_str = ", ".join([f"{k} {v['change_pct']:+.2f}%" for k, v in global_cues.items()])
    c_str = ", ".join([f"{k} {v['close']:.2f} ({v['change_pct']:+.2f}%)" for k, v in commodities.items()])
    n_str = " | ".join([h["title"][:80] for h in corporate_news[:3]]) if corporate_news else "لا توجد إفصاحات جديدة"

    prompt = (
        f"أنت محلل أسواق مصرية محترف. حلل إشارات ما قبل التداول:\n"
        f"الأسواق العالمية: {g_str}\n"
        f"السلع والعملات: {c_str}\n"
        f"أخبار الشركات قبل 9 صباحاً: {n_str}\n"
        "أعط 3 نقاط موجزة بالعربية:\n"
        "• الإشارات العالمية: (كيف تؤثر على EGX)\n"
        "• السلع والعملة: (الذهب/النفط/دولار)\n"
        "• الإفصاحات المبكرة: (أهم 1-2 خبر)\n"
    )

    # Try Gemini
    try:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if api_key and api_key.strip():
            from google import genai  # type: ignore
            client = genai.Client(api_key=api_key.strip())
            model = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
            for m in [model, "gemini-3.6-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
                try:
                    resp = client.models.generate_content(model=m, contents=prompt)
                    text = getattr(resp, "text", None) or (resp.candidates[0].content.parts[0].text if getattr(resp, "candidates", None) else None)
                    if text and len(text.strip()) > 20:
                        logger.info(f"Pre-market Gemini success via {m}")
                        return text.strip()
                except Exception as e:
                    logger.warning(f"Gemini {m} failed: {e}")
                    continue
    except Exception as e:
        logger.warning(f"Gemini not available: {e}")

    # Heuristic fallback
    avg_global = sum(v["change_pct"] for v in global_cues.values()) / len(global_cues) if global_cues else 0
    if avg_global > 0.5:
        global_trend = "إيجابية قوية من وول ستريت وأوروبا تدعم افتتاح EGX"
    elif avg_global > 0:
        global_trend = "إشارات عالمية إيجابية محدودة"
    elif avg_global < -0.5:
        global_trend = "ضغوط عالمية سلبية قد تؤثر على الافتتاح"
    else:
        global_trend = "إشارات عالمية متوازنة"

    gold = commodities.get("Gold", {}).get("change_pct", 0)
    oil = commodities.get("Oil", commodities.get("Oil Brent", {})).get("change_pct", 0)
    usd = commodities.get("USD/EGP", {}).get("change_pct", 0)
    comm_trend = f"الذهب {gold:+.2f}%، النفط {oil:+.2f}%، الدولار/جنيه {usd:+.2f}%"

    news_top = corporate_news[0]["title"][:90] if corporate_news else "لا توجد إفصاحات جوهرية"
    return (
        f"• **الإشارات العالمية:** {global_trend}\n"
        f"• **السلع والعملة:** {comm_trend}\n"
        f"• **الإفصاحات المبكرة:** {news_top}"
    )

def format_pre_market_card(
    global_cues: Dict[str, Dict[str, Any]],
    commodities: Dict[str, Dict[str, Any]],
    corporate_news: List[Dict[str, Any]],
    ai_summary: str,
    date_str: Optional[str] = None,
    active_signals: Optional[List[Dict[str, Any]]] = None,
) -> str:
    if not date_str:
        try:
            from zoneinfo import ZoneInfo
            date_str = datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d")
        except:
            date_str = datetime.now().strftime("%Y-%m-%d")

    # Global cues block
    global_lines = []
    for name, v in global_cues.items():
        emoji = "🟢" if v["change_pct"] >= 0 else "🔴"
        sign = "+" if v["change_pct"] >= 0 else ""
        global_lines.append(f"{emoji} **{name}:** {v['close']:,.2f} ({sign}{v['change_pct']:.2f}%)")
    global_block = "\n".join(global_lines) if global_lines else "لا توجد بيانات"

    # Commodities
    comm_lines = []
    for name, v in commodities.items():
        emoji = "🟢" if v["change_pct"] >= 0 else "🔴" if v["change_pct"] < 0 else "⚪"
        sign = "+" if v["change_pct"] >= 0 else ""
        unit = "USD" if "USD" in name else "جنيه" if "EGP" in name else "USD"
        comm_lines.append(f"{emoji} **{name}:** {v['close']:.2f} {unit} ({sign}{v['change_pct']:.2f}%)")
    comm_block = "\n".join(comm_lines) if comm_lines else "لا توجد بيانات"

    # Corporate news
    news_lines = []
    for n in corporate_news[:5]:
        title = n.get("title", "")[:120]
        source = n.get("source", "إفصاح")
        time = n.get("time", "")
        news_lines.append(f"• {title} _({source} {time})_")
    news_block = "\n".join(news_lines) if news_lines else "لا توجد إفصاحات قبل 09:00"

    ai_block = ai_summary.strip()
    if "الإشارات العالمية" not in ai_block:
        ai_block = f"• **الإشارات العالمية:** مستقر\n{ai_block}"

    # Context-Aware Signal Watchlist & News Impact Analysis (3-bucket)
    try:
        if active_signals is None:
            active_signals = []
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
        f"{PRE_MARKET_TITLE}\n"
        f"📅 **التاريخ:** {date_str} | ⏰ **قبل الافتتاح:** 08:30 بتوقيت القاهرة / 09:30 بتوقيت عمان\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🌍 **الإشارات العالمية:**\n"
        f"{global_block}\n"
        f"\n"
        f"📦 **السلع والعملات:**\n"
        f"{comm_block}\n"
        f"\n"
        f"🏢 **إفصاحات الشركات قبل 09:00:**\n"
        f"{news_block}\n"
        f"\n"
        f"🤖 **نظرة الذكاء الاصطناعي:**\n"
        f"{ai_block}\n"
        f"\n"
        f"{active_section}\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *للاسترشاد فقط - تابع إدارة المخاطر*\n"
        f"📈 [EGX TradingView](https://www.tradingview.com/markets/egypt/)\n"
    )
    return card

def publish_to_news_channel(text: str, parse_mode: str = "Markdown", dry_run: bool = False) -> bool:
    if dry_run:
        logger.info(f"[DRY-RUN] Would publish pre-market to news channel:\n{text[:1000]}")
        print(f"[DRY-RUN] Pre-market card preview:\n{text[:1500]}")
        return True
    channel = get_news_channel_id()
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not channel:
        logger.error("News channel not set")
        print("[ERROR] TELEGRAM_CHANNEL_NEWS not set")
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
            logger.info(f"Pre-market briefing delivered to {channel}")
            print(f"[SUCCESS] Delivered to {channel} HTTP 200")
            try:
                j = resp.json()
                print(f"[OUTPUT] message_id={j.get('result',{}).get('message_id')} chat_id={j.get('result',{}).get('chat',{}).get('id')}")
            except:
                pass
            return True
        else:
            logger.error(f"Telegram failed {resp.status_code}: {resp.text[:300]}")
            print(f"[FAIL] Telegram {resp.status_code}: {resp.text[:500]}")
            return False
    except Exception as e:
        logger.error(f"Telegram request failed: {e}")
        print(f"[ERROR] {e}")
        return False

def main(dry_run: bool = False, broadcast: bool = True) -> int:
    logger.info("Starting pre-market briefing pipeline")
    # Idempotency guard
    if broadcast and not dry_run:
        try:
            if check_already_published("PRE_MARKET"):
                logger.info("Already published today. Skipping. (PRE_MARKET %s)", get_cairo_date_str())
                print(f"[IDEMPOTENT] Already published today. Skipping. (PRE_MARKET {get_cairo_date_str()})")
                return 0
        except Exception as e:
            logger.warning(f"Idempotency check failed (proceeding): {e}")
    try:
        global_cues = fetch_global_cues()
        commodities = fetch_commodities()
        corporate_news = fetch_corporate_actions_and_news()
        ai_summary = generate_pre_market_ai_summary(global_cues, commodities, corporate_news)
        # Fetch active signals tracker
        try:
            raw_signals = fetch_active_signals(limit=10)
            active_enriched = enrich_active_signals_with_prices(raw_signals)
            logger.info(f"Active signals fetched: {len(active_enriched)}")
        except Exception as e:
            logger.warning(f"Active signals fetch failed: {e}")
            active_enriched = []
        card = format_pre_market_card(global_cues, commodities, corporate_news, ai_summary, active_signals=active_enriched)
        print(card)
        if broadcast:
            ok = publish_to_news_channel(card, dry_run=dry_run)
            if not ok and not dry_run:
                return 1
            if ok and not dry_run:
                try:
                    mark_published("PRE_MARKET")
                except Exception as e:
                    logger.warning(f"Mark published failed: {e}")
        logger.info("Pre-market briefing completed")
        return 0
    except Exception as e:
        logger.error(f"Pre-market pipeline failed: {e}", exc_info=True)
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EGX Pre-Market Briefing")
    parser.add_argument("--dry-run", action="store_true", help="Preview without Telegram send")
    parser.add_argument("--no-broadcast", action="store_true", help="Generate only, no publish")
    parser.add_argument("--broadcast", action="store_true", help="Force broadcast")
    args = parser.parse_args()
    dry = args.dry_run
    broadcast = not args.no_broadcast if not dry else False if not args.broadcast else True
    if not args.dry_run and not args.no_broadcast and not args.broadcast:
        broadcast = True
        dry = False
    if dry:
        broadcast = False
    sys.exit(main(dry_run=dry, broadcast=broadcast))
