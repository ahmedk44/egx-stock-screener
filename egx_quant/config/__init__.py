"""Configuration layer: stock registry and Shariah classification metadata."""

from egx_quant.config.stocks_registry import (
    ShariahStatus,
    StockMeta,
    StocksRegistry,
)

__all__ = ["ShariahStatus", "StockMeta", "StocksRegistry"]
