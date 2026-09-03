"""
Vercel Cron endpoint for Live Scanner — full-EGX batched intraday scanner.

Triggered by:
  - External ping (cron-job.org) every 15m via GET with Bearer token or ?secret=
  - GitHub Actions runner.yml schedule (fallback, */15 7-11 * * 0-4)

Optimizations for serverless budget:
  - Universe: ALL registered EGX stocks (StocksRegistry, 25+ tickers) — not a hardcoded subset
  - Batched yfinance download (BATCH_SIZE=9, threads=True) with per-batch timing logs
  - DAILY bars (3mo/1d) for SMA20 + RSI14 — vectorized pandas, no per-ticker loops.
    NOTE: Yahoo intraday endpoints (15m/30m/1h/5m) are BROKEN for EGX on yfinance 1.6.0
    (KeyError: tradingPeriods on every .CA ticker) — daily is the only reliable interval.
  - Signals are PUBLISHED: Supabase trade_signals upsert (dedup per ticker) +
    Telegram watch-alert broadcast to the scalping channel (dedup vs existing
    ACTIVE/TRACKING signals so repeated 15m cron hits never spam)
  - Trade monitor cycle (target/SL/trailing alerts) runs in the remaining budget

Behavior:
  - Returns JSON {ok, status, duration_seconds, universe_size, evaluated, signals, monitor}
  - Dry run: `python api/scanner.py --dry-run` (no Supabase writes / Telegram sends)

Timing budget (Fluid compute): target <30s; vercel.json sets maxDuration=60.
"""
import json
import os
import sys
# Ensure project root is on sys.path for Vercel runtime (/var/task)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional, Tuple

# Batch size tuned so each yf.download returns in ~2-4s and progress is logged
BATCH_SIZE = 9
# Rough wall-clock budget guard (seconds) for optional heavy extras (monitor)
TIME_BUDGET_SECONDS = 45.0


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


def _rsi_series(close: "Any") -> "Any":
    """RSI(14) via Wilder ewm — pandas-only, always returns a Series.

    Pure-gain windows (loss==0) yield inf RS -> RSI 100; flat windows (0/0)
    yield NaN -> neutral 50. Never returns a scalar (was breaking .empty checks).
    """
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss  # inf when loss==0 (pure gains), NaN when both 0 (flat)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _evaluate_batch(batch: List[str], data: "Any") -> Tuple[List[Dict[str, Any]], int]:
    """Vectorized SMA20/RSI14 evaluation (DAILY closes) for one batch frame.

    Returns (candidates, evaluated_count). Candidates carry ticker/close/sma20/rsi.
    """
    import pandas as pd  # type: ignore

    candidates: List[Dict[str, Any]] = []
    evaluated = 0
    multi = isinstance(data.columns, pd.MultiIndex)
    level0 = set(data.columns.get_level_values(0)) if multi else set()
    for ticker in batch:
        try:
            if multi:
                if ticker not in level0:
                    continue
                df = data[ticker].dropna(subset=["Close"])
            else:
                df = data.dropna(subset=["Close"])
            if df is None or df.empty or len(df) < 20:
                continue
            evaluated += 1
            close = df["Close"].astype(float)
            last_close = float(close.iloc[-1])
            sma20 = float(close.rolling(20).mean().iloc[-1])
            rsi_series = _rsi_series(close)
            last_rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else 50.0
            # Daily watch condition: trend-up above SMA20 with moderate momentum
            if last_close > sma20 and 50 < last_rsi < 70:
                candidates.append({
                    "ticker": ticker,
                    "close": round(last_close, 2),
                    "sma20": round(sma20, 2),
                    "rsi": round(last_rsi, 1),
                    "signal": "watch",
                })
        except Exception as e_ticker:
            print(f"[CRON][SCANNER] ticker {ticker} evaluation failed: {e_ticker}")
            continue
    return candidates, evaluated


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


def _build_signal_payload(cand: Dict[str, Any]) -> Dict[str, Any]:
    """Schema-aligned trade_signals payload for a scanner watch candidate."""
    close = float(cand["close"])
    payload = {
        "ticker": cand["ticker"],
        "strategy_type": "scanner_watch",
        "entry_price": close,
        "stop_loss": round(close * 0.97, 2),
        "current_stop_loss": round(close * 0.97, 2),
        "target_1": round(close * 1.05, 2),
        "target_2": round(close * 1.10, 2),
        "tqi_score": 6.0,
        "shariah_status": "COMPLIANT",
        "status": "ACTIVE",
    }
    try:
        from egx_quant.config.stocks_registry import StocksRegistry, ShariahStatus
        payload["shariah_status"] = StocksRegistry.status(cand["ticker"]).value
    except Exception:
        pass
    return payload


def _format_watch_card(cand: Dict[str, Any]) -> str:
    bare = cand["ticker"].replace(".CA", "")
    return (
        f"🔎 <b>رادار المسح اللحظي | Scanner Watch Alert</b>\n"
        f"------------------------------------\n"
        f"⚡ <b>{bare}</b> على رادار الزخم الصاعد\n"
        f"💵 السعر: {cand['close']:.2f} EGP | RSI: {cand['rsi']} | SMA20: {cand['sma20']:.2f}\n"
        f"📈 السعر يتداول فوق متوسط 20 مع زخم إيجابي — بانتظار تأكيد الاختراق\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"⚠️ تحليل استرشادي - وليس توصية شراء"
    )


