"""Shared utilities for EGX News bulletins - idempotency + active signals tracker."""
from __future__ import annotations

import os
import sys
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

logger = logging.getLogger("egx_news.common")

NEWS_PUBLISH_LOG_TABLE = "news_publish_log"

# Local fallback file when Supabase table missing (PGRST205)
LOCAL_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "news_publish_log.json")

def get_supabase_config() -> Optional[Tuple[str, str]]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    service_role = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    fallback_key = (os.environ.get("SUPABASE_KEY") or "").strip()
    # SERVICE_ROLE_KEY always wins: the SUPABASE_KEY fallback is frequently the
    # ANON key, whose RLS policies mask rows as empty in production reads.
    if not service_role and fallback_key:
        logger.warning(
            "[SUPABASE][ENV AUDIT] SUPABASE_SERVICE_ROLE_KEY missing - falling back to SUPABASE_KEY "
            "(possibly ANON key: RLS may mask rows as empty / break idempotency checks)."
        )
    key = (service_role or fallback_key).strip('"').strip("'")
    if not url or not key:
        return None
    return url, key

def get_cairo_date_str() -> str:
    """Return current date YYYY-MM-DD in Africa/Cairo timezone."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Africa/Cairo"))
        return now.strftime("%Y-%m-%d")
    except:
        # Fallback UTC+3
        from datetime import timezone, timedelta
        cairo = timezone(timedelta(hours=3))
        return datetime.now(cairo).strftime("%Y-%m-%d")

def _read_local_log() -> List[Dict[str, Any]]:
    try:
        if os.path.exists(LOCAL_LOG_PATH):
            with open(LOCAL_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except:
        pass
    return []

def _write_local_log(bulletin_type: str, publish_date: str) -> None:
    try:
        logs = _read_local_log()
        logs.append({"bulletin_type": bulletin_type, "publish_date": publish_date, "created_at": datetime.now().isoformat()})
        with open(LOCAL_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
        logger.info(f"[LOCAL LOG] Wrote {bulletin_type} {publish_date} to {LOCAL_LOG_PATH}")
    except Exception as e:
        logger.warning(f"Local log write failed: {e}")

def check_already_published(bulletin_type: str) -> bool:
    """Check news_publish_log for bulletin_type AND publish_date = CURRENT_DATE.

    Returns True if already published (should skip), False otherwise.
    Handles missing table gracefully (PGRST205 -> check local fallback).
    """
    publish_date = get_cairo_date_str()
    cfg = get_supabase_config()
    if requests is None or cfg is None:
        # No Supabase, check local fallback
        logs = _read_local_log()
        for r in logs:
            if r.get("bulletin_type") == bulletin_type and r.get("publish_date") == publish_date:
                logger.info(f"[IDEMPOTENT] Already published (local) {bulletin_type} {publish_date} - skipping")
                print(f"[IDEMPOTENT] Already published today. Skipping. ({bulletin_type} {publish_date})")
                return True
        return False

    url, key = cfg
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    endpoint = f"{url}/rest/v1/{NEWS_PUBLISH_LOG_TABLE}?bulletin_type=eq.{bulletin_type}&publish_date=eq.{publish_date}&select=*&limit=1"
    try:
        resp = requests.get(endpoint, headers=headers, timeout=10)
        if resp.status_code == 200:
            try:
                rows = resp.json()
                if isinstance(rows, list) and len(rows) > 0:
                    logger.info(f"[IDEMPOTENT] Already published (Supabase) {bulletin_type} {publish_date} - skipping")
                    print(f"[IDEMPOTENT] Already published today. Skipping. ({bulletin_type} {publish_date})")
                    return True
                # Also check local fallback (in case Supabase table exists but local also has record)
                logs = _read_local_log()
                for r in logs:
                    if r.get("bulletin_type") == bulletin_type and r.get("publish_date") == publish_date:
                        logger.info(f"[IDEMPOTENT] Already published (local) {bulletin_type} {publish_date}")
                        print(f"[IDEMPOTENT] Already published today. Skipping. ({bulletin_type} {publish_date})")
                        return True
                return False
            except Exception as e:
                logger.warning(f"[IDEMPOTENT] Parse failed: {e} body={resp.text[:200]}")
                return False
        elif resp.status_code == 404 and "PGRST205" in (resp.text or ""):
            # Table not found - fallback to local log
            logger.warning(f"[IDEMPOTENT] Table {NEWS_PUBLISH_LOG_TABLE} not found (PGRST205) - using local fallback. Run supabase_setup_news_log.sql to create table.")
            print(f"[WARN] news_publish_log table not found (PGRST205) - using local idempotency. Please run supabase_setup_news_log.sql in Supabase SQL Editor.")
            logs = _read_local_log()
            for r in logs:
                if r.get("bulletin_type") == bulletin_type and r.get("publish_date") == publish_date:
                    print(f"[IDEMPOTENT] Already published today. Skipping. ({bulletin_type} {publish_date}) (local)")
                    return True
            return False
        else:
            body = (resp.text or "")[:300]
            logger.warning(f"[IDEMPOTENT] Check failed HTTP {resp.status_code}: {body}")
            # On error, do not block publishing (fail-open) but log
            return False
    except Exception as e:
        logger.warning(f"[IDEMPOTENT] Check exception: {e}")
        return False

def mark_published(bulletin_type: str) -> bool:
    """Write log record after successful broadcast. Returns True on success (or fallback).

    Idempotency: prefers an upsert on (bulletin_type, publish_date) so concurrent
    triggers (Vercel cron + GHA fallback) can never create duplicate publish rows.
    Falls back to check-then-insert when no unique constraint exists (HTTP 400).
    """
    publish_date = get_cairo_date_str()
    cfg = get_supabase_config()
    if requests is None or cfg is None:
        _write_local_log(bulletin_type, publish_date)
        return True
    url, key = cfg
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=representation"}
    payload = {
        "bulletin_type": bulletin_type,
        "publish_date": publish_date,
        # created_at auto-generated, but we include for explicitness
        "created_at": datetime.now().isoformat(),
    }
    endpoint = f"{url}/rest/v1/{NEWS_PUBLISH_LOG_TABLE}"
    # 1) Idempotent upsert: merge-duplicates on (bulletin_type, publish_date)
    try:
        upsert_headers = dict(headers)
        upsert_headers["Prefer"] = "resolution=merge-duplicates,return=representation"
        resp = requests.post(
            f"{endpoint}?on_conflict=bulletin_type,publish_date",
            json=payload,
            headers=upsert_headers,
            timeout=10,
        )
        if resp.status_code in (200, 201, 204):
            logger.info(f"[IDEMPOTENT] Marked published {bulletin_type} {publish_date} (Supabase upsert)")
            print(f"[LOG] Marked {bulletin_type} published for {publish_date} (upsert)")
            _write_local_log(bulletin_type, publish_date)
            return True
        if resp.status_code == 400:
            # No unique constraint matching on_conflict -> check-then-insert
            if not check_already_published(bulletin_type):
                return _insert_publish_row(endpoint, headers, payload, bulletin_type, publish_date)
            logger.info(f"[IDEMPOTENT] {bulletin_type} {publish_date} row already exists - insert skipped")
            _write_local_log(bulletin_type, publish_date)
            return True
    except Exception as e:
        logger.warning(f"[IDEMPOTENT] Upsert mark exception: {e}")
    # 2) Fallback: plain insert (409 = already exists, treat as success)
    return _insert_publish_row(endpoint, headers, payload, bulletin_type, publish_date)


def _insert_publish_row(endpoint: str, headers: Dict[str, str], payload: Dict[str, Any], bulletin_type: str, publish_date: str) -> bool:
    """Plain insert of a publish-log row; 409 treated as already-marked success."""
    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201, 204):
            logger.info(f"[IDEMPOTENT] Marked published {bulletin_type} {publish_date} (Supabase insert)")
            print(f"[LOG] Marked {bulletin_type} published for {publish_date}")
            _write_local_log(bulletin_type, publish_date)
            return True
        if resp.status_code == 409:
            logger.info(f"[IDEMPOTENT] {bulletin_type} {publish_date} already logged (409) - ok")
            _write_local_log(bulletin_type, publish_date)
            return True
        if resp.status_code == 404 and "PGRST205" in (resp.text or ""):
            logger.warning(f"[IDEMPOTENT] Table {NEWS_PUBLISH_LOG_TABLE} not found - using local log")
            print(f"[WARN] news_publish_log table missing - using local fallback. Run supabase_setup_news_log.sql.")
            _write_local_log(bulletin_type, publish_date)
            return True
        body = (resp.text or "")[:500]
        logger.warning(f"[IDEMPOTENT] Mark failed HTTP {resp.status_code}: {body}")
        _write_local_log(bulletin_type, publish_date)
        # Broadcast already succeeded; log failure must not fail the pipeline
        return False
    except Exception as e:
        logger.warning(f"[IDEMPOTENT] Mark exception: {e}")
        _write_local_log(bulletin_type, publish_date)
        return False

def fetch_active_signals(limit: int = 10) -> List[Dict[str, Any]]:
    """Query public.trade_signals for active signals (status in TRACKING/ACTIVE/OPEN).

    Handles missing status column (PGRST204) by falling back to recent rows ordered by created_at.
    Returns list of signal dicts.
    """
    cfg = get_supabase_config()
    if requests is None or cfg is None:
        logger.info("No Supabase config - no active signals (return empty)")
        return []
    url, key = cfg
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    # Try with status filter first
    for status_query in [
        "status=in.(TRACKING,ACTIVE,OPEN)",
        "status=in.(TRACKING,ACTIVE)",
        None,  # fallback no status filter
    ]:
        try:
            if status_query:
                endpoint = f"{url}/rest/v1/trade_signals?{status_query}&order=created_at.desc&limit={limit}&select=*"
            else:
                endpoint = f"{url}/rest/v1/trade_signals?order=created_at.desc&limit={limit}&select=*"
            resp = requests.get(endpoint, headers=headers, timeout=10)
            if resp.status_code == 200:
                rows = resp.json()
                if isinstance(rows, list):
                    # Filter to only include rows that look like active (if no status filter, treat all recent as active for demo)
                    logger.info(f"[ACTIVE] Fetched {len(rows)} signals via query '{status_query or 'no filter'}'")
                    return rows
            elif resp.status_code == 400 and "PGRST204" in (resp.text or "") and status_query:
                logger.warning(f"[ACTIVE] status column missing (PGRST204), trying fallback without status filter")
                continue
            else:
                body = (resp.text or "")[:300]
                logger.warning(f"[ACTIVE] Fetch failed {resp.status_code}: {body} query={status_query}")
                if status_query is None:
                    return []
        except Exception as e:
            logger.warning(f"[ACTIVE] Fetch exception: {e}")
            continue
    return []

def fetch_current_price_yfinance(ticker: str) -> Optional[float]:
    """Fetch latest closing price via yfinance for a ticker, with fallbacks."""
    if yf is None:
        return None
    try:
        import math
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        if hasattr(hist.columns, "levels"):
            hist.columns = [c[0] if isinstance(c, tuple) else c for c in hist.columns]
        if "Close" not in hist.columns:
            return None
        close = float(hist["Close"].iloc[-1])
        if not math.isfinite(close) or close <= 0:
            return None
        return close
    except Exception as e:
        logger.debug(f"yfinance fetch for {ticker} failed: {e}")
        return None

def enrich_active_signals_with_prices(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each active signal, fetch current price and compute pnl_pct and status_summary.

    Returns enriched list with keys: ticker, strategy_type, current_price, pnl_pct, status_summary
    status_summary: قريب من الهدف الأول / أعلى من وقف الخسارة / قريب من وقف الخسارة
    """
    enriched: List[Dict[str, Any]] = []
    for sig in signals:
        try:
            ticker = sig.get("ticker") or sig.get("symbol") or sig.get("ticker_bare") or "UNKNOWN"
            strategy = sig.get("strategy_type") or sig.get("strategy") or "Unknown"
            entry = sig.get("entry_price")
            try:
                entry_f = float(entry) if entry is not None else None
            except:
                entry_f = None
            stop = sig.get("stop_loss") or sig.get("current_stop_loss")
            try:
                stop_f = float(stop) if stop is not None else None
            except:
                stop_f = None
            # Collect targets for status logic (first target)
            targets = []
            for k in ["target_1", "target_2", "target_3", "target_4"]:
                if sig.get(k) is not None:
                    try:
                        targets.append(float(sig.get(k)))
                    except:
                        continue
            target1 = targets[0] if targets else None

            # Fetch current price
            current = fetch_current_price_yfinance(ticker)
            if current is None and entry_f is not None:
                # Fallback to entry (no movement) or try to use close from recent hist if available via other means
                # For synthetic, use entry * (1 + 0.02) to show slight gain
                current = entry_f  # neutral

            # Compute pnl_pct
            pnl_pct = None
            if entry_f and current and entry_f != 0:
                try:
                    pnl_pct = (current - entry_f) / entry_f * 100
                except:
                    pnl_pct = 0

            # Determine status_summary
            status_summary = "مستقر"
            try:
                if target1 and current:
                    # If within 3% of target_1
                    if current >= target1 * 0.97:
                        status_summary = "قريب من الهدف الأول"
                    elif stop_f and current > stop_f * 1.02:
                        status_summary = "أعلى من وقف الخسارة"
                    elif stop_f and current <= stop_f * 1.02:
                        status_summary = "قريب من وقف الخسارة"
                    else:
                        status_summary = "أعلى من وقف الخسارة"
                elif stop_f and current:
                    if current > stop_f:
                        status_summary = "أعلى من وقف الخسارة"
                    else:
                        status_summary = "قريب من وقف الخسارة"
                else:
                    status_summary = "مستقر"
            except:
                status_summary = "مستقر"

            enriched.append({
                "ticker": ticker,
                "ticker_bare": ticker.replace(".CA",""),
                "strategy_type": strategy,
                "entry_price": entry_f,
                "stop_loss": stop_f,
                "target_1": target1,
                "current_price": current,
                "pnl_pct": pnl_pct,
                "status_summary": status_summary,
                "raw": sig,
            })
        except Exception as e:
            logger.warning(f"Enrich failed for {sig.get('ticker')}: {e}")
            continue
    return enriched

