"""EGX-QuantEngine Phase 3 - backtesting, live daemon & alerting CLI.

Modes:
    simulate : Phase-2 one-shot pipeline demo (default).
    backtest : Historical confluence-strategy validation report (--period 1y|2y|6mo).
    live     : Session daemon - scans every 5 min during EGX hours with Telegram alerts.

Examples:
    python -m egx_quant.main --mode simulate --source synthetic
    python -m egx_quant.main --mode backtest --period 1y --source synthetic
    python -m egx_quant.main --mode live --source auto
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import sys
from typing import Any, Dict, List

from dotenv import load_dotenv

load_dotenv()

from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.core.backtester import BacktestEngine, BacktestReport
from egx_quant.core.data_engine import (
    DataFetchError,
    MissingTickerError,
    SyntheticDataFetcher,
    YFinanceDataFetcher,
    resolve_fetcher,
)
from egx_quant.core.interfaces import BaseDataFetcher
from egx_quant.core.position_tracker import PositionTracker
from egx_quant.core.risk_engine import ATR_PERIOD, RiskManager, atr as atr_fn
from egx_quant.core.scheduler import SessionDaemon
from egx_quant.core.shariah_filter import ShariahFilter
from egx_quant.core.strategy_engine import StrategyEngine
from egx_quant.core.weekly_reporter import WeeklyReportEngine
from egx_quant.database.db_manager import DatabaseManager, DEFAULT_DB_PATH
from egx_quant.database.models import PriceQuote, RiskPlan, TradeSignal
from egx_quant.utils.egx_calendar import now_cairo, session_label
from egx_quant.utils.telegram_notifier import TelegramNotifier

logger = logging.getLogger("egx_quant.main")

TICK_MULTIPLIERS = (1.006, 1.008, 1.007, 1.009, 0.93)


def _setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _banner(step: str, step_ar: str) -> None:
    print(f"\n=== {step} | {step_ar} ===")


def run_simulation(source: str = "synthetic", capital: float = 100_000.0, max_positions: int = 3, reset_db: bool = False) -> None:
    _setup_logging()
    logger.info("EGX-QuantEngine starting | mode=simulate | session=%s cairo=%s", session_label(), now_cairo().strftime("%Y-%m-%d %H:%M"))

    shariah = ShariahFilter()
    strategy = StrategyEngine(shariah_filter=shariah)
    risk = RiskManager(total_capital=capital)
    notifier = TelegramNotifier()

    _banner("STEP 1/5: Initialize DB + load market data into RAM", "الخطوة 1/5: تهيئة قاعدة البيانات وتحميل بيانات السوق للذاكرة")
    db = DatabaseManager(DEFAULT_DB_PATH)
    db.initialize()
    if reset_db:
        with db._lock:
            assert db._conn is not None
            db._conn.executescript(
                "DELETE FROM executed_trades; DELETE FROM active_positions; DELETE FROM trades;"
            )
            db._conn.commit()
        logger.warning("[DB] Simulation tables reset (--reset-db)")
    tracker = PositionTracker(db)

    fetcher: BaseDataFetcher = resolve_fetcher(source)
    universe = StocksRegistry.all_symbols()
    quotes: Dict[str, PriceQuote] = fetcher.fetch_latest_prices(universe)
    if not quotes:
        logger.error("[DATA] No quotes available - aborting simulation")
        db.close()
        return
    logger.info("[DATA] RAM snapshot ready: %d tickers priced", len(quotes))

    _banner("STEP 2/5: Screen COMPLIANT stocks for confluence setups", "الخطوة 2/5: فحص الأسهم المتوافقة شرعياً لإشارات التقاطع الفني")
    compliant_universe = shariah.filter_universe(universe)
    print(f"[SCREEN] Universe={len(universe)} -> Compliant={len(compliant_universe)} (non-compliant never evaluated)")
    signals: Dict[str, TradeSignal] = {}
    for sym in compliant_universe:
        try:
            df = fetcher.get_historical_klines(sym, period="6mo", interval="1d")
            sig = strategy.evaluate(sym, df)
        except (MissingTickerError, DataFetchError) as exc:
            logger.warning("[SCREEN] %s skipped: %s", sym, exc)
            continue
        if sig is not None:
            signals[sym] = sig
    if not signals:
        print("[SCREEN] لا توجد إشارات تقاطع مؤهلة الآن | No qualifying confluence signals right now")

    _banner("STEP 3/5: Position sizing via RiskManager (ATR SL/TP)", "الخطوة 3/5: حساب حجم المركز ووقف/هدف باستخدام محرك المخاطر")
    plans: List[RiskPlan] = []
    for sym, sig in signals.items():
        if len(plans) >= max_positions:
            break
        quote = quotes.get(sym)
        entry = float(quote.price if quote is not None else sig.entry_price)
        try:
            plan = risk.build_plan(sym, entry, fetcher.get_historical_klines(sym))
        except (MissingTickerError, DataFetchError) as exc:
            logger.warning("[RISK] %s sizing skipped: %s", sym, exc)
            continue
        if not plan.approved:
            print(f"[RISK] {sym} rejected: {plan.rejection_reason_en}")
            continue
        plans.append(plan)

    _banner("STEP 4/5: Register positions in active_positions", "الخطوة 4/5: تسجيل المراكز الجديدة في جدول المراكز النشطة")
    opened: List[int] = []
    for plan in plans:
        existing = db.get_open_position(plan.symbol)
        if existing:
            print(f"[SKIP] {plan.symbol} already holds OPEN position #{existing['position_id']} - one position per symbol")
            continue
        position_id = tracker.open(plan)
        risk.allocate(plan.allocated_cost)
        opened.append(position_id)
        notifier.send_text(notifier.format_buy_alert(plan))
        print(
            f"[OPEN] Position #{position_id}: LONG {plan.symbol} x{plan.quantity} @ {plan.entry_price:.2f} EGP "
            f"| SL={plan.stop_loss:.2f} TP={plan.take_profit:.2f} (ATR14={plan.atr:.3f}) "
            f"| cost={plan.allocated_cost:.2f} ({plan.allocation_pct_of_portfolio * 100:.1f}% of portfolio) "
            f"| risked={plan.risk_amount:.2f}"
        )
    if not opened:
        print("[PORTFOLIO] لم يتم فتح أي مراكز في هذه الجلسة | No positions opened this run")

    _banner("STEP 5/5: Simulate 5 ticks - trailing stop + auto exit", "الخطوة 5/5: محاكاة خمس نبضات سعرية لاختبار وقف الخسارة المتحرك والخروج التلقائي")
    for position_id in opened:
        pos = next((p for p in db.fetch_positions(include_closed=True) if p["position_id"] == position_id), None)
        if not pos:
            continue
        sym = str(pos["symbol"])
        price = float(pos["entry_price"])
        base_atr = atr_fn(fetcher.get_historical_klines(sym), ATR_PERIOD)
        print(f"\n[TICKS] Position #{position_id} {sym} starting at entry {price:.2f}")
        exited = False
        for i, mult in enumerate(TICK_MULTIPLIERS, start=1):
            prev = db.get_open_position(sym)
            prev_sl = float(prev["stop_loss"]) if prev else float("nan")
            price = round(price * mult, 2)
            result = tracker.process_tick(sym, price, base_atr)
            if result is None:
                cur = db.get_open_position(sym)
                assert cur is not None
                ratchet = "ratcheted" if float(cur["stop_loss"]) > prev_sl else "unchanged"
                print(
                    f"  tick {i}/5: price={price:.2f} | highest={float(cur['highest_price_seen']):.2f} "
                    f"| SL={float(cur['stop_loss']):.2f} ({ratchet})"
                )
            else:
                entry_cost = float(pos["entry_price"]) * int(pos["quantity"])
                pnl_pct = (result["realized_pnl"] / entry_cost * 100.0) if entry_cost else 0.0
                print(
                    f"  tick {i}/5: price={price:.2f} -> ** {result['event_type']} ** @ {result['exit_price']:.2f} "
                    f"| PnL={result['realized_pnl']:+.2f} EGP ({pnl_pct:+.2f}%)"
                )
                risk.release(result["exit_price"] * result["quantity"])
                notifier.send_text(
                    notifier.format_exit_alert(
                        sym, str(result["event_type"]), result["exit_price"], result["quantity"], result["realized_pnl"], pnl_pct
                    )
                )
                exited = True
                break
        if not exited:
            cur = db.get_open_position(sym)
            if cur:
                print(f"  [STILL OPEN] after 5 ticks: price={price:.2f} SL={float(cur['stop_loss']):.2f} TP={float(cur['take_profit']):.2f}")

    _banner("SUMMARY", "الملخص")
    open_rows = db.fetch_positions(include_closed=False)
    closed_rows = [p for p in db.fetch_positions(include_closed=True) if p["status"] == "CLOSED"]
    print(f"[SUMMARY] Open positions : {len(open_rows)} {[r['symbol'] for r in open_rows]}")
    print(f"[SUMMARY] Closed positions: {len(closed_rows)} {[r['symbol'] for r in closed_rows]}")
    print(f"[SUMMARY] Available cash  : {risk.available_cash:.2f} / {risk.total_capital:.2f} EGP")
    for t in db.fetch_executed_trades(limit=10):
        pnl = t["realized_pnl"]
        print(f"  executed_trades: #{t['trade_id']} {t['event_type']:<16} {t['symbol']} x{t['quantity']} @ {t['price']:.2f}" + (f" | PnL={pnl:+.2f}" if pnl is not None else ""))

    fetcher.shutdown()
    db.close()
    logger.info("Phase 2 simulation finished")


def _print_backtest_report(report: BacktestReport) -> None:
    pf = f"{report.profit_factor:.2f}" if math.isfinite(report.profit_factor) else "INF"
    print("\n=== BACKTEST REPORT | تقرير الاختبار التاريخي ===")
    print(f"[PERIOD ] {report.start_date.date() if report.start_date else '-'} -> {report.end_date.date() if report.end_date else '-'}")
    print(f"[CAPITAL] Start={report.initial_capital:,.2f} EGP -> End Equity={report.final_equity:,.2f} EGP")
    print(f"[NET P/L] {report.net_pnl:+,.2f} EGP ({report.net_pnl_pct:+.2f}%)")
    print(f"[TRADES ] Total={report.total_trades} | Wins={report.wins} | Losses={report.losses}")
    print(f"[RATES  ] Win Rate={report.win_rate_pct:.1f}% | Profit Factor={pf}")
    print(f"[RISK   ] Max Drawdown={report.max_drawdown_pct:.2f}% | Avg Duration={report.avg_duration_days:.1f} days")
    for t in report.trades:
        flag = "WIN " if t.realized_pnl >= 0 else "LOSS"
        print(
            f"  [{flag}] {t.symbol:<9} {t.entry_date.date()} -> {t.exit_date.date()} "
            f"({t.duration_days:>3d}d) {t.entry_price:>8.2f} -> {t.exit_price:>8.2f} "
            f"| PnL={t.realized_pnl:+10.2f} | {t.event_type}"
        )


def _resolve_backtest_fetcher(source: str) -> BaseDataFetcher:
    if source == "live":
        return YFinanceDataFetcher()
    return SyntheticDataFetcher(confluence_tail=False, volume_spikes=True)


def _run_listen_mode() -> None:
    """Long-poll Telegram updates and route join_trade callbacks (CallbackQueryHandler).

    NOTE: Telegram allows EITHER webhook OR getUpdates. This mode deletes the
    production webhook first - run it only when the Vercel receiver is paused.
    """
    import time as time_mod

    import requests

    from egx_quant.core.callback_handler import CallbackQueryHandler
    from egx_quant.database.db_manager import DatabaseManager
    from egx_quant.utils.telegram_notifier import TelegramNotifier as _TN

    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.error("[LISTEN] TELEGRAM_BOT_TOKEN missing")
        return
    api = f"https://api.telegram.org/bot{token}"

    db = DatabaseManager(DEFAULT_DB_PATH)
    db.initialize()
    handler = CallbackQueryHandler(db, _TN())

    try:
        resp = requests.post(f"{api}/deleteWebhook", timeout=15).json()
        logger.warning("[LISTEN] Webhook deleted (%s) - long-polling takes over", resp.get("ok"))
    except requests.exceptions.RequestException as exc:
        logger.error("[LISTEN] deleteWebhook failed: %s", exc)
        return

    offset = 0
    logger.info("[LISTEN] CallbackQueryHandler live - waiting for join_trade presses (Ctrl-C to stop)")
    try:
        while True:
            try:
                payload: Dict[str, Any] = {"timeout": 25, "offset": int(offset), "allowed_updates": ["callback_query"]}
                r = requests.get(f"{api}/getUpdates", params=payload, timeout=40)
                updates = r.json().get("result", []) if r.status_code == 200 else []
            except requests.exceptions.RequestException as exc:
                logger.error("[LISTEN] Poll error: %s", exc)
                time_mod.sleep(5)
                continue
            for update in updates:
                offset = int(update.get("update_id", offset)) + 1
                processed, detail = handler.handle(update)
                logger.info("[LISTEN] update %s -> %s (%s)", update.get("update_id"), processed, detail)
    except KeyboardInterrupt:
        logger.info("[LISTEN] Stopped by user")


def main() -> None:
    parser = argparse.ArgumentParser(description="EGX-QuantEngine Phase 3")
    parser.add_argument("--mode", choices=["simulate", "backtest", "live", "weekly", "listen"], default="simulate")
    parser.add_argument("--source", choices=["auto", "live", "synthetic"], default="auto")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--period", default="1y", help="backtest history window (e.g. 6mo, 1y, 2y)")
    parser.add_argument("--reset-db", action="store_true", help="purge simulation tables before running")
    args = parser.parse_args()
    _setup_logging()

    if args.mode == "backtest":
        engine = BacktestEngine(fetcher=_resolve_backtest_fetcher(args.source), capital=args.capital)
        report = engine.run(period=args.period)
        _print_backtest_report(report)
    elif args.mode == "live":
        daemon = SessionDaemon(
            notifier=TelegramNotifier(),
            source=args.source,
            capital=args.capital,
            max_open_positions=args.max_positions,
            db_path=str(DEFAULT_DB_PATH),
        )
        try:
            asyncio.run(daemon.run())
        except KeyboardInterrupt:
            logger.info("Daemon stopped by user")
    elif args.mode == "weekly":
        reporter = WeeklyReportEngine(capital_base=args.capital)
        ok = reporter.send_weekly_summary()
        print(f"[WEEKLY] Send {'OK' if ok else 'FAILED'}")
    elif args.mode == "listen":
        _run_listen_mode()
    else:
        run_simulation(source=args.source, capital=args.capital, max_positions=args.max_positions, reset_db=args.reset_db)


if __name__ == "__main__":
    main()

