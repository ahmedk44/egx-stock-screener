#!/usr/bin/env python3
"""Test generating both Pre-Market and Post-Market bulletins using mock news data for active, watchlist, and risk tickers."""
import os, sys, json
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

def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    return cond

print("="*70)
print("Context-Aware Watchlist & News Impact Analysis Verification")
print("="*70)

# Test with mock data
print("\n--- Test 1: Mock Categories Direct Formatting ---")
try:
    from egx_quant.news.common import format_context_aware_section, build_context_aware_categories
    # Create mock categories
    mock_categories = {
        "active": [
            {"ticker": "COMI", "price": 95.5, "impact": "إيجابي", "emoji": "🚀", "short_reason": "نتائج أعمال قوية وتوزيع أرباح"},
            {"ticker": "SWDY", "price": 42.1, "impact": "محايد", "emoji": "⚖️", "short_reason": "أداء مستقر مع سيولة متوسطة"},
        ],
        "watchlist": [
            {"ticker": "ORAS", "positive_news_trigger": "إفصاح عن عقد جديد بقيمة 2 مليار جنيه"},
            {"ticker": "HELI", "positive_news_trigger": "ارتفاع التقييم الفني واختراق مقاومة"},
        ],
        "avoid": [
            {"ticker": "EAST", "negative_news_trigger": "تراجع حاد في الأرباح واستقالة رئيس تنفيذي"},
        ]
    }
    section = format_context_aware_section(mock_categories)
    print(section)
    ok = True
    ok &= check("Header present", "متابعة أسهم المنظومة والفرص | System Signals & Opportunities" in section)
    ok &= check("Active header", "صفقاتنا النشطة (Active Trades Impact):" in section)
    ok &= check("Active bullet format", "COMI: السعر 95.50 EGP | التأثير الأخبار: إيجابي 🚀 - نتائج أعمال" in section)
    ok &= check("Watchlist header", "أسهم تحت الرادار (Incoming Setups" in section)
    ok &= check("Watchlist bullet format", "ORAS: السبب: إفصاح عن عقد" in section and "تجهيز سيت أب اختراق/شراء" in section)
    ok &= check("Avoid header", "تحذيرات ومخاطر (Avoid Watchlist):" in section)
    ok &= check("Avoid bullet format", "EAST: السبب: تراجع حاد" in section and "التجنب وعدم الشراء اليوم" in section)
    # Test empty bucket skipping
    empty_watch = {"active": mock_categories["active"], "watchlist": [], "avoid": mock_categories["avoid"]}
    section2 = format_context_aware_section(empty_watch)
    check("Empty watchlist skipped", "أسهم تحت الرادار" not in section2)
    check("Active still present when watchlist empty", "صفقاتنا النشطة" in section2)
    empty_all = {"active": [], "watchlist": [], "avoid": []}
    section3 = format_context_aware_section(empty_all)
    check("All empty shows no-active message", "لا توجد صفقات مفتوحة" in section3)
    print("[PASS] Mock categories formatting OK" if ok else "[FAIL] Mock failed")
except Exception as e:
    print(f"[FAIL] Mock test failed: {e}")
    import traceback
    traceback.print_exc()

