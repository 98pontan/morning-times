"""Stock screen data: the watched stocks of an economy section, plus a reference index (default OMXS30).

Yahoo Finance is slow-ish and rate limited, so everything is cached in memory for a few minutes.
Prices are delayed about 15 minutes anyway.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src.db.models import Section
from src.sections.market import Bar, MarketData, YFinanceMarketData

log = logging.getLogger(__name__)

DEFAULT_INDEX = ("^OMX", "OMX Stockholm 30")

# Chart ranges: (yfinance period, interval). "ytd" is cut from the 1y series.
RANGES: dict[str, tuple[str, str]] = {
    "1d": ("1d", "5m"),
    "1w": ("5d", "30m"),
    "1m": ("1mo", "1d"),
    "3m": ("3mo", "1d"),
    "ytd": ("ytd", "1d"),
    "1y": ("1y", "1d"),
    "5y": ("5y", "1wk"),
}

# Look-back periods for the return columns, in calendar days ("ytd" is handled separately).
RETURN_DAYS = {"1w": 7, "1m": 30, "3m": 91, "1y": 365}
SPARK_DAYS = 91


@dataclass
class StockRow:
    name: str
    symbol: str
    watch_item_id: int | None = None
    currency: str = ""
    last: float | None = None
    change: float | None = None          # vs previous close, in currency
    change_pct: float | None = None
    returns: dict[str, float | None] = field(default_factory=dict)  # 1w, 1m, 3m, ytd, 1y in %
    high_52w: float | None = None
    low_52w: float | None = None
    volume: float | None = None          # today's shares traded
    turnover: float | None = None        # volume x last, in currency
    earnings_date: date | None = None
    as_of: datetime | None = None
    spark: list[float] = field(default_factory=list)
    error: str | None = None


@dataclass
class MarketOverview:
    index: StockRow | None
    stocks: list[StockRow]
    updated_at: datetime


class _TTLCache:
    def __init__(self):
        self._data: dict[object, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key, ttl: float, load: Callable[[], object]):
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = load()
        with self._lock:
            self._data[key] = (now, value)
        return value

    def __contains__(self, key) -> bool:
        return key in self._data

    def peek(self, key, default=None):
        """The cached value regardless of age, without loading."""
        hit = self._data.get(key)
        return hit[1] if hit else default


def _pct(now: float, then: float | None) -> float | None:
    return (now - then) / then * 100 if then else None


def _close_on_or_before(bars: list[Bar], day: date) -> float | None:
    """Close of the last bar on or before `day` (bars are oldest first)."""
    found = None
    for bar in bars:
        if bar.t.date() > day:
            break
        found = bar.close
    return found


def build_row(name: str, symbol: str, bars: list[Bar]) -> StockRow:
    """Turn daily bars (oldest first, about 2 years) into one screen row."""
    row = StockRow(name=name, symbol=symbol)
    if not bars:
        row.error = "Ingen kursdata"
        return row
    last = bars[-1]
    today = last.t.date()
    row.last, row.as_of = last.close, last.t
    if len(bars) >= 2:
        prev = bars[-2].close
        row.change, row.change_pct = last.close - prev, _pct(last.close, prev)
    row.returns = {k: _pct(last.close, _close_on_or_before(bars, today - timedelta(days=d)))
                   for k, d in RETURN_DAYS.items()}
    row.returns["ytd"] = _pct(last.close, _close_on_or_before(bars, date(today.year - 1, 12, 31)))
    year = [b.close for b in bars if b.t.date() > today - timedelta(days=365)]
    row.high_52w, row.low_52w = max(year), min(year)
    if last.volume:
        row.volume, row.turnover = last.volume, last.volume * last.close
    row.spark = [round(b.close, 4) for b in bars if b.t.date() > today - timedelta(days=SPARK_DAYS)]
    return row


class MarketService:
    def __init__(self, market: MarketData | None = None, ttl_seconds: float = 120):
        self.market = market or YFinanceMarketData()
        self.ttl = ttl_seconds
        self._cache = _TTLCache()

    @staticmethod
    def index_of(section: Section) -> tuple[str, str] | None:
        """The section's reference index; settings.index_symbol = "" turns it off."""
        symbol = section.settings.get("index_symbol", DEFAULT_INDEX[0])
        if not symbol:
            return None
        name = section.settings.get("index_name") or (DEFAULT_INDEX[1] if symbol == DEFAULT_INDEX[0] else symbol)
        return symbol, name

    def overview(self, section: Section) -> MarketOverview:
        stocks = [w for w in section.watch_items if w.enabled and w.type == "stock" and w.symbol]
        index = self.index_of(section)
        symbols = sorted({w.symbol for w in stocks} | ({index[0]} if index else set()))

        history = self._cache.get(("daily", tuple(symbols)), self.ttl, lambda: self._history(symbols, "2y", "1d"))
        details = self._details([w.symbol for w in stocks])

        rows = []
        for item in stocks:
            row = build_row(item.name, item.symbol, history.get(item.symbol, []))
            row.watch_item_id = item.id
            row.currency, row.earnings_date = details.get(item.symbol, ("", None))
            rows.append(row)
        index_row = build_row(index[1], index[0], history.get(index[0], [])) if index else None
        return MarketOverview(index=index_row, stocks=rows, updated_at=datetime.now().astimezone())

    def chart(self, symbol: str, range_: str) -> list[Bar]:
        period, interval = RANGES[range_]
        ttl = self.ttl if interval.endswith("m") else max(self.ttl, 600)
        return self._cache.get(("chart", symbol, range_), ttl,
                               lambda: self._history([symbol], period, interval).get(symbol, []))

    def _history(self, symbols: list[str], period: str, interval: str) -> dict[str, list[Bar]]:
        try:
            return self.market.history(symbols, period, interval)
        except Exception:
            log.warning("Price history failed for %s", symbols, exc_info=True)
            return {}

    def _details(self, symbols: list[str]) -> dict[str, tuple[str, date | None]]:
        """Currency and next report date per stock: one Yahoo call each, so cached for hours."""
        def load(symbol: str) -> tuple[str, date | None]:
            return self.market.currency(symbol), self.market.next_earnings_date(symbol)

        def cached(symbol: str):
            try:  # a failure is not cached, so the next request tries again
                return symbol, self._cache.get(("details", symbol), 6 * 3600, lambda: load(symbol))
            except Exception:
                log.warning("Details failed for %s", symbol, exc_info=True)
                return symbol, ("", None)

        with ThreadPoolExecutor(max_workers=8) as pool:
            return dict(pool.map(cached, symbols))
