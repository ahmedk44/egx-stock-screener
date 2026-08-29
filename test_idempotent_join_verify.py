#!/usr/bin/env python3
"""
test_idempotent_join_verify.py
Verifies Idempotent Join Check + Refined DM Card per spec:

1. First click (user never joined):
   - GET public.user_portfolio -> empty (not exists)
   - POST user_portfolio upsert -> 201 (1 DB write)
   - answerCallbackQuery with "✅ تم تسجيل الصفقة بنجاح! راجع المحادثة الخاصة."
   - sendMessage DM with full card + keyboard

2. Second click (duplicate):
   - GET public.user_portfolio -> returns existing row (already exists)
   - 0 POST to user_portfolio (0 DB writes)
   - answerCallbackQuery popup show_alert=True with duplicate Arabic text
   - 0 sendMessage DM

3. DM card layout + buttons contract

Run: py test_idempotent_join_verify.py
Exit 0 = pass, 1 = fail
"""
import importlib.util
import json
import sys
import io
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except:
        pass
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

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

def load_mod():
    spec=importlib.util.spec_from_file_location("webhook_idem","api/webhook.py")
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
            try:
                self.text=json.dumps(body) if body is not None else ""
            except:
                self.text=str(body) if body is not None else ""
    def json(self):
        return self._body

class TrackingRouter:
    def __init__(self):
        self.calls=[] # (method, url, json)
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
            # default for existence check: empty -> not exists
            return FakeResp(200, [])
        if "trade_signals" in url:
            return FakeResp(200, [{"trade_id":42,"symbol":"COMI.CA","ticker_bare":"COMI","entry_price":42.5,"stop_loss":39.9,"current_stop_loss":39.9,"target_1":44.63,"target_2":46.75,"target_3":49.73,"strategy":"scalp","strategy_type":"Scalp","tqi_score":8.5,"shariah_status":"COMPLIANT","company_name":"Commercial International Bank"}])
        return FakeResp(200, [])
    def post(self, url, **kw):
        payload=kw.get("json")
        self.calls.append(("POST", url, payload))
        m=self._match(url, self._post_map)
        if m:
            return m
        if "answerCallbackQuery" in url:
            return FakeResp(200, {"ok":True})
        if "sendMessage" in url:
            return FakeResp(200, {"ok":True})
        if "user_portfolio" in url:
            return FakeResp(201, {})
        return FakeResp(200, {})

DUPLICATE_ALERT="ℹ️ أنت تتابع هذه الصفقة بالفعل في محفظتك! يمكنك متابعة تحديثاتها عبر المحادثة الخاصة مع البوت."
SUCCESS_ALERT="✅ تم تسجيل الصفقة بنجاح! راجع المحادثة الخاصة."

