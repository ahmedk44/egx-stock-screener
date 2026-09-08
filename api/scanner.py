"""
Vercel Cron endpoint for Live Scanner — full-EGX batched intraday scanner.

Triggered by:
  - External ping (cron-job.org) every 15m via GET with Bearer token or ?secret=
  - GitHub Actions runner.yml schedule (fallback, */15 7-11 * * 0-4)

Pipeline (official project modules only — no hardcoded logic):
  a. Ticker ingestion    : StocksRegistry.all_symbols() (54 registered EGX stocks:
                            full EGX30 + liquid EGX70 leaders, extendable at
                            runtime via EXTRA_TICKERS env)
  b. Shariah transparency: ALL tickers processed (non-compliant / needs-review are
                           NOT dropped); the real status (✅ متوافق / ⚠️ يحتاج مراجعة /
                           ❌ غير متوافق) is featured on the official Telegram card
                           and stored on the Supabase row
  c. Core strategy       : StrategyEngine.evaluate() (Donchian+Volume+RSI confluence,
                           TQI score) + RiskManager.build_plan() (ATR SL/TP guardrails)
  c+.Live Price Guard    : MARKET HOURS GATE (is_market_open) aborts all live signal
                           emissions outside the EGX session; real-time quote via
                           fast_info.last_price (1m ticker fallback) validated to the
                           CURRENT session date; entry re-anchored to the LIVE price
                           and SL/TP1-3 recalculated dynamically from it. Stale-bar
                           frames and pre-open placeholder quotes DROP the candidate.
  d. Official card       : TelegramNotifier.format_channel_broadcast() — full channel
                           signal card (Shariah badge, TQI, targets, CTA) + join button
  e. Dispatch            : Supabase trade_signals publish (schema-aligned upsert) +
                           Telegram channel broadcast

Optimizations for serverless budget:
  - Batched yfinance download (BATCH_SIZE=9, threads=True) with per-batch timing logs.
    NOTE: Yahoo intraday endpoints (15m/30m/1h/5m) are BROKEN for EGX on yfinance 1.6.0
    (KeyError: tradingPeriods on every .CA ticker) — daily is the only reliable interval.
    6mo of daily bars keeps StrategyEngine.MIN_BARS=60 satisfied.
  - Dedup vs existing ACTIVE/TRACKING signals so repeated 15m cron hits never spam.
  - Trade monitor cycle (target/SL/trailing alerts) runs in the remaining budget.

Behavior:
  - Returns JSON {ok, status, duration_seconds, universe_size, shariah, evaluated, signals, monitor}
  - Dry run: `python api/scanner.py --dry-run` (no Supabase writes / Telegram sends;
    prints the generated Telegram payload for every candidate to the console)

Timing budget (Fluid compute): target <30s; vercel.json sets maxDuration=60.
"""
import json
import math
import os
import sys
# Ensure project root is on sys.path for Vercel runtime (/var/task)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple

from egx_quant.core.shariah_filter import ShariahFilter
from egx_quant.utils.egx_calendar import is_market_open, now_cairo, session_label

# Batch size: 12 tickers per yf.download keeps each batch in ~2-4s while a
# 54+ universe fits in 5 batches (inside the Vercel 60s budget - see deadline).
BATCH_SIZE = 12
# Hard wall-clock deadline (seconds) for the scan phase: stop starting new
# batches past this point so the function ALWAYS returns inside maxDuration.
# The standalone /api/monitor covers trade tracking independently.
SCAN_DEADLINE_SECONDS = 50.0
# Rough wall-clock budget guard (seconds) for optional heavy extras (monitor)
TIME_BUDGET_SECONDS = 45.0
# StrategyEngine needs >= 60 daily bars; 6mo (~125 sessions) is the safe fetch window
KLINE_PERIOD = "6mo"
KLINE_INTERVAL = "1d"


def _universe() -> List[str]:
    """Registry stocks + EXTRA_TICKERS env extension (single source of truth). Never raises.

    Operators can append tickers without a deploy:
      EXTRA_TICKERS="FOO.CA, BAR"  (normalized, deduped, validated live by the
      liquidity prescreen - dead symbols are auto-skipped + logged).
    """
    try:
        from egx_quant.config.stocks_registry import StocksRegistry
        symbols = list(StocksRegistry.all_symbols() or [])
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] StocksRegistry unavailable ({exc}) - using fallback watchlist")
        symbols = ["COMI.CA", "FWRY.CA", "TMGH.CA", "SWDY.CA", "ABUK.CA", "ETEL.CA", "HRHO.CA", "EAST.CA"]
    try:
        extra: List[str] = []
        for part in (os.environ.get("EXTRA_TICKERS") or "").replace(";", ",").split(","):
            t = part.strip().upper()
            if not t:
                continue
            if not t.endswith(".CA"):
                t = f"{t}.CA"
            if t not in symbols and t not in extra:
                extra.append(t)
        if extra:
            print(f"[CRON][SCANNER] EXTRA_TICKERS adding {len(extra)}: {', '.join(extra)}")
            symbols = symbols + extra
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] EXTRA_TICKERS parse failed ({exc}) - ignored")
    return symbols


def _is_authorized(handler: BaseHTTPRequestHandler) -> tuple[bool, str]:
    cron_secret = (os.environ.get("CRON_SECRET") or "").strip()
    vercel_cron = handler.headers.get("x-vercel-cron") or handler.headers.get("X-Vercel-Cron")
    if vercel_cron == "1":
        return True, "x-vercel-cron"
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(handler.path)
        qs = parse_qs(parsed.query)
        for key in ("secret", "cron_secret", "CRON_SECRET", "token", "auth", "key"):
            vals = qs.get(key, [])
            if vals and cron_secret and vals[0].strip() == cron_secret:
                return True, f"query:{key}"
            if vals and not cron_secret:
                return True, f"query:{key} (no-secret)"
        if cron_secret and parsed.query and cron_secret in parsed.query:
            return True, "query:raw"
    except Exception:
        pass
    auth = handler.headers.get("Authorization") or handler.headers.get("authorization") or ""
    if cron_secret and auth.strip() == f"Bearer {cron_secret}":
        return True, "bearer"
    if cron_secret:
        return False, "missing/invalid bearer (CRON_SECRET set) — use header Authorization: Bearer <CRON_SECRET> or query ?secret=<CRON_SECRET>"
    return True, "no-secret (open)"


def _get_scalping_channel_id() -> str:
    """Hard-aligned to SCAPLING_CHANNEL_ID per task spec."""
    SCALPING_FALLBACK = "-1003993921849"
    for env in ["TELEGRAM_CHANNEL_SCALPING", "SCALPING_CHANNEL_ID", "CHANNEL_SCALPING", "TELEGRAM_CHANNEL_ID"]:
        val = (os.environ.get(env) or "").strip().strip('"').strip("'")
        if val:
            return val
    return SCALPING_FALLBACK