def format_active_signals_section(enriched: List[Dict[str, Any]]) -> str:
    """Legacy format - kept for backward compat. New code should use format_context_aware_section."""
    header = "🎯 **متابعة أسهم المنظومة والمحفظة | Active Signals Tracker:**"
    if not enriched:
        return f"{header}\nلا توجد صفقات مفتوحة حالياً في المنظومة."
    lines = [header]
    for s in enriched:
        ticker = s.get("ticker_bare") or s.get("ticker", "UNKNOWN").replace(".CA","")
        strat = s.get("strategy_type", "")
        # Map strategy to short label
        strat_label = strat
        if "scal" in strat.lower():
            strat_label = "Scalp"
        elif "swing" in strat.lower():
            strat_label = "Swing"
        elif "invest" in strat.lower():
            strat_label = "Invest"
        cur = s.get("current_price")
        pnl = s.get("pnl_pct")
        status = s.get("status_summary", "مستقر")
        cur_str = f"{cur:.2f}" if isinstance(cur, (int,float)) else "-"
        pnl_str = f"{pnl:+.2f}%" if isinstance(pnl, (int,float)) else "0.00%"
        # Use emoji based on pnl
        emoji = "🟢" if (pnl or 0) >= 0 else "🔴"
        lines.append(f"• {emoji} {ticker} ({strat_label}): السعر الحالي {cur_str} EGP | نسبة التغير {pnl_str} | الحالة: {status}")
    return "\n".join(lines)


