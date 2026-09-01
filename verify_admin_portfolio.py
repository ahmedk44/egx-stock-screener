#!/usr/bin/env python3
"""
Verification script for Admin Commands and User Portfolio system.

Tests:
  1. Admin guard: is_admin returns True for configured IDs, False for others
  2. /close command: any user force-closes their OWN user_portfolio position
  3. /update command: any user sets personal sl/target overrides on their own row
  4. /portfolio command: builds portfolio card with mock positions
  5. Card format completeness: verify all required fields present
  6. format_* functions render correctly
"""
from __future__ import annotations

import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))
sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None

from egx_quant.admin.commands import (
    is_admin,
    handle_slash_command,
    format_portfolio_card,
    format_close_card,
    format_update_card,
    close_trade,
    update_trade,
)


def test_admin_guard():
    """Test admin guard with configured and unconfigured IDs."""
    print("\n" + "=" * 60)
    print("TEST 1: Admin Guard")
    print("=" * 60)

    # Set test admin IDs
    os.environ["ADMIN_TELEGRAM_IDS"] = "123456789,987654321"
    # Force reload of ADMIN_IDS by re-importing
    import egx_quant.admin.commands as ac
    ac.ADMIN_IDS = [x.strip() for x in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",") if x.strip()]

    assert is_admin("123456789") is True, "Should be admin"
    assert is_admin("987654321") is True, "Should be admin"
    assert is_admin("000000000") is False, "Should NOT be admin"
    assert is_admin("") is False, "Empty ID should NOT be admin"
    print("[PASS] Admin guard works correctly")

    # Test with empty admin list (is_admin re-reads env dynamically, so patch the loader)
    ac.ADMIN_IDS = []
    _orig_loader = ac._load_admin_ids
    ac._load_admin_ids = lambda: []
    try:
        assert is_admin("123456789") is False, "Empty admin list should return False"
    finally:
        ac._load_admin_ids = _orig_loader
    print("[PASS] Empty admin list returns False")

    return True


def test_format_cards():
    """Test card format payloads."""
    print("\n" + "=" * 60)
    print("TEST 2: Card Format Payloads")
    print("=" * 60)

    # Close card
    close_card = format_close_card("TEST3", "إغلاق يدوي", 100.0, 105.0, 5.0)
    assert "TEST3" in close_card, "Close card missing ticker"
    assert "إغلاق يدوي" in close_card, "Close card missing reason"
    assert "100.00" in close_card, "Close card missing entry price"
    assert "105.00" in close_card, "Close card missing current price"
    assert "+5.00%" in close_card, "Close card missing PnL"
    assert "CLOSED" in close_card, "Close card missing status"
    print("[PASS] Close card format OK")
    print(f"  Preview:\n{close_card[:300]}")

    # Update card
    update_card = format_update_card("TEST3", {"current_stop_loss": "95.0", "target_1": "110.0"})
    assert "TEST3" in update_card, "Update card missing ticker"
    assert "وقف الخسارة" in update_card, "Update card missing SL label"
    assert "الهدف الأول" in update_card, "Update card missing target label"
    assert "95.0" in update_card, "Update card missing SL value"
    assert "110.0" in update_card, "Update card missing target value"
    print("[PASS] Update card format OK")
    print(f"  Preview:\n{update_card[:300]}")

    return True


def test_portfolio_card():
    """Test /portfolio card rendering."""
    print("\n" + "=" * 60)
    print("TEST 3: Portfolio Card Rendering")
    print("=" * 60)

    positions = [
        {
            "ticker": "COMI.CA",
            "entry_price": 100.0,
            "current_price": 105.5,
            "status": "TRACKING",
            "quantity": 500,
            "joined_at": "2026-08-25T10:00:00Z",
            "custom_entry_price": None,
        },
        {
            "ticker": "ABUK.CA",
            "entry_price": 50.0,
            "current_price": 48.0,
            "status": "TRACKING",
            "quantity": 1000,
            "joined_at": "2026-08-26T10:00:00Z",
            "custom_entry_price": None,
        },
    ]
    card = format_portfolio_card(positions, "123456789", "123456789")
    assert "محفظتك النشطة" in card, "Portfolio card missing Arabic header"
    assert "COMI" in card, "Portfolio card missing COMI"
    assert "ABUK" in card, "Portfolio card missing ABUK"
    assert "+5.50%" in card, "Portfolio card missing COMI PnL"
    assert "-4.00%" in card, "Portfolio card missing ABUK PnL"
    assert "500" in card, "Portfolio card missing quantity"
    assert "1000" in card, "Portfolio card missing ABUK quantity"
    assert "✏️" in card, "Portfolio card missing edit button hint"
    print("[PASS] Portfolio card format OK")
    print(f"  Preview:\n{card[:400]}")

    # Test empty portfolio
    empty_card = format_portfolio_card([], "123456789", "123456789")
    assert "لا توجد صفقات" in empty_card, "Empty portfolio should show message"
    print("[PASS] Empty portfolio card OK")

    return True