def test_first_click():
    print("\n--- Test 1: First click -> DB write + DM + success popup ---")
    mod=load_mod()
    router=TrackingRouter()
    # Explicit: GET existence check returns empty, trade_signals returns row, POST upsert 201
    router.on_get("user_portfolio", FakeResp(200, []))  # not exists
    # ensure post to user_portfolio succeeds
    router.on_post("user_portfolio", FakeResp(201, {}))
    import unittest.mock as mock, os
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key"}
    query={"id":"cb-001","from":{"id":987654}}
    data="join_trade:COMI.CA:42"
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router):
            handled, detail = mod.handle_join_trade(query, data, "fake_tok", env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    check("handle returns handled True", handled is True, detail)
    # Verify existence check GET was made
    exists_gets=[c for c in router.calls if c[0]=="GET" and "user_portfolio" in c[1] and "user_id=eq.987654" in c[1]]
    check("existence check GET for (user_id, symbol) was called", len(exists_gets)>=1, str(exists_gets[0][1] if exists_gets else "none"))
    check("symbol in existence check is normalized COMI.CA", any("COMI.CA" in c[1] for c in exists_gets))
    # Verify 1 DB write POST
    writes=[c for c in router.calls if c[0]=="POST" and "user_portfolio" in c[1]]
    check("exactly 1 DB write POST to user_portfolio on first click", len(writes)==1, f"writes={len(writes)}")
    if writes:
        url,payload=writes[0][1], writes[0][2]
        check("on_conflict param present", "on_conflict=user_id,symbol" in url, url)
        check("payload user_id correct", str(payload.get("user_id"))=="987654")
        check("payload symbol normalized COMI.CA", payload.get("symbol")=="COMI.CA")
    # Verify DM sent
    dms=[c for c in router.calls if c[0]=="POST" and "sendMessage" in c[1]]
    check("private DM sendMessage called (1 DM)", len(dms)==1, f"dms={len(dms)}")
    if dms:
        dm_payload=dms[0][2]
        check("DM chat_id is user private chat", str(dm_payload.get("chat_id"))=="987654")
        text=str(dm_payload.get("text") or "")
        check("DM card header present", "🟢" in text and "[كارت انضمام للصفقة]" in text)
        check("DM ticker present", "COMI" in text)
        check("DM contains entry 42.50", "42.50" in text)
        check("DM contains SL 39.90", "39.90" in text)
        check("DM TQI 8.5/10", "8.5" in text and "تقييم الجودة" in text)
        check("DM shariah line", "التوافق الشرعي" in text)
        # Buttons
        rm=dm_payload.get("reply_markup") or {}
        check("DM has inline_keyboard", "inline_keyboard" in rm)
        if "inline_keyboard" in rm:
            kb_str=json.dumps(rm, ensure_ascii=False)
            check("Button portfolio_status:COMI", "portfolio_status:COMI" in kb_str)
            check("Button leave_trade:COMI:42", "leave_trade:COMI:42" in kb_str)
            check("Button text Check Status", "حالة الصفقة" in kb_str)
            check("Button text Exit Trade exact", "خروج من الصفقة" in kb_str)
    # Verify success popup
    callbacks=[c for c in router.calls if c[0]=="POST" and "answerCallbackQuery" in c[1]]
    # Filter for success text (last callback after immediate)
    cb_texts=[str(c[2].get("text") or "") for c in callbacks]
    check("answerCallbackQuery called at least 2 times (immediate + success)", len(callbacks)>=2, str(cb_texts))
    check(f"success popup text '{SUCCESS_ALERT}' present", any(SUCCESS_ALERT in t for t in cb_texts), str(cb_texts))
    # Duplicate popup should NOT be present on first click
    check("duplicate popup NOT present on first click", not any(DUPLICATE_ALERT in t for t in cb_texts))
    # Verify show_alert False for success (inspect payload)
    success_cbs=[c for c in callbacks if SUCCESS_ALERT in str(c[2].get("text") or "")]
    if success_cbs:
        check("success popup show_alert is False (toast)", success_cbs[0][2].get("show_alert") is False)

def test_second_click_duplicate():
    print("\n--- Test 2: Second click (duplicate) -> 0 DB writes, popup alert, 0 DM ---")
    mod=load_mod()
    router=TrackingRouter()
    # Mock existence check to return existing row (duplicate)
    router.on_get("user_portfolio", FakeResp(200, [{"user_id":"987654","symbol":"COMI.CA","trade_id":42,"status":"TRACKING"}]))
    # Even if POST somehow called, would be 409 but we assert 0 writes
    import unittest.mock as mock, os
    env={"TELEGRAM_BOT_TOKEN":"tok","SUPABASE_URL":"https://fake.supabase.co","SUPABASE_SERVICE_ROLE_KEY":"fake_key"}
    query={"id":"cb-002","from":{"id":987654}}
    data="join_trade:COMI.CA:42"
    with mock.patch.dict(os.environ, env):
        with mock.patch.object(mod, "requests", router):
            handled, detail = mod.handle_join_trade(query, data, "fake_tok", env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])
    check("handle returns handled True on duplicate", handled is True, detail)
    # Verify existence check GET
    exists_gets=[c for c in router.calls if c[0]=="GET" and "user_portfolio" in c[1]]
    check("existence check GET called for duplicate", len(exists_gets)>=1)
    # Verify 0 POST to user_portfolio (critical requirement: 0 DB writes)
    writes=[c for c in router.calls if c[0]=="POST" and "user_portfolio" in c[1]]
    check("0 DB writes on second click (duplicate blocked)", len(writes)==0, f"writes={len(writes)} actual calls={router.calls}")
    # Verify 0 DM
    dms=[c for c in router.calls if c[0]=="POST" and "sendMessage" in c[1]]
    check("0 DM re-sent on duplicate (Do NOT re-send)", len(dms)==0, f"dms={len(dms)}")
    # Verify duplicate popup with show_alert=True
    callbacks=[c for c in router.calls if c[0]=="POST" and "answerCallbackQuery" in c[1]]
    cb_texts=[str(c[2].get("text") or "") for c in callbacks]
    check("answerCallbackQuery called on duplicate", len(callbacks)>=2)
    check(f"duplicate popup text present", any(DUPLICATE_ALERT in t for t in cb_texts), str(cb_texts))
    dup_cbs=[c for c in callbacks if DUPLICATE_ALERT in str(c[2].get("text") or "")]
    if dup_cbs:
        check("duplicate popup show_alert=True (alert popup)", dup_cbs[0][2].get("show_alert") is True, str(dup_cbs[0][2]))
    else:
        check("duplicate popup found", False)

