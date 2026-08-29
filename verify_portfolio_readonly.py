#!/usr/bin/env python3
"""
Verify Portfolio Callbacks are READ-ONLY per task:
Confirm that clicking "Check Status" (portfolio_status:{ticker}) performs a READ-ONLY
query against trade_signals and user_portfolio and NEVER triggers any INSERT logic.
"""
import importlib.util, inspect, json, sys, io
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except:
        pass

def load_mod():
    spec=importlib.util.spec_from_file_location("webhook", r"D:\Egyptian Stock Exchange\api\webhook.py")
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

mod=load_mod()

# 1. Static code inspection
print("=== Static Inspection of handle_portfolio_status ===")
source = inspect.getsource(mod.handle_portfolio_status)
print(source[:2000])
# Check for forbidden operations
forbidden = [
    ("POST to trade_signals", "trade_signals" in source and "requests.post" in source and "TRADE_SIGNALS_TABLE" in source),
    ("PATCH to trade_signals", "TRADE_SIGNALS_TABLE" in source and "requests.patch" in source),
    ("INSERT into user_portfolio", "_upsert_user_portfolio" in source),
    ("POST to user_portfolio", "USER_PORTFOLIO_TABLE" in source and "requests.post" in source),
]

# More precise: check if handle_portfolio_status source contains POST to Supabase tables
has_trade_post = False
has_user_post = False
# Extract only handle_portfolio_status body
lines = source.splitlines()
for line in lines:
    if "trade_signals" in line.lower() and ("post" in line.lower() or "patch" in line.lower()):
        has_trade_post = True
    if "user_portfolio" in line.lower() and ("post" in line.lower() or "patch" in line.lower()):
        has_user_post = True

print(f"\n[CHECK] handle_portfolio_status contains POST/PATCH to trade_signals: {has_trade_post} (expected False)")
print(f"[CHECK] handle_portfolio_status contains POST/PATCH to user_portfolio: {has_user_post} (expected False)")
if has_trade_post or has_user_post:
    print("[FAIL] handle_portfolio_status is NOT read-only - contains write logic")
    sys.exit(1)
else:
    print("[PASS] handle_portfolio_status is READ-ONLY - no INSERT/PATCH to trade_signals or user_portfolio")

# 2. Dynamic mock verification: simulate portfolio_status callback and ensure no INSERT calls
print("\n=== Dynamic Mock Verification (no INSERT on portfolio_status) ===")
import unittest.mock as mock, os, json

class FakeResp:
    def __init__(self, status_code=200, body=None, text_override=None):
        self.status_code=status_code
        self._body=body
        if text_override is not None:
            self.text=text_override
        else:
            try:
                self.text=json.dumps(body) if body is not None else ""
            except:
                self.text=str(body) if body is not None else ""
    def json(self):
        return self._body

class TrackingRouter:
    def __init__(self):
        self.calls=[]
        self.exceptions=type('obj',(),{'RequestException': Exception})()
        self._get_map={}
        self._post_map={}
    def on_get(self, frag, resp):
        self._get_map[frag]=resp
    def on_post(self, frag, resp):
        self._post_map[frag]=resp
    def _match(self, url, mp):
        for frag, resp in mp.items():
            if frag in url:
                return resp
        return None
    def get(self, url, **kw):
        self.calls.append(("GET", url, kw.get("headers")))
        m=self._match(url, self._get_map)
        if m:
            return m
        if "user_portfolio" in url:
            return FakeResp(200, [{"user_id":"123","symbol":"TEST3.CA","trade_id":7,"status":"TRACKING","joined_at":"2026-08-29","entry_price":200}])
        if "trade_signals" in url:
            return FakeResp(200, [{"id":7,"ticker":"TEST3.CA","entry_price":200,"stop_loss":191,"target_1":208,"target_2":215,"target_3":224,"tqi_score":9.4,"shariah_status":"COMPLIANT","strategy_type":"Scalp"}])
        return FakeResp(200, [])
    def post(self, url, **kw):
        payload=kw.get("json")
        self.calls.append(("POST", url, payload))
        m=self._match(url, self._post_map)
        if m:
            return m
        if "answerCallbackQuery" in url or "sendMessage" in url:
            return FakeResp(200, {"ok":True})
        # Any POST to Supabase tables is forbidden for this test
        if "trade_signals" in url or "user_portfolio" in url:
            print(f"[UNEXPECTED POST] to Supabase: {url} payload={payload}")
            return FakeResp(500, {"error":"should not happen"}, text_override="unexpected INSERT")
        return FakeResp(200, {})

router=TrackingRouter()
env={"TELEGRAM_BOT_TOKEN":"fake","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key"}
query={"id":"cb-123","from":{"id":123}}
data="portfolio_status:TEST3.CA"
with mock.patch.dict(os.environ, env):
    with mock.patch.object(mod, "requests", router):
        handled, detail = mod.handle_portfolio_status(query, data, "fake", env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])

print(f"handle_portfolio_status returned handled={handled} detail={detail}")
# Analyze calls
gets=[c for c in router.calls if c[0]=="GET"]
posts=[c for c in router.calls if c[0]=="POST"]
print(f"\nGET calls ({len(gets)}):")
for c in gets:
    print(f"  GET {c[1][:120]}")
print(f"\nPOST calls ({len(posts)}):")
for c in posts:
    print(f"  POST {c[1][:120]} payload_keys={list(c[2].keys()) if isinstance(c[2], dict) else c[2]}")

# Verify no POST to Supabase tables
supabase_posts=[c for c in posts if "supabase.co" in c[1] and ("trade_signals" in c[1] or "user_portfolio" in c[1])]
telegram_posts=[c for c in posts if "api.telegram.org" in c[1]]

print(f"\n[CHECK] Supabase POST/PATCH to trade_signals/user_portfolio: {len(supabase_posts)} (expected 0)")
print(f"[CHECK] Telegram POST (sendMessage/answerCallbackQuery): {len(telegram_posts)} (expected 2)")

if len(supabase_posts)==0:
    print("[PASS] portfolio_status is READ-ONLY - no INSERT to trade_signals/user_portfolio")
else:
    print(f"[FAIL] portfolio_status triggered {len(supabase_posts)} Supabase writes - violates READ-ONLY requirement")
    for c in supabase_posts:
        print(c)
    sys.exit(1)

if len(telegram_posts)>=1:
    print("[PASS] portfolio_status correctly sends Telegram DM / answerCallbackQuery (read-only + notify)")
else:
    print("[FAIL] Expected Telegram posts not found")
    sys.exit(1)

print("\n=== All portfolio_status READ-ONLY checks PASSED ===")
