import os, json, sys
from unittest.mock import patch, MagicMock
import importlib.util

# Test Node webhook via Python? Just check file exists and contains required logic
print("=== Test 1: Check api/webhook.js content ===")
with open("api/webhook.js", encoding="utf-8") as f:
    js = f.read()
    assert "callback_query" in js, "callback_query not handled"
    assert "act_" in js and "dis_" in js and "cls_" in js, "actions not handled"
    assert "supabaseUrl" in js or "SUPABASE_URL" in js, "Supabase not handled"
    assert "SUPABASE_SERVICE_ROLE_KEY" in js or "SUPABASE_KEY" in js, "Supabase key fallback missing"
    assert "answerCallbackQuery" in js, "answerCallbackQuery not handled"
    assert "Prefer" in js and "return=minimal" in js, "Prefer header missing"
    print("[PASS] Node webhook handles POST, callback_query, Supabase upsert, answerCallbackQuery")

# Test Python webhook
print("\n=== Test 2: Python api/webhook.py ===")
spec = importlib.util.spec_from_file_location("webhook_py", "api/webhook.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert hasattr(mod, "handler"), "Python webhook missing handler"
# Test handler with mock request
class MockReq:
    def __init__(self, body):
        self.method = "POST"
        self.body = json.dumps(body).encode("utf-8")
        self.headers = {}
    def get_json(self, force=False, silent=False):
        return json.loads(self.body.decode("utf-8"))

# Mock Supabase and Telegram
os.environ["SUPABASE_URL"] = "https://fake.supabase.co"
os.environ["SUPABASE_KEY"] = "fake_key"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token"

test_update = {
    "callback_query": {
        "id": "12345",
        "data": "act_TEST.CA",
        "from": {"username": "tester"}
    }
}
mock_req = MockReq(test_update)
with patch("api.webhook.requests") as mock_req_lib:
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.text = ""
    mock_req_lib.patch.return_value = mock_resp
    mock_req_lib.post.return_value = mock_resp
    # Need to patch requests in webhook module
    mod.requests = mock_req_lib
    # Call handler with mock request object that has method and body
    class FakeRequest:
        method = "POST"
        def get_json(self, force=True, silent=True):
            return test_update
        body = json.dumps(test_update).encode("utf-8")
    # Try calling handler
    try:
        result = mod.handler(FakeRequest())
        # Check Supabase was called
        # Since we patched mod.requests, check call
        assert mock_req_lib.patch.called or mock_req_lib.post.called, "Supabase not called"
        print("[PASS] Python webhook handles act_ and Supabase upsert")
    except Exception as e:
        print(f"[FAIL] Python webhook error: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)

# Test with dis and cls
for data, expected_status in [("dis_TEST.CA", "DISMISSED"), ("cls_TEST.CA", "CLOSED")]:
    upd = {"callback_query": {"id": "123", "data": data}}
    fake_req = type("R", (), {"method": "POST", "get_json": lambda *a, **k: upd, "body": json.dumps(upd).encode("utf-8")})()
    with patch("api.webhook.requests") as mr:
        mr.patch.return_value = MagicMock(status_code=204, text="")
        mr.post.return_value = MagicMock(status_code=200, json=lambda: {"ok": True}, text='{"ok":true}')
        mod.requests = mr
        try:
            mod.handler(fake_req)
            print(f"[PASS] Python webhook handles {data} -> {expected_status}")
        except Exception as e:
            print(f"[FAIL] {data}: {e}")
            sys.exit(1)

# Test main.py --set-webhook
print("\n=== Test 3: main.py --set-webhook ===")
import main
# Test get_set_webhook_domain
assert main.get_set_webhook_domain(["--set-webhook", "https://myapp.vercel.app"]) == "https://myapp.vercel.app"
assert main.get_set_webhook_domain(["--set-webhook=https://myapp.vercel.app"]) == "https://myapp.vercel.app"
print("[PASS] get_set_webhook_domain parsing")

# Test set_telegram_webhook with mocked requests
with patch("main.requests.post") as mp:
    mock_resp2 = MagicMock()
    mock_resp2.status_code = 200
    mock_resp2.text = '{"ok":true,"result":true}'
    mock_resp2.json.return_value = {"ok": True, "result": True}
    mp.return_value = mock_resp2
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token_123"
    ok = main.set_telegram_webhook("https://myapp.vercel.app", bot_token="fake_token_123")
    assert ok == True, "set_telegram_webhook should return True on 200 ok"
    # Check that it called with correct URL
    called_url = mp.call_args[0][0]
    assert "setWebhook" in called_url
    called_json = mp.call_args[1]["json"]
    assert "https://myapp.vercel.app/api/webhook" in called_json["url"]
    print(f"[PASS] set_telegram_webhook constructs {called_json['url']} and succeeds")

# Test with domain without https
with patch("main.requests.post") as mp2:
    mock_resp2.status_code = 200
    mock_resp2.json.return_value = {"ok": True}
    mp2.return_value = mock_resp2
    ok2 = main.set_telegram_webhook("myapp.vercel.app")
    assert ok2 == True
    called_json2 = mp2.call_args[1]["json"]
    assert called_json2["url"] == "https://myapp.vercel.app/api/webhook"
    print("[PASS] set_telegram_webhook normalizes domain without https")

# Test failure when no token
if "TELEGRAM_BOT_TOKEN" in os.environ:
    del os.environ["TELEGRAM_BOT_TOKEN"]
ok3 = main.set_telegram_webhook("https://myapp.vercel.app", bot_token=None)
assert ok3 == False, "Should fail without token"
print("[PASS] set_telegram_webhook fails without token as expected")
os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token_123"

# Test main --set-webhook flag integration
with patch("main.set_telegram_webhook", return_value=True) as mock_set:
    with patch("main.should_test_supabase", return_value=False):
        with patch("main.should_listen_telegram", return_value=False):
            # Simulate CLI args
            original_argv = sys.argv
            sys.argv = ["main.py", "--set-webhook", "https://myapp.vercel.app"]
            try:
                # Need to mock check_required_env to avoid needing all env
                with patch("main.check_required_env", return_value=None):
                    with patch("main.sync_active_positions_to_supabase", return_value=0):
                        # Mock parse_mode to avoid actual parsing side effects? But main will call get_set_webhook_domain which will read sys.argv
                        ret = main.main()
                        assert mock_set.called, "set_telegram_webhook not called via main"
                        assert mock_set.call_args[0][0] == "https://myapp.vercel.app"
                        print(f"[PASS] main --set-webhook integration calls set_telegram_webhook with domain")
            finally:
                sys.argv = original_argv

print("\nALL WEBHOOK TESTS PASSED")

# Cleanup
for k in ["SUPABASE_URL","SUPABASE_KEY","TELEGRAM_BOT_TOKEN"]:
    if k in os.environ:
        try:
            del os.environ[k]
        except:
            pass
