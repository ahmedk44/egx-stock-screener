"""CallbackQueryHandler - multi-tenant opt-in engine for channel broadcasts.

Flow when a user taps [📥 انضم للصفقة | Track Signal]:
  callback_data = "join_trade:{TICKER}:{TRADE_ID}[:{tqi_x10}]"
    1. Parse + validate payload (crash-guarded; malformed payloads ignored).
    2. Register (user_id, trade_id, symbol) in user_portfolio (idempotent).
    3. Answer the callback query instantly ("تم تسجيل متابعتك ✅").
    4. DM the user's private chat with the FULL entry card (bare ticker,
       Shariah tag beside symbol, TQI score, entry, red SL, 3 fib targets).

Spot-only: the engine is structurally long-only/spot; no short or margin path
exists anywhere in the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import requests

from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.database.db_manager import DatabaseManager
from egx_quant.utils.telegram_notifier import TelegramNotifier, clean_ticker

logger = logging.getLogger("egx_quant.callback")

CALLBACK_PREFIX = "join_trade"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/answerCallbackQuery"


def _full_symbol(ticker_bare: str) -> str:
    """Normalize a bare ticker back to the full exchange symbol (ELWA -> ELWA.CA)."""
    return StocksRegistry.normalize(ticker_bare)


class CallbackQueryHandler:
    """Routes Telegram callback updates into portfolio registrations + DMs."""

    def __init__(self, db: DatabaseManager, notifier: TelegramNotifier) -> None:
        self._db = db
        self._notifier = notifier

    @staticmethod
    def parse_callback_data(data: str) -> Optional[Tuple[str, str, int, float]]:
        """Return (action, ticker_bare, trade_id, tqi) or None if invalid.

        Accepts both "join_trade:{TICKER}:{TRADE_ID}[:{tqi_x10}]" and the
        compact channel-button form "join_trade:{TICKER}" (trade_id=0; the
        handler resolves it from the latest OPEN position for the symbol).
        """
        try:
            parts = str(data).strip().split(":")
            if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
                return None
            action = parts[0]
            ticker = clean_ticker(parts[1])
            trade_id = int(parts[2]) if len(parts) >= 3 and parts[2].strip() else 0
            tqi = float(parts[3]) / 10.0 if len(parts) >= 4 and parts[3] else 7.5
            if not ticker or trade_id < 0:
                return None
            return action, ticker, trade_id, min(max(tqi, 0.0), 10.0)
        except (ValueError, TypeError):
            return None

    def _answer(self, callback_query_id: str, text: str) -> None:
        token = self._notifier._token
        if not token:
            logger.info("[CALLBACK MOCK ANSWER] %s", text)
            return
        try:
            requests.post(
                TELEGRAM_API_URL.format(token=token),
                json={"callback_query_id": callback_query_id, "text": text, "show_alert": False},
                timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            logger.error("[CALLBACK] answerCallbackQuery failed: %s", exc)

    def handle(self, update: Dict[str, Any]) -> Tuple[bool, str]:
        """Process one Telegram update. Returns (processed, detail)."""
        try:
            cq = update.get("callback_query")
            if not isinstance(cq, dict):
                return False, "no-callback-query"
            data = str(cq.get("data", ""))
            parsed = self.parse_callback_data(data)
            if parsed is None:
                logger.warning("[CALLBACK] Unrecognized callback data ignored: %r", data[:60])
                return False, "unrecognized"

            _, ticker_bare, trade_id, tqi = parsed
            from_user = cq.get("from") or {}
            user_id = str(from_user.get("id", "")).strip()
            if not user_id:
                self._answer(str(cq.get("id", "")), "⚠️ تعذر تحديد هويتك - حاول مرة أخرى")
                return False, "missing-user-id"

            full_symbol = _full_symbol(ticker_bare)
            # Compact channel buttons carry trade_id=0: resolve the latest OPEN
            # position for this symbol so registration + DM still work.
            if trade_id <= 0:
                pos_row = self._db.get_open_position(full_symbol)
                if pos_row:
                    trade_id = int(pos_row["position_id"])
            already = self._db.is_joined(user_id, trade_id) if trade_id > 0 else False
            joined = (
                self._db.join_trade(
                    user_id=user_id,
                    trade_id=trade_id,
                    symbol=full_symbol,
                    snapshot={"ticker": ticker_bare, "tqi": tqi},
                )
                if trade_id > 0
                else False
            )

            pos = self._db.get_position_by_id(trade_id)
            entry = float(pos["entry_price"]) if pos else 0.0
            sl = float(pos["stop_loss"]) if pos else 0.0
            tp_final = float(pos["take_profit"]) if pos else 0.0
            qty = int(pos["quantity"]) if pos else None
            cost = round(entry * qty, 2) if (pos and qty) else None
            risk_amt = round((entry - sl) * qty, 2) if (pos and qty and sl > 0) else None

            # Invert the fib-extension family: final TP is the 1.618 extension.
            if pos and tp_final > entry:
                unit = (tp_final - entry) / 1.618
                targets = [round(entry + unit * m, 2) for m in (0.618, 1.0, 1.618)]
            elif entry > 0:
                unit = max(entry - sl, entry * 0.01)
                targets = [round(entry + unit * m, 2) for m in (0.618, 1.0, 1.618)]
            else:
                targets = [0.0, 0.0, 0.0]

            card = self._notifier.format_full_dm_card(
                symbol=full_symbol,
                entry_price=entry,
                stop_loss=sl,
                targets=targets,
                tqi_score=tqi,
                quantity=qty,
                allocated_cost=cost,
                risk_amount=risk_amt,
            )
            delivered = self._notifier.send_to_chat(user_id, card)

            if already:
                self._answer(str(cq.get("id", "")), "ℹ️ أنت تتابع هذه الصفقة بالفعل")
                detail = "already-joined"
            else:
                self._answer(str(cq.get("id", "")), f"✅ تم تسجيل متابعتك لصفقة {ticker_bare}")
                detail = f"joined={joined}, dm_delivered={delivered}"
            logger.info("[CALLBACK] user=%s trade#%s %s (%s)", user_id, trade_id, ticker_bare, detail)
            return True, detail
        except Exception as exc:  # Crash guard - never raise into the poll loop.
            logger.error("[CALLBACK] Handler crashed: %s", exc, exc_info=True)
            return False, f"error:{exc}"
