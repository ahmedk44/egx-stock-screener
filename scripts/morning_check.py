#!/usr/bin/env python3
"""
scripts/morning_check.py — Pre-session readiness probe (READ-ONLY, safe anytime).

Verifies everything tomorrow's session depends on, BEFORE the session opens:
  1. Supabase: required tables exist + sane counts (no leftover test rows)
  2. Yahoo EGX feed: FROZEN or LIVE (last bar date vs Cairo session)
  3. cron-job.org: all jobs enabled + next run scheduled
  4. Vercel endpoints: HTTP 200 + correct CRON_SECRET auth
  5. Telegram bot token: valid (getMe)

Exit code: 0 = GO (or GO with cautions), 1 = BLOCKER found.
Env: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY, TELEGRAM_BOT_TOKEN,
     CRON_SECRET, CRONJOB_API_KEY, BASE_URL (optional, default production).
Usage: python scripts/morning_check.py
"""
import os
import sys
import json

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

try:
    import requests
except ImportError:
    requests = None

BASE_URL = (os.environ.get("BASE_URL") or "https://egx-stock-screener.vercel.app").rstrip("/")
FINDINGS = []


def note(level, msg):
    FINDINGS.append((level, msg))
    print(f"[{level}] {msg}")


def supa_cfg():
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    return (url, key) if url and key else (None, None)


def check_supabase():
    print("--- 1) Supabase ---")
    url, key = supa_cfg()
    if not url or not key:
        note("BLOCKER", "SUPABASE_URL / SERVICE_ROLE_KEY missing")
        return
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    required = ("trade_signals", "user_portfolio", "notified_events", "news_publish_log", "user_profile")
    for t in required:
        try:
            r = requests.get(f"{url}/rest/v1/{t}?select=*", headers={**h, "Prefer": "count=exact"}, timeout=15)
            if r.status_code == 200:
                n = r.headers.get("Content-Range", "?").split("/")[-1]
                note("OK", f"table {t}: exists, rows={n}")
            else:
                note("BLOCKER", f"table {t}: HTTP {r.status_code} (run migrations!)")
        except Exception as e:
            note("BLOCKER", f"table {t}: unreachable ({e})")
    try:
        r = requests.get(f"{url}/rest/v1/trade_signals?status=in.(ACTIVE,TRACKING,OPEN)&select=id,ticker",
                         headers=h, timeout=15)
        rows = r.json() if r.status_code == 200 else []
        tests = [x for x in rows if str(x.get("ticker", "")).upper().startswith("TEST")]
        if tests:
            note("CAUTION", f"{len(tests)} TEST rows still ACTIVE in trade_signals")
        else:
            note("OK", f"active trade_signals={len(rows)}, no TEST rows")
    except Exception as e:
        note("CAUTION", f"active-signals check failed: {e}")


def check_yahoo():
    print("--- 2) Yahoo EGX feed ---")
    try:
        import yfinance as yf
    except Exception:
        note("CAUTION", "yfinance not installed locally - skipping feed check")
        return
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        today = datetime.now(ZoneInfo("Africa/Cairo")).date()
    except Exception:
        from datetime import datetime
        today = datetime.utcnow().date()
    worst = None
    for sym in ("COMI.CA", "CERA.CA"):
        try:
            d = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=False)
            if d is None or d.empty:
                note("CAUTION", f"{sym}: no bars at all")
                continue
            ts = d.index[-1]
            ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
            bar = ts.date()
            print(f"    {sym}: last bar {bar} vs session {today}")
            if worst is None or bar < worst:
                worst = bar
        except Exception as e:
            note("CAUTION", f"{sym}: fetch failed ({e})")
    if worst is None:
        note("CAUTION", "could not determine feed freshness")
    elif worst == today:
        note("OK", f"feed LIVE (last bar {worst}) - strict same-session mode applies")
    else:
        note("CAUTION", f"feed DELAYED (last bar {worst}, session {today}) - delayed mode + badges active, monitor fail-closed")


def check_crons():
    print("--- 3) cron-job.org ---")
    api_key = (os.environ.get("CRONJOB_API_KEY") or "").strip()
    if not api_key:
        note("CAUTION", "CRONJOB_API_KEY not set - cannot verify schedules (check dashboard manually)")
        return
    if requests is None:
        note("CAUTION", "requests missing - cannot verify schedules")
        return
    try:
        h = {"Authorization": "Bearer " + api_key}
        r = requests.get("https://api.cron-job.org/jobs", headers=h, timeout=20)
        jobs = r.json().get("jobs", [])
        from urllib.parse import urlparse
        from datetime import datetime, timezone
        want = {"/api/pre_market", "/api/scanner", "/api/monitor", "/api/post_market"}
        seen = set()
        for j in jobs:
            d = requests.get(f"https://api.cron-job.org/jobs/{j.get('jobId')}", headers=h, timeout=15).json().get("jobDetails", {})
            path = urlparse(d.get("url", "")).path
            if path not in want:
                continue
            seen.add(path)
            en = d.get("enabled")
            sched = d.get("schedule", {}) or {}
            nxt = d.get("nextExecution") or 0
            nxt_s = datetime.fromtimestamp(nxt, timezone.utc).isoformat() if nxt else "?"
            has_secret = "secret=" in (d.get("url") or "")
            if not en:
                note("BLOCKER", f"{path}: DISABLED on cron-job.org!")
            elif not has_secret:
                note("BLOCKER", f"{path}: URL missing ?secret= (401 risk)")
            else:
                note("OK", f"{path}: enabled, next={nxt_s}")
        for p in sorted(want - seen):
            note("BLOCKER", f"{p}: job MISSING on cron-job.org!")
    except Exception as e:
        note("CAUTION", f"cron-job.org check failed: {e}")