# ==============================================================================
# Context-Aware Signal Watchlist & News Impact Analysis (3-bucket)
# ==============================================================================

def _classify_sentiment_simple(text: str) -> str:
    """Classify Arabic sentiment into إيجابي/محايد/سلبي using main's logic or heuristics."""
    if not text or not isinstance(text, str):
        return "محايد"
    try:
        # Try to use main's classify_sentiment if available
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from main import classify_sentiment  # type: ignore
        c = classify_sentiment(text)
        if c in ("إيجابي", "سلبي", "محايد"):
            return c
    except:
        pass
    # Heuristic fallback: keyword matching
    lower = text.lower()
    pos_keywords = ["إيجابي", "صعود", "ارتفاع", "أرباح", "نمو", "مكاسب", "توزيع", "مشروع جديد", "عقد", "قوي", "إنجاز"]
    neg_keywords = ["سلبي", "هبوط", "تراجع", "خسارة", "انخفاض", "تحذير", "خروج", "استقالة", "بيع مكثف", "مخاطر"]
    pos_score = sum(1 for k in pos_keywords if k in lower)
    neg_score = sum(1 for k in neg_keywords if k in lower)
    if pos_score > neg_score and pos_score > 0:
        return "إيجابي"
    if neg_score > pos_score and neg_score > 0:
        return "سلبي"
    return "محايد"

