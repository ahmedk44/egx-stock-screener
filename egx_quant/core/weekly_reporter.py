"""Weekly User Summary Engine - aggregated per-account performance report.

Collects the trailing 7-day activity from the local execution DB:
  - total signals issued (ENTRY events),
  - wins / losses and weekly win-rate,
  - realized PnL (closed trades) + unrealized PnL (open positions marked to
    the latest provided prices),
  - weekly ROI vs the configured capital base,
  - currently open positions with SL/TP levels,
  - Shariah distribution of traded symbols (COMPLIANT vs NON-COMPLIANT).

`send_weekly_summary()` formats the stats as segmented HTML and pushes them to
Telegram; the SessionDaemon calls it automatically at the end of the EGX work
week (Thursday after session close).
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.core.shariah_filter import ShariahFilter
from egx_quant.database.db_manager import DatabaseManager, DEFAULT_DB_PATH
from egx_quant.utils.egx_calendar import now_cairo
from egx_quant.utils.telegram_notifier import TelegramNotifier

logger = logging.getLogger("egx_quant.weekly")


@dataclass
class WeeklyStats:
    window_start: datetime
    window_end: datetime
    signals_issued: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    open_positions: List[Dict[str, Any]] = field(default_factory=list)
    compliant_symbols: int = 0
    non_compliant_symbols: int = 0

    @property
    def win_rate_pct(self) -> float:
        decided = self.wins + self.losses
        return (self.wins / decided * 100.0) if decided else 0.0

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl


def _parse_ts(raw: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


class WeeklyReportEngine:
    """Aggregates the account's week and renders/sends the HTML summary."""

    def __init__(
        self,
        db_path: str = str(DEFAULT_DB_PATH),
        capital_base: float = 100_000.0,
        notifier: Optional[TelegramNotifier] = None,
        lookback_days: int = 7,
    ) -> None:
        self._db_path = db_path
        self._capital_base = float(capital_base)
        self._notifier = notifier or TelegramNotifier()
        self._lookback_days = int(lookback_days)
        self._shariah = ShariahFilter()

    def collect(self, current_prices: Optional[Dict[str, float]] = None, user_id: Optional[str] = None) -> WeeklyStats:
        """Aggregate the trailing window; scope to one user's joined trades when user_id is given."""
        end = now_cairo()
        start = end - timedelta(days=self._lookback_days)
        stats = WeeklyStats(window_start=start, window_end=end)

        db = DatabaseManager(self._db_path)
        db.initialize()
        try:
            allowed_trade_ids: Optional[set] = None
            if user_id:
                joined_rows = db.user_trades(user_id)
                allowed_trade_ids = {int(r["trade_id"]) for r in joined_rows}
                # Merge remote opt-ins made via the Vercel webhook.
                from egx_quant.utils import supabase_sync

                allowed_trade_ids.update(supabase_sync.user_trade_ids(user_id))
            events = db.fetch_executed_trades(limit=2000)
            traded_symbols: set = set()
            for ev in events:
                ts = _parse_ts(str(ev.get("timestamp", "")))
                if ts is None or ts < start:
                    continue
                if allowed_trade_ids is not None and int(ev.get("position_id", -1)) not in allowed_trade_ids:
                    continue
                event_type = str(ev.get("event_type", ""))
                symbol = str(ev.get("symbol", ""))
                if event_type == "ENTRY":
                    stats.signals_issued += 1
                    traded_symbols.add(symbol)
                elif event_type in ("EXIT_STOP_LOSS", "EXIT_TAKE_PROFIT"):
                    pnl = float(ev.get("realized_pnl") or 0.0)
                    stats.realized_pnl += pnl
                    if pnl >= 0:
                        stats.wins += 1
                    else:
                        stats.losses += 1
                    traded_symbols.add(symbol)

            all_open = db.fetch_positions(include_closed=False, limit=100)
            stats.open_positions = [
                p for p in all_open
                if allowed_trade_ids is None or int(p.get("position_id", -1)) in allowed_trade_ids
            ]
            for pos in stats.open_positions:
                sym = str(pos.get("symbol"))
                qty = int(pos.get("quantity", 0))
                entry = float(pos.get("entry_price", 0.0))
                price = float((current_prices or {}).get(sym, entry))
                stats.unrealized_pnl += qty * (price - entry)
                traded_symbols.add(sym)
        finally:
            db.close()

        for sym in sorted(traded_symbols):
            if self._shariah.get_status(sym) is None:
                continue
            meta = StocksRegistry.get(sym)
            if meta is not None and meta.shariah_status.value == "COMPLIANT":
                stats.compliant_symbols += 1
            else:
                stats.non_compliant_symbols += 1

        logger.info(
            "[WEEKLY] signals=%d wins=%d losses=%d realized=%+.2f unrealized=%+.2f",
            stats.signals_issued, stats.wins, stats.losses, stats.realized_pnl, stats.unrealized_pnl,
        )
        return stats

    def format_html(self, stats: WeeklyStats) -> str:
        """Visual weekly summary card (same style as signal cards)."""
        e = html.escape
        sep = "------------------------------------"
        roi_pct = stats.total_pnl / self._capital_base * 100.0
        pnl_icon = "📈" if stats.total_pnl >= 0 else "📉"
        realized_icon = "📈" if stats.realized_pnl >= 0 else "📉"

        lines = [
            "📊 <b>[كارت التقرير الأسبوعي]</b>",
            sep,
            "<b>📈 ملخص الأداء الأسبوعي | EGX Quant</b>",
            f"<i>{e(stats.window_start.strftime('%Y-%m-%d'))} ← {e(stats.window_end.strftime('%Y-%m-%d'))}</i>",
            sep,
            "<b>🔔 الإشارات الصادرة | Signals</b>",
            f"• إجمالي الإشارات: <b>{stats.signals_issued}</b>",
            sep,
            "<b>🎯 النتائج | Results</b>",
            f"• 📈 صفقات رابحة: <b>{stats.wins}</b>",
            f"• 📉 صفقات خاسرة: <b>{stats.losses}</b>",
            f"• نسبة النجاح الأسبوعية: <b>{stats.win_rate_pct:.1f}%</b>",
            sep,
            "<b>💰 العائد | Returns</b>",
            f"• {realized_icon} PnL محقق: <b>{stats.realized_pnl:+,.2f}</b> EGP",
            f"• 🧮 PnL تخيلي (مراكز مفتوحة): <b>{stats.unrealized_pnl:+,.2f}</b> EGP",
            f"• {pnl_icon} الإجمالي: <b>{stats.total_pnl:+,.2f}</b> EGP",
            f"• 🚀 Weekly ROI: <b>{roi_pct:+.2f}%</b>",
            sep,
            "<b>📂 المراكز المفتوحة | Open Positions</b>",
        ]
        if stats.open_positions:
            for p in stats.open_positions:
                lines.append(
                    f"• <code>{e(str(p.get('symbol')))}</code> x{p.get('quantity')} @ "
                    f"{float(p.get('entry_price', 0)):.2f} | 🛑 SL {float(p.get('stop_loss', 0)):.2f} | "
                    f"🎯 TP {float(p.get('take_profit', 0)):.2f}"
                )
        else:
            lines.append("• لا توجد مراكز مفتوحة حالياً ✅")
        lines += [
            sep,
            "<b>🕌 توزيع الشريعة | Shariah Mix</b>",
            f"• ✅ متوافق (Compliant): <b>{stats.compliant_symbols}</b>",
            f"• ⛔ غير متوافق/مراجعة: <b>{stats.non_compliant_symbols}</b>",
            sep,
        ]
        return "\n".join(lines)

    def send_weekly_summary(self, user_id: Optional[str] = None, current_prices: Optional[Dict[str, float]] = None) -> bool:
        """Render + deliver the weekly card. user_id routes to that private chat only."""
        stats = self.collect(current_prices=current_prices, user_id=user_id)
        text = self.format_html(stats)
        if user_id:
            ok = self._notifier.send_to_chat(user_id, text)
        else:
            ok = self._notifier.send_html(text)
        if ok:
            logger.info("[WEEKLY] Summary sent successfully (user=%s)", user_id or "primary")
        else:
            logger.error("[WEEKLY] Summary send failed (user=%s)", user_id or "primary")
        return ok

