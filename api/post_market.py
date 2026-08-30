"""
Vercel Cron endpoint for Post-Market Bulletin — alternative to GitHub Actions

Triggered by:
  - Vercel Cron (vercel.json crons: 30 12 * * 0-4 → GET /api/cron/post_market)
  - External ping (cron-job.org, easycron, healthchecks) via GET with Bearer token

Auth:
  - If env CRON_SECRET is set, requires header `Authorization: Bearer <CRON_SECRET>`
  - Otherwise allows Vercel's `x-vercel-cron: 1` header or any caller (logs warning).

Behavior:
  - Runs egx_quant/news/post_market_summary.main() synchronously before returning HTTP 200
  - Includes stale-window check (same as workflow) — late banner appended if >60m past 12:30 UTC
  - Returns JSON {ok, scheduled, now, delay_minutes, is_stale, result_code}

Usage:
  - Deploy to Vercel: vercel --prod (cron auto-registered from vercel.json)
  - Manual test: curl https://your-app.vercel.app/api/cron/post_market -H "Authorization: Bearer $CRON_SECRET"

See docs/alternative-cron.md for cron-job.org setup.
"""
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict

try:
    logger = logging.getLogger("cron-post-market")
except Exception:
    logger = None  # type: ignore

# Lightweight auth helper — accepts header Bearer OR query string ?secret= / ?cron_secret= / ?token=
def _is_authorized(handler: BaseHTTPRequestHandler) -> tuple[bool, str]:
    cron_secret = (os.environ.get("CRON_SECRET") or "").strip()
    # Vercel Cron sends x-vercel-cron: 1 (no secret needed)
    vercel_cron = handler.headers.get("x-vercel-cron") or handler.headers.get("X-Vercel-Cron")
    if vercel_cron == "1":
        return True, "x-vercel-cron"
    # Check query string fallback (?secret=, ?cron_secret=, ?token=, ?auth=)
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(handler.path)
        qs = parse_qs(parsed.query)
        for key in ("secret", "cron_secret", "CRON_SECRET", "token", "auth", "key"):
            vals = qs.get(key, [])
            if vals and cron_secret and vals[0].strip() == cron_secret:
                return True, f"query:{key}"
            # Also accept without secret set? If no secret, query is open
            if vals and not cron_secret:
                # No secret configured — query with any value still open, but log
                return True, f"query:{key} (no-secret)"
        # Also accept plain ?cron_secret=XYZ even if key name case differs
        if cron_secret and parsed.query and cron_secret in parsed.query:
            # Fallback: secret appears anywhere in query string
            return True, "query:raw"
    except Exception:
        pass
    # External ping with Bearer token
    auth = handler.headers.get("Authorization") or handler.headers.get("authorization") or ""
    if cron_secret and auth.strip() == f"Bearer {cron_secret}":
        return True, "bearer"
    if cron_secret:
        # If secret is set, require it — reject unauthenticated
        return False, "missing/invalid bearer (CRON_SECRET set) — use header Authorization: Bearer <CRON_SECRET> or query ?secret=<CRON_SECRET>"
    # No secret configured — allow any caller (log warning)
    return True, "no-secret (open)"


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Vercel Cron uses GET
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        started = datetime.now(timezone.utc)
        auth_ok, auth_reason = _is_authorized(self)
        print(f"[CRON][POST_MARKET] incoming {self.command} {self.path} auth={auth_ok} reason={auth_reason} at {started.isoformat()}")
        if not auth_ok:
            self.send_response(401)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "unauthorized", "reason": auth_reason}).encode())
            return

        # Run post-market pipeline synchronously
        result_code = -1
        is_stale = False
        delay_minutes = 0.0
        output = ""
        try:
            # Import here to avoid cold-start import errors before auth check
            from egx_quant.news.post_market_summary import main as pm_main, check_execution_window

            # Pre-check window for logging
            try:
                is_stale, delay_minutes, now_utc, scheduled_utc, reason = check_execution_window()
                print(f"[CRON][AUDIT] Scheduled 12:30 UTC | Now {now_utc.strftime('%H:%M UTC')} | Delay {delay_minutes:.0f}m | Stale={is_stale} | {reason}")
            except Exception as e:
                print(f"[CRON][AUDIT] window check failed: {e}")

            # Run main (fetch -> AI -> publish to Telegram)
            # Use broadcast=True (default) — will handle idempotency and late banner internally
            result_code = pm_main(dry_run=False, broadcast=True)
            output = f"main returned {result_code}"
            print(f"[CRON][POST_MARKET] pipeline completed code={result_code} stale={is_stale} delay={delay_minutes:.0f}m")
        except Exception as exc:
            print(f"[CRON][POST_MARKET][ERROR] pipeline crashed: {exc}")
            import traceback
            traceback.print_exc()
            result_code = 1
            output = f"error: {exc}"

        # Build response
        ended = datetime.now(timezone.utc)
        duration = (ended - started).total_seconds()
        # Compute scheduled for response (best effort)
        try:
            from egx_quant.news.post_market_summary import check_execution_window as cw
            _, dly, now2, sched2, _ = cw()
        except Exception:
            now2 = ended
            sched2 = ended.replace(hour=12, minute=30, second=0, microsecond=0)
            dly = delay_minutes

        resp_body: Dict[str, Any] = {
            "ok": result_code in (0, 2),  # 0 = published or idempotent, 2 = strict abort (still ok)
            "result_code": result_code,
            "scheduled": sched2.isoformat(),
            "now": now2.isoformat(),
            "delay_minutes": round(float(dly), 1),
            "is_stale": bool(is_stale),
            "duration_seconds": round(duration, 1),
            "auth": auth_reason,
            "message": output,
            "schedule": "30 12 * * 0-4 → 12:30 UTC (15:30 Cairo / 16:30 Oman) Sun-Thu",
            "fallback": "If GitHub queue persists, this Vercel Cron (or cron-job.org GET) is the low-latency alternative — see docs/alternative-cron.md",
        }
        try:
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_body).encode())
            print(f"[CRON][POST_MARKET] HTTP 200 returned {json.dumps(resp_body)[:400]}")
        except Exception as e:
            print(f"[CRON][POST_MARKET][ERROR] failed to send 200: {e}")
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


# Direct execution support: `python api/post_market.py` → NEWS channel -1004492677393
if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    if not (os.environ.get("TELEGRAM_NEWS_CHANNEL_ID") or os.environ.get("TELEGRAM_CHANNEL_NEWS")):
        os.environ["TELEGRAM_NEWS_CHANNEL_ID"] = "-1004492677393"
    print("[DIRECT] Running api/post_market.py -> egx_quant.news.post_market_summary (NEWS_CHANNEL_ID=-1004492677393)")
    from egx_quant.news.post_market_summary import main as _pm_main
    sys.exit(_pm_main(dry_run=False, broadcast=True))
