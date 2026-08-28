"""Risk management & position sizing engine.

Rules enforced:
  - Capital base          : default virtual balance 100,000 EGP.
  - Fixed risk per trade  : max 1.5% of total capital.
  - Volatility model      : ATR(14); SL = entry - 1.5*ATR, TP = entry + 3.0*ATR (1:2 RR).
  - Sizing formula        : qty = floor(capital * risk% / (entry - SL)).
  - Spot-only guardrails  : single trade <= 20% of portfolio value AND
                            qty*entry <= available cash. Zero margin/leverage by design.
"""

from __future__ import annotations

import logging
import math
from typing import List, Optional

import pandas as pd

from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.database.models import RiskPlan

logger = logging.getLogger("egx_quant.risk")

ATR_PERIOD = 14
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0


def atr_series(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Full Average True Range series (Wilder-smoothed via EWMA alpha=1/period)."""
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """Latest ATR value for the frame."""
    val = float(atr_series(df, period).iloc[-1])
    return val if math.isfinite(val) else float("nan")


class RiskManager:
    """Stateful sizing desk tracking available cash across open positions."""

    def __init__(
        self,
        total_capital: float = 100_000.0,
        risk_per_trade_pct: float = 0.015,
        max_single_allocation_pct: float = 0.20,
    ) -> None:
        if total_capital <= 0:
            raise ValueError("total_capital must be positive")
        self.total_capital = float(total_capital)
        self.available_cash = float(total_capital)
        self.risk_per_trade_pct = float(risk_per_trade_pct)
        self.max_single_allocation_pct = float(max_single_allocation_pct)

    def build_plan(
        self,
        symbol: str,
        entry_price: float,
        df: pd.DataFrame,
        take_profit_override: Optional[float] = None,
        tqi_score: float = 0.0,
        targets: Optional[List[float]] = None,
    ) -> RiskPlan:
        """Compute ATR-based SL/TP and quantity under all guardrails.

        `take_profit_override` (e.g. the Fibonacci 1.618 extension) replaces the
        default 3x-ATR take profit when it is finite and above entry. `tqi_score`
        and `targets` ride along on the plan for downstream cards/broadcasts.
        """
        sym = StocksRegistry.normalize(symbol)
        entry = float(entry_price)
        atr_val = atr(df)

        def reject(en: str, ar: str) -> RiskPlan:
            logger.warning("[RISK] %s plan REJECTED: %s | %s", sym, en, ar)
            return RiskPlan(
                symbol=sym,
                entry_price=max(entry, 0.01),
                stop_loss=0.0,
                take_profit=0.0,
                atr=max(atr_val, 0.0) if math.isfinite(atr_val) else 0.0,
                approved=False,
                rejection_reason_en=en,
                rejection_reason_ar=ar,
            )

        if not math.isfinite(entry) or entry <= 0:
            return reject("invalid entry price", "سعر دخول غير صالح")
        if not math.isfinite(atr_val) or atr_val <= 0:
            return reject("ATR unavailable/non-positive - cannot size position", "لا يمكن حساب مؤشر المدى الحقيقي المتوسط")

        stop_loss = round(entry - SL_ATR_MULT * atr_val, 2)
        take_profit = round(entry + TP_ATR_MULT * atr_val, 2)
        if (
            take_profit_override is not None
            and math.isfinite(float(take_profit_override))
            and float(take_profit_override) > entry
        ):
            take_profit = round(float(take_profit_override), 2)
            logger.info("[RISK] %s TP overridden to fib extension %.2f", sym, take_profit)
        if stop_loss <= 0:
            return reject(
                f"computed stop {stop_loss} non-positive for entry {entry}",
                f"وقف الخسارة المحسوب غير صالح ({stop_loss})",
            )

        per_unit_risk = entry - stop_loss
        risk_budget = self.total_capital * self.risk_per_trade_pct
        raw_qty = int(risk_budget // per_unit_risk)

        max_alloc_value = self.total_capital * self.max_single_allocation_pct
        cash_capped_qty = int(self.available_cash // entry)
        alloc_capped_qty = int(max_alloc_value // entry)

        clamped_by = "risk-formula"
        final_qty = raw_qty
        if final_qty > alloc_capped_qty:
            final_qty, clamped_by = alloc_capped_qty, "20%-single-stock-cap"
        if final_qty > cash_capped_qty:
            final_qty, clamped_by = cash_capped_qty, "available-cash"

        if final_qty < 1:
            reason_en = (
                f"quantity rounds to zero (raw={raw_qty}, alloc-cap={alloc_capped_qty}, cash-cap={cash_capped_qty})"
            )
            return reject(reason_en, "الكمية المحسوبة أقل من سهم واحد بعد تطبيق ضوابط رأس المال")

        allocated_cost = round(final_qty * entry, 2)
        allocation_pct = allocated_cost / self.total_capital

        if allocated_cost > self.available_cash + 1e-9:
            return reject(
                f"cost {allocated_cost:.2f} exceeds available cash {self.available_cash:.2f}",
                "تكلفة الصفقة تتجاوز الرصيد النقدي المتاح",
            )

        plan = RiskPlan(
            symbol=sym,
            entry_price=round(entry, 2),
            stop_loss=stop_loss,
            take_profit=take_profit,
            atr=round(atr_val, 4),
            quantity=final_qty,
            risk_amount=round(final_qty * per_unit_risk, 2),
            allocated_cost=allocated_cost,
            allocation_pct_of_portfolio=round(allocation_pct, 4),
            tqi_score=round(max(tqi_score, 0.0), 1),
            target_1=(round(targets[0], 2) if targets and len(targets) > 0 else None),
            target_2=(round(targets[1], 2) if targets and len(targets) > 1 else None),
            target_3=(round(targets[2], 2) if targets and len(targets) > 2 else None),
            approved=True,
        )
        logger.info(
            "[RISK] %s APPROVED via %s: qty=%d @ %.2f | SL=%.2f TP=%.2f ATR=%.4f | cost=%.2f (%.1f%% of portfolio) | risk=%.2f EGP",
            sym,
            clamped_by,
            plan.quantity,
            plan.entry_price,
            plan.stop_loss,
            plan.take_profit,
            plan.atr,
            plan.allocated_cost,
            plan.allocation_pct_of_portfolio * 100,
            plan.risk_amount,
        )
        return plan

    def allocate(self, amount: float) -> None:
        """Reserve cash for a newly opened spot position."""
        self.available_cash = round(self.available_cash - float(amount), 2)
        logger.info("[RISK] Cash allocated %.2f -> available %.2f EGP", amount, self.available_cash)

    def release(self, amount: float) -> None:
        """Return proceeds to cash after a position closes."""
        self.available_cash = round(min(self.available_cash + float(amount), self.total_capital), 2)
        logger.info("[RISK] Cash released %.2f -> available %.2f EGP", amount, self.available_cash)
