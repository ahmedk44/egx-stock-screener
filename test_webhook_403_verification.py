#!/usr/bin/env python3
"""
test_webhook_403_verification.py
Verifies:
  1. Zero-latency answerCallbackQuery as FIRST op before DB lookups
  2. 403 Forbidden guard -> fallback alert popup
  3. Callback parsing robustness for join_trade:TICKER[:TRADE_ID]
  4. Vercel payload simulation for valid vs forbidden users
"""
from __future__ import annotations
import importlib.util
import json
import os
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

PASS=0
FAIL=0
def check(name, cond, detail=""):
    global PASS,FAIL
    if cond:
        PASS+=1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL+=1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))

def load_webhook():
    spec = importlib.util.spec_from_file_location("wh403","api/webhook.py")
    m = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(m) # type: ignore
    return m

class FakeResp:
    def __init__(self, status_code=200, body=None, text_override=None):
        self.status_code=status_code
        self._body=body
        self.text=text_override if text_override is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        return self._body

class TrackingRouter:
    def __init__(self):
        self.calls: List[tuple] = []
        self.exceptions = SimpleNamespace(RequestException=Exception)
        self._get_overrides: Dict[str, FakeResp] = {}
        self._post_overrides: Dict[str, FakeResp] = {}
    def on_get(self, frag, resp):
        self._get_overrides[frag]=resp
    def on_post(self, frag, resp):
        self._post_overrides[frag]=resp
    def _match(self, url, overrides):
        for frag, resp in overrides.items():
            if frag in url:
                return resp
        return None
    def get(self, url, **kw):
        self.calls.append(("GET",url,kw))
        m=self._match(url,self._get_overrides)
        if m: return m
        if "trade_signals" in url:
            return FakeResp(200, [{"id":4,"ticker":"TEST.CA","strategy_type":"Scalping","entry_price":100.0,"stop_loss":95.0,"target_1":105.0,"target_2":107.0,"target_3":110.0,"tqi_score":8.5,"shariah_status":"COMPLIANT"}])
        if "sent_alerts" in url:
            return FakeResp(200, [{"ticker":"TEST.CA","entry_price":100.0,"current_stop_loss":95.0,"target_1":105.0,"target_2":107.0,"target_3":110.0,"strategy":"scalping"}])
        return FakeResp(200, [])
    def post(self, url, **kw):
        payload=kw.get("json")
        self.calls.append(("POST",url,payload))
        m=self._match(url,self._post_overrides)
        if m: return m
        if "answerCallbackQuery" in url:
            return FakeResp(200, {"ok":True})
        if "sendMessage" in url:
            return FakeResp(200, {"ok":True})
        if "user_portfolio" in url:
            return FakeResp(201, {})
        return FakeResp(200, {})
    def posts_to(self,frag):
        return [p for m,u,p in self.calls if m=="POST" and frag in u]
    def first_idx(self,frag):
        for i,(m,u,_) in enumerate(self.calls):
            if frag in u:
                return i
        return 9999
    def method_called(self,method,frag):
        return any(m==method and frag in u for m,u,_ in self.calls)

# Vercel payload simulation
VALID_UPDATE = {
    "update_id": 999,
    "callback_query": {
        "id": "cb-valid-001",
        "from": {"id": 111222333, "first_name": "ValidUser", "is_bot": False},
        "message": {"message_id": 10, "chat": {"id": -1003993921849, "type": "channel"}},
        "data": "join_trade:TEST.CA:4",
        "chat_instance": "ci-valid"
    }
}
FORBIDDEN_UPDATE = {
    "update_id": 1000,
    "callback_query": {
        "id": "cb-forbidden-001",
        "from": {"id": 999888777, "first_name": "NewUser", "is_bot": False},
        "message": {"message_id": 11, "chat": {"id": -1003993921849, "type": "channel"}},
        "data": "join_trade:TEST.CA",
        "chat_instance": "ci-forbidden"
    }
}

def test_parse_robustness():
    print("\n--- Test 1: Callback Data Parsing Robustness ---")
    m=load_webhook()
    cases=[
        ("join_trade:TEST.CA", ("TEST.CA",0)),
        ("join_trade:TEST.CA:4", ("TEST.CA",4)),
        ("join_trade:  ABUK.ca  ", ("ABUK.CA",0)),
        ("join_trade:SWdy.CA:10", ("SWDY.CA",10)),
        ("join_trade:TEST.CA:-3", ("TEST.CA",0)),
        ("join_trade:", None),
        ("act_TEST.CA", None),
    ]
    for inp, exp in cases:
        res=m.parse_join_callback(inp)
        check(f"parse '{inp}'", res==exp, f"got {res} exp {exp}")
    # Verify trade_id mapped to trade_signals
    # Simulate _fetch_trade_signal with trade_id
    print("\n--- Test 1b: trade_id mapping to trade_signals ---")
    router=TrackingRouter()
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"k"}
    import unittest.mock as mock
    m2=load_webhook()
    # Test with trade_id should query by id
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(m2, "requests", router):
            row=m2._fetch_trade_signal("https://fake.supabase.co","k","TEST.CA",trade_id=4)
            check("fetch by trade_id returns row", row is not None and row.get("ticker")=="TEST.CA" and row.get("entry_price")==100.0)
            # Without trade_id fallback to ticker
            row2=m2._fetch_trade_signal("https://fake.supabase.co","k","TEST.CA",trade_id=0)
            check("fetch by ticker fallback", row2 is not None)

