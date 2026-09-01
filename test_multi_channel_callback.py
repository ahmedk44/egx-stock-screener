"""
test_multi_channel_callback.py
==============================
Tests for:
  1. Dynamic Channel Routing  - strategy_type -> TELEGRAM_CHANNEL_* env vars.
  2. Unified SHORT public-channel card + [ 📥 انضم للصفقة | Track Signal ] button,
     and guarantees that full detail / close cards never target public channels.
  3. Vercel Webhook CallbackQuery handling of join_trade (user_portfolio +
     private FULL card DM) without needing a local `listen` process.

Run: python test_multi_channel_callback.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest.mock as mock
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test_token")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini")

PASS = 0
FAIL = 0


def check(name: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


# --------------------------------------------------------------------------
# Load modules under test
# --------------------------------------------------------------------------

import main as screener  # noqa: E402


def load_webhook_module():
    spec = importlib.util.spec_from_file_location("webhook_py_test", "api/webhook.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FakeResp:
    def __init__(self, status_code: int = 200, body: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self) -> Any:
        return self._body


class Router:
    """Routes fake HTTP calls by URL fragment -> FakeResp."""

    def __init__(self) -> None:
        self.routes: List[tuple] = []  # (fragment, FakeResp)
        self.calls: List[tuple] = []  # (method, url, payload)
        # Mirrors requests.exceptions.* used by except clauses in webhook.py.
        self.exceptions = SimpleNamespace(RequestException=Exception)

    def on(self, fragment: str, resp: FakeResp) -> None:
        self.routes.append((fragment, resp))

    def _match(self, url: str) -> Optional[FakeResp]:
        for frag, resp in self.routes:
            if frag in url:
                return resp
        return None

    def get(self, url: str, **kw: Any) -> FakeResp:
        self.calls.append(("GET", url, kw))
        resp = self._match(url)
        return resp or FakeResp(200, [])

    def post(self, url: str, **kw: Any) -> FakeResp:
        payload = kw.get("json")
        self.calls.append(("POST", url, payload))
        resp = self._match(url)
        return resp or FakeResp(200, {})

    def patch(self, url: str, **kw: Any) -> FakeResp:
        payload = kw.get("json")
        self.calls.append(("PATCH", url, payload))
        resp = self._match(url)
        return resp or FakeResp(200, [])

    # --- helpers -------------------------------------------------------
    def posts_to(self, fragment: str) -> List[Any]:
        return [p for m, u, p in self.calls if m == "POST" and fragment in u]

    def method_called(self, method: str, fragment: str) -> bool:
        return any(m == method and fragment in u for m, u, _ in self.calls)


# --------------------------------------------------------------------------
# 1. Dynamic Channel Routing
# --------------------------------------------------------------------------

def test_multi_channel_routing() -> None:
    print("\n--- Test 1: Dynamic channel routing by strategy_type ---")
    env_new = {
        "TELEGRAM_CHANNEL_SCALPING": "-100111",
        "TELEGRAM_CHANNEL_SWING": "-100222",
        "TELEGRAM_CHANNEL_INVESTMENT": "-100333",
        "TELEGRAM_CHAT_ID": "-100999",
    }
    with mock.patch.dict(os.environ, env_new, clear=False):
        check("scalping routes to TELEGRAM_CHANNEL_SCALPING",
              screener.get_strategy_channel_id("scalping") == "-100111")
        check("swing routes to TELEGRAM_CHANNEL_SWING",
              screener.get_strategy_channel_id(screener.SWING) == "-100222")
        check("investment routes to TELEGRAM_CHANNEL_INVESTMENT",
              screener.get_strategy_channel_id("Investment") == "-100333")
        check("track label 'Scalp' routes to scalping channel",
              screener.get_channel_id_for_track("⚡ مضاربة لحظية (Scalp)") == "-100111")
        check("track label 'Swing' routes to swing channel",
              screener.get_channel_id_for_track("📈 تداول سوينغ (Swing)") == "-100222")
        check("track label 'Invest' routes to investment channel",
              screener.get_channel_id_for_track("🏛️ استثمار طويل (Invest)") == "-100333")
        check("unknown strategy falls back to TELEGRAM_CHAT_ID",
              screener.get_strategy_channel_id("mystery") == "-100999")

    # Legacy fallback when new vars are absent.
    env_legacy = {
        "TELEGRAM_CHANNEL_SCALPING": "",
        "TELEGRAM_CHANNEL_SWING": "",
        "TELEGRAM_CHANNEL_INVESTMENT": "",
        "CHANNEL_SCALPING": "-100444",
        "CHANNEL_SWING": "-100555",
        "CHANNEL_INVESTMENT": "-100666",
        "TELEGRAM_CHAT_ID": "-100999",
    }
    with mock.patch.dict(os.environ, env_legacy, clear=False):
        check("legacy CHANNEL_SCALPING used when new var missing",
              screener.get_strategy_channel_id(SCALPING_KEY()) == "-100444")
        check("legacy CHANNEL_SWING fallback works",
              screener.get_strategy_channel_id("swing") == "-100555")
        check("legacy CHANNEL_INVESTMENT fallback works",
              screener.get_strategy_channel_id("investment") == "-100666")


def SCALPING_KEY() -> str:
    return screener.SCALPING


# --------------------------------------------------------------------------
# 2. Unified short card + join button in public channels
# --------------------------------------------------------------------------

CTX: Dict[str, Any] = {
    "price": 42.5,
    "rsi": 61.0,
    "ema20": 40.0,
    "sma50": 39.0,
    "volume_ratio": 2.1,
}


def test_short_card_and_button() -> None:
    print("\n--- Test 2: Short public-channel card + Track Signal button ---")
    card = screener.build_channel_short_card(
        "swing", "COMI.CA", CTX, "🟢 إيجابي\n🎯 تقييم الجودة (TQI): 7.0/10"
    )
    check("card mentions ticker COMI", "COMI" in card)
    check("card has entry price line", "سعر الدخول" in card and f"{42.5:.2f}" in card)
    check("card has stop loss line", "وقف الخسارة" in card)
    check("card has target_1 line", "الهدف الأول" in card)
    check("card prompts to press the button", "اضغط الزر" in card)
    check("card carries TQI line", "تقييم الجودة (TQI)" in card)
    # Detail-only content must NOT leak into public channels.
    check("no suggested quantity in short card", "الكمية المقترحة" not in card)
    check("no allocation pct in short card", "نسبة الدخول من المحفظه" not in card)
    check("no news summary block in short card", "ملخص الأخبار" not in card)
    check("no macro block in short card", "التحليل الكلي" not in card)
    # Unified template renders dynamic targets 1..N (Target 2/3 included).
    check("dynamic target_2/target_3 lines in card", "الهدف الثاني" in card and "الهدف الثالث" in card)

    # Unified template: ALL broadcasts route through format_channel_short_card.
    unified = screener.build_unified_channel_card(
        strategy="swing",
        ticker="COMI.CA",
        entry_price=42.5,
        stop_loss=39.95,
        targets=(44.63, 46.75, 49.73),
        tqi_score=7.0,
        ctx=CTX,
    )
    check("unified card is HTML", "<b>" in unified)
    check("unified card has dynamic targets", "الهدف الأول" in unified and "الهدف الثاني" in unified)
    check("unified card has TQI", "تقييم الجودة (TQI)" in unified)

    markup = screener.build_join_markup("comi.ca")
    btn = markup["inline_keyboard"][0][0]
    check("button text is unified join label",
          btn["text"] == "📥 انضم للصفقة | Track Signal")
    check("callback data normalized to COMI.CA", btn["callback_data"] == "join_trade:COMI.CA")
    check("callback data within Telegram 64-byte limit",
          len(btn["callback_data"].encode("utf-8")) <= 64)


def test_process_ticker_sends_short_card_only() -> None:
    """process_ticker must publish ONLY the short card (+button) to the routed channel."""
    print("\n--- Test 3: process_ticker publishes short card only ---")
    import pandas as pd

    state: Dict[str, Any] = {"last_alerts": {}}
    sent: Dict[str, Any] = {}

    def fake_send(chat_id: Optional[str], message: str, token: Optional[str], reply_markup: Any = None, parse_mode: str = "Markdown") -> bool:
        sent["chat_id"] = chat_id
        sent["message"] = message
        sent["markup"] = reply_markup
        sent["parse_mode"] = parse_mode
        return True

    # Minimal daily frame so indicator helpers / synthetic delta run offline.
    idx = pd.date_range("2026-01-01", periods=90, freq="D")
    base = pd.DataFrame(
        {
            "Open": [10.0 + i * 0.05 for i in range(90)],
            "High": [11.0 + i * 0.05 for i in range(90)],
            "Low": [9.5 + i * 0.05 for i in range(90)],
            "Close": [10.0 + i * 0.05 for i in range(90)],
            "Volume": [1_000_000] * 89 + [5_000_000],
        },
        index=idx,
    )
    ind = base.copy()
    ind["RSI"] = 62.0
    ind["EMA20"] = base["Close"] * 0.95
    ind["SMA50"] = base["Close"] * 0.90
    ind["VolMA20"] = 1_000_000

    sentiment = "🟢 إيجابي\n🎯 تقييم الجودة (TQI): 7.2/10\n🏷️ المسار: 📈 تداول سوينغ (Swing)"

    with mock.patch.dict(
        os.environ,
        {
            "TELEGRAM_CHANNEL_SWING": "-100222",
            "SUPABASE_URL": "",
            "SUPABASE_KEY": "",
            "TELEGRAM_BOT_TOKEN": "tok",
        },
        clear=False,
    ):
        with mock.patch.object(screener, "fetch_price_history", return_value=base), \
             mock.patch.object(screener, "evaluate_strategies", return_value=["swing"]), \
             mock.patch.object(screener, "compute_indicators", return_value=ind), \
             mock.patch.object(screener, "fetch_arabic_stock_news", return_value=sentiment), \
             mock.patch.object(screener, "has_volume_spike", return_value=True), \
             mock.patch.object(screener, "get_trailing_pe", return_value=None), \
             mock.patch.object(screener.yf, "download", return_value=pd.DataFrame()), \
             mock.patch.object(screener, "send_telegram", side_effect=fake_send), \
             mock.patch.object(screener, "record_sent_alert_supabase", return_value=True), \
             mock.patch.object(screener, "add_active_position", return_value=True):
            screener.process_ticker("SWDY.CA", state)

    check("a message was dispatched to the routed channel", bool(sent))
    check("routed to swing strategy channel", sent.get("chat_id") == "-100222")
    msg = str(sent.get("message", ""))
    check("dispatched text is the unified SHORT card", "إشارة جديدة" in msg and "اضغط الزر" in msg)
    check("unified card sent with HTML parse mode", sent.get("parse_mode") == "HTML")
    check("full detail card NOT dispatched", "سبب دخول الصفقه فنيا" not in msg)
    check("news block NOT dispatched", "ملخص الأخبار" not in msg)
    markup = sent.get("markup") or {}
    btn = ((markup.get("inline_keyboard") or [[]])[0] or [{}])[0]
    check("join button attached to broadcast", btn.get("callback_data") == "join_trade:SWDY.CA")
    check("join button carries canonical text", btn.get("text") == "📥 انضم للصفقة | Track Signal")


# --------------------------------------------------------------------------
# 3. Vercel webhook: join_trade CallbackQuery handling
# --------------------------------------------------------------------------

JOIN_UPDATE: Dict[str, Any] = {
    "update_id": 1001,
    "callback_query": {
        "id": "cb-1",
        "from": {"id": 987654, "is_bot": False, "first_name": "User"},
        "message": {"message_id": 10, "chat": {"id": -100222, "type": "channel"}},
        "data": "join_trade:COMI.CA",
        "chat_instance": "-1",
    },
}

SENT_ALERT_ROW: Dict[str, Any] = {
    "ticker": "COMI.CA",
    "strategy": "swing",
    "entry_price": 42.5,
    "current_stop_loss": 39.9,
    "target_1": 44.63,
    "target_2": 46.75,
    "target_3": 49.73,
}


def test_parse_join_callback() -> None:
    print("\n--- Test 4: parse_join_callback ---")
    mod = load_webhook_module()
    parsed = mod.parse_join_callback("join_trade:COMI.CA")
    check("ticker-only payload parses", parsed == ("COMI.CA", 0))
    parsed2 = mod.parse_join_callback("join_trade:elwa:17")
    check("payload with trade id parses + normalizes", parsed2 == ("ELWA.CA", 17))
    check("invalid prefix rejected", mod.parse_join_callback("act_COMI.CA") is None)
    check("empty ticker rejected", mod.parse_join_callback("join_trade:") is None)


def test_webhook_join_flow_registers_and_dms() -> None:
    print("\n--- Test 5: webhook join_trade -> user_portfolio + private FULL card DM ---")
    mod = load_webhook_module()
    router = Router()
    router.on("sent_alerts", FakeResp(200, [SENT_ALERT_ROW]))
    router.on("user_portfolio", FakeResp(201, {}))
    router.on("sendMessage", FakeResp(200, {"ok": True}))
    router.on("answerCallbackQuery", FakeResp(200, {"ok": True}))

    env = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "SUPABASE_URL": "https://fake.supabase.co",
        "SUPABASE_KEY": "k",
    }
    req = SimpleNamespace(method="POST", body=json.dumps(JOIN_UPDATE).encode("utf-8"))
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(mod, "requests", router):
            result = mod.handler(req)

    check("handler returned OK", result in ("OK", {"statusCode": 200, "body": "OK"}))
    check("fetched trade specs from sent_alerts", router.method_called("GET", "sent_alerts"))
    portfolio_posts = router.posts_to("user_portfolio")
    check("registered into user_portfolio", len(portfolio_posts) >= 1)
    if portfolio_posts:
        row = portfolio_posts[0]
        check("row keyed by pressing user", str(row.get("user_id")) == "987654")
        check("row symbol normalized", row.get("symbol") == "COMI.CA")
        check("row starts TRACKING", row.get("status") == "TRACKING")
    dm_posts = router.posts_to("sendMessage")
    check("FULL detail card DM'd privately", len(dm_posts) >= 1)
    if dm_posts:
        dm = dm_posts[0]
        check("DM goes to user's private chat (not the channel)", str(dm.get("chat_id")) == "987654")
        text = str(dm.get("text", ""))
        check("DM is the full join card", "[كارت انضمام للصفقة]" in text)
        check("DM carries entry/SL/targets", "42.50" in text and "39.90" in text and "44.63" in text)
        check("DM promises private close/weekly follow-ups", "الخاص" in text)
    answer_posts = router.posts_to("answerCallbackQuery")
    check("callback answered to kill the spinner", len(answer_posts) >= 1)
    check("no active_positions write on join", not router.method_called("POST", "active_positions"))


def test_webhook_join_flow_already_joined() -> None:
    print("\n--- Test 6: webhook join_trade idempotency (already joined) ---")
    mod = load_webhook_module()
    router = Router()
    router.on("sent_alerts", FakeResp(200, [SENT_ALERT_ROW]))
    router.on("user_portfolio", FakeResp(409, {"message": "conflict"}))
    router.on("sendMessage", FakeResp(200, {"ok": True}))
    router.on("answerCallbackQuery", FakeResp(200, {"ok": True}))

    env = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "SUPABASE_URL": "https://fake.supabase.co",
        "SUPABASE_KEY": "k",
    }
    req = SimpleNamespace(method="POST", body=json.dumps(JOIN_UPDATE).encode("utf-8"))
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(mod, "requests", router):
            mod.handler(req)
    answer_posts = router.posts_to("answerCallbackQuery")
    check("answered with already-following popup",
          any("بالفعل" in str(p.get("text", "")) for p in answer_posts))
    check("still re-sends the private card", len(router.posts_to("sendMessage")) >= 1)


def test_webhook_act_regression() -> None:
    print("\n--- Test 7: act_/dis_/cls_ regression after refactor ---")
    mod = load_webhook_module()
    update = {
        "update_id": 1002,
        "callback_query": {
            "id": "cb-2",
            "from": {"id": 1},
            "data": "act_TEST.CA|10.00|9.70|10.25|10.50|10.80",
        },
    }
    router = Router()
    router.on("active_positions", FakeResp(201, {}))
    router.on("answerCallbackQuery", FakeResp(200, {"ok": True}))
    env = {
        "TELEGRAM_BOT_TOKEN": "tok",
        "SUPABASE_URL": "https://fake.supabase.co",
        "SUPABASE_KEY": "k",
    }
    req = SimpleNamespace(method="POST", body=json.dumps(update).encode("utf-8"))
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(mod, "requests", router):
            mod.handler(req)
    check("act_ still writes to active_positions",
          router.method_called("POST", "active_positions"))


# --------------------------------------------------------------------------
# 4. Private-only exits + weekly reports (never public channels)
# --------------------------------------------------------------------------

def test_exit_notifications_are_private_only() -> None:
    print("\n--- Test 8: close notifications go to joined users' DMs only ---")
    delivered: List[str] = []

    def fake_send(chat_id: Optional[str], message: str, token: Optional[str], reply_markup: Any = None) -> bool:
        delivered.append(str(chat_id))
        return True

    router = Router()
    router.on("user_portfolio", FakeResp(200, [{"user_id": "u1"}, {"user_id": "u2"}]))
    router.on("user_portfolio&status=eq.TRACKING", FakeResp(200, []))

    env = {"SUPABASE_URL": "https://fake.supabase.co", "SUPABASE_KEY": "k"}
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(screener.requests, "get", router.get), \
             mock.patch.object(screener.requests, "patch", router.patch), \
             mock.patch.object(screener, "send_telegram", side_effect=fake_send):
            count = screener.notify_trade_subscribers_dm(
                "COMI.CA", "🔴 [كارت إغلاق صفقة]", "tok", mark_exited=True
            )

    check("both tracking users notified", count == 2 and sorted(delivered) == ["u1", "u2"])
    check("public channels never receive the close card",
          all(not c.startswith("-100") for c in delivered))
    check("rows marked EXITED after close", router.method_called("PATCH", "user_portfolio"))


def test_weekly_reports_dm_only() -> None:
    print("\n--- Test 9: weekly reports are DM'd per joined user ---")
    dm_targets: List[str] = []
    router = Router()
    # distinct users query, then per-user rows query, then positions preload.
    router.on("?select=user_id", FakeResp(200, [{"user_id": "u1"}, {"user_id": "u1"}, {"user_id": "u2"}]))
    router.on("user_id=eq.u1", FakeResp(200, [{"symbol": "COMI.CA", "status": "TRACKING"}]))
    router.on("user_id=eq.u2", FakeResp(200, [{"symbol": "ELWA.CA", "status": "EXITED"}]))
    router.on("active_positions", FakeResp(200, [
        {"ticker": "COMI.CA", "entry_price": 42.5, "current_stop_loss": 39.9, "target_1": 44.63, "status": "ACTIVE"},
    ]))

    captured: List[Dict[str, Any]] = []

    def fake_post(url: str, json: Optional[Dict[str, Any]] = None, timeout: float = 30) -> FakeResp:
        if "sendMessage" in url and json is not None:
            captured.append(json)
            dm_targets.append(str(json.get("chat_id")))
        return FakeResp(200, {"ok": True})

    env = {
        "SUPABASE_URL": "https://fake.supabase.co",
        "SUPABASE_KEY": "k",
        "TELEGRAM_BOT_TOKEN": "tok",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        with mock.patch.object(screener.requests, "get", router.get), \
             mock.patch.object(screener.requests, "post", fake_post):
            screener.send_weekly_dm_reports()

    check("weekly report DM'd to each distinct user", sorted(set(dm_targets)) == ["u1", "u2"])
    check("weekly report never hits public channels",
          all(not t.startswith("-100") for t in dm_targets))
    u1_card = next((c for c in captured if str(c.get("chat_id")) == "u1"), {})
    check("tracking user sees their open position", "COMI.CA" in str(u1_card.get("text", "")))
    check("weekly card labeled as weekly report", "التقرير الأسبوعي" in str(u1_card.get("text", "")))


def test_listen_mode_parses_ticker_only_button() -> None:
    print("\n--- Test 10: local listen mode accepts compact join buttons ---")
    try:
        from egx_quant.core.callback_handler import CallbackQueryHandler
    except Exception as exc:  # heavy deps optional in CI
        print(f"  [SKIP] egx_quant deps unavailable: {exc}")
        return
    parsed = CallbackQueryHandler.parse_callback_data("join_trade:COMI")
    check("ticker-only join accepted (trade_id resolved later)", parsed is not None and parsed[1] == "COMI" and parsed[2] == 0)
    parsed_full = CallbackQueryHandler.parse_callback_data("join_trade:COMI:12:75")
    check("full form still parsed", parsed_full == ("join_trade", "COMI", 12, 7.5))
    check("garbage rejected", CallbackQueryHandler.parse_callback_data("act_COMI") is None)


# --------------------------------------------------------------------------

def main_test() -> None:
    print("Running test_multi_channel_callback.py")
    test_multi_channel_routing()
    test_short_card_and_button()
    test_process_ticker_sends_short_card_only()
    test_parse_join_callback()
    test_webhook_join_flow_registers_and_dms()
    test_webhook_join_flow_already_joined()
    test_webhook_act_regression()
    test_exit_notifications_are_private_only()
    test_weekly_reports_dm_only()
    test_listen_mode_parses_ticker_only_button()
    print(f"\nResults: {PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)
    print("All multi-channel/callback tests passed.")


if __name__ == "__main__":
    main_test()
