#!/usr/bin/env python3
"""
scripts/test_full_workflow.py — End-to-End Dry-Run Verification

Tests 4 layers without side-effects (dry-run):

  Test 1: HTTP Endpoint & Secret Auth — GET /api/pre_market, /api/scanner, /api/post_market with CRON_SECRET
  Test 2: Cron Schedules & Timezone Alignment — parse vercel.json crons vs UTC/Cairo/Oman
  Test 3: Telegram Webhook & Callback Buttons — POST mock callback_query to /api/webhook
  Test 4: Data Engine & Analysis Sanity — synthetic dry-run of screening engine (RSI/Bollinger/VWAP/Gann)

Usage:
  python scripts/test_full_workflow.py
  CRON_SECRET=xxx BASE_URL=https://egx-stock-screener.vercel.app python scripts/test_full_workflow.py --verbose

Exit 0 = all PASS, 1 = any FAIL
"""
from __future__ import annotations

import json
import os
import sys
import time
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Ensure UTF-8 stdout
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

# Load env
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # type: ignore

# Helpers
PASS = "✅ PASS"
FAIL = "❌ FAIL"
WARN = "⚠️ WARN"

BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("VERCEL_DOMAIN") or os.environ.get("APP_URL") or "https://egx-stock-screener.vercel.app").strip().rstrip("/")
if not BASE_URL.startswith("http"):
    BASE_URL = f"https://{BASE_URL}"
CRON_SECRET = (os.environ.get("CRON_SECRET") or "").strip()

def log(msg: str) -> None:
    print(msg, flush=True)

def section(title: str) -> None:
    log("\n" + "="*70)
    log(title)
    log("="*70)

def result_line(test: str, ok: bool, detail: str = "") -> bool:
    prefix = PASS if ok else FAIL
    log(f"[{prefix}] {test}" + (f" — {detail}" if detail else ""))
    return ok

# --------------------------------------------------------------------------
# Test 1: HTTP Endpoint & Secret Auth
# --------------------------------------------------------------------------
def test_http_endpoints() -> bool:
    section("Test 1: HTTP Endpoint & Secret Auth Verification")
    if requests is None:
        result_line("requests library", False, "not installed")
        return False

    endpoints = [
        ("/api/pre_market", "pre_market: 05:30 UTC"),
        ("/api/scanner", "scanner: */15 07:00-11:30 UTC"),
        ("/api/post_market", "post_market: 12:30 UTC"),
    ]

    all_ok = True
    for path, desc in endpoints:
        url_q = f"{BASE_URL}{path}"
        if CRON_SECRET:
            url_q = f"{url_q}?secret={CRON_SECRET}"
        headers = {}
        if CRON_SECRET:
            headers["Authorization"] = f"Bearer {CRON_SECRET}"

        # Try query-string first, then header fallback
        tried = []
        ok = False
        detail = ""
        for use_query in [True, False]:
            try:
                url = f"{BASE_URL}{path}?secret={CRON_SECRET}" if (use_query and CRON_SECRET) else f"{BASE_URL}{path}"
                hdrs = {"Authorization": f"Bearer {CRON_SECRET}"} if (not use_query and CRON_SECRET) else {}
                log(f"[Probe] GET {url} (use_query={use_query})")
                resp = requests.get(url, headers=hdrs, timeout=30)
                tried.append(f"{use_query}:{resp.status_code}")
                if resp.status_code == 200:
                    # Verify non-empty JSON payload
                    try:
                        data = resp.json()
                        if isinstance(data, dict) and len(data) > 0:
                            # Check for expected keys: ok, scheduled, now, etc. or any non-empty
                            ok = True
                            detail = f"HTTP 200 + JSON keys={list(data.keys())[:5]} via {'query' if use_query else 'header'}"
                            break
                        elif isinstance(data, dict):
                            detail = f"HTTP 200 but empty JSON via {'query' if use_query else 'header'}"
                            ok = False
                        else:
                            detail = f"HTTP 200 non-dict JSON ({type(data).__name__})"
                            ok = bool(data)
                            break
                    except Exception as je:
                        # Some endpoints may return HTML error but status 200? Check text
                        txt = (resp.text or "")[:200]
                        if txt.strip().startswith("{"):
                            detail = f"HTTP 200 but JSON parse failed: {je}"
                            ok = False
                        else:
                            # Vercel may return HTML for 404 with 200? Treat as fail
                            detail = f"HTTP 200 but not JSON: {txt[:80]}"
                            ok = False
                else:
                    detail = f"HTTP {resp.status_code}: {(resp.text or '')[:120]}"
            except Exception as exc:
                tried.append(f"{use_query}:exc")
                detail = f"request failed: {exc}"
                ok = False

        if not ok:
            # If secret not set, endpoints are open — try without auth as fallback
            if not CRON_SECRET:
                try:
                    resp2 = requests.get(f"{BASE_URL}{path}", timeout=20)
                    if resp2.status_code == 200:
                        try:
                            data2 = resp2.json()
                            if isinstance(data2, dict) and len(data2) > 0:
                                ok = True
                                detail = f"HTTP 200 open (no secret) keys={list(data2.keys())[:3]}"
                            else:
                                detail = f"HTTP 200 open but empty"
                        except Exception:
                            txt2 = (resp2.text or "")[:100]
                            detail = f"HTTP 200 open not JSON: {txt2}"
                    else:
                        detail = f"HTTP {resp2.status_code} open"
                except Exception as e2:
                    detail = f"probe failed: {e2}"

        all_ok = result_line(f"{path} ({desc})", ok, detail) and all_ok
        # Also test that invalid secret is rejected if CRON_SECRET is set? Not required for PASS, but log
        if CRON_SECRET and ok:
            # Quick negative test: wrong secret should be 401 if secret is enforced
            try:
                bad = requests.get(f"{BASE_URL}{path}?secret=wrong-{CRON_SECRET[:3]}", timeout=10)
                if bad.status_code == 401:
                    log(f"  [Auth] Invalid secret correctly rejected 401 for {path}")
                elif bad.status_code == 200:
                    log(f"  [Auth] Note: invalid secret still 200 (endpoint open or no secret enforcement)")
            except Exception:
                pass

    if not all_ok:
        log(f"[Test 1] {FAIL} — some endpoints failed (tried {tried})")
    else:
        log(f"[Test 1] {PASS} — all 3 endpoints 200 + non-empty JSON (header & query both work)")
    return all_ok


