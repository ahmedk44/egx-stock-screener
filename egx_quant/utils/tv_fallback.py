"""TradingView scanner snapshot as a live-quote fallback for EGX (no key required).

Why this exists: Yahoo intraday is broken for EGX and its daily bars freeze
for days. TV's scanner endpoint returns a live snapshot (close/change/volume)
that works server-side as JSON. It is a QUOTE source, not history: it cannot
feed Donchian/RSI evaluation - only price validation at publish/monitor time.

Freshness policy (documented judgment call):
  - TV responses carry NO timestamp. Quotes are accepted ONLY during EGX
    session hours (a frozen out-of-session snapshot is indistinguishable).
  - Every consumer ALSO applies a sanity band vs its own entry price
    (TV_SANITY_BAND) to reject absurd prints.
  - Callers must disclose TV-sourced prices on cards (badge), never silently.
All functions are fail-open (None/{} on any error) - TV must never break a cycle.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore

logger = logging.getLogger("egx_quant.tv_fallback")

TV_SCANNER_URL = "https://scanner.tradingview.com/egypt/scan"
TV_TIMEOUT_SECONDS = 12
TV_COLUMNS = ["close", "change", "volume"]

# Reject prints deviating more than this from the reference entry price.
TV_SANITY_BAND = 0.30


def tv_enabled() -> bool:
    """Kill-switch without deploy: TV_QUOTES_ENABLED=0 disables all TV usage."""
    try:
        return (os.environ.get("TV_QUOTES_ENABLED") or "1").strip() not in ("0", "false", "False", "no")
    except Exception:
        return True


def to_tv_symbol(ticker: str) -> str:
    """COMI.CA -> EGX:COMI (TradingView EGX namespace). Never raises."""
    try:
        bare = str(ticker or "").strip().upper()
        if bare.endswith(".CA"):
            bare = bare[:-3]
        bare = "".join(ch for ch in bare if ch.isalnum())
        return f"EGX:{bare}" if bare else ""
    except Exception:
        return ""


def from_tv_symbol(tv_symbol: str) -> str:
    """EGX:COMI -> COMI.CA (reverse map for responses)."""
    try:
        bare = str(tv_symbol or "").strip().upper()
        if ":" in bare:
            bare = bare.split(":", 1)[1]
        if bare.endswith(".CA"):
            return bare
        return f"{bare}.CA" if bare else ""
    except Exception:
        return ""


def fetch_tv_quotes(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """ONE batched snapshot request: {TICKER.CA: {price, change_pct, volume}}.

    Only rows actually returned are included (unknown symbols simply absent -
    TV coverage gaps are normal, e.g. some names Yahoo has and TV lacks).
    Read-only, never raises, never writes.
    """
    out: Dict[str, Dict[str, Any]] = {}
    wanted = [t for t in (tickers or []) if to_tv_symbol(t)]
    if not wanted or requests is None or not tv_enabled():
        return out
    try:
        resp = requests.post(
            TV_SCANNER_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Content-Type": "application/json",
                "Origin": "https://ar.tradingview.com",
                "Referer": "https://ar.tradingview.com/",
            },
            json={
                "symbols": {"tickers": [to_tv_symbol(t) for t in wanted], "query": {"types": []}},
                "columns": TV_COLUMNS,
            },
            timeout=TV_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            logger.warning("[TV] scanner HTTP %s", resp.status_code)
            return out
        rows = resp.json().get("data", [])
        if not isinstance(rows, list):
            return out
        for row in rows:
            try:
                if not isinstance(row, dict):
                    continue
                key = from_tv_symbol(row.get("s", ""))
                vals = row.get("d", [])
                if not key or not isinstance(vals, list) or len(vals) < 1:
                    continue
                price = float(vals[0]) if vals[0] is not None else None
                if price is None or price != price or price <= 0:
                    continue
                change_pct = None
                try:
                    change_pct = float(vals[1]) if len(vals) > 1 and vals[1] is not None else None
                except Exception:
                    pass
                volume = None
                try:
                    volume = float(vals[2]) if len(vals) > 2 and vals[2] is not None else None
                except Exception:
                    pass
                out[key] = {"price": round(price, 2), "change_pct": change_pct, "volume": volume}
            except Exception:
                continue
        logger.info("[TV] snapshot: %d/%d tickers resolved", len(out), len(wanted))
    except Exception as e:
        logger.warning("[TV] snapshot failed: %s", e)
    return out


def tv_quote_sane(price: float, reference_entry: Optional[float],
                  band: float = TV_SANITY_BAND) -> bool:
    """Sanity gate: reject prints deviating more than `band` from reference.

    Pure function (testable). No reference (None/0) -> False (fail-closed:
    a TV price without an anchor to validate against is not usable).
    """
    try:
        p = float(price)
        e = float(reference_entry) if reference_entry is not None else 0.0
        if not (p > 0) or not (e > 0):
            return False
        return abs(p - e) / e <= float(band)
    except Exception:
        return False
