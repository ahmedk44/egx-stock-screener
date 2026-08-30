"""
Vercel Cron endpoint for Pre-Market Briefing — alternative to GitHub Actions

Triggered by:
  - Vercel Cron (vercel.json crons: 30 5 * * 0-4 → GET /api/cron/pre_market)
  - External ping (cron-job.org) via GET with Bearer token or ?secret=

Auth: accepts header `Authorization: Bearer <CRON_SECRET>` or query `?secret=<CRON_SECRET>` / `?cron_secret=` / `?token=`.

Behavior:
  - Runs egx_quant/news/pre_market_briefing.main() synchronously before returning HTTP 200
  - Returns JSON {ok, scheduled, now, delay_minutes, result_code}

Usage:
  curl https://your-app.vercel.app/api/cron/pre_market -H "Authorization: Bearer $CRON_SECRET"
  curl "https://your-app.vercel.app/api/cron/pre_market?secret=$CRON_SECRET"
"""
import json
import os
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
        print(f"[CRON][PRE_MARKET] incoming {self.command} {self.path} auth={auth_ok} reason={auth_reason} at {started.isoformat()}")
        if not auth_ok:
            self.send_response(401)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "unauthorized", "reason": auth_reason}).encode())
            return
        result_code = -1
        output = ""
        try:
            from egx_quant.news.pre_market_briefing import main as pm_main
            # Quick window audit (08:30 Cairo / 09:30 Oman = 05:30 UTC)
            try:
                scheduled = started.replace(hour=5, minute=30, second=0, microsecond=0)
                # Find most recent Sun-Thu 05:30 ≤ now
                tmp = scheduled
                if started < tmp:
                    tmp = tmp - timedelta(days=1)
                for _ in range(7):
                    gh_dow = (tmp.weekday() + 1) % 7
                    if gh_dow in (0,1,2,3,4):
                        break
                    tmp = tmp - timedelta(days=1)
                    tmp = tmp.replace(hour=5, minute=30, second=0, microsecond=0)
                delay = (started - tmp).total_seconds()/60
                print(f"[CRON][AUDIT] Scheduled 05:30 UTC | Now {started.strftime('%H:%M UTC')} | Delay {delay:.0f}m")
            except Exception as e:
                print(f"[CRON][AUDIT] window check failed: {e}")

            result_code = pm_main(dry_run=False, broadcast=True)
            output = f"main returned {result_code}"
            print(f"[CRON][PRE_MARKET] pipeline completed code={result_code}")
        except Exception as exc:
            print(f"[CRON][PRE_MARKET][ERROR] pipeline crashed: {exc}")
            import traceback; traceback.print_exc()
            result_code = 1
            output = f"error: {exc}"

        ended = datetime.now(timezone.utc)
        duration = (ended - started).total_seconds()
        # Compute scheduled for response
        try:
            sched = started.replace(hour=5, minute=30, second=0, microsecond=0)
            if ended < sched:
                sched = sched - timedelta(days=1)
        except Exception:
            sched = started

        resp_body: Dict[str, Any] = {
            "ok": result_code == 0,
            "result_code": result_code,
            "scheduled": sched.isoformat(),
            "now": ended.isoformat(),
            "duration_seconds": round(duration, 1),
            "auth": auth_reason,
            "message": output,
            "schedule": "30 5 * * 0-4 → 05:30 UTC (08:30 Cairo / 09:30 Oman) Sun-Thu",
        }
        try:
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp_body).encode())
            print(f"[CRON][PRE_MARKET] HTTP 200 returned {json.dumps(resp_body)[:400]}")
        except Exception as e:
            print(f"[CRON][PRE_MARKET][ERROR] failed to send 200: {e}")
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


# Direct execution support: `python api/pre_market.py` must deliver to NEWS channel -1004492677393
if __name__ == "__main__":
    import sys
    # Ensure project root on path
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    # Hard-align env for direct run if missing
    if not (os.environ.get("TELEGRAM_NEWS_CHANNEL_ID") or os.environ.get("TELEGRAM_CHANNEL_NEWS")):
        os.environ["TELEGRAM_NEWS_CHANNEL_ID"] = "-1004492677393"
    print("[DIRECT] Running api/pre_market.py -> egx_quant.news.pre_market_briefing (NEWS_CHANNEL_ID=-1004492677393)")
    from egx_quant.news.pre_market_briefing import main as _pm_main
    sys.exit(_pm_main(dry_run=False, broadcast=True))
