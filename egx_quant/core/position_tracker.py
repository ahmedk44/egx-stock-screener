"""Active position tracking with dynamic ATR-based trailing stops.

Trail rule (never lowers the stop):
    highest_price_seen = max(highest_price_seen, current_price)
    new_stop           = max(current_stop, highest_price_seen - 1.5 * ATR14)

Exit engine (evaluated on every tick):
    price <= dynamic stop  -> EXIT_STOP_LOSS
    price >= take_profit   -> EXIT_TAKE_PROFIT
Closed positions are stamped in active_positions and an EXIT event is written
to executed_trades with realized PnL.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional

from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.database.db_manager import DatabaseManager
from egx_quant.database.models import RiskPlan
from egx_quant.utils.egx_calendar import now_cairo

logger = logging.getLogger("egx_quant.tracker")

SL_TRAIL_ATR_MULT = 1.5


class PositionTracker:
    """Lifecycle manager for spot-long positions persisted in SQLite."""

    def __init__(self, db: DatabaseManager) -> None:
        self._db = db

    def open(self, plan: RiskPlan) -> int:
        """Register an approved risk plan as a new OPEN position."""
        if not plan.approved or plan.quantity < 1:
            raise ValueError(f"cannot open position from unapproved plan for {plan.symbol}")
        opened_at = now_cairo().isoformat()
        return self._db.open_position(
            symbol=plan.symbol,
            entry_price=plan.entry_price,
            quantity=plan.quantity,
            stop_loss=plan.stop_loss,
            take_profit=plan.take_profit,
            opened_at=opened_at,
        )

    def process_tick(self, symbol: str, current_price: float, current_atr: float) -> Optional[Dict[str, Any]]:
        """Feed one price tick; trail the stop upward and trigger exits.

        Returns None while the position stays open, otherwise a dict:
            {"position_id", "symbol", "event_type", "exit_price", "quantity", "realized_pnl"}
        """
        sym = StocksRegistry.normalize(symbol)
        if not math.isfinite(current_price) or current_price <= 0:
            logger.warning("[TRACKER] %s invalid tick %.4f ignored", sym, current_price)
            return None

        pos = self._db.get_open_position(sym)
        if not pos:
            return None

        position_id = int(pos["position_id"])
        highest = max(float(pos["highest_price_seen"]), current_price)

        atr_val = current_atr if math.isfinite(current_atr) and current_atr > 0 else 0.0
        trailed = highest - SL_TRAIL_ATR_MULT * atr_val
        new_stop = max(float(pos["stop_loss"]), round(trailed, 2))

        if new_stop > float(pos["stop_loss"]):
            self._db.update_trailing_stop(position_id, highest, new_stop)
            logger.info(
                "[TRACKER] %s trail: high=%.2f SL %.2f -> %.2f",
                sym, highest, float(pos["stop_loss"]), new_stop,
            )
        elif highest > float(pos["highest_price_seen"]):
            self._db.update_trailing_stop(position_id, highest, float(pos["stop_loss"]))

        exit_event: Optional[str] = None
        exit_price = current_price
        # Spec order: stop-loss check first, take-profit second.
        if current_price <= new_stop:
            exit_event = "EXIT_STOP_LOSS"
            exit_price = min(current_price, new_stop)
        elif current_price >= float(pos["take_profit"]):
            exit_event = "EXIT_TAKE_PROFIT"
            exit_price = float(pos["take_profit"])

        if exit_event is None:
            return None

        entry_price = float(pos["entry_price"])
        qty = int(pos["quantity"])
        realized_pnl = round((exit_price - entry_price) * qty, 2)
        closed_at = now_cairo().isoformat()
        self._db.close_position(position_id, exit_event, exit_price, closed_at, realized_pnl)
        logger.info(
            "[TRACKER] %s %s @ %.2f (entry %.2f, qty %d) | PnL=%+.2f EGP",
            sym, exit_event, exit_price, entry_price, qty, realized_pnl,
        )
        return {
            "position_id": position_id,
            "symbol": sym,
            "event_type": exit_event,
            "exit_price": exit_price,
            "quantity": qty,
            "realized_pnl": realized_pnl,
        }
