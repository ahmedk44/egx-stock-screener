"""Shared utilities for EGX News bulletins - idempotency + active signals tracker."""
from __future__ import annotations

import os
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
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
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
    """Write log record after successful broadcast. Returns True on success (or fallback)."""
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
    try:
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201, 204):
            logger.info(f"[IDEMPOTENT] Marked published {bulletin_type} {publish_date} (Supabase)")
            print(f"[LOG] Marked {bulletin_type} published for {publish_date}")
            # Also write local for redundancy
            _write_local_log(bulletin_type, publish_date)
            return True
        elif resp.status_code == 404 and "PGRST205" in (resp.text or ""):
            logger.warning(f"[IDEMPOTENT] Table not found on mark, using local log")
            print(f"[WARN] news_publish_log table missing on mark - using local fallback. Run SQL migration.")
            _write_local_log(bulletin_type, publish_date)
            return True
        else:
            body = (resp.text or "")[:500]
            logger.warning(f"[IDEMPOTENT] Mark failed HTTP {resp.status_code}: {body}")
            # Try local fallback still
            _write_local_log(bulletin_type, publish_date)
            # Consider success if broadcast already succeeded, even if log failed
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
    """Format dedicated section for bulletins.

    Returns markdown string with header and bullets or no-active message.
    """
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
