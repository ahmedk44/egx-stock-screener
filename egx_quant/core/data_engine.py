"""In-memory market data engine.

YFinanceDataFetcher: batched live quotes + cached OHLCV frames (RAM only).
SyntheticDataFetcher: deterministic offline generator used for demos/tests and
as a live-failure fallback, proving the BaseDataFetcher abstraction.
"""

from __future__ import annotations

import logging
import math
import threading
import zlib
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - dependency declared in requirements.txt
    yf = None  # type: ignore[assignment]

from egx_quant.config.stocks_registry import StocksRegistry, StockMeta
from egx_quant.core.interfaces import BaseDataFetcher
from egx_quant.database.models import PriceQuote
from egx_quant.utils.egx_calendar import now_cairo

logger = logging.getLogger("egx_quant.data_engine")


class DataFetchError(RuntimeError):
    """Raised when a market-data source is entirely unavailable."""


class MissingTickerError(DataFetchError):
    """Raised when a single requested symbol returns no data."""


def _require_yfinance() -> None:
    if yf is None:
        raise DataFetchError(
            "yfinance is not installed",
            "مكتبة yfinance غير مثبتة",
        )


def _norm(symbol: str) -> str:
    return StocksRegistry.normalize(symbol)


class YFinanceDataFetcher(BaseDataFetcher):
    """Live EGX data via Yahoo Finance; candles cached in memory per (symbol|period|interval)."""

    def __init__(self) -> None:
        self._kline_cache: Dict[str, pd.DataFrame] = {}
        self._lock = threading.Lock()

    def fetch_latest_prices(self, symbols: Sequence[str]) -> Dict[str, PriceQuote]:
        _require_yfinance()
        wanted = [_norm(s) for s in symbols]
        wanted = [s for s in dict.fromkeys(wanted) if s]
        if not wanted:
            return {}

        quotes: Dict[str, PriceQuote] = {}
        try:
            raw = yf.download(
                tickers=wanted,
                period="5d",
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:
            logger.warning("[DATA] Batch download failed (%s); returning partial/empty quotes", exc)
            return quotes

        if raw is None or getattr(raw, "empty", True):
            logger.warning("[DATA] Batch download returned no rows")
            return quotes

        multi = isinstance(raw.columns, pd.MultiIndex)
        skipped: List[str] = []
        for sym in wanted:
            frame = None
            try:
                if multi and sym in raw.columns.get_level_values(0):
                    frame = raw[sym]
                elif not multi:
                    frame = raw
                quote = self._quote_from_frame(sym, frame)
                if quote is not None:
                    quotes[sym] = quote
                else:
                    skipped.append(sym)
            except Exception:
                skipped.append(sym)

        if skipped:
            logger.warning("[DATA] Skipped %d unavailable ticker(s): %s", len(skipped), ", ".join(skipped))
        logger.info("[DATA] Live quotes loaded for %d/%d tickers into RAM", len(quotes), len(wanted))
        return quotes

    def _quote_from_frame(self, sym: str, frame: Optional[pd.DataFrame]) -> Optional[PriceQuote]:
        if frame is None or frame.empty or "Close" not in frame.columns:
            raise MissingTickerError(sym)
        closes = frame["Close"].dropna()
        if len(closes) < 2:
            raise MissingTickerError(sym)
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2])
        if not math.isfinite(last) or not math.isfinite(prev) or last <= 0 or prev <= 0:
            raise MissingTickerError(sym)
        volume = 0
        if "Volume" in frame.columns:
            vol_series = frame["Volume"].dropna()
            if not vol_series.empty:
                volume = int(max(vol_series.iloc[-1], 0))
        return PriceQuote(
            symbol=sym,
            price=round(last, 2),
            previous_close=round(prev, 2),
            volume=volume,
            source="yfinance",
            timestamp=now_cairo(),
        )

    def get_historical_klines(
        self,
        symbol: str,
        period: str = "6mo",
        interval: str = "1d",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        _require_yfinance()
        sym = _norm(symbol)
        key = f"{sym}|{period}|{interval}"
        with self._lock:
            cached = self._kline_cache.get(key)
        if cached is not None and not force_refresh:
            return cached.copy()

        try:
            df = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=False)
        except Exception as exc:
            raise DataFetchError(f"kline request failed for {sym}: {exc}", f"فشل طلب البيانات التاريخية لـ {sym}") from exc
        if df is None or df.empty:
            raise MissingTickerError(sym)

        df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
        if df.empty:
            raise MissingTickerError(sym)
        with self._lock:
            self._kline_cache[key] = df
        logger.info("[DATA] Klines cached in RAM: %s (%d bars)", sym, len(df))
        return df.copy()


