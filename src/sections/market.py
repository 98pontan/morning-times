"""Market data for the economy section. Swap YFinanceMarketData for another provider if needed."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    previous_close: float
    currency: str = ""

    @property
    def change_pct(self) -> float:
        return (self.price - self.previous_close) / self.previous_close * 100 if self.previous_close else 0.0


class MarketData(ABC):
    @abstractmethod
    def quote(self, symbol: str) -> Quote | None: ...

    @abstractmethod
    def next_earnings_date(self, symbol: str) -> date | None: ...


class YFinanceMarketData(MarketData):
    """Yahoo Finance via yfinance. Unofficial, free, delayed ~15 min; good enough for alerts."""

    def quote(self, symbol: str) -> Quote | None:
        import yfinance as yf

        history = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
        closes = history["Close"].dropna() if "Close" in history else []
        if len(closes) < 2:
            log.warning("Not enough price data for %s", symbol)
            return None
        currency = ""
        try:
            currency = yf.Ticker(symbol).fast_info.get("currency", "") or ""
        except Exception:
            pass
        return Quote(symbol, float(closes.iloc[-1]), float(closes.iloc[-2]), currency)

    def next_earnings_date(self, symbol: str) -> date | None:
        import yfinance as yf

        try:
            calendar = yf.Ticker(symbol).calendar or {}
        except Exception:
            log.warning("No earnings calendar for %s", symbol, exc_info=True)
            return None
        dates = calendar.get("Earnings Date") or []
        upcoming = sorted(d.date() if isinstance(d, datetime) else d for d in dates if d)
        return upcoming[0] if upcoming else None
