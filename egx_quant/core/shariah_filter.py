"""Strict Shariah verification gate for the execution engine.

Default-deny policy: only registry entries explicitly marked COMPLIANT may
reach the broker. Unknown symbols and NEEDS_REVIEW are always blocked.
"""

from __future__ import annotations

import logging
from typing import List, Sequence

from egx_quant.config.stocks_registry import ShariahStatus, StocksRegistry

logger = logging.getLogger("egx_quant.shariah")


class ShariahFilter:
    """Hard compliance gate applied before any order construction."""

    ALLOWED_STATUSES = (ShariahStatus.COMPLIANT,)

    def get_status(self, symbol: str) -> ShariahStatus:
        return StocksRegistry.status(symbol)

    def is_execution_allowed(self, symbol: str) -> bool:
        sym = StocksRegistry.normalize(symbol)
        meta = StocksRegistry.get(sym)
        if meta is None or meta.shariah_status not in self.ALLOWED_STATUSES:
            status = meta.shariah_status.value if meta else "UNKNOWN"
            logger.warning(
                "[شريعة] %s: الحالة %s - التنفيذ مرفوض | [SHARIAH] %s: status=%s - execution BLOCKED",
                sym,
                status,
                sym,
                status,
            )
            return False
        logger.info("[شريعة] %s: متوافق - التنفيذ مسموح | [SHARIAH] %s: COMPLIANT - allowed", sym, sym)
        return True

    def filter_universe(self, symbols: Sequence[str]) -> List[str]:
        return [s for s in symbols if self.is_execution_allowed(s)]
