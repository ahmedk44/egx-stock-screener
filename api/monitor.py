"""
Vercel Cron endpoint for the standalone Trade Monitor.

WHY SEPARATE FROM THE SCANNER:
  The monitor previously ran inside the scanner request budget (only if
  remaining seconds > 2 after the 26-ticker scan). A long scan skipped the
  monitor entirely; conversely, monitor DM loops (10s timeout per subscriber)
  could push the combined request past maxDuration and get killed mid-flight.
  A dedicated endpoint gives the monitor its own budget and its own cadence.

Trigger:
  - cron-job.org: every 5 minutes during the EGX session window
    (06:00-12:30 UTC / 09:00-15:30 Cairo, Sun-Thu) — see
    scripts/setup_cronjobs.py (job key: "monitor").
  - The scanner's embedded monitor stays as a fallback; claim-first
    idempotency (notified_events) makes double execution duplicate-safe.

Pipeline (egx_quant.engine.trade_monitor.run_monitor_cycle):
  1. Fetch active trade_signals enriched with live prices.
  2. Target hits  -> claim T{level}:{ticker}:{id} -> DM subscribers only.
  3. SL hits      -> claim SL:{ticker}:{id} -> close trade FIRST by PK id
     (suppress + throttled admin alert when the close write fails) -> DM only.
  4. Trailing     -> claim TRAIL:{ticker}:{id}:{new_sl} -> persist stop FIRST
     (suppress + admin alert on failure) -> DM only.

Auth: same contract as /api/scanner — CRON_SECRET via
  Authorization: Bearer <CRON_SECRET> OR ?secret=<CRON_SECRET>
  (x-vercel-cron: 1 header accepted for Vercel-native crons).

Behavior:
  - Returns JSON {ok, status, result:{...monitor summary...}}
  - Dry run: `python api/monitor.py --dry-run` (read-only, no Telegram/DB writes)
"""
import json
import os
import sys
# Ensure project root is on sys.path for Vercel runtime (/var/task)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict

from egx_quant.utils.egx_calendar import now_cairo, session_label


def _is_authorized(handler: BaseHTTPRequestHandler) -> tuple:
    """Same auth contract as api/scanner.py (header bearer OR ?secret=)."""
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


def run_monitor_pipeline(dry_run: bool = False) -> Dict[str, Any]:
    """One monitor cycle with its own wall-clock budget (independent of the scanner)."""
    from egx_quant.engine.trade_monitor import run_monitor_cycle, format_cycle_summary

    print(
        f"[CRON][MONITOR] cycle start | session={session_label()} "
        f"at {now_cairo().strftime('%H:%M')} Cairo | dry_run={dry_run}"
    )
    result = run_monitor_cycle(dry_run=dry_run)
    print(f"[CRON][MONITOR] cycle done: scanned={result.get('signals_scanned', 0)} "
          f"targets={result.get('target_hits', 0)} sl={result.get('sl_hits', 0)} "
          f"trailing={result.get('trailing_updates', 0)} errors={len(result.get('errors', []))}")
    return result


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        started = datetime.now(timezone.utc)
        auth_ok, auth_reason = _is_authorized(self)
        print(f"[CRON][MONITOR] incoming {self.command} {self.path} auth={auth_ok} reason={auth_reason} at {started.isoformat()}")
        if not auth_ok:
            self.send_response(401)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "unauthorized", "reason": auth_reason}).encode())
            return

        # Window guard: only run during the active EGX session 06:00-12:30 UTC
        # (09:00-15:30 Cairo) Sun-Thu. Outside the window: cheap 200 no-op so
        # the external cron never sees failures at session boundaries.
        try:
            hour = started.hour + started.minute / 60.0
            gh_dow = (started.weekday() + 1) % 7  # Sun=0
            in_window = 6.0 <= hour <= 12.5 and gh_dow in (0, 1, 2, 3, 4)
            print(f"[CRON][AUDIT] Scheduled */5 6-12 UTC (09:00-15:30 Cairo) | "
                  f"Now {started.strftime('%H:%M UTC')} dow={gh_dow} | in_window={in_window}")
        except Exception as e:
            print(f"[CRON][AUDIT] window check failed: {e}")
            in_window = True

        dry_run = (os.environ.get("MONITOR_DRY_RUN") or "").strip() in ("1", "true", "True")

        if in_window:
            try:
                monitor = run_monitor_pipeline(dry_run=dry_run)
                resp_body: Dict[str, Any] = {
                    "ok": True,
                    "status": "completed",
                    "now": datetime.now(timezone.utc).isoformat(),
                    "auth": auth_reason,
                    "schedule": "external cron */5 6-12 * * 0-4 -> every 5m 06:00-12:30 UTC (09:00-15:30 Cairo) Sun-Thu",
                    "result": monitor,
                }
            except Exception as e:
                resp_body = {"ok": False, "status": "error", "error": str(e)[:200]}
        else:
            resp_body = {
                "ok": True,
                "status": "outside-window",
                "now": datetime.now(timezone.utc).isoformat(),
                "auth": auth_reason,
                "schedule": "*/5 6-12 * * 0-4 -> every 5m 06:00-12:30 UTC (09:00-15:30 Cairo) Sun-Thu",
            }

        try:
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_body, default=str).encode())
        except Exception as e:
            print(f"[CRON][MONITOR][ERROR] failed to send 200: {e}")
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true, "status": "monitor_started"}')
            except Exception:
                pass

    def log_message(self, format, *args):
        try:
            print(f"[VERCEL-CRON] {format % args}")
        except Exception:
            pass


# Direct execution support: `python api/monitor.py [--dry-run]`
if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    dry = "--dry-run" in sys.argv
    print(f"[DIRECT] Running api/monitor.py directly (dry_run={dry})")
    summary = run_monitor_pipeline(dry_run=dry)
    print(json.dumps(
        {k: v for k, v in summary.items() if k != "timestamp"}, ensure_ascii=False, indent=2, default=str))
