#!/usr/bin/env python3
"""
Test verification for DM inline button callbacks:
 - portfolio_status:{ticker} -> live summary with P&L, trailing SL, target progression
 - leave_trade:{ticker}:{trade_id} -> status CLOSED + notification
 - entry_price/joined_at_price recording on join
 - live updates propagation push_live_update_to_subscribers
"""
import importlib.util
import json
import sys
import os
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except: pass

PASS=0
FAIL=0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS+=1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL+=1
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))

def load_webhook():
    spec=importlib.util.spec_from_file_location("webhook_cb_test","api/webhook.py")
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class FakeResp:
    def __init__(self, status_code=200, body=None, text_override=None):
        self.status_code=status_code
        self._body=body
        if text_override is not None:
            self.text=text_override
        else:
            try: self.text=json.dumps(body) if body is not None else ""
            except: self.text=str(body) if body is not None else ""
    def json(self):
        return self._body

class TrackingRouter:
    def __init__(self):
        self.calls=[]
        self.exceptions=type('obj',(),{'RequestException': Exception})()
        self._get_map={}
        self._post_map={}
        self._patch_map={}
    def on_get(self, frag, resp):
        self._get_map[frag]=resp
    def on_post(self, frag, resp):
        self._post_map[frag]=resp
    def on_patch(self, frag, resp):
        self._patch_map[frag]=resp
    def _match(self, url, mp):
        for frag, resp in mp.items():
            if frag in url:
                return resp
        return None
    def get(self, url, **kw):
        self.calls.append(("GET", url, kw.get("headers")))
        m=self._match(url, self._get_map)
        if m: return m
        # default portfolio row or signal row
        if "user_portfolio" in url and "user_id=eq.111" in url:
            return FakeResp(200, [{"user_id":"111","symbol":"TEST2.CA","trade_id":5,"status":"TRACKING","joined_at":"2026-08-29T11:00:00Z","entry_price":150.0,"joined_at_price":150.0}])
        if "trade_signals" in url:
            return FakeResp(200, [{"id":5,"ticker":"TEST2.CA","strategy_type":"Scalping","entry_price":150.0,"stop_loss":142.0,"target_1":158.0,"target_2":162.0,"target_3":168.0,"tqi_score":9.1,"shariah_status":"COMPLIANT"}])
        if "user_portfolio" in url:
            return FakeResp(200, [])
        return FakeResp(200, [])
    def post(self, url, **kw):
        payload=kw.get("json")
        self.calls.append(("POST", url, payload))
        m=self._match(url, self._post_map)
        if m: return m
        if "answerCallbackQuery" in url:
            return FakeResp(200, {"ok":True})
        if "sendMessage" in url:
            return FakeResp(200, {"ok":True})
        if "user_portfolio" in url:
            return FakeResp(201, {})
        return FakeResp(200, {})
    def patch(self, url, **kw):
        payload=kw.get("json")
        self.calls.append(("PATCH", url, payload))
        m=self._match(url, self._patch_map)
        if m: return m
        # default success for leave_trade
        return FakeResp(200, [{"user_id":"111","symbol":"TEST2.CA","trade_id":5,"status":"CLOSED"}])
    def posts_to(self, frag):
        return [p for m,u,p in self.calls if m=="POST" and frag in u]
    def patches_to(self, frag):
        return [p for m,u,p in self.calls if m=="PATCH" and frag in u]
    def gets_to(self, frag):
        return [u for m,u,p in self.calls if m=="GET" and frag in u]
    def method_called(self, method, frag):
        return any(m==method and frag in u for m,u,p in self.calls)