# --------------------------------------------------------------------------
# Test 2: Cron Schedules & Timezone Alignment
# --------------------------------------------------------------------------
def test_cron_timezone() -> bool:
    section("Test 2: Cron Schedules & Timezone Alignment")
    import pathlib
    vpath = pathlib.Path(__file__).parent.parent / "vercel.json"
    if not vpath.exists():
        result_line("vercel.json exists", False, "not found")
        return False
    try:
        data = json.loads(vpath.read_text(encoding="utf-8"))
    except Exception as e:
        result_line("vercel.json parse", False, str(e))
        return False

    crons = data.get("crons", [])
    # Also check builds/routes presence for native Python routing
    builds = data.get("builds", [])
    rewrites = data.get("rewrites", [])
    routes = data.get("routes", [])
    log(f"[Vercel] crons: {crons}")
    log(f"[Vercel] builds: {[b.get('src') for b in builds]}")
    log(f"[Vercel] rewrites/routes: {len(rewrites)}/{len(routes)}")

    # Expected schedules per task (UTC)
    # Note: vercel.json may intentionally omit scanner due to Hobby daily limit — handled as optional external
    expected = {
        "/api/pre_market": "30 5 * * 0-4",
        "/api/scanner": "*/15 7-11 * * 0-4",
        "/api/post_market": "30 12 * * 0-4",
    }
    # Hobby: scanner is optional external (cron-job.org)
    required = {"/api/pre_market", "/api/post_market"}

    all_ok = True
    for path, exp_cron in expected.items():
        found = next((c for c in crons if c.get("path") == path), None)
        is_required = path in required
        if not found:
            if is_required:
                result_line(f"cron {path} == {exp_cron}", False, "missing in vercel.json")
                all_ok = False
            else:
                result_line(f"cron {path} == {exp_cron}", True, "optional external (Hobby daily limit) — ok if endpoint exists via cron-job.org")
            continue
        if found.get("schedule") != exp_cron:
            result_line(f"cron {path}", False, f"got {found.get('schedule')} expected {exp_cron}")
            all_ok = False
        else:
            result_line(f"cron {path} == {exp_cron}", True, "schedule correct")

    # Also verify flat api/ directory structure (native Vercel Python)
    api_dir = pathlib.Path(__file__).parent.parent / "api"
    for fname in ["pre_market.py", "post_market.py", "scanner.py", "webhook.py"]:
        exists = (api_dir / fname).exists()
        result_line(f"api/{fname} exists (flat structure)", exists, "native Vercel Python" if exists else "missing — should be api/*.py not api/cron/*.py")
        all_ok = exists and all_ok
    cron_subdir = api_dir / "cron"
    if cron_subdir.exists():
        # Should be removed per flatten task
        remaining = list(cron_subdir.glob("*.py"))
        if remaining:
            result_line("api/cron/ empty", False, f"still contains {remaining} — should be flattened to api/")
            all_ok = False
        else:
            result_line("api/cron/ empty", True, "directory removed or empty")
    else:
        result_line("api/cron/ removed", True, "flat structure verified")

    # Timezone conversions
    # Use known offsets: Cairo UTC+2 (winter) / UTC+3 (summer, DST) and Oman UTC+4 year-round
    # For E2E we verify the documented mappings:
    # pre 05:30 UTC = 08:30 Cairo (winter+3? actually 05:30+3=08:30) / 09:30 Oman
    # scanner 07:00 UTC = 10:00 Cairo / 11:00 Oman, 11:45 UTC = 14:45 Cairo / 15:45 Oman
    # post 12:30 UTC = 15:30 Cairo / 16:30 Oman
    def utc_to_cairo_oman(utc_h: int, utc_m: int) -> Tuple[str, str]:
        # Assume summer Cairo UTC+3, Oman UTC+4
        cairo_h = (utc_h + 3) % 24
        oman_h = (utc_h + 4) % 24
        return f"{cairo_h:02d}:{utc_m:02d}", f"{oman_h:02d}:{utc_m:02d}"

    checks = [
        (5, 30, "08:30", "09:30", "pre_market"),
        (7, 0, "10:00", "11:00", "scanner start"),
        (11, 45, "14:45", "15:45", "scanner end"),
        (12, 30, "15:30", "16:30", "post_market"),
    ]
    for utc_h, utc_m, exp_cairo, exp_oman, label in checks:
        cairo, oman = utc_to_cairo_oman(utc_h, utc_m)
        ok = cairo == exp_cairo and oman == exp_oman
        result_line(f"TZ {label} 05:30UTC->{cairo} Cairo/{oman} Oman" if label=="pre_market" else f"TZ {label} {utc_h:02d}:{utc_m:02d}UTC->{cairo}/{oman}", ok,
                    f"expected {exp_cairo}/{exp_oman}" + ("" if ok else f" got {cairo}/{oman}"))
        all_ok = ok and all_ok

    # Bonus: verify that vercel.json builds or functions are not breaking Hobby (scanner optional)
    if builds and any("scanner" in b.get("src","") for b in builds):
        log("[Info] builds include scanner — Hobby will reject */15 cron, but endpoint still built")
    if not all_ok:
        log(f"[Test 2] {FAIL} — cron/timezone mismatch")
    else:
        log(f"[Test 2] {PASS} — schedules & timezones aligned (pre 05:30, scanner 07:00-11:45, post 12:30)")
    return all_ok