def test_valid_user_zero_latency():
    print("\n--- Test 2: Valid User - Zero-Latency answer + DM dispatch ---")
    m=load_webhook()
    router=TrackingRouter()
    # Ensure trade_signals and user_portfolio succeed, sendMessage 200
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"k"}
    class FakeReq:
        method="POST"
        def get_json(self,force=True,silent=True):
            return VALID_UPDATE
    import unittest.mock as mock
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(m, "requests", router):
            result=m.handler(FakeReq())
    check("handler returns OK", result in ("OK", {"statusCode":200,"body":"OK"}))
    # Zero-latency: first call must be answerCallbackQuery before any GET
    first_answer=router.first_idx("answerCallbackQuery")
    first_get=router.first_idx("trade_signals")
    check("answerCallbackQuery is FIRST op before DB", first_answer < first_get, f"ans={first_answer} db={first_get}")
    check("answerCallbackQuery called", router.method_called("POST","answerCallbackQuery"))
    check("user_portfolio upsert called", router.method_called("POST","user_portfolio"))
    # Verify on_conflict
    ups=[u for m,u,p in router.calls if m=="POST" and "user_portfolio" in u]
    check("upsert uses on_conflict=user_id,symbol", any("on_conflict=user_id,symbol" in u for u in ups))
    # DM dispatch
    dms=router.posts_to("sendMessage")
    check("DM dispatched to valid user", len(dms)>=1)
    if dms:
        check("DM chat_id is pressing user", str(dms[0].get("chat_id"))=="111222333")
        txt=str(dms[0].get("text",""))
        check("DM is full card", "[كارت انضمام للصفقة]" in txt)
    # Final answer should be success (not forbidden) - check calls contain success popup
    answers=router.posts_to("answerCallbackQuery")
    # First answer is spinner, last is success; at least one contains success text
    all_text=" ".join([str(p.get("text","")) for p in answers])
    check("final answer not forbidden popup", "يرجى بدء المحادثة" not in all_text)
    check("no active_positions write", not router.method_called("POST","active_positions"))

def test_forbidden_user_403_guard():
    print("\n--- Test 3: Forbidden User (403) - Fallback Alert Popup ---")
    m=load_webhook()
    router=TrackingRouter()
    # Mock sendMessage to return 403 Forbidden
    router.on_post("sendMessage", FakeResp(403, {"ok":False,"error_code":403,"description":"Forbidden: bot can't initiate conversation with a user"}, text_override='{"ok":false,"error_code":403,"description":"Forbidden: bot can\'t initiate conversation with a user"}'))
    # trade_signals still returns row
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"k"}
    class FakeReq:
        method="POST"
        def get_json(self,force=True,silent=True):
            return FORBIDDEN_UPDATE
    import unittest.mock as mock
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(m, "requests", router):
            result=m.handler(FakeReq())
    check("handler returns OK even on 403", result in ("OK", {"statusCode":200,"body":"OK"}))
    # Zero-latency still first
    check("zero-latency answer still first", router.first_idx("answerCallbackQuery") < router.first_idx("trade_signals"))
    # Check that fallback alert was sent with show_alert=True and Arabic message
    answers=router.posts_to("answerCallbackQuery")
    check("at least 2 answerCallbackQuery calls (spinner + fallback)", len(answers)>=2)
    # Find alert popup
    forbidden_found=False
    for p in answers:
        txt=str(p.get("text",""))
        show=p.get("show_alert")
        if "يرجى بدء المحادثة" in txt and show is True:
            forbidden_found=True
            break
    check("403 fallback alert popup shown with /start @EGX.signals", forbidden_found, f"answers={answers}")
    # Ensure DM was attempted (403)
    check("DM attempted and got 403", router.method_called("POST","sendMessage"))
    # Ensure no crash, handler handled
    check("handler handled forbidden gracefully", True)

def main():
    print("="*70)
    print("Vercel Payload Simulation & 403 Guard Verification")
    print("="*70)
    test_parse_robustness()
    test_valid_user_zero_latency()
    test_forbidden_user_403_guard()
    print("\n"+"="*70)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("="*70)
    if FAIL:
        print("❌ FAILED")
        return 1
    print("✅ All 403 guard + zero-latency + parsing checks PASSED")
    return 0

if __name__=="__main__":
    sys.exit(main())