_BASE_PRICES: Dict[str, float] = {
    "COMI.CA": 118.0,
    "SWDY.CA": 62.0,
    "HELI.CA": 27.5,
    "FWRY.CA": 620.0,
    "EAST.CA": 34.0,
    "ABUK.CA": 96.0,
    "TMGH.CA": 58.0,
    "ETEL.CA": 22.4,
    "ISPH.CA": 8.6,
    "SKPC.CA": 21.0,
    "DOMT.CA": 27.8,
    "EGBE.CA": 9.4,
    "ESRS.CA": 4.2,
}


class SyntheticDataFetcher(BaseDataFetcher):
    """Deterministic offline generator implementing the same interface.

    Per-symbol drift is derived from the ticker name so runs are reproducible.
    The final 25 bars of every frame are replaced with an engineered
    consolidation-then-breakout pattern (Donchian break + volume spike +
    mid-band RSI) so demos and tests deterministically exercise the full
    confluence -> risk-sizing -> trailing-stop pipeline. Live fetching is
    untouched.
    """

    BARS = 180
    TAIL_LEN = 40

    def __init__(
        self,
        seed: int = 20260825,
        confluence_tail: bool = True,
        volume_spikes: bool = True,
    ) -> None:
        self._seed = seed
        self._confluence_tail = confluence_tail
        self._volume_spikes = volume_spikes
        self._frames: Dict[str, pd.DataFrame] = {}
        self._lock = threading.Lock()

    def _rng_for(self, symbol: str) -> np.random.Generator:
        local_seed = (self._seed ^ zlib.crc32(symbol.encode("utf-8"))) & 0xFFFFFFFF
        return np.random.default_rng(local_seed)

    def _drift_for(self, meta: Optional[StockMeta]) -> float:
        if meta is None:
            return 0.0003
        if meta.shariah_status.value == "COMPLIANT":
            return 0.0018
        return 0.0002

    def _frame_for(self, symbol: str) -> pd.DataFrame:
        sym = _norm(symbol)
        with self._lock:
            cached = self._frames.get(sym)
        if cached is not None:
            return cached

        rng = self._rng_for(sym)
        meta = StocksRegistry.get(sym)
        base = (_BASE_PRICES.get(sym, 50.0),)
        drift = self._drift_for(meta)
        start_price = float(base[0]) * rng.uniform(0.82, 0.92)

        rets = rng.normal(drift, 0.016, size=self.BARS)
        closes = start_price * np.exp(np.cumsum(rets))

        prev_close = np.concatenate(([start_price], closes[:-1]))
        opens = prev_close * rng.uniform(0.997, 1.003, size=self.BARS)
        spread_up = rng.uniform(0.002, 0.014, size=self.BARS)
        spread_dn = rng.uniform(0.002, 0.014, size=self.BARS)
        highs = np.maximum(opens, closes) * (1 + spread_up)
        lows = np.minimum(opens, closes) * (1 - spread_dn)
        volumes = rng.integers(80_000, 3_500_000, size=self.BARS)
        if self._volume_spikes:
            spike_mask = rng.random(self.BARS) < 0.10
            spikes = volumes[spike_mask] * rng.uniform(1.9, 3.1, size=int(spike_mask.sum()))
            volumes[spike_mask] = spikes.astype(np.int64)

        idx = pd.bdate_range(end=now_cairo().date(), periods=self.BARS).tz_localize(
            "Africa/Cairo", nonexistent="shift_forward", ambiguous="NaT"
        )
        idx = idx.dropna()
        df = pd.DataFrame(
            {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
            index=idx,
        ).round(2)
        if self._confluence_tail:
            df = self._apply_confluence_tail(df)
        with self._lock:
            self._frames[sym] = df
        return df

    def _apply_confluence_tail(self, df: pd.DataFrame) -> pd.DataFrame:
        """Replace the last TAIL_LEN bars with a deterministic confluence ending.

        Pattern: alternating +-1.0% consolidation for 39 bars (RSI decays toward
        ~50, washing out prior trend memory), then a +5.3% breakout candle on
        2.6x volume that clears the prior-20-day high. Lands RSI in (~55, 65).
        """
        anchor = float(df["Close"].iloc[-self.TAIL_LEN])
        tail_closes = np.empty(self.TAIL_LEN)
        tail_closes[0] = anchor
        sign = -1.0
        for i in range(1, self.TAIL_LEN - 1):
            tail_closes[i] = tail_closes[i - 1] * (1 + sign * 0.010)
            sign *= -1.0
        tail_closes[-1] = anchor * 1.053

        base_vol_rng = np.random.default_rng(self._seed ^ int(anchor * 100))
        tail_volumes = base_vol_rng.integers(400_000, 700_000, size=self.TAIL_LEN).astype(float)
        tail_volumes[-1] = float(np.mean(tail_volumes[:-1])) * 2.6
        tail_volumes = np.round(tail_volumes).astype("int64")

        tail_opens = np.roll(tail_closes, 1)
        tail_opens[0] = anchor
        tail_lows = np.minimum(tail_opens, tail_closes) * 0.998
        tail_highs = np.maximum(tail_opens, tail_closes) * 1.002
        tail_lows[-1] = float(tail_closes[-1]) * 0.995
        tail_highs[-1] = float(tail_closes[-1]) * 1.001

        iloc = df.index[-self.TAIL_LEN:]
        df.loc[iloc, "Open"] = np.round(tail_opens, 2)
        df.loc[iloc, "High"] = np.round(tail_highs, 2)
        df.loc[iloc, "Low"] = np.round(tail_lows, 2)
        df.loc[iloc, "Close"] = np.round(tail_closes, 2)
        df.loc[iloc, "Volume"] = tail_volumes
        return df

    def fetch_latest_prices(self, symbols: Sequence[str]) -> Dict[str, PriceQuote]:
        quotes: Dict[str, PriceQuote] = {}
        for raw_sym in symbols:
            sym = _norm(raw_sym)
            try:
                df = self._frame_for(sym)
                last = float(df["Close"].iloc[-1])
                prev = float(df["Close"].iloc[-2])
                change_pct = round((last - prev) / prev * 100.0, 2)
                quotes[sym] = PriceQuote(
                    symbol=sym,
                    price=round(last, 2),
                    previous_close=round(prev, 2),
                    change_pct=change_pct,
                    volume=int(df["Volume"].iloc[-1]),
                    source="synthetic",
                    timestamp=now_cairo(),
                )
            except Exception as exc:
                logger.warning("[DATA] Synthetic generation failed for %s: %s", sym, exc)
        logger.info("[DATA] Synthetic quotes generated for %d/%d tickers into RAM", len(quotes), len(list(symbols)))
        return quotes

    def get_historical_klines(
        self,
        symbol: str,
        period: str = "6mo",
        interval: str = "1d",
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        sym = _norm(symbol)
        return self._frame_for(sym).copy()


def resolve_fetcher(source: str) -> BaseDataFetcher:
    """Factory honoring --source auto|live|synthetic with graceful degradation."""
    if source == "synthetic":
        return SyntheticDataFetcher()
    fetcher: BaseDataFetcher = YFinanceDataFetcher()
    if source == "live":
        return fetcher
    probe = fetcher.fetch_latest_prices(["COMI.CA"])
    if probe:
        return fetcher
    logger.warning("[DATA] Live source unreachable - falling back to synthetic feed")
    return SyntheticDataFetcher()
