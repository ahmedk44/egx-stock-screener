#!/usr/bin/env python3
"""
Cleanup duplicate trade_signals rows for TEST3.CA - keeps only latest active row.
Per task requirement: delete stale duplicated test rows for TEST3.CA keeping only the latest active row.
"""
import os, sys, json
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except:
        pass
from dotenv import load_dotenv
load_dotenv(dotenv_path=r'D:\Egyptian Stock Exchange\.env')
import requests

def get_cfg():
    url=(os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key=(os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    return url, key

def cleanup_ticker(ticker="TEST3.CA"):
    url, key = get_cfg()
    headers={'apikey':key,'Authorization': f'Bearer {key}','Content-Type':'application/json'}
    # Fetch all rows for ticker ordered by created_at desc (latest first)
    resp=requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.{ticker}&order=created_at.desc&select=*", headers=headers, timeout=10)
    print(f"[CLEANUP] GET {ticker} -> HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"[FAIL] fetch failed {resp.text[:500]}")
        return False
    rows=resp.json()
    if not isinstance(rows, list):
        print(f"[FAIL] unexpected response {resp.text[:500]}")
        return False
    print(f"[CLEANUP] Found {len(rows)} rows for {ticker}:")
    for r in rows:
        print(f"  id={r.get('id')} ticker={r.get('ticker')} created_at={r.get('created_at')} entry={r.get('entry_price')} targets={r.get('target_1')}/{r.get('target_2')}/{r.get('target_3')}")
    if len(rows) <= 1:
        print(f"[CLEANUP] No duplicates for {ticker} - nothing to clean")
        return True
    # Keep latest (first after order desc), delete rest
    keep = rows[0]
    keep_id = keep.get("id")
    print(f"[CLEANUP] Keeping latest id={keep_id} created_at={keep.get('created_at')}")
    stale = rows[1:]
    deleted = 0
    for r in stale:
        stale_id = r.get("id")
        del_resp=requests.delete(f"{url}/rest/v1/trade_signals?id=eq.{stale_id}", headers=headers, timeout=10)
        print(f"[CLEANUP] DELETE id={stale_id} -> HTTP {del_resp.status_code} {del_resp.text[:200]}")
        if del_resp.status_code in (200,204):
            deleted+=1
        else:
            print(f"[WARN] delete id={stale_id} failed {del_resp.status_code}")
    print(f"[CLEANUP] Deleted {deleted}/{len(stale)} stale rows for {ticker}")
    # Verify only one remains
    verify=requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.{ticker}&order=created_at.desc&select=*", headers=headers, timeout=10)
    print(f"[VERIFY] After cleanup count: {len(verify.json()) if verify.status_code==200 else 'err'}")
    if verify.status_code==200:
        v_rows=verify.json()
        print(f"[VERIFY] Remaining rows: {json.dumps(v_rows, ensure_ascii=False, indent=2)}")
        if len(v_rows)==1 and v_rows[0].get("id")==keep_id:
            print(f"[SUCCESS] Cleanup verified - only latest id={keep_id} remains")
            return True
        else:
            print(f"[FAIL] Expected 1 row id={keep_id}, got {len(v_rows)}")
            return False
    return False

if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description="Cleanup duplicate trade_signals for TEST3.CA")
    parser.add_argument("--ticker", default="TEST3.CA", help="Ticker to cleanup")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be deleted")
    args=parser.parse_args()
    if args.dry_run:
        url, key = get_cfg()
        headers={'apikey':key,'Authorization': f'Bearer {key}'}
        r=requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.{args.ticker}&order=created_at.desc&select=*", headers=headers, timeout=10)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
    else:
        ok=cleanup_ticker(args.ticker)
        sys.exit(0 if ok else 1)
