"""Data-outlier guard for live price feeds.

Protects the execution engine from phantom exits caused by bad prints, and
detects stock splits (2x/3x/... or inverse) so they are accepted and used to
re-baseline instead of triggering false stop-loss exits.

Usage: feed every incoming tick through sanitize() before PositionTracker.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("egx_quant.data_guard")

MOVE_THRESHOLD = 0.15
SPLIT_FACTORS = (2.0, 3.0, 4.0, 5.0, 10.0)
SPLIT_TOLERANCE = 0.03


@dataclass(frozen=True)
class TickDecision:
    symbol: str
    price: float
    accepted: bool
    split_detected: bool
    reason: str


class OutlierFilter:
    """Per-symbol last-good-price memory with split-aware validation."""

    def __init__(self, move_threshold: float = MOVE_THRESHOLD) -> None:
        self._threshold = float(move_threshold)
        self._last_good: Dict[str, float] = {}
        self._lock = threading.Lock()

    def register(self, symbol: str, price: float) -> None:
        """Seed/rebaseline the filter with a trusted price (e.g., entry fill)."""
        if not math.isfinite(price) or price <= 0:
            return
        with self._lock:
            self._last_good[symbol] = float(price)

    def _is_split_ratio(self, ratio: float) -> bool:
        for factor in SPLIT_FACTORS:
            if abs(ratio - factor) / factor <= SPLIT_TOLERANCE:
                return True
            if abs(ratio - 1.0 / factor) * factor <= SPLIT_TOLERANCE:
                return True
        return False

    def sanitize(self, symbol: str, new_price: float) -> TickDecision:
        """Validate one tick. Returns decision; accepted=False means DISCARD."""
        sym = symbol
        if not math.isfinite(new_price) or new_price <= 0:
            logger.warning("[GUARD] %s invalid tick %.4f discarded", sym, new_price)
            return TickDecision(sym, new_price, False, False, "invalid-price")

        with self._lock:
            last = self._last_good.get(sym)
        if last is None or last <= 0:
            self.register(sym, new_price)
            return TickDecision(sym, new_price, True, False, "baseline")

        ratio = new_price / last
        with self._lock:
            if abs(ratio - 1.0) <= self._threshold:
                self._last_good[sym] = float(new_price)
                return TickDecision(sym, new_price, True, False, "normal")
            if self._is_split_ratio(ratio):
                self._last_good[sym] = float(new_price)
                logger.info(
                    "[GUARD] %s SPLIT detected (%.2f -> %.2f, x%.2f) - rebaselining",
                    sym, last, new_price, ratio,
                )
                return TickDecision(sym, new_price, True, True, "split")

        logger.warning(
            "[GUARD] %s OUTLIER discarded: %.2f vs last-good %.2f (%+.1f%%)",
            sym, new_price, last, (ratio - 1.0) * 100.0,
        )
        return TickDecision(sym, new_price, False, False, "outlier")
