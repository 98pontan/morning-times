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


@dataclass(frozen=True)
class Bar:
    """One price point: a day's close (daily data) or a few minutes' close (intraday)."""
    t: datetime
    close: float
    volume: float = 0.0


class MarketData(ABC):
    @abstractmethod
    def quote(self, symbol: str) -> Quote | None: ...

    @abstractmethod
    def next_earnings_date(self, symbol: str) -> date | None: ...

    def history(self, symbols: list[str], period: str = "2y", interval: str = "1d") -> dict[str, list[Bar]]:
        """Price history per symbol, oldest first. Symbols without data are left out."""
        return {}

    def currency(self, symbol: str) -> str:
        return ""


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
        # Right after a report Yahoo still lists it, so drop dates that have passed.
        days = (d.date() if isinstance(d, datetime) else d for d in dates if d)
        upcoming = sorted(d for d in days if d >= date.today())
        return upcoming[0] if upcoming else None

    def history(self, symbols: list[str], period: str = "2y", interval: str = "1d") -> dict[str, list[Bar]]:
        import yfinance as yf

        if not symbols:
            return {}
        # One request for all symbols; group_by="ticker" gives a (symbol, field) column index.
        frame = yf.download(symbols, period=period, interval=interval, auto_adjust=False,
                            progress=False, group_by="ticker", threads=True)
        result: dict[str, list[Bar]] = {}
        for symbol in symbols:
            if symbol not in frame.columns.get_level_values(0):
                continue
            rows = frame[symbol].dropna(subset=["Close"])
            bars = [
                Bar(t=ts.to_pydatetime(), close=float(row["Close"]),
                    volume=float(row["Volume"]) if row["Volume"] == row["Volume"] else 0.0)  # NaN check
                for ts, row in rows.iterrows()
            ]
            if bars:
                result[symbol] = bars
        return result

    def currency(self, symbol: str) -> str:
        import yfinance as yf

        try:
            return yf.Ticker(symbol).fast_info.get("currency", "") or ""
        except Exception:
            return ""
