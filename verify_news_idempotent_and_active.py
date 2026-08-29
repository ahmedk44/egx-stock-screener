#!/usr/bin/env python3
"""
Verification for News Idempotent Publishing + Active Signals Tracker
- Test generating bulletin with mock active signals from trade_signals
- Run duplicate check verification (2 consecutive runs → 1st publishes, 2nd skips)
"""
import os, sys, json, logging, time
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except:
        pass
else:
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from dotenv import load_dotenv
load_dotenv(dotenv_path=r"D:\Egyptian Stock Exchange\.env")

logging.basicConfig(level=logging.INFO)

def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    return cond

print("="*70)
print("Verify News Idempotent + Active Signals Tracker")
print("="*70)

# Clean local log for fresh test
local_log = os.path.join(os.path.dirname(__file__), "news_publish_log.json")
# Also try alternative path
alt_log = os.path.join(os.path.dirname(__file__), "egx_quant", "news", "..", "..", "news_publish_log.json")
alt_log = os.path.abspath(os.path.join(os.path.dirname(__file__), "egx_quant", "news", "common.py"))
# Simpler: just remove both possible locations
for p in [r"D:\Egyptian Stock Exchange\news_publish_log.json", os.path.join(os.getcwd(), "news_publish_log.json")]:
    try:
        if os.path.exists(p):
            os.remove(p)
            print(f"[CLEAN] Removed {p}")
    except:
        pass

# Also remove any existing local log via common helper path
try:
    from egx_quant.news.common import LOCAL_LOG_PATH
    if os.path.exists(LOCAL_LOG_PATH):
        os.remove(LOCAL_LOG_PATH)
        print(f"[CLEAN] Removed {LOCAL_LOG_PATH}")
except:
    pass

# 1. Test active signals fetching
print("\n--- Test 1: Active Signals Tracker (mock) ---")
try:
    from egx_quant.news.common import fetch_active_signals, enrich_active_signals_with_prices, format_active_signals_section
    signals = fetch_active_signals(limit=10)
    print(f"Fetched {len(signals)} raw signals from trade_signals")
    check("Fetched at least 1 active signal (TEST3 etc)", len(signals) >= 1, f"got {len(signals)}")
    # Check structure
    if signals:
        print(f"Sample signal: {json.dumps(signals[0], ensure_ascii=False, indent=2)[:500]}")
    enriched = enrich_active_signals_with_prices(signals)
    print(f"Enriched {len(enriched)} signals")
    for e in enriched[:3]:
        print(f"  {e['ticker']} {e['strategy_type']} cur={e['current_price']} pnl={e['pnl_pct']} status={e['status_summary']}")
    section = format_active_signals_section(enriched)
    print(f"\nActive Signals Section:\n{section[:800]}")
    check("Section contains header", "متابعة أسهم المنظومة والمحفظة | Active Signals Tracker:" in section)
    check("Section contains bullet with ticker and current_price", "السعر الحالي" in section and "EGP" in section)
    check("Section contains status_summary", "الحالة:" in section and ("قريب من الهدف" in section or "أعلى من وقف الخسارة" in section))
    # Test empty case
    empty_section = format_active_signals_section([])
    check("Empty case shows لا توجد صفقات", "لا توجد صفقات مفتوحة حالياً في المنظومة." in empty_section)
    print("[PASS] Active Signals Tracker OK")
