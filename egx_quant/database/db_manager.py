"""Lean SQLite persistence: trades only.

Raw ticks/candles are NEVER stored here - they live purely in pandas
DataFrames in RAM (decoupled architecture principle).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from egx_quant.database.models import TradeRecord

logger = logging.getLogger("egx_quant.db")

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "egx_quant.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id       INTEGER  PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT     NOT NULL,
    action         TEXT     NOT NULL CHECK(action IN ('BUY', 'SELL')),
    entry_price    REAL     NOT NULL CHECK(entry_price > 0),
    quantity       INTEGER  NOT NULL CHECK(quantity > 0),
    strategy_tag   TEXT     NOT NULL,
    market_context TEXT     NOT NULL DEFAULT '{}',
    timestamp      TEXT     NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades(symbol, timestamp);

CREATE TABLE IF NOT EXISTS active_positions (
    position_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol             TEXT    NOT NULL,
    entry_price        REAL    NOT NULL CHECK(entry_price > 0),
    quantity           INTEGER NOT NULL CHECK(quantity > 0),
    stop_loss          REAL    NOT NULL CHECK(stop_loss > 0),
    take_profit        REAL    NOT NULL CHECK(take_profit > 0),
    highest_price_seen REAL    NOT NULL CHECK(highest_price_seen > 0),
    status             TEXT    NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'CLOSED')),
    opened_at          TEXT    NOT NULL,
    closed_at          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_positions_open_symbol
    ON active_positions(symbol) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS executed_trades (
    trade_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id  INTEGER NOT NULL REFERENCES active_positions(position_id),
    symbol       TEXT    NOT NULL,
    event_type   TEXT    NOT NULL CHECK(event_type IN ('ENTRY', 'EXIT_STOP_LOSS', 'EXIT_TAKE_PROFIT')),
    price        REAL    NOT NULL CHECK(price > 0),
    quantity     INTEGER NOT NULL CHECK(quantity > 0),
    realized_pnl REAL,
    timestamp    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_executed_trades_position ON executed_trades(position_id);

CREATE TABLE IF NOT EXISTS user_portfolio (
    user_id   TEXT    NOT NULL,
    trade_id  INTEGER NOT NULL REFERENCES active_positions(position_id),
    symbol    TEXT    NOT NULL,
    joined_at TEXT    NOT NULL,
    snapshot  TEXT    NOT NULL DEFAULT '{}',
    status    TEXT    NOT NULL DEFAULT 'TRACKING' CHECK(status IN ('TRACKING', 'EXITED')),
    PRIMARY KEY (user_id, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_user_portfolio_trade ON user_portfolio(trade_id);
"""