def _extract_short_reason(text: str, max_len: int = 80) -> str:
    """Extract short reason from news summary."""
    if not text or not isinstance(text, str):
        return "لا توجد تفاصيل"
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from main import extract_news_body  # type: ignore
        body = extract_news_body(text)
        if body:
            text = body
    except:
        pass
    # Clean and truncate
    text = text.strip().replace("\n", " ").replace("  ", " ")
    # Remove markdown
    text = text.replace("**", "").replace("*", "")
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text if text else "تطور إخباري"

# ==============================================================================
# Strict Pre-AI Context Sanitization + Sector/Leadership Correlation
# (Anti-hallucination audit: LLM sees ONLY verified same-session news for
# registry-sanctioned tickers; everything else gets code-set neutral facts.)
# ==============================================================================

NO_NEWS_IMPACT: Dict[str, Any] = {
    "impact": "محايد",
    "emoji": "⚖️",
    "short_reason": "لا توجد أخبار جوهرية اليوم",
}

# Anti-hallucination system instructions appended to EVERY LLM prompt
LLM_GUARDRAILS_AR = (
    "\n\nقواعد صارمة (إلزامية — مخالفتها تُبطل التحليل):\n"
    "- لا تخترع أي أخبار أو أرقام أو أحداث لم ترد صراحةً في العناوين/البيانات المذكورة أعلاه.\n"
    "- ممنوع منعاً باتاً إعادة استخدام عبارات جاهزة أو قوالب عامة مثل: "
    "«نتائج أعمال قوية وتوزيعات أرباح مقترحة» أو «نمو أرباح وتوزيعات مقترحة» — كل جملة يجب أن تستند إلى المحتوى المرفق فقط.\n"
    "- إذا لم يوجد خبر جوهري اليوم في العناوين، اكتب حرفياً: «لا توجد أخبار جوهرية اليوم» واكتفِ بحقائق سعرية صريحة (نسبة التغير، السيولة، أداء المؤشرات).\n"
    "- قائد السوق: إذا كان الخبر عن سهم قائد (COMI / SWDY / ABUK / TMGH / ETEL) فحلّل الأثر غير المباشر (Spillover) على قطاعه وأسهمه التابعة فقط — ولا تخترع أخباراً فردية لأسهم غير نشطة.\n"
)


