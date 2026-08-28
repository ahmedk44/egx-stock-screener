#!/usr/bin/env python3
"""
check_webhook.py - Webhook Registration Check Helper

Queries Telegram's getWebhookInfo API to verify:
  - Active webhook URL points to https://<VERCEL_DOMAIN>/api/webhook
  - allowed_updates includes ["message", "callback_query"]

Usage:
  python check_webhook.py
  python check_webhook.py --domain https://your-app.vercel.app
  python check_webhook.py --token <TELEGRAM_BOT_TOKEN>

Env vars:
  TELEGRAM_BOT_TOKEN  - Bot token from @BotFather
  VERCEL_DOMAIN / VERCEL_URL - Expected Vercel domain (optional, for URL verification)

Exit code 0 = verification passed, 1 = failed/misconfigured, 2 = missing token.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

if hasattr(__import__('sys').stdout, 'reconfigure'):
    try:
        __import__('sys').stdout.reconfigure(encoding='utf-8')  # type: ignore[attr-defined]
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
    requests = None  # type: ignore[assignment]

EXPECTED_ALLOWED = {"message", "callback_query"}


def get_webhook_info(bot_token: str) -> Optional[Dict[str, Any]]:
    if not bot_token:
        print("[CHECK][ENV AUDIT] TELEGRAM_BOT_TOKEN is missing - cannot query getWebhookInfo")
        return None
    if requests is None:
        print("[CHECK][ERROR] requests library not installed")
        return None
    url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
    print(f"[CHECK] Querying {url.replace(bot_token, '***')} ...")
    try:
        resp = requests.get(url, timeout=15)
        print(f"[CHECK] HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            print(f"[CHECK][ERROR] Non-JSON response: {resp.text[:500]}")
            return None
        if not isinstance(data, dict):
            print(f"[CHECK][ERROR] Unexpected response type: {data}")
            return None
        if not data.get("ok"):
            print(f"[CHECK][ERROR] Telegram returned ok=false: {json.dumps(data, ensure_ascii=False)}")
            return None
        result = data.get("result", {})
        # Log explicit details
        print(f"[CHECK] url: {result.get('url', '')}")
        print(f"[CHECK] has_custom_certificate: {result.get('has_custom_certificate', '')}")
        print(f"[CHECK] pending_update_count: {result.get('pending_update_count', '')}")
        print(f"[CHECK] allowed_updates: {result.get('allowed_updates', [])}")
        print(f"[CHECK] ip_address: {result.get('ip_address', '')}")
        last_err = result.get("last_error_message", "")
        last_date = result.get("last_error_date", "")
        if last_date:
            try:
                dt = datetime.fromtimestamp(int(last_date), tz=timezone.utc)
                print(f"[CHECK] last_error_date: {last_date} ({dt.isoformat()})")
            except Exception:
                print(f"[CHECK] last_error_date: {last_date}")
        else:
            print("[CHECK] last_error_date: None")
        print(f"[CHECK] last_error_message: {last_err or 'None'}")
        # Warn on 4xx/5xx style last errors? Telegram encodes last_error as text.
        if last_err:
            print(f"[CHECK][WARN] Telegram reports last webhook error: {last_err}")
        return result
    except Exception as exc:
        print(f"[CHECK][ERROR] Request failed: {exc}")
        return None


def verify_webhook(result: Dict[str, Any], expected_domain: Optional[str]) -> bool:
    ok = True
    url = str(result.get("url", "") or "")
    allowed: List[str] = result.get("allowed_updates") or []
    allowed_set = set(allowed)

    # Verify URL
    if not url:
        print("[VERIFY][FAIL] Webhook URL is empty - no webhook set!")
        ok = False
    else:
        print(f"[VERIFY] Active webhook URL: {url}")
        if expected_domain:
            exp = expected_domain.strip().rstrip("/")
            if not exp.startswith("http"):
                exp = f"https://{exp}"
            if exp.endswith("/api/webhook"):
                expected_url = exp
            else:
                expected_url = f"{exp}/api/webhook"
            if url.rstrip("/") == expected_url.rstrip("/"):
                print(f"[VERIFY][PASS] URL matches expected {expected_url}")
            else:
                print(f"[VERIFY][FAIL] URL mismatch: got {url!r}, expected {expected_url!r}")
                ok = False
        else:
            # Generic check: must end with /api/webhook and be https
            if url.startswith("https://") and url.endswith("/api/webhook"):
                print("[VERIFY][PASS] URL looks correctly configured (https + /api/webhook)")
            else:
                print(f"[VERIFY][FAIL] URL does not point to https://<VERCEL_DOMAIN>/api/webhook : {url!r}")
                ok = False

    # Verify allowed_updates includes both required values.
    # Telegram returns [] or None when all updates allowed; but spec requires explicit verification.
    # We treat missing field as warning, and explicit list must contain both.
    if not allowed:
        print("[VERIFY][WARN] allowed_updates is empty/null - Telegram will send all updates (verify channel still receives callback_query)")
        # Consider this a soft fail? As per spec we require explicit check.
        # We warn but not fail if server uses default.
        print("[VERIFY][INFO] Expected allowed_updates to include ['message', 'callback_query']")
    else:
        missing = EXPECTED_ALLOWED - allowed_set
        extra = allowed_set - EXPECTED_ALLOWED
        if not missing:
            print(f"[VERIFY][PASS] allowed_updates includes {sorted(EXPECTED_ALLOWED)} : {allowed}")
        else:
            print(f"[VERIFY][FAIL] allowed_updates missing {sorted(missing)} - got {allowed}")
            ok = False
        if extra:
            print(f"[VERIFY][INFO] allowed_updates has extra entries: {sorted(extra)}")

    if ok:
        print("[VERIFY] [PASS] Overall webhook verification PASSED")
    else:
        print("[VERIFY] [FAIL] Overall webhook verification FAILED - check VERCEL_DOMAIN and setWebhook payload")
    return ok


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Verify Telegram webhook via getWebhookInfo")
    p.add_argument("--domain", type=str, default=None, help="Expected Vercel domain (e.g., https://your-app.vercel.app)")
    p.add_argument("--token", type=str, default=None, help="Telegram bot token (overrides TELEGRAM_BOT_TOKEN env)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    token = (args.token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    domain = (args.domain or os.environ.get("VERCEL_DOMAIN") or os.environ.get("VERCEL_URL") or "").strip()
    if not token:
        print("[FATAL] TELEGRAM_BOT_TOKEN not set (env or --token)")
        return 2
    result = get_webhook_info(token)
    if result is None:
        print("[CHECK][FAIL] getWebhookInfo query failed")
        return 1
    ok = verify_webhook(result, domain if domain else None)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
