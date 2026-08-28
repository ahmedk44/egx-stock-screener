"""Historical backtesting engine for the Phase 2 confluence strategy.

Replays daily candles bar-by-bar using the EXACT production rules:
  - ShariahFilter gates the universe before any evaluation.
  - StrategyEngine indicator formulas (imported directly) define entries.
  - RiskManager sizes every trade (1.5% risk, ATR SL/TP, 20% cap, cash guard).
  - PositionTracker (backed by an in-memory SQLite) enforces trailing stops
    and exit events identically to live trading.

Analytics produced: net P/L (EGP & %), win rate, total trades, profit factor,
max drawdown (equity peak-to-trough %), average trade duration (days).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.core.interfaces import BaseDataFetcher
from egx_quant.core.position_tracker import PositionTracker
from egx_quant.core.risk_engine import RiskManager, atr_series
from egx_quant.core.shariah_filter import ShariahFilter
from egx_quant.core.strategy_engine import DONCHIAN_PERIOD, RSI_LOWER_BOUND, RSI_PERIOD, RSI_UPPER_BOUND, VOLUME_SPIKE_MULT, donchian_high, rsi, sma
from egx_quant.database.db_manager import DatabaseManager

logger = logging.getLogger("egx_quant.backtester")

MIN_BARS = 60


@dataclass(frozen=True)
class ClosedTradeSummary:
    position_id: int
    symbol: str
    entry_date: datetime
    exit_date: datetime
    entry_price: float
    exit_price: float
    quantity: int
    realized_pnl: float
    event_type: str

    @property
    def duration_days(self) -> int:
        return max((self.exit_date - self.entry_date).days, 0)


@dataclass
class BacktestReport:
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    initial_capital: float = 0.0
    final_equity: float = 0.0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_win: float = 0.0
    gross_loss: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: List[ClosedTradeSummary] = field(default_factory=list)

    @property
    def net_pnl(self) -> float:
        return self.final_equity - self.initial_capital

    @property
    def net_pnl_pct(self) -> float:
        return (self.net_pnl / self.initial_capital * 100.0) if self.initial_capital else 0.0

    @property
    def win_rate_pct(self) -> float:
        return (self.wins / self.total_trades * 100.0) if self.total_trades else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_win > 0 else 0.0
        return self.gross_win / self.gross_loss

    @property
    def avg_duration_days(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.duration_days for t in self.trades) / len(self.trades)


class BacktestEngine:
    """Event-driven replay of the full Phase 2 pipeline over history."""

    def __init__(
        self,
        fetcher: BaseDataFetcher,
        capital: float = 100_000.0,
        shariah_filter: Optional[ShariahFilter] = None,
    ) -> None:
        self._fetcher = fetcher
        self._capital = float(capital)
        self._shariah = shariah_filter or ShariahFilter()
        self._db = DatabaseManager(":memory:")

    def _prepare_frames(self, period: str) -> Dict[str, pd.DataFrame]:
        frames: Dict[str, pd.DataFrame] = {}
        for sym in self._shariah.filter_universe(StocksRegistry.all_symbols()):
            try:
                df = self._fetcher.get_historical_klines(sym, period=period, interval="1d")
            except Exception as exc:
                logger.warning("[BACKTEST] %s data unavailable (%s) - skipped", sym, exc)
                continue
            if len(df) < MIN_BARS:
                logger.warning("[BACKTEST] %s only %d bars - skipped", sym, len(df))
                continue
            close = df["Close"].astype(float)
            high = df["High"].astype(float)
            volume = df["Volume"].astype(float)
            df = df.copy()
            df["sma20_vol"] = sma(volume, DONCHIAN_PERIOD)
            df["donchian_high"] = donchian_high(high)
            df["rsi14"] = rsi(close)
            df["atr14"] = atr_series(df)
            frames[sym] = df
            logger.info("[BACKTEST] Loaded %s: %d bars (%s -> %s)", sym, len(df), df.index[0].date(), df.index[-1].date())
        return frames

    @staticmethod
    def _signal(row: pd.Series) -> bool:
        close = float(row["Close"])
        don = float(row["donchian_high"])
        vol_avg = float(row["sma20_vol"])
        vol = float(row["Volume"])
        rsi_v = float(row["rsi14"])
        if not all(math.isfinite(x) for x in (close, don, vol_avg, rsi_v)) or vol_avg <= 0 or don <= 0:
            return False
        return close > don and vol > VOLUME_SPIKE_MULT * vol_avg and RSI_LOWER_BOUND < rsi_v < RSI_UPPER_BOUND

    def run(self, period: str = "1y") -> BacktestReport:
        logger.info("[BACKTEST] Starting | capital=%.2f EGP | period=%s", self._capital, period)
        self._db.initialize()
        risk = RiskManager(total_capital=self._capital)
        tracker = PositionTracker(self._db)
        frames = self._prepare_frames(period)

        report = BacktestReport(initial_capital=self._capital)
        if not frames:
            logger.error("[BACKTEST] No usable data - empty report")
            return report

        all_dates = sorted(set().union(*[set(df.index) for df in frames.values()]))
        report.start_date = all_dates[0]
        report.end_date = all_dates[-1]
        open_meta: Dict[int, Dict[str, Any]] = {}
        equity_curve: List[float] = []

        for day in all_dates:
            day_ts = day.to_pydatetime()

            # 1) Exits first: feed each open position this bar's tick.
            for pos in list(self._db.fetch_positions(include_closed=False)):
                sym = str(pos["symbol"])
                frame = frames.get(sym)
                if frame is None or day not in frame.index:
                    continue
                row = frame.loc[day]
                res = tracker.process_tick(sym, float(row["Close"]), float(row["atr14"]))
                if res:
                    meta = open_meta.pop(int(res["position_id"]), None)
                    risk.release(res["exit_price"] * res["quantity"])
                    trade = ClosedTradeSummary(
                        position_id=int(res["position_id"]),
                        symbol=sym,
                        entry_date=meta["entry_date"] if meta else day_ts,
                        exit_date=day_ts,
                        entry_price=float(meta["entry_price"]) if meta else float(pos["entry_price"]),
                        exit_price=float(res["exit_price"]),
                        quantity=int(res["quantity"]),
                        realized_pnl=float(res["realized_pnl"]),
                        event_type=str(res["event_type"]),
                    )
                    report.trades.append(trade)

            # 2) Entries on this bar's close.
            for sym, frame in frames.items():
                if day not in frame.index:
                    continue
                if self._db.get_open_position(sym):
                    continue
                row = frame.loc[day]
                if not self._signal(row):
                    continue
                plan = risk.build_plan(sym, float(row["Close"]), frame.loc[:day])
                if not plan.approved or plan.quantity < 1:
                    continue
                pid = tracker.open(plan)
                risk.allocate(plan.allocated_cost)
                open_meta[pid] = {"symbol": sym, "entry_date": day_ts, "entry_price": plan.entry_price}

            # 3) Mark-to-market equity.
            equity = risk.available_cash
            for pos in self._db.fetch_positions(include_closed=False):
                frame = frames.get(str(pos["symbol"]))
                if frame is not None and day in frame.index:
                    equity += int(pos["quantity"]) * float(frame.loc[day, "Close"])
                else:
                    equity += int(pos["quantity"]) * float(pos["entry_price"])
            equity_curve.append(equity)

        report.final_equity = equity_curve[-1] if equity_curve else self._capital
        peak = 0.0
        for eq in equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak * 100.0
                report.max_drawdown_pct = max(report.max_drawdown_pct, dd)

        report.total_trades = len(report.trades)
        for t in report.trades:
            if t.realized_pnl >= 0:
                report.wins += 1
                report.gross_win += t.realized_pnl
            else:
                report.losses += 1
                report.gross_loss += abs(t.realized_pnl)

        logger.info(
            "[BACKTEST] Done | trades=%d | net=%+.2f EGP (%+.2f%%) | win=%d/%d | PF=%.2f | MaxDD=%.2f%%",
            report.total_trades,
            report.net_pnl,
            report.net_pnl_pct,
            report.wins,
            report.total_trades,
            report.profit_factor,
            report.max_drawdown_pct,
        )
        self._db.close()
        return report
