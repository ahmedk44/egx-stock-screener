#!/usr/bin/env python3
"""
scripts/setup_webhook.py - Register Telegram Webhook for Vercel deployment

Adds/Verifies script that registers the Vercel deployment URL
(https://<your-vercel-domain>/api/webhook) with Telegram Bot API (setWebhook).
Prints clear confirmation upon HTTP 200 from Telegram API.

Usage:
  python scripts/setup_webhook.py --domain https://your-app.vercel.app
  python scripts/setup_webhook.py --domain your-app.vercel.app
  python scripts/setup_webhook.py  # reads VERCEL_DOMAIN / VERCEL_URL / VERCEL_PROJECT_PRODUCTION_URL from env
  VERCEL_DOMAIN=https://your-app.vercel.app python scripts/setup_webhook.py

Env vars:
  TELEGRAM_BOT_TOKEN - required
  VERCEL_DOMAIN / VERCEL_URL / VERCEL_PROJECT_PRODUCTION_URL / WEBHOOK_URL / APP_URL - optional domain source

Exit code 0 = webhook set successfully, 1 = failure, 2 = missing token/domain
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

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

EXPECTED_ALLOWED = ["message", "callback_query"]


def normalize_webhook_url(domain: str) -> str:
    domain = (domain or "").strip()
    if not domain:
        return ""
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    domain = domain.rstrip("/")
    if domain.endswith("/api/webhook"):
        return domain
    return f"{domain}/api/webhook"


def set_telegram_webhook(vercel_domain: str, bot_token: Optional[str] = None) -> bool:
    """Set Telegram webhook to Vercel domain and print clear confirmation on HTTP 200."""
    try:
        token = (bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if not token:
            print("[FATAL] TELEGRAM_BOT_TOKEN not set (env or --token)")
            print("[HINT] Set it via: export TELEGRAM_BOT_TOKEN=123456:ABC... or add to .env")
            return False
        if not vercel_domain:
            print("[FATAL] Vercel domain not provided. Usage: python scripts/setup_webhook.py --domain https://your-app.vercel.app")
            return False

        webhook_url = normalize_webhook_url(vercel_domain)
        if not webhook_url:
            print(f"[FATAL] Invalid domain: {vercel_domain!r}")
            return False

        print(f"[SETUP] Registering Telegram webhook to: {webhook_url}")
        print(f"[SETUP] Using token: {token[:6]}...{token[-4:] if len(token) > 10 else '***'}")

        if requests is None:
            print("[FATAL] requests library not installed. Run: pip install requests")
            return False

        url = f"https://api.telegram.org/bot{token}/setWebhook"
        payload = {"url": webhook_url, "allowed_updates": EXPECTED_ALLOWED}

        print(f"[SETUP] Calling Telegram Bot API setWebhook ...")
        print(f"[SETUP] Payload: {json.dumps(payload, ensure_ascii=False)}")

        resp = requests.post(url, json=payload, timeout=15)
        status = resp.status_code
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:1000] if resp.text else ""

        print(f"[SETUP] HTTP {status} Response: {json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body}")

        if status == 200:
            if isinstance(body, dict):
                if body.get("ok"):
                    # Clear confirmation per task spec
                    print(f"[SUCCESS] ✅ Webhook was set successfully!")
                    print(f"[SUCCESS] Telegram API returned: {json.dumps(body, ensure_ascii=False)}")
                    print(f"[SUCCESS] Webhook URL: {webhook_url}")
                    print(f"[SUCCESS] allowed_updates: {EXPECTED_ALLOWED}")
                    # Expected spec: {"ok": true, "result": true, "description": "Webhook was set"}
                    if body.get("result") is True and "Webhook was set" in str(body.get("description", "")):
                        print(f"[SUCCESS] Confirmation: {body}")
                    return True
                else:
                    print(f"[FAIL] Telegram returned ok=false: {json.dumps(body, ensure_ascii=False)}")
                    return False
            else:
                # Non-JSON but HTTP 200
                print(f"[SUCCESS] ✅ Webhook set to {webhook_url} (HTTP 200)")
                print(f"[SUCCESS] Raw response: {body}")
                return True
        else:
            print(f"[FAIL] Failed to set webhook: HTTP {status} {body}")
            if isinstance(body, dict) and body.get("description"):
                print(f"[FAIL] Telegram says: {body.get('description')}")
            return False

    except Exception as exc:
        print(f"[FAIL] Unexpected error: {exc}")
        import traceback
        traceback.print_exc()
        return False


def get_webhook_info(bot_token: str) -> Optional[dict]:
    """Query getWebhookInfo for verification."""
    if not bot_token or requests is None:
        return None
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getWebhookInfo"
        print(f"\n[VERIFY] Querying getWebhookInfo for confirmation ...")
        resp = requests.get(url, timeout=15)
        print(f"[VERIFY] HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            print(f"[VERIFY][ERROR] Non-JSON response: {resp.text[:500]}")
            return None
        if data.get("ok"):
            result = data.get("result", {})
            print(f"[VERIFY] Current webhook URL: {result.get('url', '')}")
            print(f"[VERIFY] pending_update_count: {result.get('pending_update_count', '')}")
            print(f"[VERIFY] allowed_updates: {result.get('allowed_updates', [])}")
            if result.get("last_error_message"):
                print(f"[VERIFY][WARN] last_error_message: {result.get('last_error_message')}")
            return result
        else:
            print(f"[VERIFY][ERROR] getWebhookInfo returned ok=false: {data}")
            return None
    except Exception as exc:
        print(f"[VERIFY][ERROR] getWebhookInfo failed: {exc}")
        return None


def resolve_domain(args_domain: Optional[str]) -> Optional[str]:
    if args_domain:
        return args_domain.strip()
    # Try common Vercel env vars
    for var in ("VERCEL_DOMAIN", "VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL", "WEBHOOK_URL", "APP_URL", "NEXT_PUBLIC_VERCEL_URL"):
        val = (os.environ.get(var) or "").strip()
        if val:
            print(f"[SETUP] Using domain from env {var}={val}")
            return val
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Register Telegram webhook for Vercel deployment")
    p.add_argument("--domain", type=str, default=None, help="Vercel domain (e.g., https://your-app.vercel.app or your-app.vercel.app)")
    p.add_argument("--token", type=str, default=None, help="Telegram bot token (overrides TELEGRAM_BOT_TOKEN env)")
    p.add_argument("--verify", action="store_true", help="Verify with getWebhookInfo after setting (default: true)", default=True)
    p.add_argument("--no-verify", dest="verify", action="store_false", help="Skip verification")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    token = (args.token or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    domain = resolve_domain(args.domain)

    if not token:
        print("[FATAL] TELEGRAM_BOT_TOKEN not set (env or --token)")
        print("[ENV AUDIT] TELEGRAM_BOT_TOKEN: MISSING")
        return 2
    else:
        print("[ENV AUDIT] TELEGRAM_BOT_TOKEN: OK")

    # Env audit for other required vars (informational)
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "ADMIN_USER_IDS"):
        alt = var
        if var == "SUPABASE_SERVICE_ROLE_KEY":
            present = bool((os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip())
        elif var == "ADMIN_USER_IDS":
            present = bool((os.environ.get("ADMIN_USER_IDS") or os.environ.get("ADMIN_TELEGRAM_IDS") or "").strip())
        else:
            present = bool((os.environ.get(var) or "").strip())
        print(f"[ENV AUDIT] {alt}: {'OK' if present else 'MISSING' + (' (fallback check)' if alt=='ADMIN_USER_IDS' else '')}")

    if not domain:
        print("[FATAL] Vercel domain not provided (use --domain or set VERCEL_DOMAIN env)")
        print("[HINT] Example: python scripts/setup_webhook.py --domain https://your-app.vercel.app")
        return 2

    ok = set_telegram_webhook(domain, bot_token=token)
    if not ok:
        print("[SETUP][FAIL] setWebhook failed")
        return 1

    if args.verify:
        info = get_webhook_info(token)
        if info:
            url = str(info.get("url", "") or "")
            expected = normalize_webhook_url(domain)
            if url.rstrip("/") == expected.rstrip("/"):
                print(f"[VERIFY][PASS] Webhook URL matches expected {expected}")
            else:
                print(f"[VERIFY][WARN] URL mismatch: got {url!r}, expected {expected!r}")
            allowed = info.get("allowed_updates") or []
            if not allowed:
                print(f"[VERIFY][INFO] allowed_updates empty - Telegram will send all updates (ensure 'message' and 'callback_query' are delivered)")
            else:
                missing = set(EXPECTED_ALLOWED) - set(allowed)
                if missing:
                    print(f"[VERIFY][WARN] allowed_updates missing {sorted(missing)} - got {allowed}")
                else:
                    print(f"[VERIFY][PASS] allowed_updates includes {EXPECTED_ALLOWED}")

    print("\n[SUCCESS] 🎉 Webhook registration completed successfully!")
    print("[SUCCESS] Test by sending /portfolio in private bot chat - bot should respond with portfolio card")
    return 0


if __name__ == "__main__":
    sys.exit(main())
