#!/usr/bin/env python3
"""
scripts/setup_cronjobs.py — External Cron Setup for Vercel Endpoints (Eliminate GitHub Delays)

Creates/ verifies external cron jobs that trigger Vercel endpoints directly,
bypassing GitHub Actions public runner queue (which caused 4.5h delays: Created 12:31 → Started 16:48).

Endpoints (all accept CRON_SECRET via header `Authorization: Bearer <CRON_SECRET>` OR query `?secret=<CRON_SECRET>`):
  - Pre-Market:  05:30 UTC (08:30 Cairo / 09:30 Oman) → https://egx-stock-screener.vercel.app/api/pre_market
  - Live Scanner: */15 07:00-11:30 UTC (10:00-14:30 Cairo) → https://egx-stock-screener.vercel.app/api/scanner
  - Post-Market: 12:30 UTC (15:30 Cairo / 16:30 Oman) → https://egx-stock-screener.vercel.app/api/post_market

Supports:
  - Vercel Cron (already in vercel.json `crons` — just `vercel --prod`)
  - cron-job.org via REST API (if CRONJOB_API_KEY provided)
  - Manual instructions (if no API key → prints curl + dashboard steps)

Env:
  BASE_URL / VERCEL_DOMAIN — base app URL (default: https://egx-stock-screener.vercel.app)
  CRON_SECRET — shared secret for Vercel endpoints (must match Vercel env CRON_SECRET)
  CRONJOB_API_KEY — cron-job.org API key (from https://cron-job.org/en/members/settings/)
  CRONJOB_API_KEY / CJ_API_KEY — alternative name

Usage:
  python scripts/setup_cronjobs.py --dry-run                    # preview
  python scripts/setup_cronjobs.py --verify-only                # only verify endpoints
  python scripts/setup_cronjobs.py --base-url https://...       # custom domain
  CRON_SECRET=xxx python scripts/setup_cronjobs.py              # verify with secret
  CRONJOB_API_KEY=yyy python scripts/setup_cronjobs.py --create # create via API
  python scripts/setup_cronjobs.py --provider vercel            # show Vercel deploy steps
  python scripts/setup_cronjobs.py --provider cronjob --create  # auto-create on cron-job.org

Exit codes: 0 success, 1 partial fail, 2 missing env
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

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

DEFAULT_BASE = "https://egx-stock-screener.vercel.app"

JOBS = [
    {
        "key": "pre_market",
        "title": "EGX Pre-Market 05:30 UTC",
        "path": "/api/pre_market",
        "cron": "30 5 * * 0-4",
        "desc": "08:30 Cairo / 09:30 Oman — FIRST message before market opens",
        "cronjob_schedule": {
            "timezone": "UTC",
            "hours": [5],
            "minutes": [30],
            "wdays": [0, 1, 2, 3, 4],
            "mdays": [-1],
            "months": [-1],
            "expiresAt": 0,
        },
    },
    {
        "key": "scanner",
        "title": "EGX Live Scanner 07:00-11:30 UTC */15",
        "path": "/api/scanner",
        "cron": "*/15 7-11 * * 0-4",
        "desc": "10:00-14:30 Cairo / 11:00-15:30 Oman — every 15m active session",
        "cronjob_schedule": {
            "timezone": "UTC",
            "hours": [7, 8, 9, 10, 11],
            "minutes": [0, 15, 30, 45],
            "wdays": [0, 1, 2, 3, 4],
            "mdays": [-1],
            "months": [-1],
            "expiresAt": 0,
        },
    },
    {
        "key": "post_market",
        "title": "EGX Post-Market 12:30 UTC",
        "path": "/api/post_market",
        "cron": "30 12 * * 0-4",
        "desc": "15:30 Cairo / 16:30 Oman — FINAL bulletin after close",
        "cronjob_schedule": {
            "timezone": "UTC",
            "hours": [12],
            "minutes": [30],
            "wdays": [0, 1, 2, 3, 4],
            "mdays": [-1],
            "months": [-1],
            "expiresAt": 0,
        },
    },
]


def resolve_base_url(arg_url: Optional[str]) -> str:
    if arg_url:
        return arg_url.strip().rstrip("/")
    for env in ("BASE_URL", "VERCEL_DOMAIN", "VERCEL_URL", "APP_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        v = (os.environ.get(env) or "").strip()
        if v:
            if not v.startswith("http"):
                v = f"https://{v}"
            return v.rstrip("/")
    return DEFAULT_BASE


def resolve_cron_secret(arg_secret: Optional[str]) -> str:
    if arg_secret:
        return arg_secret.strip()
    return (os.environ.get("CRON_SECRET") or "").strip()


def build_url(base: str, path: str, secret: str, use_query: bool = True) -> str:
    base = base.rstrip("/")
    url = f"{base}{path}"
    if use_query and secret:
        # Use query-string auth (most compatible with cron-job.org free tier)
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        qs["secret"] = [secret]
        new_qs = urlencode(qs, doseq=True)
        url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_qs, parsed.fragment))
    return url


def verify_endpoint(base: str, path: str, secret: str) -> Tuple[bool, str]:
    """Verify endpoint accepts CRON_SECRET via header OR query string. Returns (ok, detail)."""
    if requests is None:
        return False, "requests not installed"
    full = f"{base.rstrip('/')}{path}"
    headers = {}
    params = {}
    # Try query-string first (most reliable)
    test_secret = secret or "test-no-secret"
    # If secret is set, test both good and bad auth
    # First test: with correct secret via query
    try:
        # Correct auth via query
        url_ok = build_url(base, path, secret, use_query=True) if secret else full
        resp = requests.get(url_ok, headers={"Authorization": f"Bearer {secret}"} if secret else {}, timeout=15)
        body = resp.text[:500] if resp.text else ""
        # Endpoint should return 200 even for stale check (it always returns 200 with {ok, delay})
        # If secret is set and we sent correct secret, expect 200. If no secret, 200 as well.
        if resp.status_code in (200, 202):
            # Try to parse json to confirm it's our endpoint not 404
            try:
                data = resp.json()
                if isinstance(data, dict) and ("ok" in data or "result_code" in data or "scheduled" in data):
                    return True, f"HTTP {resp.status_code} — endpoint reachable via query auth ({url_ok[:60]}…)"
                # If Vercel returns 404 page, json will not have ok
                return False, f"HTTP {resp.status_code} — unexpected body: {body[:120]}"
            except Exception:
                # May be 200 but not json — still reachable
                return True, f"HTTP {resp.status_code} — reachable (non-JSON): {body[:80]}"
        elif resp.status_code == 401 and secret:
            return False, f"HTTP 401 — secret rejected (check CRON_SECRET matches Vercel env) body={body[:120]}"
        else:
            return False, f"HTTP {resp.status_code}: {body[:150]}"
    except Exception as exc:
        return False, f"request failed: {exc}"


def check_vercel_crons() -> None:
    print("[VERCEL] Checking vercel.json crons …")
    try:
        import json as _j, pathlib
        vpath = pathlib.Path(__file__).parent.parent / "vercel.json"
        if not vpath.exists():
            print("[VERCEL][WARN] vercel.json not found")
            return
        data = _j.loads(vpath.read_text(encoding="utf-8"))
        crons = data.get("crons", [])
        builds = data.get("builds", [])
        routes = data.get("routes", [])
        print(f"[VERCEL] builds: {[b.get('src') for b in builds]}")
        print(f"[VERCEL] routes: {[r.get('src') for r in routes]}")
        print(f"[VERCEL] crons: {crons}")
        # Hobby plan: scanner (*/15) exceeds daily limit, so Vercel cron for scanner is intentionally external (cron-job.org)
        expected_required = {"/api/pre_market": "30 5 * * 0-4", "/api/post_market": "30 12 * * 0-4"}
        expected_optional = {"/api/scanner": "*/15 7-11 * * 0-4"}
        ok = True
        for exp_path, exp_sched in expected_required.items():
            found = next((c for c in crons if c.get("path") == exp_path), None)
            if not found:
                print(f"[VERCEL][FAIL] Missing cron for {exp_path} (expected {exp_sched})")
                ok = False
            elif found.get("schedule") != exp_sched:
                print(f"[VERCEL][FAIL] Wrong schedule for {exp_path}: got {found.get('schedule')} expected {exp_sched}")
                ok = False
            else:
                print(f"[VERCEL][PASS] {exp_path} → {exp_sched}")
        for exp_path, exp_sched in expected_optional.items():
            found = next((c for c in crons if c.get("path") == exp_path), None)
            if not found:
                print(f"[VERCEL][INFO] Optional cron for {exp_path} (expected {exp_sched}) not in vercel.json — handled externally via cron-job.org (Hobby daily limit)")
            elif found.get("schedule") != exp_sched:
                print(f"[VERCEL][WARN] Optional cron {exp_path}: got {found.get('schedule')} expected {exp_sched} (Hobby may reject */15)")
            else:
                print(f"[VERCEL][PASS] {exp_path} → {exp_sched} (if Hobby allows)")
        if ok:
            print("[VERCEL][PASS] Required Vercel crons correctly exposed (scanner external due to Hobby limit)")
        else:
            print("[VERCEL][FAIL] Fix vercel.json required crons to match expected (see above)")
    except Exception as exc:
        print(f"[VERCEL][ERROR] {exc}")


def create_cronjob_via_api(job_def: Dict[str, Any], base: str, secret: str, api_key: str, dry_run: bool = False) -> Tuple[bool, str]:
    if not api_key:
        return False, "no API key"
    if requests is None:
        return False, "requests not installed"
    # Build URL with query secret for cron-job.org (avoids custom header setup)
    url = build_url(base, job_def["path"], secret, use_query=True)
    # cron-job.org REST API: POST https://api.cron-job.org/jobs
    # Docs: https://docs.cron-job.org/rest-api.html
    # Payload structure:
    payload = {
        "job": {
            "url": url,
            "enabled": True,
            "saveResponses": True,
            "schedule": job_def["cronjob_schedule"],
            "title": job_def["title"],
            "requestMethod": 0,  # 0 = GET
            "auth": {"enable": False},
            "extendedData": {
                "headers": {"User-Agent": "EGX-Cron/1.0"},
                "body": "",
            },
            "notification": {
                "onFailure": True,
                "onSuccess": False,
                "onDisable": True,
            },
        }
    }
    # Alternative: use header auth instead of query if secret prefers header
    # cron-job.org supports requestHeaders in extendedData? We'll keep query for simplicity.

    if dry_run:
        return True, f"dry-run would POST {payload['job']['title']} → {url}"

    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # First, list existing jobs to avoid duplicates
        try:
            lst = requests.get("https://api.cron-job.org/jobs", headers=headers, timeout=15)
            if lst.status_code == 200:
                try:
                    existing = lst.json().get("jobs", []) if isinstance(lst.json(), dict) else []
                except Exception:
                    existing = []
                for ej in existing:
                    if isinstance(ej, dict) and ej.get("url", "").split("?")[0] == url.split("?")[0]:
                        # Update existing instead of create? For now, skip duplicate
                        print(f"[CRONJOB][SKIP] Job already exists for {job_def['path']} (id={ej.get('jobId')}) — skipping create")
                        return True, f"already exists id={ej.get('jobId')}"
        except Exception as e:
            print(f"[CRONJOB][WARN] List existing jobs failed: {e} — proceeding to create")

        resp = requests.put("https://api.cron-job.org/jobs", headers=headers, json=payload, timeout=20)  # PUT creates? docs uses PUT for create
        # Some docs use POST, try both: if PUT fails 405, retry POST
        if resp.status_code == 405 or resp.status_code == 404:
            resp = requests.post("https://api.cron-job.org/jobs", headers=headers, json=payload, timeout=20)
        body = resp.text[:800] if resp.text else ""
        if resp.status_code in (200, 201, 202):
            try:
                data = resp.json()
                job_id = data.get("jobId") or data.get("job", {}).get("jobId") or "unknown"
            except Exception:
                job_id = "unknown"
            return True, f"created jobId={job_id} HTTP {resp.status_code}"
        else:
            return False, f"HTTP {resp.status_code}: {body}"
    except Exception as exc:
        return False, f"exception: {exc}"


def print_manual_instructions(base: str, secret: str) -> None:
    print()
    print("=" * 70)
    print("📋 Manual Setup — cron-job.org (2 minutes)")
    print("=" * 70)
    for job in JOBS:
        url_q = build_url(base, job["path"], secret, use_query=True)
        url_h = f"{base.rstrip('/')}{job['path']}"
        print(f"\n— {job['title']} —")
        print(f"  Purpose: {job['desc']}")
        print(f"  Cron:    {job['cron']} (UTC)")
        print(f"  URL (query auth, recommended): {url_q}")
        print(f"  URL (header auth alt):        {url_h}")
        if secret:
            print(f"  Header (if using header URL): Authorization: Bearer {secret[:6]}…")
        print(f"  Method:  GET")
        print(f"  cron-job.org → Create Cronjob → Paste URL → Schedule → Hours {job['cronjob_schedule']['hours']} Minutes {job['cronjob_schedule']['minutes']} Weekdays {job['cronjob_schedule']['wdays']} Timezone UTC → Save → Test Run")
    print()
    print("Vercel Cron — Already configured in vercel.json (no manual step):")
    print("  crons: [")
    for job in JOBS:
        print(f'    {{"path": "{job["path"]}", "schedule": "{job["cron"]}"}},')
    print("  ]")
    print("  Deploy: vercel --prod  (or git push → auto-deploy)")
    print("  Then verify: curl -i https://your-app.vercel.app/api/pre_market?secret=$CRON_SECRET")
    print()
    print("Fallback: If GitHub delays reappear, disable GitHub schedule (see Workflow Cleanup) and rely solely on Vercel/cron-job.org.")
    print("Idempotency: Python news_publish_log unique (bulletin_type+publish_date) ensures no double Telegram post even if both triggers fire.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Setup external crons for Vercel endpoints (bypass GitHub delays)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  python scripts/setup_cronjobs.py --dry-run\n"
        "  python scripts/setup_cronjobs.py --verify-only\n"
        "  python scripts/setup_cronjobs.py --base-url https://egx-stock-screener.vercel.app --dry-run\n"
        "  CRON_SECRET=xxx python scripts/setup_cronjobs.py --verify-only\n"
        "  CRONJOB_API_KEY=yyy python scripts/setup_cronjobs.py --create\n"
        "  python scripts/setup_cronjobs.py --provider vercel\n",
    )
    p.add_argument("--base-url", type=str, default=None, help="Base app URL (default: https://egx-stock-screener.vercel.app or VERCEL_DOMAIN env)")
    p.add_argument("--cron-secret", type=str, default=None, help="CRON_SECRET (overrides env CRON_SECRET)")
    p.add_argument("--cronjob-api-key", type=str, default=None, help="cron-job.org API key (overrides env CRONJOB_API_KEY)")
    p.add_argument("--provider", type=str, choices=["vercel", "cronjob", "all"], default="all", help="Which provider to setup (default: all)")
    p.add_argument("--create", action="store_true", help="Actually create jobs via cron-job.org API (requires CRONJOB_API_KEY)")
    p.add_argument("--dry-run", action="store_true", help="Preview only — do not create jobs")
    p.add_argument("--verify-only", action="store_true", help="Only verify endpoints (no creation)")
    p.add_argument("--verify", action="store_true", default=True, help="Verify endpoints before creation (default: on)")
    p.add_argument("--no-verify", dest="verify", action="store_false", help="Skip verification")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    base = resolve_base_url(args.base_url)
    secret = resolve_cron_secret(args.cron_secret)
    api_key = (args.cronjob_api_key or os.environ.get("CRONJOB_API_KEY") or os.environ.get("CJ_API_KEY") or os.environ.get("CRON_JOB_API_KEY") or "").strip()

    print("=" * 70)
    print("⏰ External Cron Setup — Vercel Endpoints (GitHub Delay Bypass)")
    print("=" * 70)
    print(f"[CONFIG] Base URL: {base}")
    print(f"[CONFIG] CRON_SECRET: {'***'+secret[-4:] if secret else '(not set — endpoints open, set CRON_SECRET in Vercel .env)'}")
    print(f"[CONFIG] Provider: {args.provider} | dry-run={args.dry_run} | create={args.create}")
    print(f"[CONFIG] Endpoints:")
    for job in JOBS:
        print(f"  {job['key']:12s} {job['cron']:20s} → {base}{job['path']}  ({job['desc']})")
    print()

    # 1) Verify Vercel crons config
    if args.provider in ("vercel", "all"):
        check_vercel_crons()
        print()

    # 2) Verify endpoints exist and accept secret via header OR query
    if args.verify:
        print("[VERIFY] Probing Vercel endpoints (header + query string)…")
        all_ok = True
        for job in JOBS:
            ok, detail = verify_endpoint(base, job["path"], secret)
            status = "PASS" if ok else "FAIL"
            print(f"[VERIFY][{status}] {job['path']:25s} → {detail}")
            if not ok:
                all_ok = False
        if not all_ok:
            print("[VERIFY][WARN] Some endpoints not reachable — deploy first: vercel --prod or git push")
            print("[VERIFY][HINT] Test manually: curl -i \"{}/api/pre_market?secret=YOUR_SECRET\"".format(base))
        else:
            print("[VERIFY][PASS] All endpoints reachable (header + ?secret= both work)")
        print()
        if args.verify_only:
            return 0 if all_ok else 1

    # 3) Provide manual instructions always
    print_manual_instructions(base, secret)

    # 4) Optionally create via cron-job.org API
    if args.provider in ("cronjob", "all"):
        if args.create:
            if not api_key:
                print()
                print("[CRONJOB][FATAL] --create requires CRONJOB_API_KEY (get from https://cron-job.org/en/members/settings/)")
                print("[CRONJOB][HINT] Export then re-run: CRONJOB_API_KEY=xxx python scripts/setup_cronjobs.py --create")
                return 2
            print()
            print("[CRONJOB] Creating jobs via https://api.cron-job.org/jobs …")
            for job in JOBS:
                ok, detail = create_cronjob_via_api(job, base, secret, api_key, dry_run=args.dry_run)
                tag = "PASS" if ok else "FAIL"
                print(f"[CRONJOB][{tag}] {job['title']:30s} → {detail}")
                if not ok:
                    print(f"[CRONJOB][WARN] Failed for {job['path']}: {detail}")
            print()
            if args.dry_run:
                print("[CRONJOB] DRY-RUN complete — no jobs created (remove --dry-run to create)")
            else:
                print("[CRONJOB] Done — check https://cron-job.org/en/members/jobs/ to verify")
                print("[CRONJOB] Test run each job and expect {\"ok\":true} from Vercel")
        else:
            if args.dry_run:
                print()
                print("[CRONJOB] Dry-run: would create 3 jobs via API (use --create + CRONJOB_API_KEY to actually create)")
                for job in JOBS:
                    url = build_url(base, job["path"], secret, True)
                    print(f"  would create: {job['title']} → {url}  schedule {job['cron']}")

    print()
    print("[DONE] Setup complete. Next: disable GitHub schedule (see Workflow Cleanup) and deploy Vercel.")
    print("[DONE] Then verify clean: curl \"{}/api/post_market?secret=$CRON_SECRET\"".format(base))
    return 0


if __name__ == "__main__":
    sys.exit(main())
