#!/usr/bin/env python3
"""
Live test: trigger Vercel webhook synchronously and verify Supabase user_portfolio insertion.
Steps:
1. Ensure a test row exists in trade_signals (ticker TEST.CA)
2. POST callback_query to https://egx-stock-screener.vercel.app/api/webhook
3. Query public.user_portfolio via Supabase REST API to confirm insertion
4. Print explicit trace logs
"""
import os, json, time, requests
from pathlib import Path
from datetime import datetime, timezone

# Load env
env={}
for line in Path('.env').read_text(encoding='utf-8').splitlines():
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k,v=line.split('=',1)
    v=v.strip().strip('"').strip("'")
    env[k.strip()]=v

SUPABASE_URL = env.get('SUPABASE_URL','').rstrip('/')
SUPABASE_KEY = env.get('SUPABASE_SERVICE_ROLE_KEY') or env.get('SUPABASE_KEY','')
SUPABASE_KEY = SUPABASE_KEY.strip().strip('"').strip("'")
TELEGRAM_BOT_TOKEN = env.get('TELEGRAM_BOT_TOKEN','').strip()
VERCEL_URL = "https://egx-stock-screener.vercel.app/api/webhook"

TEST_TICKER = "TEST.CA"
TEST_TICKER_BARE = "TEST"
TEST_USER_ID = "999999999"  # synthetic test user
TEST_TRADE_ID = 4  # matches existing trade_signals id=4 ticker TEST.CA

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

def supabase_get(table, query):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    r = requests.get(url, headers=headers, timeout=10)
    print(f"[SUPABASE GET] {table}?{query} -> {r.status_code} {r.text[:500]}")
    return r

def supabase_post(table, payload, on_conflict=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    h = dict(headers)
    h["Prefer"] = "return=representation"
    r = requests.post(url, headers=h, json=payload, timeout=10)
    print(f"[SUPABASE POST] {table} -> {r.status_code} {r.text[:800]}")
    return r

def ensure_trade_signal():
    print("\n=== Ensure trade_signals test row ===")
    # Try to fetch existing
    r = supabase_get("trade_signals", f"symbol=eq.{TEST_TICKER}&select=*&limit=1")
    if r.status_code==200 and r.json():
        print(f"Found existing trade_signal: {r.json()[0]}")
        return r.json()[0]
    # Try ticker column
    r = supabase_get("trade_signals", f"ticker=eq.{TEST_TICKER}&select=*&limit=1")
    if r.status_code==200 and r.json():
        print(f"Found existing via ticker: {r.json()[0]}")
        return r.json()[0]
    # Insert new test signal
    payload = {
        "symbol": TEST_TICKER,
        "ticker": TEST_TICKER,
        "ticker_bare": TEST_TICKER_BARE,
        "entry_price": 10.5,
        "stop_loss": 9.8,
        "target_1": 11.0,
        "target_2": 12.0,
        "target_3": 13.0,
        "strategy": "swing",
        "trade_id": TEST_TRADE_ID,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    print(f"Inserting test trade_signal: {payload}")
    r = supabase_post("trade_signals", payload)
    if r.status_code in (200,201,204):
        print("Inserted trade_signal OK")
        # fetch again
        r2 = supabase_get("trade_signals", f"symbol=eq.{TEST_TICKER}&select=*&limit=1")
        if r2.json():
            return r2.json()[0]
    else:
        print(f"Failed to insert trade_signal: {r.status_code} {r.text}")
        # try without trade_id
        payload2 = {k:v for k,v in payload.items() if k!="trade_id"}
        r = supabase_post("trade_signals", payload2)
        print(f"Retry without trade_id: {r.status_code} {r.text[:500]}")
    return None

def clean_user_portfolio():
    print("\n=== Clean existing user_portfolio for test user ===")
    # Delete existing test rows
    url = f"{SUPABASE_URL}/rest/v1/user_portfolio?user_id=eq.{TEST_USER_ID}&symbol=eq.{TEST_TICKER}"
    r = requests.delete(url, headers=headers, timeout=10)
    print(f"DELETE user_portfolio test rows -> {r.status_code} {r.text[:500]}")
    # Also try without .CA
    url2 = f"{SUPABASE_URL}/rest/v1/user_portfolio?user_id=eq.{TEST_USER_ID}&symbol=eq.{TEST_TICKER_BARE}"
    r2 = requests.delete(url2, headers=headers, timeout=10)
    print(f"DELETE bare -> {r2.status_code} {r2.text[:500]}")

def trigger_webhook():
    print("\n=== Trigger Vercel webhook POST ===")
    payload = {
        "update_id": 999999,
        "callback_query": {
            "id": f"test-{int(time.time())}",
            "from": {"id": int(TEST_USER_ID), "is_bot": False, "first_name": "TestUser", "username": "testuser"},
            "message": {"message_id": 123, "chat": {"id": -1003993921849, "type": "channel"}},
            "data": f"join_trade:{TEST_TICKER}:{TEST_TRADE_ID}",
            "chat_instance": "test-ci-001"
        }
    }
    print(f"[WEBHOOK] POST payload: {json.dumps(payload)[:800]}")
    start = time.time()
    r = requests.post(VERCEL_URL, json=payload, headers={"Content-Type":"application/json"}, timeout=20)
    elapsed = (time.time()-start)*1000
    print(f"[WEBHOOK] Response code={r.status_code} elapsed={elapsed:.1f}ms body={r.text[:1000]}")
    print(f"[WEBHOOK] Headers: {dict(r.headers)}")
    return r

def verify_portfolio():
    print("\n=== Verify public.user_portfolio insertion ===")
    # Wait a bit for synchronous processing (though should be immediate)
    time.sleep(2)
    for attempt in range(3):
        r = supabase_get("user_portfolio", f"user_id=eq.{TEST_USER_ID}&symbol=eq.{TEST_TICKER}&select=*")
        if r.status_code==200:
            data = r.json()
            print(f"[VERIFY] Attempt {attempt+1}: found {len(data)} rows")
            if data:
                print(json.dumps(data, indent=2, ensure_ascii=False))
                # Check fields
                row = data[0]
                print(f"SUCCESS: user_portfolio row exists user_id={row.get('user_id')} symbol={row.get('symbol')} trade_id={row.get('trade_id')} status={row.get('status')}")
                return True
            else:
                print(f"No rows yet, retry...")
        else:
            print(f"Query failed {r.status_code} {r.text}")
        time.sleep(2)
    # Try bare symbol
    r = supabase_get("user_portfolio", f"user_id=eq.{TEST_USER_ID}&select=*&limit=5")
    print(f"[VERIFY] All rows for user: {r.status_code} {r.text[:2000]}")
    return False

if __name__ == "__main__":
    print("=== LIVE TEST: Telegram webhook -> Supabase user_portfolio ===")
    print(f"SUPABASE_URL={SUPABASE_URL}")
    print(f"VERCEL_URL={VERCEL_URL}")
    ensure_trade_signal()
    clean_user_portfolio()
    resp = trigger_webhook()
    success = verify_portfolio()
    print("\n=== FINAL RESULT ===")
    if resp.status_code==200 and success:
        print("[OVERALL PASS] Webhook 200 and DB insertion verified")
    elif resp.status_code==200 and not success:
        print("[PARTIAL] Webhook 200 but DB insertion NOT found - check Vercel logs for [SUPABASE] prints")
    else:
        print(f"[FAIL] Webhook returned {resp.status_code}")