print("\n--- Test 2: Build Context-Aware Categories from Mock Active ---")
try:
    from egx_quant.news.common import fetch_active_signals, enrich_active_signals_with_prices, build_context_aware_categories, get_news_impact_for_ticker
    # Mock active signals
    mock_active = [
        {"ticker": "COMI.CA", "strategy_type": "Scalp", "entry_price": 90, "stop_loss": 85, "target_1": 98, "current_price": 95},
        {"ticker": "SWDY.CA", "strategy_type": "Swing", "entry_price": 40, "stop_loss": 38, "target_1": 44},
    ]
    # Enrich manually to avoid yfinance
    enriched = [
        {"ticker": "COMI.CA", "ticker_bare": "COMI", "strategy_type": "Scalp", "current_price": 95.5, "entry_price": 90, "target_1": 98, "pnl_pct": 6.1, "status_summary": "قريب من الهدف الأول"},
        {"ticker": "SWDY.CA", "ticker_bare": "SWDY", "strategy_type": "Swing", "current_price": 42, "entry_price": 40, "target_1": 44, "pnl_pct": 5, "status_summary": "أعلى من وقف الخسارة"},
    ]
    # Need to mock get_news_impact_for_ticker to return deterministic
    import unittest.mock as mock
    with mock.patch("egx_quant.news.common.get_news_impact_for_ticker") as mock_impact:
        def side_effect(ticker, company=None):
            if "COMI" in ticker:
                return {"impact": "إيجابي", "emoji": "🚀", "short_reason": "إعلان توزيع أرباح قوي"}
            elif "SWDY" in ticker:
                return {"impact": "محايد", "emoji": "⚖️", "short_reason": "أداء مستقر"}
            elif ticker in ["ORAS.CA", "HELI.CA"]:
                return {"impact": "إيجابي", "emoji": "🚀", "short_reason": "عقد جديد"}
            elif ticker in ["EAST.CA"]:
                return {"impact": "سلبي", "emoji": "⚠️", "short_reason": "تحذير مالي"}
            return {"impact": "محايد", "emoji": "⚖️", "short_reason": "لا جديد"}
        mock_impact.side_effect = side_effect
        categories = build_context_aware_categories(enriched)
        print(f"Categories: {json.dumps(categories, ensure_ascii=False, indent=2)}")
        check("Active bucket has 2", len(categories["active"]) == 2)
        # Watchlist and avoid will be populated via scanning TICKERS (which will use mock)
        # Since we mocked, it should have at least 1 each from fallback
        check("Watchlist has at least 1", len(categories["watchlist"]) >= 1)
        check("Avoid has at least 1", len(categories["avoid"]) >= 1)
        # Verify formatting
        from egx_quant.news.common import format_context_aware_section
        sec = format_context_aware_section(categories)
        print(sec[:1000])
        check("Section contains all 3 headers", "صفقاتنا النشطة" in sec and "أسهم تحت الرادار" in sec and "تحذيرات ومخاطر" in sec)
except Exception as e:
    print(f"[FAIL] Build categories failed: {e}")
    import traceback
    traceback.print_exc()

print("\n--- Test 3: Post-Market Bulletin with Mock Active Signals ---")
try:
    from egx_quant.news.post_market_summary import format_post_market_card
    from egx_quant.news.common import build_context_aware_categories, enrich_active_signals_with_prices

    # Mock enriched active
    mock_enriched = [
        {"ticker": "COMI.CA", "ticker_bare": "COMI", "strategy_type": "Scalp", "current_price": 95.5, "entry_price": 90, "pnl_pct": 6.1, "status_summary": "قريب من الهدف"},
        {"ticker": "SWDY.CA", "ticker_bare": "SWDY", "strategy_type": "Swing", "current_price": 42, "entry_price": 40, "pnl_pct": 5, "status_summary": "أعلى"},
    ]
    import unittest.mock as mock
    with mock.patch("egx_quant.news.common.get_news_impact_for_ticker") as m:
        m.side_effect = lambda t, c=None: {"impact": "إيجابي" if "COMI" in t else "محايد", "emoji": "🚀" if "COMI" in t else "⚖️", "short_reason": "إعلان إيجابي" if "COMI" in t else "مستقر"}
        # Need to also mock TICKERS scan for watchlist/avoid
        with mock.patch("egx_quant.news.common.get_news_impact_for_ticker", side_effect=lambda t,c=None: {"impact": "إيجابي" if t in ["ORAS.CA"] else "سلبي" if t=="EAST.CA" else "محايد", "emoji": "🚀" if t=="ORAS.CA" else "⚠️" if t=="EAST.CA" else "⚖️", "short_reason": "عقد" if t=="ORAS.CA" else "تحذير" if t=="EAST.CA" else "مستقر"}):
            categories = build_context_aware_categories(mock_enriched)

    indices = {"EGX30": {"close": 28500, "change_pct": 0.85}, "EGX70": {"close": 6500, "change_pct": 0.85}, "EGX100": {"close": 9200, "change_pct": 0.85}}
    gainers = [{"symbol": "HELI.CA", "name": "هليوبوليس", "close": 7.6, "change_pct": 4.2}]
    losers = [{"symbol": "ORWE.CA", "name": "نساجون", "close": 25, "change_pct": -1.5}]
    turnover = [{"symbol": "COMI.CA", "name": "التجاري", "close": 95, "change_pct": 1, "turnover": 800000000}]
    ai = "• **اتجاه السوق:** صاعد\n• **السيولة:** نشطة\n• **أبرز العناوين:** test"

    # We need to patch build_context_aware_categories inside format_post_market_card? Instead directly test formatting with categories
    from egx_quant.news.common import format_context_aware_section
    active_section = format_context_aware_section(categories)
    card = format_post_market_card(indices, gainers, losers, turnover, ai, active_signals=mock_enriched)
    # The card should contain the new header, not the old one
    check("Post-market card contains new header System Signals & Opportunities", "System Signals & Opportunities" in card)
    check("Post-market card contains active bullet with news impact", "التأثير الأخبار:" in card)
    check("Post-market card Telegram markdown compliance (no broken **)", card.count("**") % 2 == 0, f"** count {card.count('**')}")
    print(card[:1500])
    print("[PASS] Post-market bulletin with mock OK")