def _publish_candidate(cand: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """Publish one candidate: Supabase upsert + channel watch-alert broadcast.

    Dedup: skips both when an ACTIVE/TRACKING signal already exists for the ticker
    (repeated 15-min cron hits, or the daemon/GH screener already signaled it).
    """
    outcome: Dict[str, Any] = {"ticker": cand["ticker"], "supabase": "skipped", "telegram": "skipped"}
    if _has_active_signal(cand["ticker"]):
        outcome["supabase"] = "dedup-active-exists"
        outcome["telegram"] = "dedup-active-exists"
        print(f"[CRON][SCANNER] {cand['ticker']} already has an ACTIVE signal - publish skipped (dedup)")
        return outcome
    if dry_run:
        outcome["supabase"] = "dry-run (would upsert trade_signals)"
        outcome["telegram"] = "dry-run (would broadcast watch alert)"
        print(f"[CRON][SCANNER][DRY-RUN] would publish {cand['ticker']} -> Supabase + Telegram")
        return outcome
    try:
        from egx_quant.utils import supabase_sync
        ok = supabase_sync.publish_trade_signal(_build_signal_payload(cand))
        outcome["supabase"] = "published" if ok else "failed"
        print(f"[CRON][SCANNER] Supabase publish {cand['ticker']}: {'OK' if ok else 'FAILED'}")
    except Exception as exc:
        outcome["supabase"] = f"error: {str(exc)[:120]}"
        print(f"[CRON][SCANNER][ERROR] Supabase publish {cand['ticker']} crashed: {exc}")
    try:
        from egx_quant.utils.telegram_notifier import TelegramNotifier, build_join_markup
        notifier = TelegramNotifier(channel_id=_get_scalping_channel_id())
        ok = notifier.broadcast_signal(_format_watch_card(cand), build_join_markup(0, cand["ticker"].replace(".CA", "")))
        outcome["telegram"] = "broadcast" if ok else "failed/mock"
        print(f"[CRON][SCANNER] Telegram watch alert {cand['ticker']}: {'OK' if ok else 'FAILED/mock'}")
    except Exception as exc:
        outcome["telegram"] = f"error: {str(exc)[:120]}"
        print(f"[CRON][SCANNER][ERROR] Telegram broadcast {cand['ticker']} crashed: {exc}")
    return outcome


def run_scan_pipeline(dry_run: bool = False) -> Dict[str, Any]:
    """Full batched scan: fetch -> evaluate -> publish. Returns a JSON-safe summary."""
    started = datetime.now(timezone.utc)
    universe = _universe()
    print(f"[CRON][SCANNER] pipeline start | universe={len(universe)} tickers | batch_size={BATCH_SIZE} | dry_run={dry_run}")
    result: Dict[str, Any] = {
        "mode": "full-egx-batched" + (" (dry-run)" if dry_run else ""),
        "universe_size": len(universe),
        "universe": universe,
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

    batches = [universe[i:i + BATCH_SIZE] for i in range(0, len(universe), BATCH_SIZE)]
    all_candidates: List[Dict[str, Any]] = []
    for idx, batch in enumerate(batches, start=1):
        t0 = datetime.now(timezone.utc)
        try:
            print(f"[CRON][SCANNER] batch {idx}/{len(batches)}: downloading {len(batch)} tickers (3mo/1d daily bars)")
            data = yf.download(
                tickers=batch,
                period="3mo",
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
            b_secs = (datetime.now(timezone.utc) - t0).total_seconds()
            cands, evaluated = _evaluate_batch(batch, data)
            all_candidates.extend(cands)
            result["batches"].append({
                "batch": idx,
                "tickers": len(batch),
                "download_seconds": round(b_secs, 1),
                "evaluated": evaluated,
                "candidates": len(cands),
            })
            result["evaluated"] += evaluated
            print(f"[CRON][SCANNER] batch {idx}/{len(batches)} done in {b_secs:.1f}s | evaluated={evaluated} candidates={len(cands)}")
        except Exception as exc:
            b_secs = (datetime.now(timezone.utc) - t0).total_seconds()
            print(f"[CRON][SCANNER][WARN] batch {idx}/{len(batches)} failed after {b_secs:.1f}s: {exc}")
            result["batches"].append({"batch": idx, "tickers": len(batch), "download_seconds": round(b_secs, 1), "error": str(exc)[:150]})

    print(f"[CRON][SCANNER] evaluation complete: {result['evaluated']} evaluated, {len(all_candidates)} candidate(s)")
    for cand in all_candidates:
        print(f"[CRON][SCANNER] candidate: {cand['ticker']} close={cand['close']} sma20={cand['sma20']} rsi={cand['rsi']}")
        outcome = _publish_candidate(cand, dry_run)
        result["signals"].append({**cand, **{"publish": outcome}})
    result["signals_found"] = len(all_candidates)

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
    print(f"[CRON][SCANNER] pipeline complete in {result['duration_seconds']}s | evaluated={result['evaluated']} signals={len(all_candidates)}")
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
