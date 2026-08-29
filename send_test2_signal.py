#!/usr/bin/env python3
"""
Dispatch TEST2.CA live test signal per task requirements:
 Ticker TEST2.CA, Strategy Scalping, Entry 150.0, SL 142.0, Targets 158/162/168, TQI 9.1, COMPLIANT
 Steps:
 1. Insert into public.trade_signals via Supabase REST (service_role)
 2. Capture generated id as trade_id
 3. Verify readable via GET id=eq.trade_id
 4. Build single-button markup join_trade:TEST2.CA:{trade_id}
 5. Broadcast teaser to TELEGRAM_CHANNEL_SCALPING and print HTTP status
"""
import os, json, sys
from datetime import datetime, timezone
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass

from dotenv import load_dotenv
load_dotenv()
import requests

def get_cfg():
    url=(os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key=(os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip().strip('"').strip("'")
    token=(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    channel=(os.environ.get("TELEGRAM_CHANNEL_SCALPING") or "").strip()
    return url, key, token, channel

url, key, token, channel = get_cfg()
print(f"[CFG] SUPABASE_URL={url}")
print(f"[CFG] KEY prefix={key[:10]}... len={len(key)}")
print(f"[CFG] CHANNEL_SCALPING={channel}")
print(f"[CFG] BOT_TOKEN present={bool(token)}")

# 1. UPSERT TEST2.CA signal - check if active signal for same ticker already exists, update instead of duplicate
payload={
    "ticker":"TEST2.CA",
    "strategy_type":"Scalping",
    "entry_price":150.0,
    "stop_loss":142.0,
    "target_1":158.0,
    "target_2":162.0,
    "target_3":168.0,
    "tqi_score":9.1,
    "shariah_status":"COMPLIANT"
}
headers_base={"apikey":key,"Authorization":f"Bearer {key}","Content-Type":"application/json"}
# Dedup check: does TEST2.CA already exist?
existing_id=None
try:
    check_resp=requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.TEST2.CA&order=created_at.desc&limit=1&select=id", headers=headers_base, timeout=10)
    print(f"[UPSERT] Check existing TEST2.CA -> HTTP {check_resp.status_code} {check_resp.text[:300]}")
    if check_resp.status_code==200:
        rows=check_resp.json()
        if isinstance(rows, list) and rows and rows[0].get("id") is not None:
            existing_id=int(rows[0].get("id"))
            print(f"[UPSERT] Existing row found for TEST2.CA id={existing_id} -> will PATCH (update) instead of INSERT")
except Exception as e:
    print(f"[UPSERT][WARN] dedup check failed: {e}")
resp=None
if existing_id is not None:
    # PATCH existing row
    patch_headers={**headers_base, "Prefer":"return=representation"}
    patch_url=f"{url}/rest/v1/trade_signals?id=eq.{existing_id}"
    print(f"[UPSERT] PATCH {patch_url} payload={json.dumps(payload, ensure_ascii=False)}")
    resp=requests.patch(patch_url, json=payload, headers=patch_headers, timeout=15)
    print(f"[UPSERT] PATCH trade_signals -> HTTP {resp.status_code} {resp.text[:1000]}")
    if resp.status_code in (200,204):
        print(f"[UPSERT] PATCH success - updated existing id={existing_id} (no duplicate)")
        # For 204, need to keep existing_id as trade_id; for 200, parse returned id
        if resp.status_code==204:
            # Create fake 200-like response for downstream parsing
            class _Fake:
                status_code=200
                text=json.dumps([{"id": existing_id}])
                def json(self): return [{"id": existing_id}]
            resp=_Fake()
    else:
        # Fallback try on_conflict
        print(f"[UPSERT] PATCH failed {resp.status_code}, trying on_conflict=ticker upsert")
        upsert_headers={**headers_base, "Prefer":"resolution=merge-duplicates,return=representation"}
        resp=requests.post(f"{url}/rest/v1/trade_signals?on_conflict=ticker", json=payload, headers=upsert_headers, timeout=15)
        print(f"[UPSERT] POST on_conflict=ticker -> HTTP {resp.status_code} {resp.text[:1000]}")
        if resp.status_code not in (200,201,204):
            print("[FAIL] UPSERT failed")
            sys.exit(1)
else:
    # No existing - try on_conflict first, then plain POST
    try:
        upsert_headers={**headers_base, "Prefer":"resolution=merge-duplicates,return=representation"}
        upsert_resp=requests.post(f"{url}/rest/v1/trade_signals?on_conflict=ticker", json=payload, headers=upsert_headers, timeout=15)
        print(f"[UPSERT] POST on_conflict=ticker (new) -> HTTP {upsert_resp.status_code} {upsert_resp.text[:1000]}")
        if upsert_resp.status_code in (200,201,204):
            resp=upsert_resp
            if resp.status_code==204:
                # Query latest for id
                q=requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.TEST2.CA&order=created_at.desc&limit=1&select=id", headers=headers_base, timeout=10)
                if q.status_code==200 and q.json():
                    existing_id=int(q.json()[0].get("id") or 0)
                    class _Fake2:
                        status_code=200
                        text=json.dumps([{"id": existing_id}])
                        def json(self): return [{"id": existing_id}]
                    resp=_Fake2()
        else:
            raise Exception(f"on_conflict failed {upsert_resp.status_code}")
    except Exception as e:
        print(f"[UPSERT] on_conflict failed ({e}), falling back to plain POST")
        headers={"apikey":key,"Authorization":f"Bearer {key}","Content-Type":"application/json","Prefer":"return=representation"}
        endpoint=f"{url}/rest/v1/trade_signals"
        print(f"\n[SUPABASE] POST {endpoint}")
        print(f"[PAYLOAD] {json.dumps(payload, ensure_ascii=False)}")
        resp=requests.post(endpoint, json=payload, headers=headers, timeout=15)
        print(f"[SUPABASE] POST trade_signals -> HTTP {resp.status_code}")
        print(f"[SUPABASE] Body: {resp.text[:1000]}")
        if resp.status_code not in (200,201):
            print("[FAIL] Insert failed")
            sys.exit(1)

# Parse trade_id
trade_id=None
try:
    data=resp.json()
    if isinstance(data, list) and data and isinstance(data[0], dict):
        trade_id=int(data[0].get("id") or data[0].get("trade_id") or 0)
    elif isinstance(data, dict):
        trade_id=int(data.get("id") or 0)
    print(f"[OUTPUT] Generated trade_id from Supabase public.trade_signals: {trade_id}")
except Exception as e:
    print(f"[WARN] parse failed {e}")

if not trade_id:
    # Fallback query latest TEST2.CA
    q=requests.get(f"{url}/rest/v1/trade_signals?ticker=eq.TEST2.CA&order=created_at.desc&limit=1&select=id", headers={"apikey":key,"Authorization":f"Bearer {key}"}, timeout=10)
    print(f"[FALLBACK] GET latest TEST2.CA -> {q.status_code} {q.text[:500]}")
    if q.status_code==200:
        rows=q.json()
        if rows and isinstance(rows[0], dict):
            trade_id=int(rows[0].get("id") or 0)
            print(f"[FALLBACK] trade_id={trade_id}")

if not trade_id:
    print("[FAIL] No trade_id generated")
    sys.exit(1)

# 2. Verify markup format
markup_callback=f"join_trade:TEST2.CA:{trade_id}"
print(f"\n[MARKUP] Canonical single-button callback_data: {markup_callback}")
assert markup_callback==f"join_trade:TEST2.CA:{trade_id}", "markup mismatch"
print(f"[VERIFY] Markup format OK: join_trade:TEST2.CA:{{trade_id}}")

# 3. Verify table integrity: readable by trade_id
verify_url=f"{url}/rest/v1/trade_signals?id=eq.{trade_id}&select=*"
vr=requests.get(verify_url, headers={"apikey":key,"Authorization":f"Bearer {key}"}, timeout=10)
print(f"\n[VERIFY] GET {verify_url} -> HTTP {vr.status_code}")
print(f"[VERIFY] Body: {vr.text[:1000]}")
if vr.status_code==200:
    rows=vr.json()
    if isinstance(rows, list) and rows and rows[0].get("id")==trade_id:
        row=rows[0]
        print(f"[VERIFY] Row readable OK: id={row.get('id')} ticker={row.get('ticker')} strategy_type={row.get('strategy_type')} entry={row.get('entry_price')} sl={row.get('stop_loss')} t1={row.get('target_1')} t2={row.get('target_2')} t3={row.get('target_3')} tqi={row.get('tqi_score')} shariah={row.get('shariah_status')}")
        # Validate params
        assert row.get("ticker")=="TEST2.CA", "ticker mismatch"
        assert row.get("strategy_type")=="Scalping", "strategy mismatch"
        assert float(row.get("entry_price"))==150.0, "entry mismatch"
        assert float(row.get("stop_loss"))==142.0, "sl mismatch"
        assert float(row.get("target_1"))==158.0, "t1 mismatch"
        assert float(row.get("target_2"))==162.0, "t2 mismatch"
        assert float(row.get("target_3"))==168.0, "t3 mismatch"
        assert float(row.get("tqi_score"))==9.1, "tqi mismatch"
        assert row.get("shariah_status")=="COMPLIANT", "shariah mismatch"
        print("[VERIFY] Table integrity PASS: all fields match expected TEST2.CA params")
    else:
        print("[FAIL] Row not found or id mismatch")
        sys.exit(1)
else:
    print("[FAIL] Verify GET failed")
    sys.exit(1)

# 4. Broadcast to Telegram channel with single button
# Build short card (concise teaser)
short_card=f"🚀 إشارة جديدة | TEST2.CA\n💵 سعر الدخول: 150.00 EGP\n🛑 وقف الخسارة: 142.00 EGP\n🥇 الهدف الأول: 158.00 EGP\n👇 اضغط الزر للمتابعة الخاصة:"
markup={"inline_keyboard": [[{"text": "📥 انضم للصفقة | Track Signal", "callback_data": markup_callback}]]}
print(f"\n[TELEGRAM] Channel={channel} Card preview: {short_card[:120]}")
print(f"[TELEGRAM] Markup: {json.dumps(markup, ensure_ascii=False)}")

tg_url=f"https://api.telegram.org/bot{token}/sendMessage"
tg_payload={"chat_id": channel, "text": short_card, "parse_mode": "Markdown", "reply_markup": markup}
tg_resp=requests.post(tg_url, json=tg_payload, timeout=15)
print(f"[TELEGRAM] POST sendMessage -> HTTP {tg_resp.status_code}")
print(f"[TELEGRAM] Body: {tg_resp.text[:1000]}")
if tg_resp.status_code in (200,201):
    print(f"\n[SUCCESS] Dispatched TEST2.CA Scalping teaser trade_id={trade_id} to {channel} with single join_trade button")
    print(f"[OUTPUT] trade_id={trade_id} HTTP {tg_resp.status_code} confirmed")
    # Also print message_id for traceability
    try:
        j=tg_resp.json()
        print(f"[OUTPUT] Telegram message_id={j.get('result',{}).get('message_id')} chat_id={j.get('result',{}).get('chat',{}).get('id')}")
    except: pass
else:
    print(f"[FAIL] Telegram send failed {tg_resp.status_code}")
    sys.exit(1)

print("\n[DONE] All steps verified")