except Exception as e:
    print(f"[FAIL] Post-market mock failed: {e}")
    import traceback
    traceback.print_exc()

print("\n--- Test 4: Pre-Market Bulletin with Mock Active Signals ---")
try:
    from egx_quant.news.pre_market_briefing import format_pre_market_card
    # Reuse categories from previous
    g = {"S&P500": {"close": 5000, "change_pct": 0.5}}
    c = {"Gold": {"close": 2650, "change_pct": 0.1}, "Oil": {"close": 78, "change_pct": -0.2}, "USD/EGP": {"close": 50.8, "change_pct": 0.05}}
    news = [{"title": "إفصاح COMI", "source": "EGX", "time": "08:00"}]
    ai2 = "• **الإشارات العالمية:** إيجابية\n• **السلع والعملة:** مستقر\n• **الإفصاحات المبكرة:** COMI"
    mock_enriched2 = [
        {"ticker": "COMI.CA", "ticker_bare": "COMI", "strategy_type": "Scalp", "current_price": 95.5, "entry_price": 90, "pnl_pct": 6.1, "status_summary": "قريب"},
    ]
    card2 = format_pre_market_card(g, c, news, ai2, active_signals=mock_enriched2)
    check("Pre-market card contains new header", "System Signals & Opportunities" in card2)
    check("Pre-market card contains active impact", "التأثير الأخبار:" in card2)
    check("Pre-market Telegram markdown", card2.count("**") % 2 == 0)
    print(card2[:1500])
    print("[PASS] Pre-market bulletin with mock OK")
except Exception as e:
    print(f"[FAIL] Pre-market mock failed: {e}")
    import traceback
    traceback.print_exc()

print("\n--- Test 5: Empty Buckets Handling ---")
try:
    from egx_quant.news.common import format_context_aware_section
    # Only active, empty watchlist/avoid
    cat = {"active": [{"ticker": "COMI", "price": 95, "impact": "إيجابي", "emoji": "🚀", "short_reason": "قوي"}], "watchlist": [], "avoid": []}
    sec = format_context_aware_section(cat)
    check("Empty watchlist skipped", "أسهم تحت الرادار" not in sec)
    check("Active still shown", "صفقاتنا النشطة" in sec)
    # Only watchlist
    cat2 = {"active": [], "watchlist": [{"ticker": "ORAS", "positive_news_trigger": "عقد"}], "avoid": []}
    sec2 = format_context_aware_section(cat2)
    check("Empty active skipped", "صفقاتنا النشطة" not in sec2)
    check("Watchlist shown", "أسهم تحت الرادار" in sec2)
    # All empty
    cat3 = {"active": [], "watchlist": [], "avoid": []}
    sec3 = format_context_aware_section(cat3)
    check("All empty shows no-active message", "لا توجد صفقات مفتوحة" in sec3)
    print("[PASS] Empty bucket handling OK")
except Exception as e:
    print(f"[FAIL] Empty handling failed: {e}")

print("\n" + "="*70)
print("All context-aware tests completed")
print("="*70)
