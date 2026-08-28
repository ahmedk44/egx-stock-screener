"""EGX live-session daemon.

Asyncio loop tied to utils/egx_calendar:
  - Runs a full scan cycle every CYCLE_SECONDS while the EGX session is open
    (Sun-Thu 10:00-14:30 Africa/Cairo).
  - Outside session hours it sleeps until the next session open, logging
    OPEN/CLOSED state transitions and sending an end-of-day Telegram summary.
  - Blocking network/DB work is pushed to threads via asyncio.to_thread so the
    loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.core.data_engine import MissingTickerError, DataFetchError, resolve_fetcher
from egx_quant.core.interfaces import BaseDataFetcher
from egx_quant.core.market_breadth import MarketBreadthAnalyzer
from egx_quant.core.position_tracker import PositionTracker
from egx_quant.core.risk_engine import RiskManager, atr as compute_atr
from egx_quant.core.shariah_filter import ShariahFilter
from egx_quant.core.strategy_engine import StrategyEngine
from egx_quant.core.weekly_reporter import WeeklyReportEngine
from egx_quant.database.db_manager import DatabaseManager, DEFAULT_DB_PATH
from egx_quant.database.models import PriceQuote, RiskPlan, TradeSignal
from egx_quant.utils.data_guard import OutlierFilter
from egx_quant.utils.telegram_notifier import TelegramNotifier, build_join_markup, clean_ticker
from egx_quant.utils.egx_calendar import CAIRO_TZ, is_market_open, now_cairo
from egx_quant.utils import supabase_sync

logger = logging.getLogger("egx_quant.scheduler")

CYCLE_SECONDS = 5 * 60
MAX_IDLE_CHUNK_SECONDS = 15 * 60
DEDUP_SECONDS = 24 * 3600


class SessionDaemon:
    """Live trading-loop scheduler bounded to EGX session hours."""

    def __init__(
        self,
        notifier: Optional[TelegramNotifier] = None,
        source: str = "auto",
        capital: float = 100_000.0,
        max_open_positions: int = 3,
        db_path: str = str(DEFAULT_DB_PATH),
    ) -> None:
        self._notifier = notifier or TelegramNotifier()
        self._source = source
        self._capital = float(capital)
        self._max_open_positions = max_open_positions
        self._db_path = db_path

        self._fetcher: Optional[BaseDataFetcher] = None
        self._shariah: Optional[ShariahFilter] = None
        self._strategy: Optional[StrategyEngine] = None
        self._risk: Optional[RiskManager] = None
        self._tracker: Optional[PositionTracker] = None
        self._breadth = MarketBreadthAnalyzer()
        self._db: Optional[DatabaseManager] = None
        self._guard = OutlierFilter()
        self._weekly = WeeklyReportEngine(db_path=db_path, capital_base=capital, notifier=self._notifier)
        self._last_weekly_key: Optional[str] = None

    def _ensure_components(self) -> None:
        if self._db is not None:
            return
        self._db = DatabaseManager(self._db_path)
        self._db.initialize()
        self._fetcher = resolve_fetcher(self._source)
        self._shariah = ShariahFilter()
        self._strategy = StrategyEngine(shariah_filter=self._shariah)
        self._risk = RiskManager(total_capital=self._capital)
        self._tracker = PositionTracker(self._db)
        logger.info("[DAEMON] Components ready | capital=%.2f | source=%s", self._capital, self._source)

    @staticmethod
    def _seconds_until_next_open(now: datetime) -> float:
        candidate_date = now.date()
        for _ in range(10):
            candidate = CAIRO_TZ.localize(datetime.combine(candidate_date, datetime.min.time()).replace(hour=10))
            if candidate > now and candidate.weekday() in {6, 0, 1, 2, 3}:
                return (candidate - now).total_seconds()
            candidate_date = candidate_date + timedelta(days=1)
        return MAX_IDLE_CHUNK_SECONDS

    async def run(self) -> None:
        """Main daemon loop; Ctrl-C cancels gracefully."""
        self._ensure_components()
        assert self._db is not None and self._risk is not None
        logger.info("[DAEMON] Session daemon started (cycle every %ds during EGX hours)", CYCLE_SECONDS)
        was_open = False
        try:
            while True:
                now = now_cairo()
                if is_market_open(now):
                    if not was_open:
                        logger.info("[DAEMON] === EGX SESSION OPENED (%s) ===", now.strftime("%Y-%m-%d %H:%M"))
                    was_open = True
                    try:
                        await self.run_cycle()
                    except Exception as exc:
                        logger.error("[DAEMON] Cycle failed: %s", exc, exc_info=True)
                    await asyncio.sleep(CYCLE_SECONDS)
                else:
                    if was_open:
                        logger.info("[DAEMON] === EGX SESSION CLOSED (%s) - sending daily summary ===", now.strftime("%Y-%m-%d %H:%M"))
                        await self._send_daily_summary()
                        was_open = False
                    await self._maybe_send_weekly(now)
                    wait_s = min(self._seconds_until_next_open(now), MAX_IDLE_CHUNK_SECONDS)
                    logger.info("[DAEMON] Market closed - sleeping %.0fs (next open in %.1fh)", wait_s, self._seconds_until_next_open(now) / 3600.0)
                    await asyncio.sleep(max(wait_s, 1.0))
        except asyncio.CancelledError:
            logger.info("[DAEMON] Daemon cancelled - shutting down")
            raise
        finally:
            if self._fetcher is not None:
                self._fetcher.shutdown()
            if self._db is not None:
                self._db.close()

    async def _maybe_send_weekly(self, now: datetime) -> None:
        """Send per-user weekly summaries once per ISO week, Thursday after 14:00 Cairo."""
        if now.weekday() != 3 or now.hour < 14:
            return
        iso = now.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        if key == self._last_weekly_key:
            return
        logger.info("[DAEMON] End of EGX work week - sending per-user weekly summaries (%s)", key)
        try:
            db = self._db
            assert db is not None
            users = set(await asyncio.to_thread(db.portfolio_users))
            users.update(await asyncio.to_thread(supabase_sync.portfolio_users))
            ordered = sorted(users)
            if not ordered:
                logger.info("[DAEMON] No portfolio users this week - skipping weekly reports")
            for uid in ordered:
                try:
                    await asyncio.to_thread(self._weekly.send_weekly_summary, uid)
                except Exception as exc:  # Crash guard per user.
                    logger.error("[DAEMON] Weekly summary failed for user %s: %s", uid, exc)
        except Exception as exc:
            logger.error("[DAEMON] Weekly summary pass failed: %s", exc)
        finally:
            self._last_weekly_key = key

    async def _send_daily_summary(self) -> None:
        db = self._db
        risk = self._risk
        assert db is not None and risk is not None
        open_rows = await asyncio.to_thread(db.fetch_positions, False, 50)
        balance = self._capital + sum(
            t.get("realized_pnl") or 0.0 for t in await asyncio.to_thread(db.fetch_executed_trades, 500)
        )
        await self._notifier.send_daily_summary_async(balance, risk.available_cash, open_rows)

    async def run_cycle(self) -> List[int]:
        """One market scan: exits -> breadth gate -> entries -> alerts."""
        self._ensure_components()
        db = self._db
        fetcher = self._fetcher
        shariah = self._shariah
        strategy = self._strategy
        risk = self._risk
        tracker = self._tracker
        guard = self._guard
        assert db is not None and fetcher is not None and shariah is not None
        assert strategy is not None and risk is not None and tracker is not None

        universe = StocksRegistry.all_symbols()
        quotes: Dict[str, PriceQuote] = await asyncio.to_thread(fetcher.fetch_latest_prices, universe)
        if not quotes:
            logger.warning("[CYCLE] No quotes this cycle - skipped")
            return []

        # Outlier guard: drop phantom prints / accept splits before anything else.
        clean_quotes: Dict[str, PriceQuote] = {}
        for sym, q in quotes.items():
            decision = self._guard.sanitize(sym, q.price)
            if decision.accepted:
                clean_quotes[sym] = q
        discarded = len(quotes) - len(clean_quotes)
        if discarded:
            logger.warning("[CYCLE] Outlier filter discarded %d quote(s)", discarded)
        quotes = clean_quotes
        if not quotes:
            logger.warning("[CYCLE] All quotes discarded by outlier filter - skipped")
            return []

        # Exits on current prices (sanitized -> no phantom exits).
        for pos in list(await asyncio.to_thread(db.fetch_positions, False, 50)):
            sym = str(pos["symbol"])
            quote = quotes.get(sym)
            if not quote:
                continue
            try:
                frame = await asyncio.to_thread(fetcher.get_historical_klines, sym, "6mo", "1d", True)
                atr_val: Optional[float] = compute_atr(frame)
            except (MissingTickerError, DataFetchError):
                atr_val = None
            res = tracker.process_tick(sym, quote.price, atr_val if atr_val else plan_atr_fallback(pos))
            if res:
                entry_cost = float(pos["entry_price"]) * int(pos["quantity"])
                pnl_pct = res["realized_pnl"] / entry_cost * 100.0 if entry_cost else 0.0
                risk.release(res["exit_price"] * res["quantity"])
                # Targeted exits: DM ONLY users who joined (local + Supabase opt-ins).
                trade_id = int(res["position_id"])
                subscribers = set(await asyncio.to_thread(db.trade_subscribers, trade_id))
                subscribers.update(await asyncio.to_thread(supabase_sync.list_subscribers, trade_id))
                exit_card = self._notifier.format_exit_alert(
                    sym,
                    str(res["event_type"]),
                    res["exit_price"],
                    res["quantity"],
                    res["realized_pnl"],
                    pnl_pct,
                )
                if not subscribers:
                    logger.info("[CYCLE] Exit #%s (%s) has no tracking users - no broadcast", trade_id, sym)
                for uid in sorted(subscribers):
                    await self._notifier.send_to_chat_async(uid, exit_card)
                await asyncio.to_thread(db.close_position_subscribers_exit, trade_id)

        # Breadth threat gate.
        market_state = self._breadth.analyze(quotes)

        # Entries (only when buys allowed and capacity remains).
        opened: List[int] = []
        if not market_state.allows_new_buys():
            logger.info("[CYCLE] Buys blocked by market threat=%s", market_state.threat_level.value)
            return []
        open_count = len(await asyncio.to_thread(db.fetch_positions, False, 50))
        slots = self._max_open_positions - open_count
        if slots <= 0:
            return []

        for sym in shariah.filter_universe(universe):
            if len(opened) >= slots:
                break
            quote = quotes.get(sym)
            if not quote or db.get_open_position(sym):
                continue
            # 24h dedup: never re-signal the same symbol within one day.
            last_entry = await asyncio.to_thread(db.last_event_ts, sym, "ENTRY")
            if last_entry is not None and (now_cairo() - last_entry).total_seconds() < DEDUP_SECONDS:
                logger.info("[CYCLE] %s dedup: ENTRY broadcast <24h ago - skipped", sym)
                continue
            try:
                frame = await asyncio.to_thread(fetcher.get_historical_klines, sym, "6mo", "1d", True)
                sig: Optional[TradeSignal] = strategy.evaluate(sym, frame)
            except (MissingTickerError, DataFetchError) as exc:
                logger.warning("[CYCLE] %s skipped: %s", sym, exc)
                continue
            if sig is None:
                continue
            plan: RiskPlan = risk.build_plan(
                sym,
                quote.price,
                frame,
                take_profit_override=sig.take_profit,
                tqi_score=sig.tqi_score,
                targets=[t for t in (sig.target_1, sig.target_2, sig.target_3) if t is not None],
            )
            if not plan.approved:
                continue
            position_id = tracker.open(plan)
            risk.allocate(plan.allocated_cost)
            self._guard.register(plan.symbol, plan.entry_price)
            opened.append(position_id)
            logger.info("[CYCLE] Opened #%d %s x%d @ %.2f TQI=%.1f", position_id, plan.symbol, plan.quantity, plan.entry_price, sig.tqi_score)
            # Publish card fields so the Vercel webhook can DM the full card on join.
            await asyncio.to_thread(
                supabase_sync.publish_trade_signal,
                {
                    "trade_id": position_id,
                    "symbol": plan.symbol,
                    "ticker_bare": clean_ticker(plan.symbol),
                    "entry_price": plan.entry_price,
                    "stop_loss": plan.stop_loss,
                    "target_1": plan.target_1,
                    "target_2": plan.target_2,
                    "target_3": plan.target_3,
                    "tqi": plan.tqi_score,
                    "quantity": plan.quantity,
                    "allocated_cost": plan.allocated_cost,
                    "risk_amount": plan.risk_amount,
                    "shariah_status": shariah.get_status(plan.symbol).value,
                },
            )
            # Interactive channel broadcast: STRICT teaser + [Track Signal] button.
            markup = build_join_markup(position_id, clean_ticker(plan.symbol))
            await self._notifier.broadcast_signal_async(
                self._notifier.format_channel_broadcast(plan, position_id), markup
            )
        return opened


def plan_atr_fallback(pos: Dict[str, Any]) -> float:
    """ATR fallback when live klines are unavailable: ~1% of entry price."""
    return float(pos["entry_price"]) * 0.01

