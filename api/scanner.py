"""
Vercel Cron endpoint for Live Scanner — alternative to GitHub Actions

Triggered by:
  - Vercel Cron (vercel.json crons: */15 7-11 * * 0-4 -> GET /api/cron/scanner)
  - External ping (cron-job.org) via GET with Bearer token or ?secret=

Auth: accepts header `Authorization: Bearer <CRON_SECRET>` or query `?secret=<CRON_SECRET>`.

Behavior:
  - Runs EGX screener (main.py intraday) and trade-monitor engine
  - Returns JSON {ok, signals, monitors, duration}

Notes:
  - Idempotent: duplicate runs within same 15-min window are deduped via state.json / sent_alerts
  - Runs synchronously before returning HTTP 200 (Vercel kills background threads)

Usage:
  curl https://your-app.vercel.app/api/cron/scanner -H "Authorization: Bearer $CRON_SECRET"
  curl "https://your-app.vercel.app/api/cron/scanner?secret=$CRON_SECRET"
"""
import json
import os
import sys
# Ensure project root is on sys.path for Vercel runtime (/var/task)
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict

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

        # Window guard: only run during active session 07:00-11:30 UTC Sun-Thu
        try:
            hour = started.hour + started.minute/60.0
            gh_dow = (started.weekday() + 1) % 7  # Sun=0
            in_window = 7.0 <= hour <= 11.5 and gh_dow in (0,1,2,3,4)
            print(f"[CRON][AUDIT] Scheduled */15 7-11 UTC (10:00-14:30 Cairo) | Now {started.strftime('%H:%M UTC')} dow={gh_dow} | in_window={in_window}")
            # We still run even outside window (manual dispatch), just log
        except Exception as e:
            print(f"[CRON][AUDIT] window check failed: {e}")

        result: Dict[str, Any] = {"signals": None, "monitor": None}
        overall_ok = True
        try:
            # 1) Run EGX screener (main.py)
            try:
                import main as egx_main  # type: ignore
                # main.main() expects to run full scan; we call directly
                # main.py uses sys.argv parsing; invoke via python -m style
                # Instead call the internal scan function if available, fallback to subprocess
                print("[CRON][SCANNER] Running EGX screener (main.py)...")
                # Try to run via imported main; it will use env and handle state
                # Use a simple approach: call main.main() with no webhook flag
                import sys
                orig_argv = sys.argv[:]
                sys.argv = ["main.py"]
                try:
                    ret = egx_main.main()  # type: ignore[attr-defined]
                    result["signals"] = {"ok": True, "return_code": ret}
                except SystemExit as se:
                    result["signals"] = {"ok": True, "exit_code": se.code}
                except Exception as e:
                    result["signals"] = {"ok": False, "error": str(e)}
                    overall_ok = False
                finally:
                    sys.argv = orig_argv
            except Exception as e:
                print(f"[CRON][SCANNER] screener import/run failed: {e}, trying subprocess fallback")
                import subprocess
                main_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")
                proc = subprocess.run([sys.executable, main_path], capture_output=True, text=True, timeout=120)
                result["signals"] = {"ok": proc.returncode == 0, "returncode": proc.returncode, "stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:]}
                if proc.returncode != 0:
                    overall_ok = False

            # 2) Run trade monitor (target/SL)
            try:
                from egx_quant.engine.trade_monitor import run_monitor_cycle  # type: ignore
                print("[CRON][SCANNER] Running trade monitor engine...")
                mon_res = run_monitor_cycle(dry_run=False)
                result["monitor"] = mon_res
                print(f"[CRON][SCANNER] monitor completed signals_scanned={mon_res.get('signals_scanned')} target_hits={mon_res.get('target_hits')} sl_hits={mon_res.get('sl_hits')}")
            except Exception as e:
                print(f"[CRON][SCANNER] monitor failed: {e}")
                import traceback; traceback.print_exc()
                result["monitor"] = {"ok": False, "error": str(e)}
                # Don't mark overall as fail for monitor — it's secondary
        except Exception as exc:
            print(f"[CRON][SCANNER][ERROR] pipeline crashed: {exc}")
            import traceback; traceback.print_exc()
            overall_ok = False
            result["error"] = str(exc)

        ended = datetime.now(timezone.utc)
        duration = (ended - started).total_seconds()
        resp_body: Dict[str, Any] = {
            "ok": bool(overall_ok),
            "now": ended.isoformat(),
            "duration_seconds": round(duration, 1),
            "auth": auth_reason,
            "schedule": "*/15 7-11 * * 0-4 -> every 15m 07:00-11:30 UTC (10:00-14:30 Cairo / 11:00-15:30 Oman) Sun-Thu",
            "result": result,
        }
        try:
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_body).encode())
            print(f"[CRON][SCANNER] HTTP 200 returned ok={overall_ok} duration={duration:.1f}s")
        except Exception as e:
            print(f"[CRON][SCANNER][ERROR] failed to send 200: {e}")
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            except Exception:
                pass
    def log_message(self, format, *args):
        try:
            print(f"[VERCEL-CRON] {format % args}")
        except Exception:
            pass


def _get_scalping_channel_id() -> str:
    """Hard-aligned to SCAPLING_CHANNEL_ID per task spec."""
    SCALPING_FALLBACK = "-1003993921849"
    for env in ["TELEGRAM_CHANNEL_SCALPING", "SCALPING_CHANNEL_ID", "CHANNEL_SCALPING", "TELEGRAM_CHANNEL_ID"]:
        val = (os.environ.get(env) or "").strip().strip('"').strip("'")
        if val:
            return val
    return SCALPING_FALLBACK


# Direct execution support: `python api/scanner.py` -> SCALPING channel -1003993921849
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    if not os.environ.get("TELEGRAM_CHANNEL_SCALPING"):
        os.environ["TELEGRAM_CHANNEL_SCALPING"] = "-1003993921849"
    print(f"[DIRECT] Running api/scanner.py -> SCAPLING_CHANNEL_ID={_get_scalping_channel_id()} (fallback -1003993921849)")
    # Scanner delegates to main.py + trade_monitor; trigger via handler logic
    # For direct run, invoke the same pipeline as Vercel handler would
    from egx_quant.news.pre_market_briefing import main as _sm_main  # fallback to ensure import works
    # Actually run main scanner via handler simulation
    import importlib.util, pathlib
    # Re-use handler's internal logic by simulating a GET
    # Simplest: run main.py directly
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    try:
        import main as egx_main
        sys.argv = ["main.py"]
        print(f"[DIRECT] Executing main.py scanner -> targeting SCALPING {_get_scalping_channel_id()}")
        ret = egx_main.main()
        print(f"[DIRECT] main.py returned {ret}")
    except SystemExit as se:
        print(f"[DIRECT] main.py exit {se.code}")
    except Exception as e:
        print(f"[DIRECT] scanner failed: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    # Also run trade monitor
    try:
        from egx_quant.engine.trade_monitor import run_monitor_cycle
        res = run_monitor_cycle(dry_run=False)
        print(f"[DIRECT] trade_monitor: {res}")
    except Exception as e:
        print(f"[DIRECT] trade_monitor failed: {e}")