def test_portfolio_status():
    print("\n--- Test A: portfolio_status live summary ---")
    mod=load_webhook()
    router=TrackingRouter()
    # Mock yfinance fetch to avoid network: patch _fetch_current_market_price to return 160.0
    # Router will handle GETs for portfolio and signal; we also need GET for market price not via supabase so mock separately
    import unittest.mock as mock
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key"}
    query={"id":"cb-ps-001","from":{"id":111}}
    data="portfolio_status:TEST2.CA"
    # Need to ensure portfolio fetch returns entry_price row
    # Default router.get already returns that for user 111
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router):
            # Mock market price to 160 to hit target 1 progression and compute P&L
            with mock.patch.object(mod, "_fetch_current_market_price", return_value=160.0):
                ok, detail = mod.handle_portfolio_status(query, data, "tok", env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    check("handle returns True", ok is True, detail)
    check("portfolio_status queried user_portfolio", any("user_portfolio" in u for u in router.gets_to("user_portfolio")))
    check("portfolio_status queried trade_signals", any("trade_signals" in u for u in router.gets_to("trade_signals")))
    # Check answerCallbackQuery called
    callbacks=[c for c in router.calls if c[0]=="POST" and "answerCallbackQuery" in c[1]]
    check("answerCallbackQuery called", len(callbacks)>=1)
    # Check DM sent with live summary card
    dms=[c for c in router.calls if c[0]=="POST" and "sendMessage" in c[1]]
    check("DM sendMessage with live summary sent", len(dms)>=1, f"dms={len(dms)}")
    if dms:
        payload=dms[0][2]
        text=str(payload.get("text") or "")
        check("card contains حالة الصفقة", "حالة الصفقة" in text)
        check("card contains user entry 150", "150.00" in text)
        check("card contains current price 160", "160.00" in text)
        # P&L: (160-150)/150=6.67%
        check("card contains P&L 6.67%", "6.67" in text)
        check("card contains trailing SL 142", "142.00" in text)
        check("card contains target progression (hit target1)", "الهدف الأول" in text)
        check("card contains تقييم الجودة", "تقييم الجودة" in text or "TQI" in text)
        # Ensure built with custom entry_price respected
        check("card mentions مخصص or رسمي entry", "سعر دخولك" in text)
    else:
        print("No DM to inspect")

def test_leave_trade():
    print("\n--- Test B: leave_trade close and notify ---")
    mod=load_webhook()
    router=TrackingRouter()
    router.on_patch("user_portfolio", FakeResp(200, [{"status":"CLOSED"}]))
    import unittest.mock as mock
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key"}
    query={"id":"cb-lv-001","from":{"id":222}}
    data="leave_trade:TEST2.CA:5"
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router):
            ok, detail = mod.handle_leave_trade(query, data, "tok", env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    check("handle returns True", ok is True, detail)
    check("PATCH to user_portfolio called", router.method_called("PATCH","user_portfolio"))
    # Verify PATCH payload status CLOSED or EXITED
    patches=router.patches_to("user_portfolio")
    if patches:
        # patches list contains payloads dicts
        payload_str=json.dumps(patches, ensure_ascii=False)
        check("PATCH status CLOSED or EXITED", "CLOSED" in payload_str or "EXITED" in payload_str, payload_str[:200])
    # Check notification text
    callbacks=[c for c in router.calls if c[0]=="POST" and "answerCallbackQuery" in c[1]]
    combined=json.dumps([c[2] for c in callbacks], ensure_ascii=False)
    check("notification contains closed Arabic text", "تم إغلاق الصفقة" in combined, combined[:300])
    # Check that answerCallbackQuery used show_alert=True for closed
    found_alert=False
    for _,url,payload in router.calls:
        if "answerCallbackQuery" in url and payload and "تم إغلاق الصفقة" in str(payload.get("text","")):
            if payload.get("show_alert") is True:
                found_alert=True
    check("closed popup show_alert=True", found_alert)
    # Also check confirmation DM
    dms=[c for c in router.calls if c[0]=="POST" and "sendMessage" in c[1]]
    # There should be at least one DM with closed text if PATCH succeeded
    dm_texts=json.dumps([c[2] for c in dms], ensure_ascii=False)
    check("confirmation DM sent", len(dms)>=1, f"dms={len(dms)}")
    if dms:
        check("DM contains closed text", "تم إغلاق الصفقة" in dm_texts)

def test_entry_price_recording():
    print("\n--- Test C: entry_price / joined_at_price recording on join ---")
    mod=load_webhook()
    router=TrackingRouter()
    # Simulate first join with entry_price; capture payload
    # Need GET existence check empty, GET trade signal returns row, then POST upsert should include entry_price
    router.on_get("user_portfolio", FakeResp(200, []))  # not exists for idempotent check
    # trade_signals GET will be default via router.get -> returns  TEST2.CA signal
    import unittest.mock as mock
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key"}
    query={"id":"cb-join-001","from":{"id":333}}
    data="join_trade:TEST2.CA:5"
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router):
            # Ensure _fetch_trade_signal returns expected
            with mock.patch.object(mod, "_fetch_trade_signal", return_value={"ticker":"TEST2.CA","strategy_type":"Scalping","entry_price":150.0,"stop_loss":142.0,"target_1":158.0,"target_2":162.0,"target_3":168.0,"tqi_score":9.1}):
                ok, detail = mod.handle_join_trade(query, data, "tok", env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    check("join handled True", ok is True, detail)
    # Find POST payload to user_portfolio
    writes=[c for c in router.calls if c[0]=="POST" and "user_portfolio" in c[1]]
    check("1 DB write on join", len(writes)==1, f"writes={len(writes)}")
    if writes:
        payload=writes[0][2]
        check("payload contains entry_price 150", payload.get("entry_price")==150.0, str(payload))
        check("payload contains joined_at_price 150", payload.get("joined_at_price")==150.0, str(payload))
        check("payload user_id correct", str(payload.get("user_id"))=="333")
    # Simulate status check uses custom entry_price for P&L
    # Now test portfolio_status uses that custom entry_price
    router2=TrackingRouter()
    # Mock _fetch_user_portfolio_row to return custom entry_price 155 (user joined at different price)
    import unittest.mock as mock
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router2):
            with mock.patch.object(mod, "_fetch_user_portfolio_row", return_value={"user_id":"333","symbol":"TEST2.CA","trade_id":5,"status":"TRACKING","entry_price":155.0,"joined_at_price":155.0}):
                with mock.patch.object(mod, "_fetch_trade_signal", return_value={"ticker":"TEST2.CA","strategy_type":"Scalping","entry_price":150.0,"stop_loss":142.0,"target_1":158.0,"target_2":162.0,"target_3":168.0,"tqi_score":9.1}):
                    with mock.patch.object(mod, "_fetch_current_market_price", return_value=160.0):
                        # Use build_portfolio_status_card directly to verify P&L based on 155 entry
                        card=mod.build_portfolio_status_card("TEST2.CA", {"entry_price":155.0,"joined_at_price":155.0,"status":"TRACKING"}, {"entry_price":150.0,"stop_loss":142.0,"target_1":158.0,"target_2":162.0,"target_3":168.0}, 160.0)
                        # P&L should be (160-155)/155=3.23% not 6.67%
                        check("P&L based on custom entry 155 -> 3.23%", "3.23" in card, card[:300])
                        check("card shows مخصص for custom entry", "مخصص" in card)