def check_endpoints():
    print("--- 4) Vercel endpoints ---")
    secret = (os.environ.get("CRON_SECRET") or "").strip()
    if requests is None:
        note("CAUTION", "requests missing - cannot probe endpoints")
        return
    for path in ("/api/monitor", "/api/scanner", "/api/pre_market", "/api/post_market"):
        try:
            # NOTE: the secret MUST be percent-encoded (+ -> %2B). A raw '+'
            # decodes as space server-side and false-401s (lesson learned).
            from urllib.parse import urlencode
            qs = urlencode({"secret": secret}) if secret else ""
            url = f"{BASE_URL}{path}?{qs}" if qs else f"{BASE_URL}{path}"
            r = requests.get(url, timeout=40)
            auth = ""
            try:
                auth = r.json().get("auth", "")
            except Exception:
                pass
            if r.status_code == 200 and "secret" in str(auth):
                note("OK", f"{path}: HTTP 200 auth={auth}")
            elif r.status_code == 200:
                note("CAUTION", f"{path}: HTTP 200 but auth={auth} (CRON_SECRET mismatch or not deployed?)")
            elif r.status_code == 401:
                note("BLOCKER", f"{path}: HTTP 401 secret rejected - Vercel CRON_SECRET differs from this value AND from cron-job.org URLs (align them!)")
            else:
                note("BLOCKER", f"{path}: HTTP {r.status_code}")
        except Exception as e:
            note("CAUTION", f"{path}: probe failed ({type(e).__name__} - cold start?) ")


def check_telegram():
    print("--- 5) Telegram ---")
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token or requests is None:
        note("BLOCKER", "TELEGRAM_BOT_TOKEN missing")
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        j = r.json()
        if j.get("ok"):
            u = j.get("result", {})
            note("OK", f"bot token valid (@{u.get('username')})")
        else:
            note("BLOCKER", f"getMe rejected: {j}")
    except Exception as e:
        note("CAUTION", f"getMe failed: {e}")


def check_oanor():
    print("--- 6) oanor EGX API (quote fallback) ---")
    key = (os.environ.get("OANOR_API_KEY") or "").strip()
    if not key:
        note("CAUTION", "OANOR_API_KEY not set - Yahoo->TV chain only (set key to enable oanor fallback)")
        return
    if requests is None:
        note("CAUTION", "requests missing - cannot probe oanor")
        return
    try:
        r = requests.get("https://api.oanor.com/egx-api/v1/quote",
                         headers={"x-oanor-key": key}, params={"codes": "COMI"},
                         timeout=20)
        if r.status_code == 200:
            try:
                px = ((r.json().get("data", {}) or {}).get("quotes", []) or [{}])[0].get("price")
            except Exception:
                px = "?"
            note("OK", f"oanor key live (COMI={px} EGP, quota left={r.headers.get('x-quota-remaining', '?')})")
        elif r.status_code == 402:
            note("BLOCKER", "oanor 402: key not subscribed to EGX API (subscribe on oanor.com)")
        elif r.status_code == 429:
            note("CAUTION", "oanor 429: quota exhausted - TV fallback covers quotes")
        else:
            note("CAUTION", f"oanor HTTP {r.status_code} - TV fallback covers quotes")
    except Exception as e:
        note("CAUTION", f"oanor probe failed: {e}")


def main():
    print("=" * 64)
    print("EGX pre-session readiness check (read-only)")
    print("=" * 64)
    check_supabase()
    check_yahoo()
    check_crons()
    check_endpoints()
    check_telegram()
    check_oanor()
    print("=" * 64)
    blockers = [m for lv, m in FINDINGS if lv == "BLOCKER"]
    cautions = [m for lv, m in FINDINGS if lv == "CAUTION"]
    if blockers:
        print(f"VERDICT: HOLD - {len(blockers)} blocker(s). Fix before session:")
        for m in blockers:
            print(f"  ! {m}")
        return 1
    if cautions:
        print(f"VERDICT: GO WITH CAUTION ({len(cautions)} notes above - all expected/harmless if understood)")
    else:
        print("VERDICT: GO - all systems nominal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
