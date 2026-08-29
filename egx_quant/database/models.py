"""Pydantic domain models: trades, stock metadata, market state, orders.

Validation guardrails enforced here (data-validation level):
  - No NaN / infinite prices may enter the pipeline.
  - STRICT SPOT-ONLY execution: long-only side, zero margin, unit leverage;
    any short-selling or leveraged order raises SpotOnlyViolation.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from egx_quant.config.stocks_registry import ShariahStatus


class TradeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class MarketSentiment(str, Enum):
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"


class ThreatLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SpotOnlyViolation(ValueError):
    """Raised when an order attempts shorts, margin, or leverage."""

    def __init__(self, reason_en: str, reason_ar: str) -> None:
        super().__init__(reason_en)
        self.reason_en = reason_en
        self.reason_ar = reason_ar


def _ensure_finite(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("non-finite price rejected")
    return value


class StockMetadataModel(BaseModel):
    """Pydantic mirror of registry metadata."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    name_ar: str
    name_en: str
    sector: str
    shariah_status: ShariahStatus

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        t = str(v).strip().upper()
        return t if t.endswith(".CA") else f"{t}.CA"


class PriceQuote(BaseModel):
    """Latest validated price snapshot for one ticker. Rejects NaN/inf."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    price: float = Field(gt=0, allow_inf_nan=False)
    previous_close: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    change_pct: Optional[float] = Field(default=None, allow_inf_nan=False)
    volume: int = Field(default=0, ge=0)
    source: str = "yfinance"
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        t = str(v).strip().upper()
        return t if t.endswith(".CA") else f"{t}.CA"

    @model_validator(mode="after")
    def _derive_change(self) -> "PriceQuote":
        if self.change_pct is None and self.previous_close is not None:
            object.__setattr__(
                self,
                "change_pct",
                round((self.price - self.previous_close) / self.previous_close * 100.0, 2),
            )
        return self


class MarketState(BaseModel):
    """Aggregated market-wide context produced by the breadth analyzer."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    sentiment: MarketSentiment = MarketSentiment.NEUTRAL
    threat_level: ThreatLevel = ThreatLevel.LOW
    comi_price: Optional[float] = Field(default=None, allow_inf_nan=False)
    comi_change_pct: Optional[float] = Field(default=None, allow_inf_nan=False)
    index_status: str = "UNKNOWN"
    advancers: int = Field(default=0, ge=0)
    decliners: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    buy_window_open: bool = True
    note_en: str = ""
    note_ar: str = ""

    def allows_new_buys(self) -> bool:
        return self.buy_window_open


class SpotOrder(BaseModel):
    """Spot-only long-side order request. Validation forbids shorts/margin/leverage."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    action: TradeAction
    quantity: int = Field(gt=0)
    ref_price: float = Field(gt=0, allow_inf_nan=False)
    side: Literal["LONG"] = "LONG"
    order_kind: Literal["SPOT"] = "SPOT"
    margin: bool = False
    leverage: float = Field(default=1.0)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        t = str(v).strip().upper()
        return t if t.endswith(".CA") else f"{t}.CA"

    @model_validator(mode="after")
    def _enforce_spot_only(self) -> "SpotOrder":
        if self.side != "LONG":
            raise SpotOnlyViolation(
                "Short selling is forbidden - long-only spot engine",
                "البيع على المكشوف ممنوع - المحرك يدعم الشراء الفوري فقط",
            )
        if self.order_kind != "SPOT":
            raise SpotOnlyViolation(
                "Only SPOT orders are allowed",
                "يُسمح فقط بأوامر الصفقات الفورية (SPOT)",
            )
        if self.margin:
            raise SpotOnlyViolation(
                "Margin trading is forbidden in this engine",
                "التداول بالهامش ممنوع في هذا المحرك",
            )
        if not math.isfinite(self.leverage) or abs(self.leverage - 1.0) > 1e-9:
            raise SpotOnlyViolation(
                "Leverage is forbidden - leverage must be exactly 1.0",
                "الرفع المالي ممنوع - يجب أن يكون الرفع 1.0 فقط",
            )
        return self


class FillResult(BaseModel):
    """Broker acknowledgment for a submitted spot order."""

    order_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    symbol: str
    action: TradeAction
    quantity: int
    fill_price: float = Field(allow_inf_nan=False)
    status: Literal["FILLED", "REJECTED"]
    reason_en: str = ""
    reason_ar: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class PositionSummary(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    avg_price: float = Field(gt=0, allow_inf_nan=False)
    opened_at: datetime = Field(default_factory=datetime.utcnow)


class TradeSignal(BaseModel):
    """Technical strategy output candidate for the execution gate."""

    symbol: str
    strategy_tag: str
    direction: Literal["LONG"] = "LONG"
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)
    reason_en: str = ""
    reason_ar: str = ""
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    tqi_score: float = Field(default=0.0, ge=0, le=10)
    target_1: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    target_2: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    target_3: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    target_4: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    ote_in_zone: bool = False
    smc_ob_confluence: bool = False
    rsi_divergence: bool = False

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        t = str(v).strip().upper()
        return t if t.endswith(".CA") else f"{t}.CA"


class RiskPlan(BaseModel):
    """Approved (or rejected) position-sizing plan produced by RiskManager."""

    symbol: str
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    stop_loss: float = Field(allow_inf_nan=False)
    take_profit: float = Field(gt=0, allow_inf_nan=False)
    atr: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    quantity: int = Field(default=0, ge=0)
    risk_amount: float = Field(default=0.0, ge=0)
    allocated_cost: float = Field(default=0.0, ge=0)
    allocation_pct_of_portfolio: float = Field(default=0.0, ge=0)
    tqi_score: float = Field(default=0.0, ge=0, le=10)
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    target_3: Optional[float] = None
    target_4: Optional[float] = None
    approved: bool = False
    rejection_reason_en: str = ""
    rejection_reason_ar: str = ""

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        t = str(v).strip().upper()
        return t if t.endswith(".CA") else f"{t}.CA"


class TradeRecord(BaseModel):
    """Persistent trade row mirroring the SQLite `trades` schema."""

    trade_id: Optional[int] = None
    symbol: str
    action: TradeAction
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    quantity: int = Field(gt=0)
    strategy_tag: str
    market_context: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("symbol")
    @classmethod
    def _norm_symbol(cls, v: str) -> str:
        t = str(v).strip().upper()
        return t if t.endswith(".CA") else f"{t}.CA"

    def to_db_row(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "action": self.action.value,
            "entry_price": float(self.entry_price),
            "quantity": int(self.quantity),
            "strategy_tag": self.strategy_tag,
            "market_context": json.dumps(self.market_context, ensure_ascii=False, default=str),
            "timestamp": self.timestamp.isoformat(),
        }