class DatabaseManager:
    """Thread-safe SQLite wrapper with a single pooled connection."""

    def __init__(self, db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
                self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("[DB] SQLite ready at %s", self._path)

    def insert_trade(self, record: TradeRecord) -> int:
        if self._conn is None:
            raise RuntimeError("DatabaseManager not initialized - call initialize() first")
        row = record.to_db_row()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO trades (symbol, action, entry_price, quantity, strategy_tag, market_context, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["symbol"],
                    row["action"],
                    row["entry_price"],
                    row["quantity"],
                    row["strategy_tag"],
                    row["market_context"],
                    row["timestamp"],
                ),
            )
            self._conn.commit()
            trade_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
        logger.info("[DB] Trade #%s persisted: %s %s x%s @ %.2f", trade_id, row["action"], row["symbol"], row["quantity"], row["entry_price"])
        return trade_id

    def fetch_recent_trades(self, limit: int = 20) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY trade_id DESC LIMIT ?", (limit,)
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            try:
                item["market_context"] = json.loads(item.get("market_context") or "{}")
            except json.JSONDecodeError:
                item["market_context"] = {}
            out.append(item)
        return out

    def count_trades(self) -> int:
        if self._conn is None:
            return 0
        with self._lock:
            n = int(self._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
        return n

    def open_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: int,
        stop_loss: float,
        take_profit: float,
        opened_at: str,
    ) -> int:
        """Insert a new OPEN position and its ENTRY event. Returns position_id."""
        if self._conn is None:
            raise RuntimeError("DatabaseManager not initialized - call initialize() first")
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO active_positions
                    (symbol, entry_price, quantity, stop_loss, take_profit, highest_price_seen, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (symbol, entry_price, quantity, stop_loss, take_profit, entry_price, opened_at),
            )
            position_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
            self._conn.execute(
                """
                INSERT INTO executed_trades (position_id, symbol, event_type, price, quantity, realized_pnl, timestamp)
                VALUES (?, ?, 'ENTRY', ?, ?, NULL, ?)
                """,
                (position_id, symbol, entry_price, quantity, opened_at),
            )
            self._conn.commit()
        logger.info("[DB] Position #%s opened: %s x%s @ %.2f SL=%.2f TP=%.2f", position_id, symbol, quantity, entry_price, stop_loss, take_profit)
        return position_id

    def get_open_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        if self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM active_positions WHERE symbol = ? AND status = 'OPEN'",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def update_trailing_stop(self, position_id: int, highest_price: float, new_stop: float) -> bool:
        if self._conn is None:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE active_positions SET highest_price_seen = ?, stop_loss = ? WHERE position_id = ? AND status = 'OPEN'",
                (highest_price, new_stop, position_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def close_position(
        self,
        position_id: int,
        exit_event_type: str,
        exit_price: float,
        closed_at: str,
        realized_pnl: float,
    ) -> bool:
        """Mark CLOSED, stamp closed_at, and append the EXIT event to executed_trades."""
        if self._conn is None:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT symbol, quantity FROM active_positions WHERE position_id = ? AND status = 'OPEN'",
                (position_id,),
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE active_positions SET status = 'CLOSED', closed_at = ? WHERE position_id = ?",
                (closed_at, position_id),
            )
            self._conn.execute(
                """
                INSERT INTO executed_trades (position_id, symbol, event_type, price, quantity, realized_pnl, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (position_id, row["symbol"], exit_event_type, exit_price, int(row["quantity"]), realized_pnl, closed_at),
            )
            self._conn.commit()
        logger.info("[DB] Position #%s closed (%s) @ %.2f | PnL=%.2f EGP", position_id, exit_event_type, exit_price, realized_pnl)
        return True

    def fetch_positions(self, include_closed: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        query = "SELECT * FROM active_positions"
        if not include_closed:
            query += " WHERE status = 'OPEN'"
        query += " ORDER BY position_id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def fetch_executed_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM executed_trades ORDER BY trade_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_position_by_id(self, position_id: int) -> Optional[Dict[str, Any]]:
        if self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM active_positions WHERE position_id = ?", (position_id,)
            ).fetchone()
        return dict(row) if row else None

    def last_event_ts(self, symbol: str, event_type: str) -> Optional[datetime]:
        """Most recent ISO timestamp for a symbol's event type (24h-dedup gate)."""
        if self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT timestamp FROM executed_trades WHERE symbol = ? AND event_type = ? "
                "ORDER BY trade_id DESC LIMIT 1",
                (symbol, event_type),
            ).fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(str(row["timestamp"]))
        except ValueError:
            return None

    def join_trade(self, user_id: str, trade_id: int, symbol: str, snapshot: Dict[str, Any]) -> bool:
        """Register a user against a broadcast trade. False when already joined."""
        if self._conn is None:
            raise RuntimeError("DatabaseManager not initialized - call initialize() first")
        joined_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO user_portfolio (user_id, trade_id, symbol, joined_at, snapshot) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(user_id), int(trade_id), symbol, joined_at, json.dumps(snapshot, ensure_ascii=False, default=str)),
            )
            self._conn.commit()
        if cur.rowcount > 0:
            logger.info("[DB] user %s joined trade #%s (%s)", user_id, trade_id, symbol)
        return cur.rowcount > 0

    def is_joined(self, user_id: str, trade_id: int) -> bool:
        if self._conn is None:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM user_portfolio WHERE user_id = ? AND trade_id = ?",
                (str(user_id), int(trade_id)),
            ).fetchone()
        return row is not None

    def trade_subscribers(self, trade_id: int) -> List[str]:
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM user_portfolio WHERE trade_id = ? AND status = 'TRACKING'",
                (int(trade_id),),
            ).fetchall()
        return [str(r["user_id"]) for r in rows]

    def user_trades(self, user_id: str) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM user_portfolio WHERE user_id = ? ORDER BY joined_at DESC",
                (str(user_id),),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            try:
                item["snapshot"] = json.loads(item.get("snapshot") or "{}")
            except json.JSONDecodeError:
                item["snapshot"] = {}
            out.append(item)
        return out

    def mark_user_trade_exited(self, user_id: str, trade_id: int) -> None:
        if self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE user_portfolio SET status = 'EXITED' WHERE user_id = ? AND trade_id = ?",
                (str(user_id), int(trade_id)),
            )
            self._conn.commit()

    def portfolio_users(self) -> List[str]:
        if self._conn is None:
            return []
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT user_id FROM user_portfolio").fetchall()
        return [str(r["user_id"]) for r in rows]

    def close_position_subscribers_exit(self, trade_id: int) -> None:
        """Mark all tracking rows of a closed trade as EXITED."""
        if self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE user_portfolio SET status = 'EXITED' WHERE trade_id = ?", (int(trade_id),)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "DatabaseManager":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