class _TransparentShariahGate(ShariahFilter):
    """Scanner-level Shariah TRANSPARENCY policy (per task spec).

    The execution engine keeps its strict default-deny gate; the SCANNER, however,
    must process ALL registry tickers and surface the exact compliance status on
    every card (✅ متوافق / ⚠️ يحتاج مراجعة / ❌ غير متوافق) instead of silently
    dropping non-compliant symbols. Subclassing ShariahFilter keeps get_status()
    (the official evaluator) fully intact — only the blocking behavior is relaxed
    at this scan boundary.
    """

    def is_execution_allowed(self, symbol: str) -> bool:
        return True


def _bar_session_date(ts: "Any") -> Optional["Any"]:
    """Session (Africa/Cairo) date of a bar timestamp. Naive = exchange-local."""
    try:
        import pytz  # type: ignore
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if getattr(ts, "tzinfo", None) is None:
            return ts.date()
        return ts.astimezone(pytz.timezone("Africa/Cairo")).date()
    except Exception:
        return None


def _sessions_lag(bar_date: "Any", today: "Any") -> int:
    """Trading-session lag (Sun-Thu) between a bar date and today.

    Weekends (Fri/Sat) are not sessions and never count. Unknown dates -> 999
    (fail-closed). Sep-6 bar on Sep-8 session = lag 2 (Mon 7 + Tue 8).
    """
    try:
        if bar_date is None or today is None:
            return 999
        if bar_date >= today:
            return 0
        from datetime import timedelta
        lag, day = 0, bar_date + timedelta(days=1)
        while day <= today:
            if day.weekday() in (6, 0, 1, 2, 3):  # Sun-Thu EGX sessions
                lag += 1
            day += timedelta(days=1)
        return lag
    except Exception:
        return 999


def _scanner_max_lag() -> int:
    """Max tolerated bar lag for EVALUATION (SCANNER_MAX_BAR_LAG, default 2).

    0 = strict same-session only (old behavior). Publishing additionally
    requires a live quote (or an explicitly badged delayed quote).
    """
    try:
        v = int((os.environ.get("SCANNER_MAX_BAR_LAG") or "2").strip())
        return max(0, min(5, v))
    except Exception:
        return 2


def _fetch_live_quote(symbol: str, allow_delayed: bool = False, allow_tv: bool = False, tv_hint: Optional[Dict[str, Any]] = None, allow_oanor: bool = False, oanor_hint: Optional[Dict[str, Any]] = None) -> Tuple[Optional[float], str, Optional[str]]:
    """Live execution price: fast_info.last_price primary, 1m ticker fallback.

    Session validation: the supporting bar MUST belong to the CURRENT Cairo
    session date. Stale bars from yesterday or pre-open placeholders return
    (None, reason, None) so the candidate is dropped immediately.

    Delayed mode (allow_delayed): when the strict path fails, fall back to the
    latest daily close REGARDLESS of date, returned as
    (price, "delayed_daily", bar_date) so the card carries an explicit
    delayed-data badge. Never fabricates: the price IS a real traded close.

    TV snapshot fallback (allow_tv): TradingView scanner quote, accepted ONLY
    in-session (its response carries no timestamp). Sanity vs entry happens in
    _publish_signal. tv_hint (pre-fetched dict) avoids per-ticker POSTs.
    """
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        return None, f"yfinance-unavailable: {exc}", None
    today = now_cairo().date()
    daily = None
    try:
        t = yf.Ticker(symbol)
        # Primary: fast_info.last_price, validated against the latest daily bar date
        try:
            fi_price = float(t.fast_info.last_price)
        except Exception:
            fi_price = None
        if fi_price and math.isfinite(fi_price) and fi_price > 0:
            try:
                d = t.history(period="5d", interval="1d", auto_adjust=False)
                if d is not None and not d.empty:
                    daily = d
                    if _bar_session_date(d.index[-1]) == today:
                        return round(fi_price, 2), "fast_info", str(today)
            except Exception:
                pass
        # Fallback: 1m intraday last close (self-timestamped)
        try:
            hist = t.history(period="1d", interval="1m")
            if hist is not None and not hist.empty:
                closes = hist["Close"].dropna()
                if not closes.empty and _bar_session_date(hist.index[-1]) == today:
                    px = float(closes.iloc[-1])
                    if math.isfinite(px) and px > 0:
                        return round(px, 2), "intraday_1m", str(today)
        except Exception:
            pass
        # oanor live quote (documented multi-ticker API + P/E, session-gated).
        # Preferred over TV: richer fields + quota observability. TV stays as
        # the free backup below; delayed-daily (known-old) comes last.
        if allow_oanor:
            hint = oanor_hint if isinstance(oanor_hint, dict) else None
            if hint is None:
                try:
                    from egx_quant.utils.oanor_client import fetch_oanor_quotes
                    hint = fetch_oanor_quotes([symbol]).get(symbol.upper())
                except Exception:
                    hint = None
            if hint:
                try:
                    px = float(hint.get("price"))
                    if is_market_open() and math.isfinite(px) and px > 0:
                        print(f"[CRON][SCANNER][OANOR-QUOTE] {symbol} {px:.2f} (pe {hint.get('pe_ratio')})")
                        return round(px, 2), "oanor", "live"
                except Exception:
                    pass
            print(f"[CRON][SCANNER][OANOR-QUOTE] {symbol} unavailable - continuing to next source")
        # TV snapshot fallback (session-only: TV responses carry no timestamp,
        # so out-of-session quotes are indistinguishable from frozen ones).
        if allow_tv:
            hint = tv_hint if isinstance(tv_hint, dict) else None
            if hint is None:
                try:
                    from egx_quant.utils.tv_fallback import fetch_tv_quotes
                    hint = fetch_tv_quotes([symbol]).get(symbol.upper())
                except Exception:
                    hint = None
            if hint:
                try:
                    px = float(hint.get("price"))
                    if is_market_open() and math.isfinite(px) and px > 0:
                        print(f"[CRON][SCANNER][TV-QUOTE] {symbol} {px:.2f} (change {hint.get('change_pct')})")
                        return round(px, 2), "tv", "live"
                except Exception:
                    pass
            print(f"[CRON][SCANNER][TV-QUOTE] {symbol} unavailable - continuing to next source")
        # Delayed fallback: latest daily close with explicit as-of date
        if allow_delayed:
            try:
                d = daily
                if d is None:
                    d = t.history(period="5d", interval="1d", auto_adjust=False)
                if d is not None and not d.empty:
                    closes = d["Close"].dropna()
                    if not closes.empty:
                        asof = _bar_session_date(d.index[-1])
                        px = float(closes.iloc[-1])
                        if math.isfinite(px) and px > 0:
                            print(f"[CRON][SCANNER][DELAYED-QUOTE] {symbol} using daily close {px:.2f} as of {asof}")
                            return round(px, 2), "delayed_daily", str(asof)
            except Exception:
                pass
            return None, "delayed-unavailable", None
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] live quote fetch failed for {symbol}: {exc}")
        return None, "fetch-error", None
    return None, "stale-or-pre-open (no same-session bar)", None