def _is_sanctioned_ticker(ticker: Any) -> bool:
    """True only for registry-verified, non-test/mock tickers."""
    t = str(ticker or "").strip().upper()
    if not t:
        return False
    bare = t[:-3] if t.endswith(".CA") else t
    if t.startswith(("TEST", "MOCK")) or bare.startswith(("TEST", "MOCK")):
        return False
    try:
        from egx_quant.config.stocks_registry import StocksRegistry
        return StocksRegistry.get(t) is not None
    except Exception:
        return False


def _verified_session_headlines(headlines: Any) -> List[str]:
    """Keep only headlines whose embedded publish date falls on the CURRENT Cairo session date.

    fetch_arabic_headlines appends "(RFC822 date)" to each title; unparsable or
    stale-dated headlines are rejected so the LLM never sees yesterday's news.
    """
    import re
    from email.utils import parsedate_to_datetime

    try:
        from egx_quant.utils.egx_calendar import now_cairo
        today = now_cairo().date()
    except Exception:
        today = datetime.now().date()
    verified: List[str] = []
    for h in headlines or []:
        title = str(h or "").strip()
        if not title:
            continue
        m = re.search(r"\(([^()]*)\)\s*$", title)
        if not m:
            continue
        try:
            dt = parsedate_to_datetime(m.group(1))
        except Exception:
            continue
        try:
            from egx_quant.utils.egx_calendar import CAIRO_TZ
            pub_date = dt.astimezone(CAIRO_TZ).date() if dt.tzinfo else dt.date()
        except Exception:
            pub_date = dt.date() if hasattr(dt, "date") else None
        if pub_date == today:
            verified.append(title)
    return verified


