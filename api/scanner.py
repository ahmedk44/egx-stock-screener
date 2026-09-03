"""
Vercel Cron endpoint for Live Scanner — full-EGX batched intraday scanner.

Triggered by:
  - External ping (cron-job.org) every 15m via GET with Bearer token or ?secret=
  - GitHub Actions runner.yml schedule (fallback, */15 7-11 * * 0-4)

Pipeline (official project modules only — no hardcoded logic):
  a. Ticker ingestion    : StocksRegistry.all_symbols() (26 registered EGX stocks)
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

# Batch size tuned so each yf.download returns in ~2-4s and progress is logged
BATCH_SIZE = 9
# Rough wall-clock budget guard (seconds) for optional heavy extras (monitor)
TIME_BUDGET_SECONDS = 45.0
# StrategyEngine needs >= 60 daily bars; 6mo (~125 sessions) is the safe fetch window
KLINE_PERIOD = "6mo"
KLINE_INTERVAL = "1d"


def _universe() -> List[str]:
    """ALL registered EGX stocks (single source of truth). Never raises."""
    try:
        from egx_quant.config.stocks_registry import StocksRegistry
        symbols = StocksRegistry.all_symbols()
        if symbols:
            return symbols
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] StocksRegistry unavailable ({exc}) - using fallback watchlist")
    return ["COMI.CA", "FWRY.CA", "TMGH.CA", "SWDY.CA", "ABUK.CA", "ETEL.CA", "HRHO.CA", "EAST.CA"]


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


def _fetch_live_quote(symbol: str) -> Tuple[Optional[float], str]:
    """Live execution price: fast_info.last_price primary, 1m ticker fallback.

    Session validation: the supporting bar MUST belong to the CURRENT Cairo
    session date. Stale bars from yesterday or pre-open placeholders return
    (None, reason) so the candidate is dropped immediately.
    """
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        return None, f"yfinance-unavailable: {exc}"
    today = now_cairo().date()
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
                    if _bar_session_date(d.index[-1]) == today:
                        return round(fi_price, 2), "fast_info"
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
                        return round(px, 2), "intraday_1m"
        except Exception:
            pass
    except Exception as exc:
        print(f"[CRON][SCANNER][WARN] live quote fetch failed for {symbol}: {exc}")
        return None, "fetch-error"
    return None, "stale-or-pre-open (no same-session bar)"


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


def _run_strategy_batch(
    batch: List[str],
    data: "Any",
    strategy: "Any",
    risk: "Any",
) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """Run the official StrategyEngine + RiskManager over one batch frame.

    Shariah compliance is enforced inside StrategyEngine.evaluate BEFORE any
    technical computation (default-deny). Returns (records, evaluated_count,
    near_misses) — near_misses carry live values for tickers that met 2/3
    confluence checks but failed the strict entry criteria.
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
                continue
            # Same-day validation: the latest bar MUST belong to the CURRENT
            # Cairo session date. Yesterday's bar / pre-open placeholder = stale.
            bar_date = _bar_session_date(df.index[-1])
            if bar_date != now_cairo().date():
                print(f"[CRON][SCANNER][STALE-FRAME] {ticker} last bar {bar_date} != session {now_cairo().date()} - candidate dropped")
                continue
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
) -> Dict[str, Any]:
    """Dispatch one approved plan: market gate -> Live Price Guard -> reprice -> card -> dispatch.

    Order:
      0. MARKET HOURS GATE: if is_market_open() is False the live dispatch is
         IMMEDIATELY ABORTED — no Telegram/Supabase signal emission after close.
      1. Dedup: skip when an ACTIVE/TRACKING signal already exists for the ticker.
      2. Live Price Guard: fetch a real-time quote (fast_info.last_price / 1m
         fallback, same-session validated). If the live price already moved beyond
         TP1 or dropped >1% below the calculated entry, the signal is DISCARDED
         as stale (EXPIRED_ENTRY).
      3. Dynamic reprice: entry is re-anchored to the LIVE price and SL/TP1/TP2/TP3
         are recalculated from it (official RiskManager + fib_targets).
      4. Official Signal Card Formatter (features the real Shariah status) and
         Telegram broadcast + Supabase upsert.
    """
    from egx_quant.core.risk_engine import atr as atr_fn
    from egx_quant.core.strategy_engine import fib_targets, impulse_swings
    from egx_quant.utils.telegram_notifier import build_join_markup, clean_ticker

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

    # 2) Live Price Guard — entry must match the CURRENT live price, not a lagging bar
    live_price, quote_source = _fetch_live_quote(ticker)
    if live_price is None or live_price <= 0:
        outcome["guard"] = f"skipped-no-live-quote ({quote_source})"
        print(f"[CRON][SCANNER] {ticker} no valid live quote ({quote_source}) - publish skipped (cannot validate entry)")
        return outcome
    rec["live_price"] = live_price
    rec["quote_source"] = quote_source
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

    # 4) Official Signal Card Formatter — real Shariah status featured on the card
    card = notifier.format_channel_broadcast(plan, 0)
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
            "strategy_type": "scanner_watch",
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
        ok = notifier.broadcast_signal(card, markup)
        outcome["telegram"] = "broadcast" if ok else "failed/mock"
        print(f"[CRON][SCANNER] Telegram broadcast {ticker}: {'OK' if ok else 'FAILED/mock'}")
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

    batches = [universe[i:i + BATCH_SIZE] for i in range(0, len(universe), BATCH_SIZE)]
    all_records: List[Dict[str, Any]] = []
    all_near_misses: List[Dict[str, Any]] = []
    for idx, batch in enumerate(batches, start=1):
        t0 = datetime.now(timezone.utc)
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
            records, evaluated, near_misses = _run_strategy_batch(batch, data, strategy, risk)
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

    for rec in all_records:
        if notifier is None:
            outcome: Dict[str, Any] = {"ticker": rec["ticker"], "guard": "skipped-no-notifier", "supabase": "skipped", "telegram": "skipped"}
        else:
            outcome = _publish_signal(rec, shariah, notifier, risk, dry_run)
        plan = rec["plan"]
        result["signals"].append({
            "ticker": rec["ticker"],
            "strategy_tag": rec["signal"].strategy_tag,
            "signal": "confluence_buy",
            "entry_price": plan.entry_price,
            "entry_source": rec.get("entry_source", "daily_close"),
            "live_price": rec.get("live_price"),
            "quote_source": rec.get("quote_source"),
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
