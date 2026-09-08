"""EGX stock registry with Shariah compliance classification.

Single source of truth for tradable universe metadata: symbol, Arabic/English
name, sector, and Shariah status. The analytical engine may scan ALL entries
for market breadth; the execution engine must ONLY trade COMPLIANT symbols.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class ShariahStatus(str, Enum):
    """Shariah classification of a listed instrument."""

    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True)
class StockMeta:
    """Immutable metadata record for one EGX listing."""

    symbol: str
    name_ar: str
    name_en: str
    sector: str
    shariah_status: ShariahStatus


def _normalize_symbol(symbol: str) -> str:
    """Normalize an EGX ticker to upper-case with the .CA suffix (COMI -> COMI.CA)."""
    t = str(symbol or "").strip().upper()
    if t and not t.endswith(".CA"):
        t = f"{t}.CA"
    return t


_STOCKS: List[StockMeta] = [
    StockMeta("COMI.CA", "البنك التجاري الدولي", "Commercial International Bank", "Banking", ShariahStatus.COMPLIANT),
    StockMeta("ADIB.CA", "بنك أبوظبي الإسلامي - مصر", "Abu Dhabi Islamic Bank Egypt", "Banking", ShariahStatus.COMPLIANT),
    StockMeta("SAUD.CA", "البركة بنك مصر", "Al Baraka Bank Egypt", "Banking", ShariahStatus.COMPLIANT),
    StockMeta("EGBE.CA", "بنك تنمية الصادرات", "Export Development Bank of Egypt", "Banking", ShariahStatus.NEEDS_REVIEW),
    StockMeta("HELI.CA", "هليوبوليس للإسكان والتعمير", "Heliopolis Housing", "Real Estate", ShariahStatus.COMPLIANT),
    StockMeta("TMGH.CA", "طلعت مصطفى - تطوير عقاري", "Talaat Moustafa Group", "Real Estate", ShariahStatus.NEEDS_REVIEW),
    StockMeta("EMFD.CA", "عمار مصر", "Emaar Misr Development", "Real Estate", ShariahStatus.COMPLIANT),
    StockMeta("PHDC.CA", "بالم هيلز للتطوير العقاري", "Palm Hills Developments", "Real Estate", ShariahStatus.NEEDS_REVIEW),
    StockMeta("MFPC.CA", "مصر للأسمدة - موبكو", "MOPCO (Misr Fertilizers)", "Fertilizers", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ORHD.CA", "أوراسكوم للتطوير والعمران", "Orascom Development Egypt", "Real Estate", ShariahStatus.NEEDS_REVIEW),
    StockMeta("SWDY.CA", "السويدي إلكتريك", "Elsewedy Electric", "Industrials", ShariahStatus.COMPLIANT),
    StockMeta("ORAS.CA", "أوراسكوم للإنشاء والتطوير", "Orascom Construction", "Construction", ShariahStatus.COMPLIANT),
    StockMeta("ABUK.CA", "أبو قير للأسمدة", "Abu Qir Fertilizers", "Fertilizers", ShariahStatus.COMPLIANT),
    StockMeta("SKPC.CA", "سيد كير للبتروكيماويات", "Sidi Kerir Petrochemicals", "Petrochemicals", ShariahStatus.NEEDS_REVIEW),
    StockMeta("AMOC.CA", "الإسكندرية للزيوت المعدنية", "Alexandria Mineral Oils", "Oil & Gas", ShariahStatus.NON_COMPLIANT),
    StockMeta("EFID.CA", "إيديتا للصناعات الغذائية", "Edita Food Industries", "Food & Beverage", ShariahStatus.COMPLIANT),
    StockMeta("ESRS.CA", "عز لصناعة الصلب", "Ezz Steel", "Steel", ShariahStatus.NON_COMPLIANT),
    StockMeta("ETEL.CA", "المصرية للاتصالات", "Telecom Egypt", "Telecom", ShariahStatus.NEEDS_REVIEW),
    StockMeta("FWRY.CA", "فوري للخدمات المصرفية والتكنولوجيا", "Fawry Banking Technology", "Technology", ShariahStatus.COMPLIANT),
    StockMeta("RAYA.CA", "راية القابضة للاستثمار", "Raya Holding", "Technology", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ISPH.CA", "ابن سينا فارما", "Ibnsina Pharma", "Pharmaceuticals", ShariahStatus.NEEDS_REVIEW),
    StockMeta("CLHO.CA", "مستشفيات كليوباترا", "Cleopatra Hospitals", "Healthcare", ShariahStatus.COMPLIANT),
    StockMeta("EAST.CA", "الشرقية للدخان", "Eastern Company", "Tobacco", ShariahStatus.NON_COMPLIANT),
    StockMeta("DOMT.CA", "دومتي للصناعات الغذائية", "Arabian Food Industries (Domty)", "Food & Beverage", ShariahStatus.COMPLIANT),
    StockMeta("JUFO.CA", "جهينة للصناعات الغذائية", "Juhayna Food Industries", "Food & Beverage", ShariahStatus.NEEDS_REVIEW),
    StockMeta("SUGR.CA", "دلتا للسكر", "Delta Sugar", "Food & Beverage", ShariahStatus.COMPLIANT),
    # --- Expanded 2026-09-08: liquid mid-caps from the news universe (main.TICKERS).
    # Shariah flags follow the team's own EGX33 list (FAIT/ORWE/EFIH COMPLIANT);
    # CERA/MCRO are NEEDS_REVIEW (not vetted yet - shown honestly on cards).
    StockMeta("CERA.CA", "سيراميكا ريماس", "Ceramica Remas", "Industrials", ShariahStatus.NEEDS_REVIEW),
    StockMeta("MCRO.CA", "ماكرو جروب للأدوية", "Macro Group Pharmaceuticals", "Pharmaceuticals", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ORWE.CA", "الشرقية للسجاد", "Oriental Weavers", "Industrials", ShariahStatus.COMPLIANT),
    StockMeta("FAIT.CA", "بنك فيصل الإسلامي", "Faisal Islamic Bank", "Banking", ShariahStatus.COMPLIANT),
    StockMeta("EFIH.CA", "إي فاينانس للاستثمارات المالية والرقمنة", "e-finance", "Technology", ShariahStatus.COMPLIANT),
    # --- Expanded 2026-09-09: full EGX30 coverage + liquid EGX70 leaders.
    # Shariah flags follow the team's own EGX33 list (main.EGX33_SHARIAH_TICKERS);
    # HRHO/QNBA/ALCN/BINV/EKHO/LCSW are NEEDS_REVIEW (not vetted yet).
    StockMeta("HRHO.CA", "إي إف جي القابضة", "EFG Holding", "Financial Services", ShariahStatus.NEEDS_REVIEW),
    StockMeta("QNBA.CA", "بنك قطر الوطني الأهلي", "QNB Alahli", "Banking", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ALCN.CA", "الإسكندرية لتداول الحاويات", "Alexandria Containers", "Industrials", ShariahStatus.NEEDS_REVIEW),
    StockMeta("OLFI.CA", "عبور لاند للصناعات الغذائية", "Obour Land Foods", "Food & Beverage", ShariahStatus.COMPLIANT),
    StockMeta("BINV.CA", "بي إنفستمنتس القابضة", "B Investments Holding", "Financial Services", ShariahStatus.NEEDS_REVIEW),
    StockMeta("EKHO.CA", "القابضة المصرية الكويتية", "Egyptian Kuwaiti Holding", "Conglomerate", ShariahStatus.NEEDS_REVIEW),
    StockMeta("LCSW.CA", "ليسيكو مصر", "Lecico Egypt", "Industrials", ShariahStatus.NEEDS_REVIEW),
    # --- Expanded 2026-09-09: full EGX30 + liquid EGX70 coverage (54 total).
    # Tier A (main.py team universe, traded daily): bare-ticker names (no
    # fabricated company data), NEEDS_REVIEW, neutral "General" sector.
    StockMeta("ATQA.CA", "ATQA", "ATQA", "General", ShariahStatus.NEEDS_REVIEW),
    StockMeta("EALR.CA", "EALR", "EALR", "General", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ELWA.CA", "ELWA", "ELWA", "General", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ETRS.CA", "ETRS", "ETRS", "General", ShariahStatus.NEEDS_REVIEW),
    StockMeta("GBCO.CA", "GBCO", "GBCO", "General", ShariahStatus.NEEDS_REVIEW),
    # Tier B (liquid EGX70 leaders, best-effort symbols): validated live by the
    # liquidity prescreen each cycle; dead symbols are auto-skipped + logged.
    # Send verified names/sectors to upgrade any entry from bare ticker.
    StockMeta("MASR.CA", "MASR", "MASR", "Real Estate", ShariahStatus.NEEDS_REVIEW),
    StockMeta("CIEB.CA", "CIEB", "CIEB", "Banking", ShariahStatus.NEEDS_REVIEW),
    StockMeta("HDBK.CA", "HDBK", "HDBK", "Banking", ShariahStatus.NEEDS_REVIEW),
    StockMeta("TAQA.CA", "TAQA", "TAQA", "Oil & Gas", ShariahStatus.NEEDS_REVIEW),
    StockMeta("AJWA.CA", "AJWA", "AJWA", "Food & Beverage", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ACGC.CA", "ACGC", "ACGC", "Industrials", ShariahStatus.NEEDS_REVIEW),
    StockMeta("DICE.CA", "DICE", "DICE", "Industrials", ShariahStatus.NEEDS_REVIEW),
    StockMeta("OCDI.CA", "OCDI", "OCDI", "Real Estate", ShariahStatus.NEEDS_REVIEW),
    StockMeta("AMER.CA", "AMER", "AMER", "Real Estate", ShariahStatus.NEEDS_REVIEW),
    StockMeta("ACTF.CA", "ACTF", "ACTF", "Financial Services", ShariahStatus.NEEDS_REVIEW),
    StockMeta("CCAP.CA", "CCAP", "CCAP", "Conglomerate", ShariahStatus.NEEDS_REVIEW),
]

_REGISTRY: Dict[str, StockMeta] = {meta.symbol: meta for meta in _STOCKS}


class StocksRegistry:
    """Read-only access helpers over the static EGX universe definition."""

    @staticmethod
    def all_symbols() -> List[str]:
        return list(_REGISTRY.keys())

    @staticmethod
    def all_stocks() -> List[StockMeta]:
        return list(_STOCKS)

    @staticmethod
    def get(symbol: str) -> Optional[StockMeta]:
        return _REGISTRY.get(_normalize_symbol(symbol))

    @staticmethod
    def status(symbol: str) -> ShariahStatus:
        meta = StocksRegistry.get(symbol)
        return meta.shariah_status if meta else ShariahStatus.NEEDS_REVIEW

    @staticmethod
    def compliant_symbols() -> List[str]:
        return [m.symbol for m in _STOCKS if m.shariah_status is ShariahStatus.COMPLIANT]

    @staticmethod
    def by_sector(sector: str) -> List[StockMeta]:
        return [m for m in _STOCKS if m.sector.lower() == sector.lower()]

    @staticmethod
    def normalize(symbol: str) -> str:
        return _normalize_symbol(symbol)


HEAVYWEIGHT_SYMBOLS: tuple = ("COMI.CA", "SWDY.CA", "ABUK.CA", "TMGH.CA", "ETEL.CA")