def test_live_updates_propagation():
    print("\n--- Test D: Live updates propagation to tracking users ---")
    mod=load_webhook()
    router=TrackingRouter()
    # Mock GET subscribers for trade_id 5 returns 2 users
    router.on_get("user_portfolio", FakeResp(200, [{"user_id":"111"},{"user_id":"222"}]))
    import unittest.mock as mock
    env={"SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key","TELEGRAM_BOT_TOKEN":"tok"}
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router):
            delivered = mod.push_live_update_to_subscribers(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"], 5, "TEST2.CA", "🔴 تحديث وقف الخسارة إلى 145.00", "tok")
    check("push delivered to 2 subscribers", delivered==2, f"delivered={delivered}")
    dms=[c for c in router.calls if c[0]=="POST" and "sendMessage" in c[1]]
    check("2 sendMessage calls", len(dms)==2, f"dms={len(dms)}")
    if dms:
        check("update text propagated", "وقف الخسارة" in json.dumps([p for _,_,p in dms], ensure_ascii=False))

def test_via_webhook_handler():
    print("\n--- Test E: Webhook handler dispatch for portfolio_status/leave_trade via _handler_impl ---")
    mod=load_webhook()
    router=TrackingRouter()
    # For portfolio_status via handler: need to go through _handler_impl
    import unittest.mock as mock, json as js
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key"}
    # Mock portfolio_status via handler
    update_ps={"update_id":1,"callback_query":{"id":"cb-ps-002","from":{"id":444},"data":"portfolio_status:TEST2.CA","chat_instance":"ci"}}
    class FakeReq:
        method="POST"
        def get_json(self, force=True, silent=True): return update_ps
        body=js.dumps(update_ps).encode()
    router.on_get("user_portfolio", FakeResp(200, [{"user_id":"444","symbol":"TEST2.CA","trade_id":5,"status":"TRACKING","entry_price":150.0}]))
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router):
            with mock.patch.object(mod, "_fetch_current_market_price", return_value=158.5):
                resp=mod._handler_impl(FakeReq())
    check("handler returns OK for portfolio_status", resp in ("OK", {"statusCode":200,"body":"OK"}), str(resp))
    check("handler issued answerCallbackQuery", router.method_called("POST","answerCallbackQuery"))
    # Leave via handler
    router2=TrackingRouter()
    update_lv={"update_id":2,"callback_query":{"id":"cb-lv-002","from":{"id":555},"data":"leave_trade:TEST2.CA:5","chat_instance":"ci"}}
    class FakeReq2:
        method="POST"
        def get_json(self, force=True, silent=True): return update_lv
        body=js.dumps(update_lv).encode()
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router2):
            resp2=mod._handler_impl(FakeReq2())
    check("handler returns OK for leave_trade", resp2 in ("OK", {"statusCode":200,"body":"OK"}), str(resp2))
    check("leave handler PATCH called", router2.method_called("PATCH","user_portfolio"))

def main():
    print("="*70)
    print("DM Callbacks Verification: portfolio_status / leave_trade / entry_price / push updates")
    print("="*70)
    try:
        test_portfolio_status()
        test_leave_trade()
        test_entry_price_recording()
        test_live_updates_propagation()
        test_via_webhook_handler()
    except Exception as e:
        print(f"[FATAL] {e}")
        import traceback; traceback.print_exc()
        return 1
    print("\n"+"="*70)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("="*70)
    if FAIL:
        print("❌ FAILED")
        return 1
    print("✅ All DM callbacks PASSED")
    return 0

if __name__=="__main__":
    import sys
    sys.exit(main())