def _has_active_signal(ticker: str) -> bool:
    """True when an ACTIVE/TRACKING trade_signals row already exists for ticker."""
    try:
        from egx_quant.utils import supabase_sync
        cfg = supabase_sync._cfg()
        if cfg is None:
            return False
        url, _ = cfg
        resp = supabase_sync.requests.get(
            f"{url}/rest/v1/{supabase_sync.TRADE_SIGNALS_TABLE}"
            f"?ticker=eq.{ticker}&status=in.(ACTIVE,TRACKING)&limit=1&select=id",
            headers=supabase_sync._headers(prefer="return=minimal"),
            timeout=8,
        )
        if resp.status_code == 200:
            rows = resp.json()
            return isinstance(rows, list) and len(rows) > 0
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] active-signal guard failed for {ticker}: {exc}")
    return False


def _ticker_frame(data: "Any", ticker: str, multi: bool, level0: set) -> Optional["Any"]:
    """Extract one ticker's OHLCV frame from a batched yf.download result."""
    import pandas as pd  # type: ignore

    if multi:
        if ticker not in level0:
            return None
        df = data[ticker]
    else:
        df = data
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["Close"])
    if df.empty or not {"Open", "High", "Low", "Close", "Volume"}.issubset(df.columns):
        return None
    return df


def _rr_ratio(plan: "Any") -> Optional[float]:
    """Risk/Reward on Target 1: (TP1 - entry) / (entry - SL)."""
    try:
        risk = float(plan.entry_price) - float(plan.stop_loss)
        reward = float(plan.target_1) - float(plan.entry_price)
        if risk > 0 and reward > 0:
            return round(reward / risk, 2)
    except Exception:
        pass
    return None


# Dynamic track classification thresholds (aligned with STRATEGY_PLAN in
# main.py: scalp SL<=3.5%/T1<=4%, invest TQI>=8 & T3>=12%, else balanced swing).
TRACK_SCALP_MAX_SL = 0.035
TRACK_SCALP_MAX_T1 = 0.04
TRACK_INVEST_MIN_TQI = 8.0
TRACK_INVEST_MIN_T3 = 0.12
# Absurd P/E ceiling for the invest track (EGX large-cap context): a TQI>=8
# wide-target setup with higher P/E is demoted to swing (anti-hype guard).
# None (unknown) never blocks - only a KNOWN absurd value demotes.
TRACK_INVEST_MAX_PE = 15.0

# Per-track channel env chains (first set var wins). Missing track channel
# falls back to the scalping channel - a signal is never dropped for routing.
TRACK_CHANNEL_ENVS: Dict[str, tuple] = {
    "scalping": ("TELEGRAM_CHANNEL_SCALPING", "SCALPING_CHANNEL_ID", "CHANNEL_SCALPING", "TELEGRAM_CHANNEL_ID"),
    "swing": ("TELEGRAM_CHANNEL_SWING", "CHANNEL_SWING"),
    "investment": ("TELEGRAM_CHANNEL_INVESTMENT", "CHANNEL_INVESTMENT"),
}


def classify_track(entry: Any, stop: Any, t1: Any, t3: Any, tqi: Any, pe: Any = None) -> str:
    """Dynamic track from the REALIZED signal fingerprint (pure, testable).

    invest:     TQI >= 8.0 with wide third target (>= +12%) and sane P/E
                (None = unknown, never blocks; > 15 demotes to swing).
    scalping:   tight stop (<= 3.5%) with close first target (<= 4%) - fast setup.
    swing:      default balanced profile (Donchian confluence standard).
    invest is checked first (quality dominates speed).
    """
    try:
        e = float(entry)
        sl_d = (e - float(stop)) / e if e else 1.0
        t1_d = (float(t1) - e) / e if e and t1 else 1.0
        t3_d = (float(t3) - e) / e if e and t3 else 0.0
        tq = float(tqi)
    except Exception:
        return "swing"
    try:
        pe_v = float(pe) if pe is not None else None
    except Exception:
        pe_v = None
    if tq >= TRACK_INVEST_MIN_TQI and t3_d >= TRACK_INVEST_MIN_T3:
        if pe_v is not None and pe_v > TRACK_INVEST_MAX_PE:
            print(f"[CRON][SCANNER] TRACK demote: invest blocked by absurd P/E {pe_v} -> swing")
            return "swing"
        return "investment"
    if sl_d <= TRACK_SCALP_MAX_SL and t1_d <= TRACK_SCALP_MAX_T1:
        return "scalping"
    return "swing"
    if tq >= TRACK_INVEST_MIN_TQI and t3_d >= TRACK_INVEST_MIN_T3:
        return "investment"
    if sl_d <= TRACK_SCALP_MAX_SL and t1_d <= TRACK_SCALP_MAX_T1:
        return "scalping"
    return "swing"


def _channel_for_track(track: str, fallback: str) -> str:
    """Resolve the Telegram channel for a track (fallback = scalping channel)."""
    for env in TRACK_CHANNEL_ENVS.get(str(track or "").strip().lower(), ()):
        try:
            val = (os.environ.get(env) or "").strip().strip('"').strip("'")
        except Exception:
            val = ""
        if val:
            return val
    print(f"[CRON][SCANNER][WARN] no channel configured for track={track} - falling back (signal never dropped)")
    return fallback


