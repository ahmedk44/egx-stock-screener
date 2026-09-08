"""oanor EGX API client: live quotes (+P/E) as fallback #2 after Yahoo.

Why oanor (verified 2026-09-08): purpose-built EGX endpoints (quote batch up
to 20 codes, screener, EGX30 index), documented OpenAPI, quota headers, and
rich fields Yahoo lacks (P/E ratio, sector, company names). Free tier:
12,200 calls/month @ 2 req/sec (headers confirm; plenty for cron cadence
~3-4k/month when fetched lazily only for tickers Yahoo missed).

Upstream note: oanor's values match TradingView 1:1 (same feed family), so it
is DIVERSITY of infrastructure, not an independent oracle. TV scanner stays as
final free fallback. Chain everywhere: Yahoo -> oanor -> TV -> neutral/silence.

Auth: OANOR_API_KEY env (never commit values; see .env.example).
All functions fail-open ({}  / None) - oanor must never break a cycle.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore

logger = logging.getLogger("egx_quant.oanor")

OANOR_BASE = "https://api.oanor.com/egx-api"
OANOR_TIMEOUT_SECONDS = 12
OANOR_BATCH_MAX = 20


def oanor_enabled() -> bool:
    """True when a key is configured (kill-switch without deploy: unset the key)."""
    try:
        return bool((os.environ.get("OANOR_API_KEY") or "").strip())
    except Exception:
        return False


def _headers() -> Dict[str, str]:
    key = (os.environ.get("OANOR_API_KEY") or "").strip().strip('"').strip("'")
    return {"x-oanor-key": key, "User-Agent": "EGX-Monitor/1.0"}


def to_oanor_code(ticker: str) -> str:
    """COMI.CA -> COMI (oanor codes are bare EGX tickers). Never raises."""
    try:
        bare = str(ticker or "").strip().upper()
        if bare.endswith(".CA"):
            bare = bare[:-3]
        return "".join(ch for ch in bare if ch.isalnum())
    except Exception:
        return ""


def from_oanor_code(code: str) -> str:
    """COMI -> COMI.CA (reverse map for responses)."""
    try:
        bare = str(code or "").strip().upper()
        if not bare:
            return ""
        return bare if bare.endswith(".CA") else f"{bare}.CA"
    except Exception:
        return ""


def _fnum(x) -> Optional[float]:
    try:
        v = float(x)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def fetch_oanor_quotes(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """ONE batched quote call (<=20 codes): {TICKER.CA: quote dict}.

    Quote dict: {price, change_pct, change, open, high, low, volume,
    market_cap, pe_ratio (may be None), sector, company}.
    Only returned rows are included (unknown codes simply absent).
    Lazy by design: callers fetch ONLY for tickers Yahoo missed (quota care).
    Read-only, never raises.
    """
    out: Dict[str, Dict[str, Any]] = {}
    codes = []
    seen = set()
    for t in tickers or []:
        c = to_oanor_code(t)
        if c and c not in seen:
            seen.add(c)
            codes.append(c)
    if not codes or requests is None or not oanor_enabled():
        return out
    try:
        resp = requests.get(
            f"{OANOR_BASE}/v1/quote",
            headers=_headers(),
            params={"codes": ",".join(codes[:OANOR_BATCH_MAX])},
            timeout=OANOR_TIMEOUT_SECONDS,
        )
        if resp.status_code == 429:
            logger.warning("[OANOR] 429 rate-limited - backing off this cycle")
            return out
        if resp.status_code == 402:
            logger.warning("[OANOR] 402 subscription/quota issue - check key status")
            return out
        if resp.status_code != 200:
            logger.warning("[OANOR] quote HTTP %s", resp.status_code)
            return out
        try:
            remain = resp.headers.get("x-quota-remaining")
            if remain is not None:
                logger.info("[OANOR] quota remaining: %s", remain)
        except Exception:
            pass
        data = resp.json().get("data", {}) or {}
        for q in data.get("quotes", []) or []:
            try:
                if not isinstance(q, dict):
                    continue
                key = from_oanor_code(q.get("ticker", ""))
                price = _fnum(q.get("price"))
                if not key or price is None or price <= 0:
                    continue
                out[key] = {
                    "price": round(price, 2),
                    "change_pct": _fnum(q.get("change_percent")),
                    "change": _fnum(q.get("change")),
                    "open": _fnum(q.get("open")),
                    "high": _fnum(q.get("high")),
                    "low": _fnum(q.get("low")),
                    "volume": _fnum(q.get("volume")),
                    "market_cap": _fnum(q.get("market_cap")),
                    "pe_ratio": _fnum(q.get("pe_ratio")),
                    "sector": q.get("sector"),
                    "company": q.get("company"),
                }
            except Exception:
                continue
        logger.info("[OANOR] quotes resolved %d/%d", len(out), len(codes))
    except Exception as e:
        logger.warning("[OANOR] quote fetch failed: %s", e)
    return out