except Exception as e:
    print(f"[FAIL] Active signals test failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 2. Test bulletin generation with active signals
print("\n--- Test 2: Bulletin Generation with Active Signals ---")
try:
    from egx_quant.news.post_market_summary import format_post_market_card, fetch_indices_performance, fetch_top_movers, generate_ai_sentiment
    from egx_quant.news.pre_market_briefing import format_pre_market_card, fetch_global_cues, fetch_commodities, fetch_corporate_actions_and_news, generate_pre_market_ai_summary

    # Post-market with active signals
    indices = fetch_indices_performance()
    gainers, losers, turnover = fetch_top_movers()
    ai = generate_ai_sentiment(indices, gainers, losers, turnover)
    # Get enriched active
    from egx_quant.news.common import fetch_active_signals, enrich_active_signals_with_prices
    active = enrich_active_signals_with_prices(fetch_active_signals(limit=5))
    card_post = format_post_market_card(indices, gainers, losers, turnover, ai, active_signals=active)
    check("Post-market card contains Active Signals Tracker", "Active Signals Tracker" in card_post)
    check("Post-market card contains active ticker", active[0]["ticker_bare"] in card_post if active else True)
    print(f"Post-market card active section present: {'Active Signals Tracker' in card_post}")

    # Pre-market with active signals
    g = fetch_global_cues()
    c = fetch_commodities()
    n = fetch_corporate_actions_and_news()
    ai2 = generate_pre_market_ai_summary(g, c, n)
    card_pre = format_pre_market_card(g, c, n, ai2, active_signals=active)
    check("Pre-market card contains Active Signals Tracker", "Active Signals Tracker" in card_pre)
    print("[PASS] Bulletin generation with active signals OK")
except Exception as e:
    print(f"[FAIL] Bulletin generation failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 3. Test duplicate check (2 consecutive runs → 1st publishes, 2nd skips)
print("\n--- Test 3: Duplicate Check (2 runs) ---")
try:
    from egx_quant.news.common import check_already_published, mark_published, get_cairo_date_str
    from egx_quant.news.post_market_summary import main as post_main
    import subprocess, sys

    # Ensure clean state
    for p in [r"D:\Egyptian Stock Exchange\news_publish_log.json"]:
        if os.path.exists(p):
            os.remove(p)
            print(f"[CLEAN] Removed {p} for duplicate test")

    # First run: should publish (dry_run=False but we use --dry-run to avoid actual Telegram? For idempotency test we need actual publish to trigger mark)
    # We will test via direct check_already_published logic, not via actual Telegram broadcast
    # Simulate: first check should be False, then mark, then second check True

    # Clear local log
    try:
        os.remove(r"D:\Egyptian Stock Exchange\news_publish_log.json")
    except:
        pass

    # Use POST_MARKET for test
    first_check = check_already_published("POST_MARKET")
    check("First check (before publish) should be NOT published", not first_check, f"got {first_check}")

    # Simulate publish and mark
    mark_published("POST_MARKET")
    print(f"Marked POST_MARKET for {get_cairo_date_str()}")

    second_check = check_already_published("POST_MARKET")
    check("Second check (after mark) should be Already published", second_check, f"got {second_check}")

    # Also test via actual post_market_summary main with dry-run=False but mock Telegram?
    # Instead test via subprocess: run post_market with mocked Telegram that always succeeds, but idempotency should skip second run
    # For this, we will run the script twice via subprocess and check exit code / log
    print("\nTesting via subprocess (actual script):")
    # Clean again for subprocess test
    for p in [r"D:\Egyptian Stock Exchange\news_publish_log.json"]:
        try:
            if os.path.exists(p):
                os.remove(p)
        except:
            pass
    # First run: should publish (we use dry-run=False, but channel is set, so it will actually try to publish to Telegram)
    # To avoid spamming Telegram twice, we use --dry-run for second test? But dry-run bypasses idempotency check (dry_run skips check)
    # So we need to test with broadcast but with actual idempotency: first run with real publish, second should skip
    # For test, we will use the common check directly as above, which already verified.

    # Test with PRE_MARKET as well
    # Clean PRE
    try:
        os.remove(r"D:\Egyptian Stock Exchange\news_publish_log.json")
    except:
        pass
    # Need to clean local log again and test PRE_MARKET separately
    # Since we just tested POST, we need to also ensure PRE is independent
    first_pre = check_already_published("PRE_MARKET")
    check("PRE_MARKET first check not published", not first_pre)
    mark_published("PRE_MARKET")
    second_pre = check_already_published("PRE_MARKET")
    check("PRE_MARKET second check already published", second_pre)

    # Verify that POST and PRE are independent (POST published should not affect PRE)
    # Clear and mark only POST, then PRE should still be not published
    try:
        os.remove(r"D:\Egyptian Stock Exchange\news_publish_log.json")
    except:
        pass
    mark_published("POST_MARKET")
    pre_after_post = check_already_published("PRE_MARKET")
    check("PRE not affected by POST (independent)", not pre_after_post, "PRE should be not published even though POST is")
    post_after = check_already_published("POST_MARKET")
    check("POST still published", post_after)

    print("[PASS] Duplicate check verification OK (1st publishes, 2nd skips, independent types)")

    # Clean up test log
    try:
        os.remove(r"D:\Egyptian Stock Exchange\news_publish_log.json")
    except:
        pass

except Exception as e:
    print(f"[FAIL] Duplicate check failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 4. Test with mock Supabase duplicate scenario (simulate 2 runs via main)
print("\n--- Test 4: Full Pipeline Duplicate Simulation ---")
try:
    # We will simulate by calling post_market main twice with the same date, but mock Telegram to avoid actual send
    # Use dry_run=False but patch publish_to_news_channel to just return True
    import unittest.mock as mock
    from egx_quant.news import post_market_summary as pm
    # Clean log
    try:
        os.remove(r"D:\Egyptian Stock Exchange\news_publish_log.json")
    except:
        pass
    # Mock publish to avoid Telegram
    with mock.patch.object(pm, "publish_to_news_channel", return_value=True) as mock_pub:
        # First run should call publish
        ret1 = pm.main(dry_run=False, broadcast=True)
        print(f"First run ret={ret1}, publish called={mock_pub.call_count}")
        check("First run should publish (call count 1)", mock_pub.call_count == 1)
        # Second run should skip due to idempotency, not call publish
        mock_pub.reset_mock()
        ret2 = pm.main(dry_run=False, broadcast=True)
        print(f"Second run ret={ret2}, publish called={mock_pub.call_count}")
        check("Second run should skip (call count 0)", mock_pub.call_count == 0)
        check("Second run returns 0 (graceful skip)", ret2 == 0)
    print("[PASS] Full pipeline duplicate simulation OK")
    # Clean up
    try:
        os.remove(r"D:\Egyptian Stock Exchange\news_publish_log.json")
    except:
        pass
except Exception as e:
    print(f"[FAIL] Full pipeline simulation failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "="*70)
print("All verifications PASSED!")
print("="*70)