def _liquidity_prescreen(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fast availability pre-check: ONE light 5d/1d download for the universe.

    Marks tickers with no usable frame (delisted/unknown/empty/bad price) as
    ok=False so the heavy 6mo pass skips them instantly (Vercel budget guard).
    This is AVAILABILITY only - selectivity stays with the strategy engine.
    Fail-OPEN: any download failure marks everything ok=True (the prescreen
    must never kill the scan).
    Returns {ticker: {"ok": bool, "reason": str}}.
    """
    result: Dict[str, Dict[str, Any]] = {t: {"ok": True, "reason": "prescreen-bypassed"} for t in tickers}
    try:
        import yfinance as yf  # type: ignore
        import pandas as pd  # type: ignore
        import math as _math
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] prescreen deps unavailable ({exc}) - all tickers pass")
        return result
    try:
        data = yf.download(
            tickers=list(tickers),
            period="5d",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] prescreen download failed ({exc}) - all tickers pass")
        return result
    try:
        multi = isinstance(data.columns, pd.MultiIndex)
        level0 = set(data.columns.get_level_values(0)) if multi else set()
        for t in tickers:
            try:
                if multi and t not in level0:
                    result[t] = {"ok": False, "reason": "no-frame"}
                    continue
                df = data[t] if multi else data
                if df is None or df.empty:
                    result[t] = {"ok": False, "reason": "empty"}
                    continue
                df = df.dropna(subset=["Close"])
                if df.empty:
                    result[t] = {"ok": False, "reason": "no-close"}
                    continue
                last_close = float(df["Close"].iloc[-1])
                if not _math.isfinite(last_close) or last_close <= 0:
                    result[t] = {"ok": False, "reason": "bad-price"}
                    continue
                result[t] = {"ok": True, "reason": "ok"}
            except Exception:
                result[t] = {"ok": False, "reason": "parse-error"}
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] prescreen parse failed ({exc}) - all tickers pass")
        return {t: {"ok": True, "reason": "prescreen-bypassed"} for t in tickers}
    dropped = sorted(t for t, v in result.items() if not v["ok"])
    if dropped:
        print(f"[CRON][SCANNER] prescreen: {len(tickers) - len(dropped)}/{len(tickers)} liquid, skipped: {', '.join(dropped)}")
    else:
        print(f"[CRON][SCANNER] prescreen: all {len(tickers)} tickers liquid")
    return result


def _run_strategy_batch(
    batch: List[str],
    data: "Any",
    strategy: "Any",
    risk: "Any",
    stats: Optional[Dict[str, Any]] = None,
    max_lag: int = 0,
) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """Run the official StrategyEngine + RiskManager over one batch frame.

    Shariah compliance is enforced inside StrategyEngine.evaluate BEFORE any
    technical computation (default-deny). Returns (records, evaluated_count,
    near_misses) — near_misses carry live values for tickers that met 2/3
    confluence checks but failed the strict entry criteria.

    Delayed-data mode (max_lag > 0): bars up to max_lag sessions old are
    EVALUATED (never fabricated); stats["delayed_eval"] counts them and the
    published card carries an explicit delayed-data badge.
    """
    import math
    import pandas as pd  # type: ignore
    from egx_quant.core.strategy_engine import (
        MIN_BARS,
        DONCHIAN_PERIOD,
        RSI_LOWER_BOUND,
        RSI_UPPER_BOUND,
        VOLUME_SPIKE_MULT,
        donchian_high,
        rsi as rsi_fn,
        sma as sma_fn,
    )

    records: List[Dict[str, Any]] = []
    near_misses: List[Dict[str, Any]] = []
    evaluated = 0
    multi = isinstance(data.columns, pd.MultiIndex)
    level0 = set(data.columns.get_level_values(0)) if multi else set()
    for ticker in batch:
        try:
            df = _ticker_frame(data, ticker, multi, level0)
            if df is None or len(df) < MIN_BARS:
                print(f"[CRON][SCANNER][NO-DATA] {ticker} no usable frame (empty or <{MIN_BARS} bars) - skipped")
                continue
            # Same-day validation: the latest bar MUST belong to the CURRENT
            # Cairo session date. Yesterday's bar / pre-open placeholder = stale.
            bar_date = _bar_session_date(df.index[-1])
            lag = _sessions_lag(bar_date, now_cairo().date())
            if lag > max_lag:
                print(f"[CRON][SCANNER][STALE-FRAME] {ticker} last bar {bar_date} (lag {lag} sessions) != session {now_cairo().date()} - candidate dropped")
                if stats is not None:
                    stats["stale"] = int(stats.get("stale", 0) or 0) + 1
                    try:
                        stats.setdefault("stale_dates", []).append(str(bar_date))
                    except Exception:
                        pass
                continue
            if lag > 0 and stats is not None:
                stats["delayed_eval"] = int(stats.get("delayed_eval", 0) or 0) + 1
                print(f"[CRON][SCANNER][DELAYED-EVAL] {ticker} bar {bar_date} (lag {lag}) - evaluating with delayed badge")
            evaluated += 1
            signal = strategy.evaluate(ticker, df)
            if signal is None:
                # Per-ticker rejection diagnostics (same formulas as the engine)
                close = df["Close"].astype(float)
                high = df["High"].astype(float)
                volume = df["Volume"].astype(float)
                last_close = float(close.iloc[-1])
                don = float(donchian_high(high).iloc[-1])
                vol_avg = float(sma_fn(volume, DONCHIAN_PERIOD).iloc[-1])
                last_vol = float(volume.iloc[-1])
                last_rsi = float(rsi_fn(close).iloc[-1])
                last_sma20 = float(sma_fn(close, 20).iloc[-1])
                checks = {
                    "donchian": math.isfinite(don) and last_close > don,
                    "volume": math.isfinite(vol_avg) and vol_avg > 0 and last_vol > VOLUME_SPIKE_MULT * vol_avg,
                    "rsi": RSI_LOWER_BOUND < last_rsi < RSI_UPPER_BOUND,
                }
                met = sum(checks.values())
                print(
                    f"[CRON][SCANNER][DIAG] {ticker} | close={last_close:.2f} sma20={last_sma20:.2f} "
                    f"rsi={last_rsi:.1f} | donchian: close>{don:.2f}={checks['donchian']} | "
                    f"volume: {last_vol:.0f}>{VOLUME_SPIKE_MULT}x{vol_avg:.0f}={checks['volume']} | "
                    f"rsi-band(50-70)={checks['rsi']} | confluence={met}/3"
                )
                if met >= 2:
                    failed = [k for k, v in checks.items() if not v]
                    near_misses.append({
                        "ticker": ticker,
                        "close": round(last_close, 2),
                        "sma20": round(last_sma20, 2),
                        "rsi": round(last_rsi, 1),
                        "checks_met": met,
                        "failed": failed,
                        "tqi": "not scored (entry confluence incomplete)",
                    })
                continue
            plan = risk.build_plan(
                ticker,
                signal.entry_price,
                df,
                take_profit_override=signal.take_profit,
                tqi_score=signal.tqi_score,
                targets=[t for t in (signal.target_1, signal.target_2, signal.target_3) if t is not None],
            )
            if not plan.approved:
                print(f"[CRON][SCANNER] {ticker} plan rejected: {plan.rejection_reason_en}")
                continue
            close = df["Close"].astype(float)
            last_rsi = float(rsi_fn(close).iloc[-1])
            last_sma20 = float(sma_fn(close, 20).iloc[-1])
            records.append({
                "ticker": ticker,
                "plan": plan,
                "signal": signal,
                "df": df,
                "rr_tp1": _rr_ratio(plan),
                "rsi": round(last_rsi, 1),
                "sma20": round(last_sma20, 2),
            })
            print(
                f"[CRON][SCANNER] SIGNAL {ticker} | entry={plan.entry_price} sl={plan.stop_loss} "
                f"tp1={plan.target_1} tp2={plan.target_2} tp3={plan.target_3} tqi={plan.tqi_score} "
                f"rr_tp1={_rr_ratio(plan)} rsi={last_rsi:.1f} sma20={last_sma20:.2f}"
            )
        except Exception as e_ticker:
            print(f"[CRON][SCANNER] ticker {ticker} evaluation failed: {e_ticker}")
            continue
    return records, evaluated, near_misses


def _publish_signal(
    rec: Dict[str, Any],
    shariah: "Any",
    notifier: "Any",
    risk: "Any",
    dry_run: bool,
    allow_delayed: bool = False,
    allow_tv: bool = False,
    tv_map: Optional[Dict[str, Any]] = None,
    allow_oanor: bool = False,
    oanor_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dispatch one approved plan: market gate -> Live Price Guard -> reprice -> track -> card -> dispatch.

    Order:
      0. MARKET HOURS GATE: if is_market_open() is False the live dispatch is
         IMMEDIATELY ABORTED — no Telegram/Supabase signal emission after close.
      1. Dedup: skip when an ACTIVE/TRACKING signal already exists for the ticker.
      2. Live Price Guard: strict Yahoo quote (fast_info / 1m, same-session),
         then oanor snapshot, then TV snapshot (both session-gated + sanity
         banded), then delayed-daily (badged). EXPIRED_ENTRY discards stale
         entries; phantom prints are rejected.
      3. Dynamic reprice: entry is re-anchored to the LIVE price and SL/TP1/TP2/TP3
         are recalculated from it (official RiskManager + fib_targets).
      4. Dynamic track classification from the realized fingerprint
         (invest: TQI>=8 & wide T3 & sane P/E; scalping: tight SL & close T1;
         else swing).
      5. Official Signal Card Formatter (real Shariah status + explicit track
         badge + price-source badge) and per-track channel broadcast + Supabase upsert.
    """
    from egx_quant.core.risk_engine import atr as atr_fn
    from egx_quant.core.strategy_engine import fib_targets, impulse_swings
    from egx_quant.utils.telegram_notifier import build_join_markup, clean_ticker, TelegramNotifier

    plan = rec["plan"]
    ticker = str(plan.symbol)
    outcome: Dict[str, Any] = {"ticker": ticker, "guard": "pending", "supabase": "skipped", "telegram": "skipped"}

    # 0) MARKET HOURS GATE — no live signal emissions outside the EGX session
    if not dry_run and not is_market_open():
        outcome["guard"] = (
            f"aborted-market-closed (session={session_label()} at "
            f"{now_cairo().strftime('%H:%M')} Cairo)"
        )
        print(f"[CRON][SCANNER] {ticker} dispatch ABORTED: EGX market closed - no live Telegram emission")
        return outcome

    # 1) Dedup (repeated 15-min cron hits never spam)
    if _has_active_signal(ticker):
        outcome["guard"] = "dedup-active-exists"
        outcome["supabase"] = "dedup-active-exists"
        outcome["telegram"] = "dedup-active-exists"
        print(f"[CRON][SCANNER] {ticker} already has an ACTIVE signal - publish skipped (dedup)")
        return outcome

    # 2) Live Price Guard — entry must match the CURRENT live price, not a lagging bar.
    # Delayed mode (allow_delayed): accept the latest daily close with an
    # explicit as-of date; the card carries a delayed-data badge.
    live_price, quote_source, quote_asof = _fetch_live_quote(
        ticker, allow_delayed=allow_delayed, allow_tv=allow_tv,
        tv_hint=(tv_map or {}).get(ticker) if tv_map else None,
        allow_oanor=allow_oanor,
        oanor_hint=(oanor_map or {}).get(ticker) if oanor_map else None,
    )
    if live_price is None or live_price <= 0:
        outcome["guard"] = f"skipped-no-live-quote ({quote_source})"
        print(f"[CRON][SCANNER] {ticker} no valid live quote ({quote_source}) - publish skipped (cannot validate entry)")
        return outcome
    rec["live_price"] = live_price
    rec["quote_source"] = quote_source
    rec["quote_asof"] = quote_asof
    rec["delayed_quote"] = (quote_source == "delayed_daily")
    calc_entry = float(plan.entry_price)
    tp1 = float(plan.target_1 or 0)
    if tp1 > 0 and live_price > tp1:
        outcome["guard"] = f"EXPIRED_ENTRY (live {live_price} already beyond TP1 {tp1:.2f})"
        print(f"[CRON][SCANNER] {ticker} DISCARDED: {outcome['guard']}")
        return outcome
    if live_price < calc_entry * 0.99:
        outcome["guard"] = f"EXPIRED_ENTRY (live {live_price} dropped >1% below calculated entry {calc_entry:.2f})"
        print(f"[CRON][SCANNER] {ticker} DISCARDED: {outcome['guard']}")
        return outcome
    # Phantom-print guard for non-Yahoo sources (oanor / tv / delayed_daily):
    # reject quotes deviating wildly from the calculated entry.
    if quote_source in ("oanor", "tv", "delayed_daily"):
        try:
            from egx_quant.utils.tv_fallback import tv_quote_sane
            if not tv_quote_sane(live_price, calc_entry):
                outcome["guard"] = f"phantom-reject ({quote_source} {live_price} vs entry {calc_entry:.2f})"
                print(f"[CRON][SCANNER] {ticker} DISCARDED: {outcome['guard']}")
                return outcome
        except Exception:
            pass

    # 3) Dynamic reprice — SL/TP1-3 recalculated from the CURRENT LIVE price
    df = rec.get("df")
    t1 = t2 = t3 = None
    if df is not None:
        try:
            swing_low, swing_high = impulse_swings(df)
            range_ = swing_high - swing_low
            atr_val = atr_fn(df)
            t1, t2, t3 = fib_targets(live_price, swing_high, range_, atr_val)
        except Exception as exc:
            print(f"[CRON][SCANNER][WARN] {ticker} fib reprice failed ({exc}) - falling back to ATR-only plan")
            t1 = t2 = t3 = None
    final_plan = risk.build_plan(
        ticker,
        live_price,
        df,
        take_profit_override=t3,
        tqi_score=rec["signal"].tqi_score,
        targets=[t for t in (t1, t2, t3) if t is not None],
    )
    if not final_plan.approved:
        outcome["guard"] = "passed"
        outcome["supabase"] = f"skipped-risk-reject: {final_plan.rejection_reason_en}"
        print(f"[CRON][SCANNER] {ticker} plan rejected after live reprice: {final_plan.rejection_reason_en}")
        return outcome
    rec["plan"] = final_plan
    rec["entry_source"] = "live_intraday_quote"
    outcome["guard"] = f"passed (entry re-anchored {calc_entry:.2f} -> live {live_price})"
    plan = final_plan
    print(
        f"[CRON][SCANNER] LIVE-REPRICE {ticker} | entry={plan.entry_price} sl={plan.stop_loss} "
        f"tp1={plan.target_1} tp2={plan.target_2} tp3={plan.target_3} rr_tp1={_rr_ratio(plan)}"
    )

    # 4) Dynamic track classification from the REALIZED fingerprint (TQI + SL/TP
    # profile + P/E when oanor provided it). Absurd P/E demotes invest->swing.
    pe_ratio = None
    try:
        _pq = (oanor_map or {}).get(ticker) or {}
        _pev = _pq.get("pe_ratio")
        pe_ratio = float(_pev) if _pev is not None else None
    except Exception:
        pe_ratio = None
    rec["pe_ratio"] = pe_ratio
    track = classify_track(plan.entry_price, plan.stop_loss, plan.target_1, plan.target_3, plan.tqi_score, pe=pe_ratio)
    rec["track"] = track
    print(f"[CRON][SCANNER] TRACK {ticker}: {track} (tqi={plan.tqi_score} rr_tp1={_rr_ratio(plan)} pe={pe_ratio})")

    # 5) Official Signal Card Formatter — real Shariah status + explicit track badge
    card = notifier.format_channel_broadcast(plan, 0, trade_track=track)
    if rec.get("delayed_quote"):
        card += (
            f"\n⚠️ دخول محسوب على سعر متأخر (آخر تحديث {rec.get('quote_asof') or 'غير معروف'}) - "
            f"راجع السعر الحالي قبل التنفيذ."
        )
    if quote_source == "tv":
        card += "\n⚠️ تم التحقق من السعر عبر TradingView (لحظي) - راجع السعر الحالي قبل التنفيذ."
    if quote_source == "oanor":
        card += "\n⚠️ تم التحقق من السعر عبر oanor (لحظي) - راجع السعر الحالي قبل التنفيذ."
    markup = build_join_markup(0, clean_ticker(plan.symbol))
    if dry_run:
        print(f"[CRON][SCANNER][DRY-RUN][TELEGRAM PAYLOAD] {ticker}")
        print(card)
        print(f"[CRON][SCANNER][DRY-RUN][TELEGRAM MARKUP] {json.dumps(markup, ensure_ascii=False)}")
        outcome["supabase"] = "dry-run (would upsert trade_signals)"
        outcome["telegram"] = "dry-run (would broadcast official card)"
        return outcome
    try:
        from egx_quant.utils import supabase_sync
        payload = {
            "ticker": ticker,
            "strategy_type": track,
            "entry_price": plan.entry_price,
            "stop_loss": plan.stop_loss,
            "current_stop_loss": plan.stop_loss,
            "target_1": plan.target_1,
            "target_2": plan.target_2,
            "target_3": plan.target_3,
            "tqi_score": plan.tqi_score,
            "shariah_status": shariah.get_status(plan.symbol).value,
            "status": "ACTIVE",
        }
        ok = supabase_sync.publish_trade_signal(payload)
        outcome["supabase"] = "published" if ok else "failed"
        print(f"[CRON][SCANNER] Supabase publish {ticker}: {'OK' if ok else 'FAILED'}")
    except Exception as exc:
        outcome["supabase"] = f"error: {str(exc)[:120]}"
        print(f"[CRON][SCANNER][ERROR] Supabase publish {ticker} crashed: {exc}")
    try:
        scalp_fallback = _get_scalping_channel_id()
        target_channel = _channel_for_track(track, scalp_fallback)
        sender = notifier if target_channel == scalp_fallback else TelegramNotifier(channel_id=target_channel)
        ok = sender.broadcast_signal(card, markup)
        outcome["telegram"] = f"broadcast:{track}" if ok else "failed/mock"
        outcome["channel"] = target_channel
        print(f"[CRON][SCANNER] Telegram broadcast {ticker} -> {track} channel: {'OK' if ok else 'FAILED/mock'}")
    except Exception as exc:
        outcome["telegram"] = f"error: {str(exc)[:120]}"
        print(f"[CRON][SCANNER][ERROR] Telegram broadcast {ticker} crashed: {exc}")
    return outcome


def run_scan_pipeline(dry_run: bool = False) -> Dict[str, Any]:
    """Full batched scan: ingest -> shariah -> strategy -> card -> dispatch."""
    started = datetime.now(timezone.utc)
    if dry_run:
        try:
            import logging
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        except Exception:
            pass
    universe = _universe()
    print(f"[CRON][SCANNER] pipeline start | universe={len(universe)} tickers | batch_size={BATCH_SIZE} | dry_run={dry_run}")
    print(
        f"[CRON][SCANNER] market hours gate: is_market_open={is_market_open()} "
        f"(session={session_label()} at {now_cairo().strftime('%H:%M')} Cairo) - "
        f"live signal emissions {'ACTIVE' if is_market_open() else 'SUPPRESSED'}"
    )
    result: Dict[str, Any] = {
        "mode": "full-egx-batched" + (" (dry-run)" if dry_run else ""),
        "universe_size": len(universe),
        "universe": universe,
        "session": {"is_market_open": is_market_open(), "cairo_local": now_cairo().isoformat()},
        "shariah": {"policy": "transparent-scan", "counts": {}, "statuses": {}},
        "evaluated": 0,
        "batches": [],
        "signals": [],
        "monitor": None,
    }
    try:
        import yfinance as yf  # type: ignore
        import pandas as pd  # type: ignore
    except Exception as exc:
        print(f"[CRON][SCANNER][ERROR] yfinance/pandas unavailable: {exc}")
        result["error"] = f"deps: {exc}"
        return result

    # --- (b) Shariah TRANSPARENCY: all registry tickers are processed; the real
    #     status (COMPLIANT / NEEDS_REVIEW / NON_COMPLIANT) is featured on every card
    try:
        from egx_quant.core.strategy_engine import StrategyEngine
        from egx_quant.core.risk_engine import RiskManager
        shariah = _TransparentShariahGate()
        strategy = StrategyEngine(shariah_filter=shariah)
        risk = RiskManager()
    except Exception as exc:
        print(f"[CRON][SCANNER][ERROR] engine modules unavailable: {exc}")
        result["error"] = f"engines: {exc}"
        return result
    statuses = {s: shariah.get_status(s).value for s in universe}
    status_counts: Dict[str, int] = {}
    for st in statuses.values():
        status_counts[st] = status_counts.get(st, 0) + 1
    result["shariah"] = {
        "policy": "transparent-scan (all tickers processed; exact status featured on card)",
        "counts": status_counts,
        "statuses": statuses,
    }
    print(
        f"[CRON][SCANNER] shariah transparency: processing ALL {len(universe)} tickers "
        f"({status_counts.get('COMPLIANT', 0)} compliant, {status_counts.get('NEEDS_REVIEW', 0)} needs-review, "
        f"{status_counts.get('NON_COMPLIANT', 0)} non-compliant) - status featured on card"
    )

    # Liquidity pre-check: skip dead tickers before the heavy 6mo pass (budget guard).
    liquid_map = _liquidity_prescreen(universe)
    skipped = sorted(t for t in universe if not liquid_map.get(t, {}).get("ok", True))
    for t in skipped:
        print(f"[CRON][SCANNER][ILLIQUID-SKIP] {t} ({liquid_map.get(t, {}).get('reason')}) - heavy pass skipped")
    trade_universe = [t for t in universe if t not in set(skipped)]
    result["prescreen_skipped"] = skipped
    result["trade_universe_size"] = len(trade_universe)
    if not trade_universe:
        print("[CRON][SCANNER][WARN] prescreen dropped everything - failing open to full universe")
        trade_universe = list(universe)
        result["prescreen_skipped"] = []
        result["trade_universe_size"] = len(trade_universe)
    batches = [trade_universe[i:i + BATCH_SIZE] for i in range(0, len(trade_universe), BATCH_SIZE)]
    all_records: List[Dict[str, Any]] = []
    all_near_misses: List[Dict[str, Any]] = []
    scan_stats: Dict[str, Any] = {}  # stale-frame census for the feed-freeze monitor
    # Delayed-data mode: evaluate bars up to max_lag sessions old (SCANNER_MAX_BAR_LAG,
    # default 2). 0 = strict same-session only. Publishing still requires a live
    # quote, or an explicitly badged delayed quote.
    max_lag = _scanner_max_lag()
    allow_delayed = max_lag > 0
    result["max_bar_lag"] = max_lag
    if max_lag > 0:
        print(f"[CRON][SCANNER] delayed-data mode ON: evaluating bars up to {max_lag} session(s) old (badged)")
    result["deadline_cut"] = False
    for idx, batch in enumerate(batches, start=1):
        t0 = datetime.now(timezone.utc)
        if idx > 1 and (t0 - started).total_seconds() > SCAN_DEADLINE_SECONDS:
            print(f"[CRON][SCANNER][DEADLINE] {(t0 - started).total_seconds():.0f}s > {SCAN_DEADLINE_SECONDS:.0f}s - "
                  f"stopping after {idx - 1}/{len(batches)} batches (remainder resumes next cycle)")
            result["deadline_cut"] = True
            break
        try:
            print(f"[CRON][SCANNER] batch {idx}/{len(batches)}: downloading {len(batch)} tickers ({KLINE_PERIOD}/{KLINE_INTERVAL} daily bars)")
            data = yf.download(
                tickers=batch,
                period=KLINE_PERIOD,
                interval=KLINE_INTERVAL,
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
            b_secs = (datetime.now(timezone.utc) - t0).total_seconds()
            records, evaluated, near_misses = _run_strategy_batch(batch, data, strategy, risk, stats=scan_stats, max_lag=max_lag)
            all_records.extend(records)
            all_near_misses.extend(near_misses)
            result["batches"].append({
                "batch": idx,
                "tickers": len(batch),
                "download_seconds": round(b_secs, 1),
                "evaluated": evaluated,
                "candidates": len(records),
            })
            result["evaluated"] += evaluated
            print(f"[CRON][SCANNER] batch {idx}/{len(batches)} done in {b_secs:.1f}s | evaluated={evaluated} signals={len(records)}")
        except Exception as exc:
            b_secs = (datetime.now(timezone.utc) - t0).total_seconds()
            print(f"[CRON][SCANNER][WARN] batch {idx}/{len(batches)} failed after {b_secs:.1f}s: {exc}")
            result["batches"].append({"batch": idx, "tickers": len(batch), "download_seconds": round(b_secs, 1), "error": str(exc)[:150]})

    print(f"[CRON][SCANNER] evaluation complete: {result['evaluated']} evaluated, {len(all_records)} signal(s)")
    stale_total = int(scan_stats.get("stale", 0) or 0)
    result["stale_frames"] = stale_total
    result["delayed_eval"] = int(scan_stats.get("delayed_eval", 0) or 0)
    # DATA-FEED FREEZE MONITOR: a silent Yahoo freeze drops the whole universe
    # (evaluated=0) with zero signals and zero errors. Page the admin instead
    # of staying silent (throttled to one alert per day by _notify_admin).
    # NOTE: on genuine market holidays this also fires - the message says so.
    if stale_total >= max(5, (len(universe) // 2) or 1) and not dry_run:
        try:
            from collections import Counter as _Counter
            _dates = [d for d in (scan_stats.get("stale_dates") or []) if d and d != "None"]
            top_date = _Counter(_dates).most_common(1)[0][0] if _dates else "unknown"
        except Exception:
            top_date = "unknown"
        try:
            from egx_quant.engine.trade_monitor import _notify_admin
            _notify_admin(
                "تجمد مصدر أسعار البورصة",
                f"آخر شمعة يومية لأغلب الأسهم بتاريخ {top_date} بينما جلسة اليوم {now_cairo().date()} "
                f"({stale_total}/{len(universe)} سهم مرفوض) - تقييم السكانر متوقف والنشرات قد تعرض بيانات متأخرة. "
                f"تحقق من مصدر Yahoo (أو عطلة رسمية للسوق).",
                throttle_key="feed-freeze",
            )
        except Exception as e_mon:
            print(f"[CRON][SCANNER][WARN] freeze admin alert failed: {e_mon}")
    result["near_miss"] = all_near_misses
    for nm in all_near_misses:
        print(
            f"[CRON][SCANNER][NEAR-MISS] {nm['ticker']} | Price={nm['close']} SMA20={nm['sma20']} "
            f"RSI={nm['rsi']} | confluence {nm['checks_met']}/3 | failed: {', '.join(nm['failed'])} | TQI: {nm['tqi']}"
        )
    if not all_records:
        print(
            f"NO_LIVE_SIGNALS_TODAY: Evaluated {result['evaluated']}/{len(universe)} "
            f"registry tickers, 0 satisfied strict entry criteria."
        )
    try:
        from egx_quant.utils.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier(channel_id=_get_scalping_channel_id())
    except Exception as exc:
        print(f"[CRON][SCANNER][ERROR] TelegramNotifier unavailable: {exc}")
        notifier = None

    # oanor pre-fetch (ONE batched call for all candidates; skipped when empty).
    # TV hints resolve per ticker inside _fetch_live_quote (few candidates).
    oanor_map: Dict[str, Any] = {}
    allow_oanor = False
    try:
        from egx_quant.utils.oanor_client import oanor_enabled, fetch_oanor_quotes
        allow_oanor = bool(oanor_enabled())
    except Exception:
        pass
    if allow_oanor and all_records:
        try:
            oanor_map = fetch_oanor_quotes([rec["ticker"] for rec in all_records])
            if oanor_map:
                print(f"[CRON][SCANNER] oanor pre-fetch resolved {len(oanor_map)} quote(s)")
        except Exception as e_oa:
            print(f"[CRON][SCANNER][WARN] oanor pre-fetch failed: {e_oa}")
            oanor_map = {}
    result["allow_oanor"] = allow_oanor

    for rec in all_records:
        if notifier is None:
            outcome: Dict[str, Any] = {"ticker": rec["ticker"], "guard": "skipped-no-notifier", "supabase": "skipped", "telegram": "skipped"}
        else:
            outcome = _publish_signal(rec, shariah, notifier, risk, dry_run, allow_delayed=allow_delayed, allow_oanor=allow_oanor, oanor_map=oanor_map)
        plan = rec["plan"]
        result["signals"].append({
            "ticker": rec["ticker"],
            "strategy_tag": rec["signal"].strategy_tag,
            "signal": "confluence_buy",
            "track": rec.get("track", "swing"),
            "entry_price": plan.entry_price,
            "entry_source": rec.get("entry_source", "daily_close"),
            "live_price": rec.get("live_price"),
            "quote_source": rec.get("quote_source"),
            "delayed": bool(rec.get("delayed_quote", False)),
            "price_asof": rec.get("quote_asof"),
            "pe_ratio": rec.get("pe_ratio"),
            "stop_loss": plan.stop_loss,
            "target_1": plan.target_1,
            "target_2": plan.target_2,
            "target_3": plan.target_3,
            "tqi_score": plan.tqi_score,
            "rr_tp1": _rr_ratio(plan),
            "rsi": rec["rsi"],
            "sma20": rec["sma20"],
            "shariah_status": shariah.get_status(rec["ticker"]).value,
            "publish": outcome,
        })
    result["signals_found"] = len(all_records)

    # Trade monitor (target/SL/trailing alerts) within remaining budget
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    remaining = TIME_BUDGET_SECONDS - elapsed
    if remaining > 2:
        try:
            from egx_quant.engine.trade_monitor import run_monitor_cycle
            print(f"[CRON][SCANNER] Running trade monitor (remaining budget {remaining:.0f}s, dry_run={dry_run})...")
            mon_res = run_monitor_cycle(dry_run=dry_run)
            result["monitor"] = {
                "ok": True,
                "signals_scanned": mon_res.get("signals_scanned", 0),
                "target_hits": mon_res.get("target_hits", 0),
                "sl_hits": mon_res.get("sl_hits", 0),
            }
            print(f"[CRON][SCANNER] monitor done: scanned={mon_res.get('signals_scanned', 0)} targets={mon_res.get('target_hits', 0)} sl={mon_res.get('sl_hits', 0)}")
        except Exception as e_mon:
            print(f"[CRON][SCANNER][WARN] monitor skipped/failed: {e_mon}")
            result["monitor"] = {"ok": False, "error": str(e_mon)[:150]}
    else:
        result["monitor"] = {"ok": True, "skipped": "time budget <2s"}

    ended = datetime.now(timezone.utc)
    result["started"] = started.isoformat()
    result["ended"] = ended.isoformat()
    result["duration_seconds"] = round((ended - started).total_seconds(), 1)
    print(f"[CRON][SCANNER] pipeline complete in {result['duration_seconds']}s | evaluated={result['evaluated']} signals={len(all_records)}")
    return result


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()
    def do_POST(self):
        self._handle()
    def _handle(self):
        started = datetime.now(timezone.utc)
        auth_ok, auth_reason = _is_authorized(self)
        print(f"[CRON][SCANNER] incoming {self.command} {self.path} auth={auth_ok} reason={auth_reason} at {started.isoformat()}")
        if not auth_ok:
            self.send_response(401)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "unauthorized", "reason": auth_reason}).encode())
            return

        # Window guard: only run during active session 06:00-12:30 UTC (09:00-15:30 Cairo) Sun-Thu
        try:
            hour = started.hour + started.minute / 60.0
            gh_dow = (started.weekday() + 1) % 7  # Sun=0
            in_window = 6.0 <= hour <= 12.5 and gh_dow in (0, 1, 2, 3, 4)
            print(f"[CRON][AUDIT] Scheduled */15 6-12 UTC (09:00-15:30 Cairo) | Now {started.strftime('%H:%M UTC')} dow={gh_dow} | in_window={in_window}")
        except Exception as e:
            print(f"[CRON][AUDIT] window check failed: {e}")
            in_window = True

        dry_run = (os.environ.get("SCANNER_DRY_RUN") or "").strip() in ("1", "true", "True")
        # Safe-probe mode: ?dry_run=1 forces dry-run (no Supabase writes / Telegram
        # sends) regardless of env - health checks must be side-effect free.
        try:
            from urllib.parse import urlparse as _up, parse_qs as _pqs
            _vals = _pqs(_up(self.path).query).get("dry_run", [])
            if any(str(v or "").strip().lower() in ("1", "true", "yes") for v in _vals):
                dry_run = True
                print("[CRON][SCANNER] dry-run probe via ?dry_run=1")
        except Exception:
            pass

        if in_window:
            scan = run_scan_pipeline(dry_run=dry_run)
            duration = scan.get("duration_seconds", 0.0)
            status = "completed" if duration < 50 else "scan_started"
            resp_body: Dict[str, Any] = {
                "ok": True,
                "status": status,
                "now": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": duration,
                "auth": auth_reason,
                "schedule": "external cron */15 6-12 * * 0-4 -> every 15m 06:00-12:30 UTC (09:00-15:30 Cairo) Sun-Thu",
                "result": scan,
            }
        else:
            resp_body = {
                "ok": True,
                "status": "outside-window",
                "now": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
                "auth": auth_reason,
                "schedule": "*/15 6-12 * * 0-4 -> every 15m 06:00-12:30 UTC (09:00-15:30 Cairo) Sun-Thu",
            }

        try:
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_body).encode())
            print(f"[CRON][SCANNER] HTTP 200 returned status={resp_body.get('status')} duration={resp_body.get('duration_seconds')}s signals={len((resp_body.get('result') or {}).get('signals', []))}")
        except Exception as e:
            print(f"[CRON][SCANNER][ERROR] failed to send 200: {e}")
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true, "status": "scan_started"}')
            except Exception:
                pass

    def log_message(self, format, *args):
        try:
            print(f"[VERCEL-CRON] {format % args}")
        except Exception:
            pass


# Direct execution support: `python api/scanner.py [--dry-run]`
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    dry = "--dry-run" in sys.argv
    print(f"[DIRECT] Running api/scanner.py directly (dry_run={dry}) -> SCALPING channel {_get_scalping_channel_id()}")
    summary = run_scan_pipeline(dry_run=dry)
    print(json.dumps({k: v for k, v in summary.items() if k != "universe"}, ensure_ascii=False, indent=2, default=str))
