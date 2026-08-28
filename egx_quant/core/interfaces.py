"""Abstract provider interfaces for data fetching and broker execution.

Phase 1 ships a yfinance fetcher and a paper (simulated) broker.
Phase 2 can drop in live broker adapters (e.g., Thndr, EFG Hermes) by
implementing these exact contracts without touching pipeline logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Sequence

import pandas as pd

from egx_quant.database.models import FillResult, PositionSummary, PriceQuote, SpotOrder


class BaseDataFetcher(ABC):
    """Market-data source contract: latest quotes + historical klines."""

    @abstractmethod
    def fetch_latest_prices(self, symbols: Sequence[str]) -> Dict[str, PriceQuote]:
        """Return validated latest quotes keyed by normalized symbol.

        Missing/failed tickers are skipped gracefully (never raise for one bad symbol).
        """

    @abstractmethod
    def get_historical_klines(
        self,
        symbol: str,
        period: str = "6mo",
        interval: str = "1d",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame indexed by datetime, kept purely in memory."""

    def shutdown(self) -> None:
        """Optional resource cleanup hook."""
        return None


class BaseBrokerAdapter(ABC):
    """Execution-provider contract: spot-only long orders and position reads."""

    @abstractmethod
    def execute_buy_order(self, order: SpotOrder) -> FillResult:
        """Fill a long BUY order; must reject any non-spot/non-long request."""

    @abstractmethod
    def execute_sell_order(self, order: SpotOrder) -> FillResult:
        """Close an existing long position via SELL; rejects naked shorts."""

    @abstractmethod
    def get_open_positions(self) -> List[PositionSummary]:
        """Currently open long positions held by the broker adapter."""