def test_dm_card_layout():
    print("\n--- Test 3: DM Card Layout & Buttons Contract ---")
    mod=load_mod()
    # Strategy mappings
    for strat, emoji in [("Scalp","⚡"),("scalping","⚡"),("Swing","📈"),("swing","📈"),("Investment","🏛️"),("investment","🏛️")]:
        card=mod.build_full_dm_card("COMI.CA", {"ticker":"COMI.CA","strategy_type":strat,"entry_price":10,"stop_loss":9,"target_1":11,"target_2":12,"target_3":13,"tqi_score":7.5,"shariah_status":"COMPLIANT"})
        ok = emoji in card
        check(f"strategy {strat} maps to {emoji}", ok, card[:120])
        check(f"card header present for {strat}", "🟢" in card and "[كارت انضمام للصفقة]" in card)
        check(f"card ticker line", "السهم" in card and "COMI" in card)
        check(f"card TQI line", "تقييم الجودة" in card)
        check(f"card shariah line", "التوافق الشرعي" in card)
        check(f"card entry/SL/targets", "الدخول" in card and "وقف الخسارة" in card and "الهدف الأول" in card)
        if not ok:
            print(card[:500])
    # Keyboard exact contract
    kb=mod.build_dm_inline_keyboard("SWDY.CA", 99)
    check("keyboard has inline_keyboard", "inline_keyboard" in kb)
    s=json.dumps(kb, ensure_ascii=False)
    check("keyboard portfolio_status:SWDY", "portfolio_status:SWDY" in s)
    check("keyboard leave_trade:SWDY:99", "leave_trade:SWDY:99" in s)
    check("keyboard button Check Status text", "حالة الصفقة | Check Status" in s)
    check("keyboard button Exit Trade text exact per spec", "خروج من الصفقة | Exit Trade" in s and "خروج / إغلاق" not in s)
    # Bare vs .CA handling
    kb2=mod.build_dm_inline_keyboard("HELI.CA", 0)
    s2=json.dumps(kb2, ensure_ascii=False)
    check("keyboard strips .CA for callback", "HELI" in s2 and "HELI.CA" not in s2)

def main():
    print("="*70)
    print("Idempotent Join Check Verification (Spec Requirements)")
    print("="*70)
    try:
        test_first_click()
        test_second_click_duplicate()
        test_dm_card_layout()
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
    print("✅ All idempotent checks PASSED")
    return 0

if __name__=="__main__":
    import sys
    sys.exit(main())