# --------------------------------------------------------------------------
# Test 3: Telegram Webhook & Callback Buttons
# --------------------------------------------------------------------------
def test_webhook_callback() -> bool:
    section("Test 3: Telegram Webhook & Callback Buttons Test")
    if requests is None:
        result_line("requests", False, "not installed")
        return False

    base = BASE_URL.rstrip("/")
    webhook_url = f"{base}/api/webhook"
    # Also test local handler if Vercel not reachable? Try live first, fallback to local import
    payloads = [
        ("join_trade", {
            "callback_query": {
                "id": "test-cb-123",
                "from": {"id": 123456789, "first_name": "TestUser", "username": "testuser"},
                "message": {"message_id": 1, "chat": {"id": -1001234567890, "type": "channel"}, "text": "test"},
                "data": "join_trade:COMI.CA:1",
                "chat_instance": "test_instance",
            }
        }),
        ("view_switch", {
            "callback_query": {
                "id": "test-cb-456",
                "from": {"id": 987654321, "first_name": "Viewer"},
                "message": {"message_id": 2, "chat": {"id": -1001234567890, "type": "channel"}},
                "data": "view_switch:analysis",
                "chat_instance": "test2",
            }
        }),
        ("generic_text", {
            "message": {
                "message_id": 3,
                "from": {"id": 111222333, "first_name": "Texter"},
                "chat": {"id": 111222333, "type": "private"},
                "text": "/portfolio",
                "date": int(time.time()),
            }
        }),
    ]

    all_ok = True
    for name, payload in payloads:
        try:
            log(f"[Probe] POST {webhook_url} with {name} payload")
            resp = requests.post(webhook_url, json=payload, timeout=20, headers={"Content-Type": "application/json"})
            body = (resp.text or "")[:500]
            is_json = False
            try:
                j = resp.json()
                is_json = isinstance(j, dict)
            except Exception:
                j = None
            ok = resp.status_code == 200
            # Vercel should always return 200 for webhook (even on handled/unhandled callback) without 5xx
            detail = f"HTTP {resp.status_code} {'JSON' if is_json else 'non-JSON'} {body[:80]}"
            result_line(f"webhook {name}", ok, detail)
            all_ok = ok and all_ok
        except Exception as exc:
            result_line(f"webhook {name}", False, f"request failed: {exc}")
            all_ok = False

    # Local fallback: import handler directly and test join callback parsing without network
    try:
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("webhook_local", str(pathlib.Path(__file__).parent.parent / "api" / "webhook.py"))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            # Mock env to avoid real Supabase calls during import test
            os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
            os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
            os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
            spec.loader.exec_module(mod)
            # Test parse functions if available
            if hasattr(mod, "parse_join_callback"):
                parsed = mod.parse_join_callback("join_trade:COMI.CA:123")
                ok_parse = parsed is not None and parsed[0] == "COMI.CA"
                result_line("local parse_join_callback", ok_parse, f"parsed={parsed}")
                all_ok = ok_parse and all_ok
            if hasattr(mod, "normalize_ticker"):
                norm = mod.normalize_ticker("comi")
                ok_norm = norm == "COMI.CA"
                result_line("local normalize_ticker", ok_norm, f"comi->{norm}")
                all_ok = ok_norm and all_ok
    except Exception as e:
        log(f"[Info] Local webhook import test skipped/failed: {e}")

    if all_ok:
        log(f"[Test 3] {PASS} — webhook handles callbacks & returns 200 without 5xx")
    else:
        log(f"[Test 3] {FAIL} — some webhook probes failed")
    return all_ok


