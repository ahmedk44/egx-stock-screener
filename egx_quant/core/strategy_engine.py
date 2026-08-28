"""Technical strategy evaluation on in-memory pandas candles.

Phase 2/3 entry model - multi-indicator CONFLUENCE (all three must hold on the
latest candle):
  1. Price Breakout : Close > 20-period Donchian High (prior 20 highs).
  2. Volume Spike   : Volume > 1.5 x SMA20(Volume).
  3. RSI Momentum   : 50 < RSI(14) < 70 (bullish, not overbought).

Phase "Interactive" upgrades:
  - Fibonacci OTE golden zone (50% / 61.8% / 78.6% retracement of the impulse).
  - SMC Order-Block touch confluence (+TQI) and bullish RSI divergence (+TQI).
  - TQI quality score (base 5.0 from confluence, max 10); only >= 5.0 passes.
  - Three Fibonacci-extension targets: swing_high + range x {0.618, 1.0, 1.618}.

Guardrail: a ShariahFilter instance is injected and evaluated STRICTLY BEFORE
any technical computation, so non-compliant tickers never emit signals.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from egx_quant.core.shariah_filter import ShariahFilter
from egx_quant.database.models import TradeSignal

logger = logging.getLogger("egx_quant.strategy")

MIN_BARS = 60
RSI_PERIOD = 14
DONCHIAN_PERIOD = 20
VOLUME_SPIKE_MULT = 1.5
RSI_LOWER_BOUND = 50.0
RSI_UPPER_BOUND = 70.0

IMPULSE_LOOKBACK = 45
OTE_MIN_RETRACE = 0.48
OTE_MAX_RETRACE = 0.81
FIB_EXTENSIONS = (0.618, 1.0, 1.618)
TQI_BASE = 5.0
TQI_MIN_THRESHOLD = 5.0


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window=window, min_periods=window).mean()


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def donchian_high(high: pd.Series, window: int = DONCHIAN_PERIOD) -> pd.Series:
    """Highest high of the PRIOR `window` bars (excludes current bar)."""
    return high.shift(1).rolling(window=window, min_periods=window).max()


def impulse_swings(df: pd.DataFrame, lookback: int = IMPULSE_LOOKBACK) -> Tuple[float, float]:
    """Swing low/high of the recent impulse leg (last `lookback` bars)."""
    window = df.iloc[-lookback:]
    return float(window["Low"].min()), float(window["High"].max())


def in_ote_zone(close: float, swing_high: float, range_: float) -> bool:
    """True when price sits inside the 50%-78.6% golden retracement zone."""
    if range_ <= 0 or not math.isfinite(close):
        return False
    retrace = (swing_high - close) / range_
    return OTE_MIN_RETRACE <= retrace <= OTE_MAX_RETRACE


def order_block_touch(df: pd.DataFrame, touch_lookback: int = 3) -> bool:
    """Bullish SMC check: did price recently tap the last demand order block?

    Order block = last bearish candle engulfed/momently consumed by a strong
    bullish candle; zone = that bearish candle's low..high.
    """
    o = df["Open"].astype(float)
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    n = len(df)
    start = max(0, n - 30)
    for j in range(n - 2, start - 1, -1):
        bearish = bool(c.iloc[j] < o.iloc[j])
        body_span = float(h.iloc[j + 1] - l.iloc[j + 1])
        strong_up = bool(c.iloc[j + 1] > o.iloc[j])
        momentum = body_span > 0 and (float(c.iloc[j + 1] - o.iloc[j + 1]) / body_span) > 0.5
        if bearish and strong_up and momentum:
            ob_low = float(l.iloc[j])
            ob_high = float(h.iloc[j])
            touched = bool(
                (l.iloc[-touch_lookback:] <= ob_high).any() and c.iloc[-1] >= ob_low
            )
            return touched
    return False


def bullish_rsi_divergence(df: pd.DataFrame, rsi_series: pd.Series, window: int = 16) -> bool:
    """Price lower-low while RSI higher-low across the last `window` bars."""
    lows = df["Low"].astype(float).iloc[-window:].reset_index(drop=True)
    rsi_w = rsi_series.iloc[-window:].reset_index(drop=True)
    if len(lows) < 6:
        return False
    i1 = int(lows.idxmin())
    rest = lows.drop(index=i1)
    if rest.empty:
        return False
    i2 = int(rest.idxmin())
    if abs(i1 - i2) < 3:
        return False
    first, second = sorted((i1, i2))
    price_ll = lows.iloc[second] < lows.iloc[first]
    rsi_hl = rsi_w.iloc[second] > rsi_w.iloc[first] + 2.0
    return bool(price_ll and rsi_hl)


def fib_targets(entry: float, swing_high: float, range_: float, atr_val: float) -> Tuple[float, float, float]:
    """Three long targets at 0.618 / 1.0 / 1.618 extensions of the impulse.

    Falls back to ATR multiples when the measured range is degenerate.
    """
    base = max(swing_high, entry)
    unit = range_ if range_ > 0 else atr_val
    if not math.isfinite(unit) or unit <= 0:
        unit = entry * 0.02
    raw = [entry + unit * m for m in FIB_EXTENSIONS]
    floor = entry * 1.005
    out = [max(t, floor) for t in raw]
    t1, t2, t3 = (round(v, 2) for v in out)
    return t1, t2, t3


class StrategyEngine:
    """Confluence scanner; refuses to evaluate non-compliant symbols."""

    def __init__(self, shariah_filter: Optional[ShariahFilter] = None) -> None:
        self._shariah = shariah_filter or ShariahFilter()

    def evaluate(self, symbol: str, df: pd.DataFrame) -> Optional[TradeSignal]:
        sym = symbol

        # GUARDRAIL: compliance gate strictly BEFORE any strategy evaluation.
        if not self._shariah.is_execution_allowed(sym):
            logger.warning(
                "[STRATEGY] %s blocked by Shariah gate BEFORE evaluation - no signal will ever be emitted",
                sym,
            )
            return None

        required = {"Open", "High", "Low", "Close", "Volume"}
        if df is None or df.empty or not required.issubset(df.columns):
            logger.warning("[STRATEGY] %s: insufficient/invalid candle data - skipped", sym)
            return None
        if len(df) < MIN_BARS:
            logger.warning("[STRATEGY] %s: only %d bars (<%d) - skipped", sym, len(df), MIN_BARS)
            return None

        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        volume = df["Volume"].astype(float)

        last_close = float(close.iloc[-1])
        if not math.isfinite(last_close) or last_close <= 0:
            logger.warning("[STRATEGY] %s: non-finite close - skipped", sym)
            return None

        don_high = float(donchian_high(high).iloc[-1])
        vol_avg = float(sma(volume, DONCHIAN_PERIOD).iloc[-1])
        last_volume = float(volume.iloc[-1])
        rsi_series = rsi(close)
        last_rsi = float(rsi_series.iloc[-1])

        checks = {
            "donchian": math.isfinite(don_high) and last_close > don_high,
            "volume": math.isfinite(vol_avg) and vol_avg > 0 and last_volume > VOLUME_SPIKE_MULT * vol_avg,
            "rsi": RSI_LOWER_BOUND < last_rsi < RSI_UPPER_BOUND,
        }
        if not all(checks.values()):
            logger.info(
                "[STRATEGY] %s confluence: close=%.2f vs %.2f->%s | vol->%s | rsi=%.1f->%s",
                sym, last_close, don_high, checks["donchian"], checks["volume"], last_rsi, checks["rsi"],
            )
            return None

        # --- Fibonacci / SMC context ---
        atr_val = atr_of(df)
        swing_low, swing_high = impulse_swings(df)
        range_ = swing_high - swing_low
        ote_ok = in_ote_zone(last_close, swing_high, range_)
        ob_hit = order_block_touch(df)
        divergence = bullish_rsi_divergence(df, rsi_series)

        tqi = TQI_BASE
        tqi += 1.0 if ote_ok else 0.0
        tqi += 1.25 if ob_hit else 0.0
        tqi += 1.25 if divergence else 0.0
        tqi = round(min(tqi, 10.0), 1)

        # TQI execution filter: accept >= 5.0 only.
        if tqi < TQI_MIN_THRESHOLD:
            logger.info("[STRATEGY] %s filtered out (TQI %.1f < %.1f)", sym, tqi, TQI_MIN_THRESHOLD)
            return None

        stop_loss = round(min(float(df["Low"].astype(float).iloc[-5:].min()) * 0.995, last_close * 0.97), 2)
        stop_loss = max(stop_loss, 0.01)
        t1, t2, t3 = fib_targets(last_close, swing_high, range_, atr_val)

        signal = TradeSignal(
            symbol=sym,
            strategy_tag="CONFLUENCE_DONCHIAN_VOL_RSI",
            entry_price=round(last_close, 2),
            stop_loss=stop_loss,
            take_profit=t3,
            target_1=t1,
            target_2=t2,
            target_3=t3,
            tqi_score=tqi,
            ote_in_zone=ote_ok,
            smc_ob_confluence=ob_hit,
            rsi_divergence=divergence,
            reason_en=(
                f"Confluence BUY: Donchian-{DONCHIAN_PERIOD} break {don_high:.2f}; vol {last_volume:.0f} "
                f"> {VOLUME_SPIKE_MULT}x avg; RSI={last_rsi:.1f}; TQI={tqi}/10 "
                f"(OTE={'Y' if ote_ok else 'N'}, OB={'Y' if ob_hit else 'N'}, Div={'Y' if divergence else 'N'})"
            ),
            reason_ar=(
                f"تقاطع شرائي: اختراق أعلى {DONCHIAN_PERIOD} يوماً؛ فوليوم مرتفع؛ "
                f"القوة النسبية {last_rsi:.1f}؛ درجة الجودة {tqi}/10"
            ),
        )
        logger.info(
            "[STRATEGY] %s FULL CONFLUENCE - BUY | TQI=%.1f (OTE=%s OB=%s DIV=%s) targets=%.2f/%.2f/%.2f",
            sym, tqi, ote_ok, ob_hit, divergence, t1, t2, t3,
        )
        return signal


def atr_of(df: pd.DataFrame, period: int = RSI_PERIOD) -> float:
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    val = float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    return val if math.isfinite(val) else float("nan")