def _extract_sentiment_label(text: Any) -> str:
    """Pull the explicit sentiment label from LLM output; default محايد (never guess up)."""
    t = str(text or "")
    import re
    m = re.search(r"التصنيف\s*[:：\-]*\s*(إيجابي|سلبي|محايد)", t)
    if m:
        return m.group(1)
    for token in ("إيجابي", "سلبي", "محايد"):
        if token in t:
            return token
    return "محايد"


def get_news_impact_for_ticker(ticker: str, company_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Verified-news-only sentiment with strict anti-hallucination rules.

    - Unknown / mock / test tickers -> None (excluded from AI analysis entirely).
    - NO verified raw news for the current session date -> exact code-set neutral
      («محايد ⚖️ - لا توجد أخبار جوهرية اليوم») WITHOUT calling the LLM.
    - LLM invoked ONLY when same-session verified headlines exist, under
      LLM_GUARDRAILS_AR (no invention, no generic templates, leader spillover only).
    """
    ticker = str(ticker or "").strip().upper()
    bare = ticker.replace(".CA", "")
    if not _is_sanctioned_ticker(ticker):
        logger.info(f"[SANITIZE] {ticker}: unknown/mock/test ticker - excluded from AI news analysis")
        return None

    raw_headlines: List[Any] = []
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from main import fetch_arabic_headlines, STOCK_NAMES_AR as MAIN_NAMES  # type: ignore
        name = (company_name or MAIN_NAMES.get(ticker, bare))
        raw_headlines = fetch_arabic_headlines(name, ticker) or []
    except Exception as e:
        logger.debug(f"News fetch for {ticker} failed: {e}")
        raw_headlines = []

    verified = _verified_session_headlines(raw_headlines)
    if not verified:
        logger.info(f"[NO-NEWS] {ticker}: 0 verified same-session headlines - code-set neutral (no LLM call)")
        return dict(NO_NEWS_IMPACT)

    news_text = ""
    try:
        from main import build_news_prompt, _summarize_with_gemini  # type: ignore
        prompt = build_news_prompt(verified) + LLM_GUARDRAILS_AR
        news_text = _summarize_with_gemini(prompt, ticker) or ""
    except Exception as e:
        logger.debug(f"Gemini for {ticker} failed: {e}")
        news_text = ""

    sentiment = _extract_sentiment_label(news_text)
    short_reason = _extract_short_reason(news_text) if news_text.strip() else NO_NEWS_IMPACT["short_reason"]
    emoji = "🚀" if sentiment == "إيجابي" else "⚠️" if sentiment == "سلبي" else "⚖️"
    return {
        "impact": sentiment,
        "emoji": emoji,
        "short_reason": short_reason,
        "raw_text": news_text,
        "verified_headlines": verified,
    }


def build_context_aware_categories(
    active_enriched: List[Dict[str, Any]],
    watchlist_limit: int = 3,
    avoid_limit: int = 3,
) -> Dict[str, List[Dict[str, Any]]]:
    """Map verified news to 3 categories: Active, Watchlist (Bullish), Avoid (Risk).

    STRICT CONTEXT RULES (anti-hallucination audit):
    - Sanitization: unknown / mock / test tickers never reach the LLM or the report.
    - Rule A (Sector Correlation): "Impact on Active Trades" covers ONLY tickers
      stored in active_positions plus registry stocks in the SAME sector as an
      active position.
    - Rule B (Leadership Spillover): a Market Leader headline (COMI / SWDY / ABUK /
      TMGH / ETEL) is analyzed as spillover on its sector followers ONLY — never
      invented as individual fake news for non-active stocks.
    - Watchlist/Avoid entries REQUIRE verified same-session headlines; when a
      bucket has no qualifying news it stays EMPTY (no synthetic fills, no
      generic templates).
    """
    try:
        from egx_quant.config.stocks_registry import StocksRegistry, HEAVYWEIGHT_SYMBOLS
    except Exception:
        StocksRegistry = None  # type: ignore
        HEAVYWEIGHT_SYMBOLS: tuple = ("COMI.CA", "SWDY.CA", "ABUK.CA", "TMGH.CA", "ETEL.CA")

    # ── A. Active Trades Impact (sanitized active positions only) ──
    active_list: List[Dict[str, Any]] = []
    active_tickers: set = set()
    active_sectors: set = set()
    for sig in active_enriched or []:
        ticker = str(sig.get("ticker") or sig.get("full_ticker") or sig.get("ticker_bare") or "").strip().upper()
        if not ticker:
            continue
        if not _is_sanctioned_ticker(ticker):
            logger.info(f"[SANITIZE] active signal {ticker}: unknown/mock/test - excluded from report")
            continue
        try:
            from egx_quant.config.stocks_registry import StocksRegistry as _SR
            meta = _SR.get(ticker)
            if meta is not None:
                active_sectors.add(meta.sector)
        except Exception:
            pass
        active_tickers.add(ticker)
        impact = get_news_impact_for_ticker(ticker)
        if impact is None:
            impact = dict(NO_NEWS_IMPACT)
        active_list.append({
            "ticker": (ticker[:-3] if ticker.endswith(".CA") else ticker),
            "full_ticker": ticker,
            "price": sig.get("current_price"),
            "strategy_type": sig.get("strategy_type"),
            "impact": impact["impact"],
            "emoji": impact["emoji"],
            "short_reason": impact["short_reason"],
            "raw_impact": impact,
        })

    # ── B. Sector Correlation + Leadership Spillover (verified news only) ──
    watchlist: List[Dict[str, Any]] = []
    avoid: List[Dict[str, Any]] = []
    sector_notes: List[Dict[str, Any]] = []

    universe = StocksRegistry.all_symbols() if StocksRegistry else []
    candidates: List[Dict[str, Any]] = []
    for ticker in universe:
        if ticker in active_tickers:
            continue
        meta = StocksRegistry.get(ticker) if StocksRegistry else None
        if meta is None:
            continue
        is_leader = ticker in set(HEAVYWEIGHT_SYMBOLS)
        same_sector = bool(active_sectors) and meta.sector in active_sectors
        if is_leader or same_sector:
            candidates.append({"ticker": ticker, "meta": meta, "is_leader": is_leader, "same_sector": same_sector})

    for cand in candidates:
        ticker = cand["ticker"]
        meta = cand["meta"]
        try:
            impact = get_news_impact_for_ticker(ticker)
        except Exception as e:
            logger.debug(f"Sector candidate {ticker} analysis failed: {e}")
            continue
        if impact is None:
            continue
        # No verified same-session news -> code-set neutral -> NOT reportable
        if impact.get("short_reason") == NO_NEWS_IMPACT["short_reason"]:
            continue
        followers = [
            s.symbol for s in (StocksRegistry.all_stocks() if StocksRegistry else [])
            if s.sector == meta.sector and s.symbol != ticker
        ]
        note = {
            "ticker": (ticker[:-3] if ticker.endswith(".CA") else ticker),
            "full_ticker": ticker,
            "sector": meta.sector,
            "impact": impact["impact"],
            "short_reason": impact["short_reason"],
            "is_leader": cand["is_leader"],
            "same_sector_as_active": cand["same_sector"],
            "sector_followers": followers,
        }
        sector_notes.append(note)
        bare = note["ticker"]
        if cand["is_leader"]:
            rel = f"قائد قطاع {meta.sector} - تأثير غير مباشر محتمل على: {', '.join(f[:-3] for f in followers) or 'لا يوجد'}"
        elif cand["same_sector"]:
            rel = f"نفس قطاع صفقة نشطة ({meta.sector})"
        else:
            rel = meta.sector
        if impact["impact"] == "إيجابي":
            if len(watchlist) < watchlist_limit:
                watchlist.append({
                    "ticker": bare,
                    "full_ticker": ticker,
                    "positive_news_trigger": f"{impact['short_reason']} | {rel}",
                    "impact": impact["impact"],
                })
        elif impact["impact"] == "سلبي":
            if len(avoid) < avoid_limit:
                avoid.append({
                    "ticker": bare,
                    "full_ticker": ticker,
                    "negative_news_trigger": f"{impact['short_reason']} | {rel}",
                    "impact": impact["impact"],
                })

    logger.info(
        f"[CONTEXT] active={len(active_list)} sector_candidates={len(candidates)} "
        f"verified_notes={len(sector_notes)} watchlist={len(watchlist)} avoid={len(avoid)} "
        f"(no synthetic fills - empty buckets stay empty)"
    )
    return {
        "active": active_list,
        "watchlist": watchlist,
        "avoid": avoid,
        "sector_notes": sector_notes,
    }

def format_context_aware_section(categories: Dict[str, List[Dict[str, Any]]]) -> str:
    """Format the 🎯 System Signals & Opportunities section with 3 clean parts.

    Structure into 3 parts, skipping empty buckets cleanly.
    """
    header = "🎯 **متابعة أسهم المنظومة والفرص | System Signals & Opportunities**"
    lines: List[str] = [header]

    active = categories.get("active", [])
    watchlist = categories.get("watchlist", [])
    avoid = categories.get("avoid", [])

    # If all empty, show no-active message
    if not active and not watchlist and not avoid:
        return f"{header}\nلا توجد صفقات مفتوحة حالياً في المنظومة."

    # A. Active Trades Impact
    if active:
        lines.append("")
        lines.append("🟢 **صفقاتنا النشطة (Active Trades Impact):**")
        for a in active:
            ticker = a.get("ticker", "UNKNOWN")
            price = a.get("price")
            price_str = f"{price:.2f}" if isinstance(price, (int, float)) else "-"
            impact = a.get("impact", "محايد")
            emoji = a.get("emoji", "⚖️")
            reason = a.get("short_reason", "لا توجد تفاصيل")
            # Format: • {ticker}: السعر {price} EGP | التأثير الأخبار: [إيجابي 🚀 / محايد ⚖️ / سلبي ⚠️] - {short_reason}
            lines.append(f"• {ticker}: السعر {price_str} EGP | التأثير الأخبار: {impact} {emoji} - {reason}")
    # B. Incoming Setups
    if watchlist:
        lines.append("")
        lines.append("🎯 **أسهم تحت الرادار (Incoming Setups / Bullish News):**")
        for w in watchlist:
            ticker = w.get("ticker", "UNKNOWN")
            trigger = w.get("positive_news_trigger", w.get("short_reason", "إيجابي"))
            lines.append(f"• {ticker}: السبب: {trigger} | التوصية: تجهيز سيت أب اختراق/شراء")

    # C. Avoid Watchlist
    if avoid:
        lines.append("")
        lines.append("🛑 **تحذيرات ومخاطر (Avoid Watchlist):**")
        for av in avoid:
            ticker = av.get("ticker", "UNKNOWN")
            trigger = av.get("negative_news_trigger", av.get("short_reason", "سلبي"))
            lines.append(f"• {ticker}: السبب: {trigger} | التوصية: التجنب وعدم الشراء اليوم")

    return "\n".join(lines)
