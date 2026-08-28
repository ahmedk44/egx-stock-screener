#!/usr/bin/env python3
"""
test_live_vercel.py - Live Vercel Webhook Endpoint Direct Probe

Sends a mock Telegram callback_query HTTP POST directly to the public Vercel URL
https://egx-stock-screener.vercel.app/api/webhook

Logs:
  - HTTP response code
  - Response body
  - Execution latency (ms)
  - Headers

Simulates Vercel payload for join_trade:TEST.CA:4 from a user who has /start active.
"""
from __future__ import annotations

import json
import sys
import time
import os
from typing import Any, Dict

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

try:
    import requests  # type: ignore
except ImportError:
    print("[FATAL] requests not installed")
    sys.exit(1)

VERCEL_URL = os.environ.get("VERCEL_URL") or "https://egx-stock-screener.vercel.app/api/webhook"
# Ensure correct endpoint
if not VERCEL_URL.endswith("/api/webhook"):
    VERCEL_URL = VERCEL_URL.rstrip("/") + "/api/webhook"
if not VERCEL_URL.startswith("http"):
    VERCEL_URL = "https://" + VERCEL_URL

# Mock Telegram callback_query payloads
VALID_PAYLOAD: Dict[str, Any] = {
    "update_id": 999999,
    "callback_query": {
        "id": "live-test-001",
        "from": {"id": 8903435825, "is_bot": False, "first_name": "LiveTest", "username": "livetest"},
        "message": {"message_id": 123, "chat": {"id": -1003993921849, "type": "channel"}},
        "data": "join_trade:TEST.CA:4",
        "chat_instance": "live-ci-001",
    }
}

# Alternative: ticker only without trade_id
TICKER_ONLY_PAYLOAD: Dict[str, Any] = {
    "update_id": 999998,
    "callback_query": {
        "id": "live-test-002",
        "from": {"id": 8903435825, "is_bot": False, "first_name": "LiveTest2"},
        "message": {"message_id": 124, "chat": {"id": -1003993921849, "type": "channel"}},
        "data": "join_trade:TEST.CA",
        "chat_instance": "live-ci-002",
    }
}

def probe(payload: Dict[str, Any], label: str) -> Dict[str, Any]:
    print(f"\n{'='*70}")
    print(f"[PROBE] {label} -> {VERCEL_URL}")
    print(f"[PAYLOAD] {json.dumps(payload, ensure_ascii=False)[:300]}")
    start = time.perf_counter()
    try:
        resp = requests.post(
            VERCEL_URL,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "EGX-Test-Probe/1.0"},
            timeout=30,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        print(f"[RESPONSE] HTTP {resp.status_code} in {latency_ms:.1f} ms")
        # Headers
        print(f"[HEADERS] {dict(resp.headers)}")
        # Body
        body = resp.text or "(empty)"
        preview = body[:2000]
        # Try to handle utf-8
        try:
            print(f"[BODY] {preview[:1000]}")
        except Exception:
            print(f"[BODY] {repr(preview[:500])}")
        # Try JSON
        try:
            j = resp.json()
            print(f"[JSON] {json.dumps(j, ensure_ascii=False)[:500]}")
        except Exception:
            pass
        return {"status": resp.status_code, "body": body, "latency_ms": latency_ms, "headers": dict(resp.headers)}
    except requests.exceptions.RequestException as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        print(f"[ERROR] Request failed after {latency_ms:.1f} ms: {exc}")
        return {"status": 0, "body": str(exc), "latency_ms": latency_ms, "headers": {}}
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        print(f"[ERROR] Unexpected: {exc}")
        return {"status": 0, "body": str(exc), "latency_ms": latency_ms, "headers": {}}

def main() -> int:
    print("="*70)
    print("Live Vercel Webhook Direct Probe")
    print(f"Target: {VERCEL_URL}")
    print("="*70)
    # Check env locally for reference
    local_token = bool((os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip())
    local_supabase = bool((os.environ.get("SUPABASE_URL") or "").strip())
    local_service = bool((os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip())
    print(f"[LOCAL ENV] TELEGRAM_BOT_TOKEN present: {local_token}")
    print(f"[LOCAL ENV] SUPABASE_URL present: {local_supabase}")
    print(f"[LOCAL ENV] SUPABASE_SERVICE_ROLE_KEY present: {local_service}")
    print(f"[NOTE] Vercel runtime env may differ - check Vercel Dashboard > Settings > Environment Variables")

    # Probe 1: valid with trade_id
    r1 = probe(VALID_PAYLOAD, "Valid join_trade:TEST.CA:4 (with trade_id)")
    # Probe 2: ticker only
    r2 = probe(TICKER_ONLY_PAYLOAD, "Ticker-only join_trade:TEST.CA (fallback to latest)")

    print("\n" + "="*70)
    print("Summary")
    print("="*70)
    for label, r in [("With trade_id", r1), ("Ticker-only", r2)]:
        status = r["status"]
        latency = r["latency_ms"]
        body_snip = r["body"][:200].replace("\n"," ")
        print(f"{label}: HTTP {status} in {latency:.1f}ms | Body: {body_snip[:150]}")
        if status in (200, 201, 204):
            print(f"  -> [PASS] Vercel endpoint reachable and returned success")
        elif status == 500:
            print(f"  -> [FAIL] Vercel returned 500 Internal Server Error - check Vercel logs and env vars")
        elif status == 0:
            print(f"  -> [FAIL] Network error")
        else:
            print(f"  -> [WARN] Unexpected status")

    # Overall
    if r1["status"] in (200,201,204) and r2["status"] in (200,201,204):
        print("\n[OVERALL PASS] Live endpoint executed successfully for both payloads")
        return 0
    print("\n[OVERALL FAIL] One or more probes failed - Vercel deployment may still be building or env missing")
    print("Hint: Wait 30-60s after git push for Vercel build, then re-run; check Vercel Dashboard > Deployments > Logs")
    return 1

if __name__ == "__main__":
    sys.exit(main())
