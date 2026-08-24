"""
test_dedup.py
=============
Unit tests for the signal-level fingerprinting deduplication implemented in main.py.

Tests 3 scenarios for the same ticker:
  1. Initial signal          -> should be sent (not a duplicate)
  2. Exact duplicate signal  -> should be SKIPPED (same hash)
  3. Updated signal          -> should be sent (different hash / better criteria)

Run:  python test_dedup.py
"""

import main

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


def test_signal_hash() -> None:
    print("\n--- Test 1: generate_signal_hash ---")
    # Same signal -> same hash
    h1 = main.generate_signal_hash("COMI", "swing", 42.5, "price crossed EMA20", "2026-08-24")
    h2 = main.generate_signal_hash("comi.ca", "SWING", 42.5, "Price Crossed EMA20", "2026-08-24")
    check("hash identical for COMI vs comi.ca same signal", h1 == h2)

    # Different entry price -> different hash
    h3 = main.generate_signal_hash("COMI", "swing", 45.0, "price crossed EMA20", "2026-08-24")
    check("hash differs for different entry price", h1 != h3)

    # Different reason -> different hash
    h4 = main.generate_signal_hash("COMI", "swing", 42.5, "RSI breakout", "2026-08-24")
    check("hash differs for different reason", h1 != h4)

    # Different date -> different hash
    h5 = main.generate_signal_hash("COMI", "swing", 42.5, "price crossed EMA20", "2026-08-25")
    check("hash differs for different date", h1 != h5)


def test_is_exact_duplicate_signal() -> None:
    print("\n--- Test 2: is_exact_duplicate_signal (in-memory cache) ---")

    # 1. Initial signal -> not duplicate
    main._SENT_SIGNAL_HASH_CACHE.clear()
    dup = main.is_exact_duplicate_signal("COMI", "swing", 42.5, "price crossed EMA20", "2026-08-24")
    check("initial signal is NOT a duplicate", dup is False)

    # 2. Exact duplicate -> duplicate (same hash already cached)
    main._SENT_SIGNAL_HASH_CACHE.add(
        main.generate_signal_hash("COMI.CA", "swing", 42.5, "price crossed EMA20", "2026-08-24")
    )
    dup = main.is_exact_duplicate_signal("comi", "swing", 42.5, "price crossed EMA20", "2026-08-24")
    check("exact duplicate signal IS skipped", dup is True)

    # 3. Updated signal (better entry price) -> NOT duplicate, should be allowed
    main._SENT_SIGNAL_HASH_CACHE.clear()
    main._SENT_SIGNAL_HASH_CACHE.add(
        main.generate_signal_hash("COMI.CA", "swing", 42.5, "price crossed EMA20", "2026-08-24")
    )
    dup = main.is_exact_duplicate_signal("COMI", "swing", 40.0, "price crossed EMA20", "2026-08-24")
    check("updated signal (new entry) IS allowed", dup is False)


def main_test() -> None:
    print("Running test_dedup.py")
    test_signal_hash()
    test_is_exact_duplicate_signal()
    print(f"\nResults: {PASS} passed, {FAIL} failed")
    if FAIL:
        raise SystemExit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main_test()