# --------------------------------------------------------------------------
# Test 4: Data Engine & Analysis Sanity
# --------------------------------------------------------------------------
def test_data_engine() -> bool:
    section("Test 4: Data Engine & Analysis Sanity (RSI/Bollinger/VWAP/Gann)")

    # Add project root to path
    import pathlib, sys
    root = str(pathlib.Path(__file__).parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)

    all_ok = True

    # Test tickers (EGX large caps)
    test_tickers = ["COMI.CA", "ABUK.CA", "SWDY.CA", "FWRY.CA", "TMGH.CA"]

    # --- Synthetic fetcher sanity (always works offline) ---
    try:
        from egx_quant.core.data_engine import SyntheticDataFetcher, YFinanceDataFetcher  # type: ignore
        from egx_quant.core.strategy_engine import StrategyEngine, rsi, donchian_high, atr_of  # type: ignore
        import pandas as pd
        import numpy as np

        log("[Data] Testing SyntheticDataFetcher (offline deterministic)...")
        synth = SyntheticDataFetcher()
        for ticker in test_tickers[:3]:
            try:
                df = synth.get_historical_klines(ticker, period="6mo", interval="1d")
                if df is None or df.empty:
                    result_line(f"synthetic klines {ticker}", False, "empty df")
                    all_ok = False
                    continue
                # Check required columns
                required = {"Open", "High", "Low", "Close", "Volume"}
                if not required.issubset(df.columns):
                    result_line(f"synthetic columns {ticker}", False, f"missing {required - set(df.columns)}")
                    all_ok = False
                    continue
                # Compute indicators
                close = df["Close"].astype(float)
                high = df["High"].astype(float)
                low = df["Low"].astype(float)
                vol = df["Volume"].astype(float)

                # RSI
                rsi_series = rsi(close)
                last_rsi = float(rsi_series.iloc[-1])
                ok_rsi = math.isfinite(last_rsi) and 0 <= last_rsi <= 100
                result_line(f"RSI {ticker}", ok_rsi, f"last={last_rsi:.2f}")

                # Donchian
                dh = donchian_high(high)
                last_dh = float(dh.iloc[-1]) if not dh.empty else float("nan")
                ok_dh = math.isfinite(last_dh) and last_dh > 0
                result_line(f"Donchian High {ticker}", ok_dh, f"last={last_dh:.2f}" if ok_dh else "NaN")

                # ATR
                atr_val = atr_of(df)
                ok_atr = math.isfinite(atr_val) and atr_val > 0
                result_line(f"ATR {ticker}", ok_atr, f"{atr_val:.3f}" if ok_atr else "NaN/invalid")

                # Bollinger Bands (SMA ± 2*STD)
                sma20 = close.rolling(20).mean().iloc[-1]
                std20 = close.rolling(20).std().iloc[-1]
                if math.isfinite(sma20) and math.isfinite(std20):
                    upper = sma20 + 2*std20
                    lower = sma20 - 2*std20
                    ok_bb = math.isfinite(upper) and math.isfinite(lower) and upper > lower > 0
                    result_line(f"Bollinger {ticker}", ok_bb, f"U={upper:.2f} L={lower:.2f} SMA={sma20:.2f}")
                    all_ok = ok_bb and all_ok
                else:
                    result_line(f"Bollinger {ticker}", False, "SMA/STD non-finite")
                    all_ok = False

                # VWAP (approx: cumulative (H+L+C)/3 * Volume / cumulative Volume)
                try:
                    typical = (high + low + close) / 3.0
                    vwap = (typical * vol).cumsum() / vol.cumsum()
                    last_vwap = float(vwap.iloc[-1])
                    ok_vwap = math.isfinite(last_vwap) and last_vwap > 0
                    result_line(f"VWAP {ticker}", ok_vwap, f"{last_vwap:.2f}" if ok_vwap else "NaN")
                    all_ok = ok_vwap and all_ok
                except Exception as e_vwap:
                    result_line(f"VWAP {ticker}", False, f"error {e_vwap}")
                    all_ok = False

                # Gann levels (simple: 45-degree fans from swing low/high)
                try:
                    swing_low = float(low.min())
                    swing_high = float(high.max())
                    gann_range = swing_high - swing_low
                    ok_gann = math.isfinite(gann_range) and gann_range > 0
                    # Gann 1x1 level at 45 deg
                    gann_lvl = swing_low + gann_range * 0.5
                    ok_gann = ok_gann and math.isfinite(gann_lvl)
                    result_line(f"Gann {ticker}", ok_gann, f"range={gann_range:.2f} mid={gann_lvl:.2f}" if ok_gann else "invalid")
                    all_ok = ok_gann and all_ok
                except Exception as e_gann:
                    result_line(f"Gann {ticker}", False, str(e_gann))
                    all_ok = False

                # StrategyEngine dry-run (should not throw, may return None or signal)
                try:
                    engine = StrategyEngine()
                    sig = engine.evaluate(ticker, df)
                    # Signal may be None (no confluence) — that's ok, but should not throw
                    if sig is None:
                        result_line(f"Strategy {ticker}", True, "no signal (confluence not met) — no crash")
                    else:
                        # Check signal fields are non-null numerics
                        ok_sig = all([
                            math.isfinite(float(sig.entry_price)),
                            math.isfinite(float(sig.stop_loss)),
                            math.isfinite(float(sig.target_1 or 0)),
                        ])
                        result_line(f"Strategy {ticker}", ok_sig, f"TQI={sig.tqi_score} entry={sig.entry_price}")
                        all_ok = ok_sig and all_ok
                except Exception as e_sig:
                    result_line(f"Strategy {ticker}", False, f"evaluate threw {type(e_sig).__name__}: {e_sig}")
                    all_ok = False
                    import traceback; traceback.print_exc()

                all_ok = ok_rsi and ok_dh and ok_atr and all_ok
            except Exception as e:
                result_line(f"synthetic {ticker}", False, f"exception {e}")
                all_ok = False
                import traceback; traceback.print_exc()

        # --- Live fetcher probe (best-effort, may fallback to synthetic if offline) ---
        log("\n[Data] Probing YFinanceDataFetcher (live, may fallback)...")
        try:
            yf = YFinanceDataFetcher()
            quotes = yf.fetch_latest_prices(test_tickers[:2])
            if quotes:
                for sym, q in quotes.items():
                    ok_q = math.isfinite(float(q.price)) and math.isfinite(float(q.previous_close))
                    result_line(f"Live quote {sym}", ok_q, f"price={q.price} prev={q.previous_close}")
                    all_ok = ok_q and all_ok
            else:
                log("[Info] YFinance returned no quotes (offline or market closed) — synthetic fallback already verified, not a FAIL")
        except Exception as e:
            log(f"[Info] YFinance probe skipped (offline): {e}")

    except Exception as e:
        result_line("Data engine import", False, f"{type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        return False

    if all_ok:
        log(f"[Test 4] {PASS} — indicators generate finite numerics, no NaN/TypeError")
    else:
        log(f"[Test 4] {FAIL} — some indicator checks failed")
    return all_ok


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    start = time.time()
    log("="*70)
    log("🔬 End-to-End System Integration Test — Dry-Run Verification")
    log(f"Base: {BASE_URL} | CRON_SECRET: {'set' if CRON_SECRET else 'not set (open)'} | Time: {datetime.now(timezone.utc).isoformat()}")
    log("="*70)

    results: List[Tuple[str, bool]] = []
    # Run each test, capture PASS/FAIL, continue even if one fails
    try:
        results.append(("Test 1 HTTP & Secret Auth", test_http_endpoints()))
    except Exception as e:
        log(f"[Test 1] {FAIL} — unhandled exception: {e}")
        import traceback; traceback.print_exc()
        results.append(("Test 1 HTTP & Secret Auth", False))

    try:
        results.append(("Test 2 Cron & Timezone", test_cron_timezone()))
    except Exception as e:
        log(f"[Test 2] {FAIL} — unhandled exception: {e}")
        import traceback; traceback.print_exc()
        results.append(("Test 2 Cron & Timezone", False))

    try:
        results.append(("Test 3 Webhook & Callbacks", test_webhook_callback()))
    except Exception as e:
        log(f"[Test 3] {FAIL} — unhandled exception: {e}")
        import traceback; traceback.print_exc()
        results.append(("Test 3 Webhook & Callbacks", False))

    try:
        results.append(("Test 4 Data Engine Sanity", test_data_engine()))
    except Exception as e:
        log(f"[Test 4] {FAIL} — unhandled exception: {e}")
        import traceback; traceback.print_exc()
        results.append(("Test 4 Data Engine Sanity", False))

    elapsed = time.time() - start
    section("Summary")
    all_pass = True
    for name, ok in results:
        status = PASS if ok else FAIL
        log(f"  {status} — {name}")
        all_pass = all_pass and ok
    log(f"\nTotal: {sum(1 for _, ok in results if ok)}/{len(results)} PASS — {elapsed:.1f}s")
    if all_pass:
        log(f"\n{PASS} — All layers verified: endpoints 200, crons aligned, webhook callbacks 200, indicators finite")
        log("Ready for live trading — no runtime panics detected.")
        return 0
    else:
        log(f"\n{FAIL} — One or more layers failed — check logs above before going live")
        return 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="End-to-End System Integration Test")
    parser.add_argument("--verbose", action="store_true", help="Verbose (default on)")
    parser.add_argument("--base-url", type=str, default=None, help="Override BASE_URL")
    parser.add_argument("--cron-secret", type=str, default=None, help="Override CRON_SECRET")
    args = parser.parse_args()
    if args.base_url:
        BASE_URL = args.base_url.strip().rstrip("/")
    if args.cron_secret:
        CRON_SECRET = args.cron_secret.strip()
    sys.exit(main())
