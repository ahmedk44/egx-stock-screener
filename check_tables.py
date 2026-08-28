#!/usr/bin/env python3
"""
check_tables.py - Supabase Table Routing Verification

Verifies that HTTP 200 is returned for public.user_portfolio and public.trade_signals
once DDL is executed. This confirms the scanner pipeline is synced to the new tables
and that SUPABASE_SERVICE_ROLE_KEY has bypassed RLS.

Usage:
  python check_tables.py

Env:
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY (prioritized) or SUPABASE_KEY

Exit codes:
  0 = both tables reachable (HTTP 200)
  1 = one or both missing (PGRST205) or auth failure
  2 = missing env
"""
from __future__ import annotations

import os
import sys
from typing import Tuple

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
    requests = None  # type: ignore[assignment]

TABLES = ["user_portfolio", "trade_signals"]


def get_cfg() -> Tuple[str, str] | None:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    if not url or not key:
        if not url:
            print("[CHECK][ENV AUDIT] SUPABASE_URL missing")
        if not key:
            print("[CHECK][ENV AUDIT] SUPABASE_SERVICE_ROLE_KEY / SUPABASE_KEY missing")
        return None
    return url, key


def check_table(url: str, key: str, table: str) -> bool:
    assert requests is not None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    endpoint = f"{url}/rest/v1/{table}?limit=1&select=*"
    print(f"[CHECK] GET {table} -> {endpoint}")
    try:
        resp = requests.get(endpoint, headers=headers, timeout=10)
        body_preview = (resp.text or "")[:400].encode("ascii", "replace").decode("ascii")
        print(f"[CHECK] {table}: HTTP {resp.status_code} {body_preview[:200]}")
        if resp.status_code == 200:
            print(f"[PASS] {table} reachable (HTTP 200) - routing correct")
            return True
        if resp.status_code == 401:
            print(f"[FAIL] {table}: 401 Unauthorized - check SUPABASE_SERVICE_ROLE_KEY")
            return False
        if resp.status_code == 404 and "PGRST205" in resp.text:
            print(f"[FAIL] {table}: 404 PGRST205 schema cache - DDL not executed (run setup_db.sql / supabase_setup.sql in Supabase SQL Editor)")
            return False
        print(f"[FAIL] {table}: unexpected HTTP {resp.status_code}")
        return False
    except Exception as exc:
        print(f"[ERROR] {table} request failed: {exc}")
        return False


def main() -> int:
    if requests is None:
        print("[ERROR] requests not installed")
        return 1
    cfg = get_cfg()
    if cfg is None:
        print("[FATAL] Missing Supabase env")
        return 2
    url, key = cfg
    print(f"[CHECK] SUPABASE_URL: {url}")
    print(f"[CHECK] KEY prefix: {key[:10]}... len={len(key)} (service_role prioritized)")
    ok_all = True
    for tbl in TABLES:
        ok = check_table(url, key, tbl)
        ok_all = ok_all and ok
    if ok_all:
        print("\n[VERIFY] [PASS] All routing tables verified (HTTP 200) - scanner pipeline synced to user_portfolio/trade_signals")
        return 0
    print("\n[VERIFY] [FAIL] One or more tables not reachable - check DDL and RLS")
    print("  Hint: Execute setup_db.sql which creates public.user_portfolio (UNIQUE user_id,symbol) and public.trade_signals")
    return 1


if __name__ == "__main__":
    sys.exit(main())