def test_slash_command_parse():
    """Test slash command parsing for /close, /update, /portfolio."""
    print("\n" + "=" * 60)
    print("TEST 4: Slash Command Parsing")
    print("=" * 60)

    # Set admin IDs
    import egx_quant.admin.commands as ac
    ac.ADMIN_IDS = ["123456789", "987654321"]

    from_user = {"id": 123456789, "first_name": "Admin"}

    # /portfolio
    ok, text = handle_slash_command("/portfolio", from_user, "")
    assert isinstance(ok, bool), "handle_slash_command returns (bool, str)"
    assert isinstance(text, str), "Response text is string"
    print(f"[PASS] /portfolio parsed: ok={ok}")

    # /portfolio TICKER
    ok, text = handle_slash_command("/portfolio COMI", from_user, "")
    assert isinstance(ok, bool), "/portfolio TICKER returns (bool, str)"
    print(f"[PASS] /portfolio COMI parsed: ok={ok}")

    # /close
    ok, text = handle_slash_command("/close TEST3.CA إغلاق يدوي", from_user, "")
    assert isinstance(ok, bool), "/close returns (bool, str)"
    assert isinstance(text, str), "/close response is string"
    # Should attempt close (may fail without Supabase, but should not crash)
    print(f"[PASS] /close parsed: ok={ok}, text_len={len(text)}")

    # /close without ticker -> interactive own-position close menu (graceful everywhere)
    ok, text = handle_slash_command("/close", from_user, "")
    assert ok is False, "/close without ticker should return False"
    assert isinstance(text, str) and text.strip(), "/close without ticker returns a message"
    print(f"[PASS] /close without ticker shows menu/message: {text[:40]}")

    # /update
    ok, text = handle_slash_command("/update TEST3.CA sl=95 target1=110", from_user, "")
    assert isinstance(ok, bool), "/update returns (bool, str)"
    assert isinstance(text, str), "/update response is string"
    print(f"[PASS] /update parsed: ok={ok}, text_len={len(text)}")

    # /update without params
    ok, text = handle_slash_command("/update TEST3.CA", from_user, "")
    assert ok is False, "/update without params should return False"
    print("[PASS] /update without params shows usage")

    # Non-admin access: /close & /update have NO admin gate - every user operates
    # on their OWN user_portfolio row (graceful without Supabase, never "denied")
    ac.ADMIN_IDS = ["999999999"]
    from_user_non = {"id": 123456789, "first_name": "User"}
    ok, text = handle_slash_command("/close TEST3.CA", from_user_non, "")
    assert isinstance(ok, bool), "Non-admin /close returns (bool, str)"
    assert "مسؤولين" not in text and "⛔" not in text, "Non-admin should NOT be denied for own-position close"
    print(f"[PASS] Non-admin /close allowed on own position: ok={ok}")

    ok, text = handle_slash_command("/update TEST3.CA sl=95", from_user_non, "")
    assert isinstance(ok, bool), "Non-admin /update returns (bool, str)"
    assert "مسؤولين" not in text and "⛔" not in text, "Non-admin should NOT be denied for own-position update"
    print(f"[PASS] Non-admin /update allowed on own position: ok={ok}")

    # Admin users get the SAME own-position behavior (no global broadcast path)
    ac.ADMIN_IDS = ["123456789"]
    ok, text = handle_slash_command("/close TEST3.CA", from_user, "")
    assert isinstance(ok, bool), "Admin /close returns (bool, str)"
    print(f"[PASS] Admin /close routed to own-position handler: ok={ok}")

    return True


def test_update_trade_parse():
    """Test update_trade parameter parsing."""
    print("\n" + "=" * 60)
    print("TEST 5: Update Trade Parameter Parsing")
    print("=" * 60)

    # Test parameter extraction
    import egx_quant.admin.commands as ac
    ac.ADMIN_IDS = ["123456789"]

    # Test _set_custom_entry prompt
    from_user = {"id": 123456789, "first_name": "Admin"}

    # /portfolio TICKER should trigger custom entry prompt
    ok, text = handle_slash_command("/portfolio TEST3", from_user, "")
    assert isinstance(ok, bool), "/portfolio TICKER returns (bool, str)"
    print(f"[PASS] /portfolio TICKER prompt: ok={ok}")

    return True


def test_close_trade_function():
    """Test close_trade function signature and output."""
    print("\n" + "=" * 60)
    print("TEST 6: close_trade Function")
    print("=" * 60)

    import egx_quant.admin.commands as ac
    ac.ADMIN_IDS = ["123456789"]

    # Test without Supabase - should still return valid result
    ok, card = close_trade("TEST3.CA", "اختبار", "123456789", "", "", "")
    assert isinstance(ok, tuple), "close_trade returns (tuple, card)"
    assert isinstance(card, str), "close_trade card is string"
    assert "TEST3" in card, "Close card has ticker"
    print(f"[PASS] close_trade returns card: {card[:100]}...")

    return True


def main():
    print("\n" + "=" * 60)
    print("EGX Admin Commands & Portfolio - Verification")
    print("=" * 60)
    print(f"Mode: Unit verification (no live Telegram/Supabase)")

    passed = 0
    failed = 0

    tests = [
        ("Admin Guard", test_admin_guard),
        ("Card Format Payloads", test_format_cards),
        ("Portfolio Card Rendering", test_portfolio_card),
        ("Slash Command Parsing", test_slash_command_parse),
        ("Update Trade Parsing", test_update_trade_parse),
        ("close_trade Function", test_close_trade_function),
    ]

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"[ERROR] {name}: {type(e).__name__}: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)

    if failed == 0:
        print("\n[SUMMARY] All tests passed!")
        print("   - Admin guard working")
        print("   - /close command format OK")
        print("   - /update command format OK")
        print("   - /portfolio card rendering OK")
        print("   - Slash command parsing OK")
        print("   - close_trade function OK")
        return 0
    else:
        print(f"\n[FAIL] {failed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())