"""Section kinds. Add a new kind by subclassing SectionPlugin and adding it to PLUGINS."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from src.db.models import Section, WatchItem
from src.sections.base import AlertCandidate, SectionPlugin
from src.sections.market import MarketData, YFinanceMarketData
from src.sections.weather import (
    DEFAULT_ALERT_LEVELS, LEVELS, WARNING_AREAS_BY_ID, SmhiWeatherData, Warning, WeatherData, local_text,
)

log = logging.getLogger(__name__)


class GeneralSection(SectionPlugin):
    kind = "general"
    description = "General news (world, politics, Sweden). Optional keywords to highlight."


class RegionalSection(SectionPlugin):
    kind = "regional"
    description = "Local news. Watch regions/cities, e.g. Stockholm, Göteborg, Norrland."
    item_types = frozenset({"region", "keyword"})


class SportsSection(SectionPlugin):
    kind = "sports"
    description = "Sports. Watch teams (symbol = sport/league) and keywords."
    item_types = frozenset({"team", "keyword"})

    def describe_item(self, item: WatchItem) -> str:
        return f"{item.name} ({item.symbol})" if item.symbol else item.name


class EconomySection(SectionPlugin):
    """Watches stocks: price moves above a threshold and upcoming quarterly reports.

    Section settings: move_threshold_pct (default 5), earnings_notice_days (default 1).
    A stock's own settings.move_threshold_pct overrides the section default.
    """

    kind = "economy"
    description = "Economy and markets. Watch stocks (symbol = Yahoo ticker, e.g. VOLV-B.ST)."
    item_types = frozenset({"stock", "keyword"})

    def __init__(self, market: MarketData | None = None, today=date.today):
        self.market = market or YFinanceMarketData()
        self.today = today

    def terms(self, item: WatchItem) -> list[str]:
        terms = super().terms(item)
        if item.type == "stock" and item.symbol:
            terms.append(item.symbol.split(".")[0])  # "VOLV-B.ST" -> "VOLV-B"
        return terms

    def check_alerts(self, section: Section, items: list[WatchItem]) -> list[AlertCandidate]:
        default_threshold = float(section.settings.get("move_threshold_pct", 5))
        notice_days = int(section.settings.get("earnings_notice_days", 1))
        today = self.today()
        alerts: list[AlertCandidate] = []
        for item in items:
            if not item.enabled or item.type != "stock" or not item.symbol:
                continue
            try:
                alerts += self._price_alert(item, today, float(item.settings.get("move_threshold_pct", default_threshold)))
                alerts += self._earnings_alert(item, today, notice_days)
            except Exception:
                log.warning("Market check failed for %s", item.symbol, exc_info=True)
        return alerts

    def _price_alert(self, item: WatchItem, today: date, threshold: float) -> list[AlertCandidate]:
        quote = self.market.quote(item.symbol)
        if not quote or abs(quote.change_pct) < threshold:
            return []
        direction = "up" if quote.change_pct > 0 else "down"
        return [AlertCandidate(
            dedup_key=f"move:{item.symbol}:{today.isoformat()}:{direction}",
            kind="stock_move",
            subject=f"{item.name} {quote.change_pct:+.1f}% today",
            body=(
                f"{item.name} ({item.symbol}) is {direction} {abs(quote.change_pct):.1f}% "
                f"({quote.previous_close:.2f} -> {quote.price:.2f} {quote.currency}).\n"
                f"Alert threshold: {threshold:.1f}%."
            ),
            watch_item_id=item.id,
        )]

    def _earnings_alert(self, item: WatchItem, today: date, notice_days: int) -> list[AlertCandidate]:
        report_date = self.market.next_earnings_date(item.symbol)
        if not report_date or not 0 <= (report_date - today).days <= notice_days:
            return []
        when = "today" if report_date == today else f"on {report_date.isoformat()}"
        return [AlertCandidate(
            dedup_key=f"earnings:{item.symbol}:{report_date.isoformat()}",
            kind="earnings",
            subject=f"{item.name} reports {when}",
            body=f"{item.name} ({item.symbol}) publishes its quarterly report {when}.",
            watch_item_id=item.id,
        )]


SMHI_WARNINGS_PAGE = "https://www.smhi.se/vader/varningar-och-brandrisk/varningar-och-meddelanden"


class WeatherSection(SectionPlugin):
    """Weather: SMHI warnings for chosen counties/sea districts, and forecasts for chosen places.

    Watch types:
      warning_area  symbol = SMHI area id (GET /api/sections/{id}/warning-areas lists them);
                    settings.levels = which levels to email, e.g. ["YELLOW", "ORANGE"]
      place         symbol = "lat,lon"; shown with a forecast on the weather page and in the brief
    Section settings: warning_levels = default levels for areas without their own (YELLOW, ORANGE, RED).
    """

    kind = "weather"
    description = "Weather. SMHI warnings for chosen areas (warning_area) and forecasts for places (place)."
    item_types = frozenset({"warning_area", "place", "keyword"})

    def __init__(self, weather: WeatherData | None = None, now=lambda: datetime.now(timezone.utc)):
        self.weather = weather or SmhiWeatherData()
        self.now = now

    def terms(self, item: WatchItem) -> list[str]:
        # "Stockholms län" as a keyword would miss most articles; match places and keywords only.
        return [] if item.type == "warning_area" else super().terms(item)

    @staticmethod
    def alert_levels(section: Section, item: WatchItem) -> list[str]:
        levels = item.settings.get("levels") or section.settings.get("warning_levels") or DEFAULT_ALERT_LEVELS
        return [lv for lv in levels if lv in LEVELS]

    def check_alerts(self, section: Section, items: list[WatchItem]) -> list[AlertCandidate]:
        areas = {int(i.symbol): i for i in items
                 if i.enabled and i.type == "warning_area" and (i.symbol or "").isdigit()}
        if not areas:
            return []
        try:
            warnings = self.weather.warnings()
        except Exception:
            log.warning("Fetching SMHI warnings failed", exc_info=True)
            return []
        now = self.now()
        alerts: dict[str, AlertCandidate] = {}
        for warning in warnings:
            if warning.end and warning.end < now:
                continue
            for area_id in warning.area_ids:
                item = areas.get(area_id)
                if item is None or warning.level not in self.alert_levels(section, item):
                    continue
                # Keyed on level too: an upgrade from yellow to orange is a new alert.
                key = f"smhi:{warning.id}:{warning.level}"
                alerts.setdefault(key, self._alert(key, warning, item))
        return list(alerts.values())

    @staticmethod
    def _alert(key: str, warning: Warning, item: WatchItem) -> AlertCandidate:
        counties = ", ".join(WARNING_AREAS_BY_ID[a].name for a in warning.area_ids if a in WARNING_AREAS_BY_ID)
        lines = [
            f"{warning.level_name}: {warning.event} – {warning.area_name}",
            f"Gäller {local_text(warning.start)} – {local_text(warning.end)} (svensk tid). Berör: {counties or item.name}.",
            *(f"{title}: {text}" for title, text in warning.descriptions.items()),
            SMHI_WARNINGS_PAGE,
        ]
        return AlertCandidate(
            dedup_key=key,
            kind="weather_warning",
            subject=f"{warning.level_name} från SMHI: {warning.event}, {warning.area_name}"[:512],
            body="\n".join(lines),
            watch_item_id=item.id,
        )


class EnergySection(SectionPlugin):
    """Electricity: spot prices per bidding zone, production, nuclear status and a weekly price outlook.

    Section settings: zone = the reader's bidding zone, SE1-SE4 (default SE3). Keywords highlight news.
    """

    kind = "energy"
    description = "Electricity. Spot prices (settings.zone = SE1-SE4), production, nuclear status, weekly outlook."


PLUGINS: dict[str, SectionPlugin] = {
    p.kind: p for p in [GeneralSection(), RegionalSection(), SportsSection(), EconomySection(), WeatherSection(),
                        EnergySection()]
}


def get_plugin(kind: str) -> SectionPlugin:
    try:
        return PLUGINS[kind]
    except KeyError:
        raise ValueError(f"Unknown section kind '{kind}'. Available: {sorted(PLUGINS)}") from None


__all__ = ["AlertCandidate", "SectionPlugin", "PLUGINS", "get_plugin", "EconomySection", "WeatherSection", "EnergySection"]
