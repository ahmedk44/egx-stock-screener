"""Market breadth & heavyweight-pressure analyzer.

Sentiment/threat model:
  - COMI.CA (largest EGX heavyweight) drop <= -2.5%  -> HIGH threat, buys blocked.
  - Breadth < 35% advancing                          -> MEDIUM threat warning.
  - Breadth > 65% advancing                          -> BULLISH.
"""

from __future__ import annotations

import logging
from typing import Dict

from egx_quant.database.models import (
    MarketSentiment,
    MarketState,
    PriceQuote,
    ThreatLevel,
)
from egx_quant.config.stocks_registry import StocksRegistry
from egx_quant.utils.egx_calendar import now_cairo

logger = logging.getLogger("egx_quant.breadth")

HEAVYWEIGHT_WEIGHTS: Dict[str, float] = {
    "COMI.CA": 0.28,
    "SWDY.CA": 0.12,
    "ABUK.CA": 0.08,
    "TMGH.CA": 0.07,
    "ETEL.CA": 0.06,
}

COMI_DROP_BLOCK_PCT: float = -2.5
BREADTH_BEARISH_RATIO: float = 0.35
BREADTH_BULLISH_RATIO: float = 0.65


class MarketBreadthAnalyzer:
    """Computes market-wide sentiment from full-universe quotes held in RAM."""

    def analyze(self, quotes: Dict[str, PriceQuote]) -> MarketState:
        advancers = decliners = unchanged = 0
        for quote in quotes.values():
            pct = quote.change_pct
            if pct is None:
                unchanged += 1
            elif pct > 0:
                advancers += 1
            elif pct < 0:
                decliners += 1
            else:
                unchanged += 1

        directional = advancers + decliners
        adv_ratio = (advancers / directional) if directional else 0.5

        comi = quotes.get("COMI.CA")
        comi_price = comi.price if comi else None
        comi_pct = comi.change_pct if comi else None

        sentiment = MarketSentiment.NEUTRAL
        threat = ThreatLevel.LOW
        buy_open = True
        note_en = "Neutral breadth"
        note_ar = "اتساع سوق محايد"

        if comi_pct is not None and comi_pct <= COMI_DROP_BLOCK_PCT:
            threat = ThreatLevel.HIGH
            sentiment = MarketSentiment.BEARISH
            buy_open = False
            note_en = f"COMI heavy sell-off {comi_pct}% - new buys blocked market-wide"
            note_ar = f"ضغط بيعي ثقيل على كومي {comi_pct}% - تم إيقاف مشتريات جديدة في السوق"
        elif adv_ratio < BREADTH_BEARISH_RATIO and decliners >= advancers:
            threat = ThreatLevel.MEDIUM
            sentiment = MarketSentiment.BEARISH
            note_en = f"Bearish breadth ({advancers}/{directional} advancing) - caution on new longs"
            note_ar = f"اتساع هابط ({advancers} من {directional} صاعدة) - حذر من المراكز الجديدة"
        elif adv_ratio > BREADTH_BULLISH_RATIO:
            sentiment = MarketSentiment.BULLISH
            note_en = f"Bullish breadth ({advancers}/{directional} advancing)"
            note_ar = f"اتساع صاعد ({advancers} من {directional} صاعدة)"

        index_status = (
            f"{sentiment.value} | ADV={advancers}/DEC={decliners}/UNCH={unchanged}"
            + (f" | COMI={comi_pct:+.2f}%" if comi_pct is not None else "")
        )

        state = MarketState(
            timestamp=now_cairo(),
            sentiment=sentiment,
            threat_level=threat,
            comi_price=comi_price,
            comi_change_pct=comi_pct,
            index_status=index_status,
            advancers=advancers,
            decliners=decliners,
            unchanged=unchanged,
            buy_window_open=buy_open,
            note_en=note_en,
            note_ar=note_ar,
        )
        logger.info(
            "[BREADTH] %s | threat=%s | buy_window=%s",
            index_status,
            threat.value,
            "OPEN" if buy_open else "CLOSED",
        )
        return state
