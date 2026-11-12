"""Weather page and brief data: SMHI warnings for the watched areas plus forecasts for the watched places.

SMHI updates forecasts about once an hour and warnings a few times a day, so results are cached.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from src.db.models import Section
from src.sections.weather import (
    LOCAL_TZ, WARNING_AREAS_BY_ID, ForecastStep, SmhiWeatherData, Warning, WeatherData, parse_position,
)
from src.services.market import _TTLCache

log = logging.getLogger(__name__)

HOURS = 24
DAYS = 7


@dataclass
class DayForecast:
    date: date
    min_temp: float | None
    max_temp: float | None
    precipitation: float          # mm, whole day
    symbol: int | None            # the step closest to 12:00
    symbol_text: str = ""
    max_wind: float | None = None


@dataclass
class PlaceForecast:
    name: str
    lat: float
    lon: float
    watch_item_id: int | None = None
    now: ForecastStep | None = None
    hours: list[ForecastStep] = field(default_factory=list)
    days: list[DayForecast] = field(default_factory=list)
    error: str | None = None


@dataclass
class WarningView:
    warning: Warning
    areas: list[str]              # watched counties/sea districts it touches
    alerting: bool                # at a level that is emailed for at least one of those areas


@dataclass
class WeatherOverview:
    places: list[PlaceForecast]
    warnings: list[WarningView]
    updated_at: datetime
    error: str | None = None


def summarize_days(steps: list[ForecastStep], days: int = DAYS) -> list[DayForecast]:
    by_day: dict[date, list[ForecastStep]] = {}
    for step in steps:
        by_day.setdefault(step.time.astimezone(LOCAL_TZ).date(), []).append(step)
    result = []
    for day, day_steps in sorted(by_day.items())[:days]:
        temps = [s.temperature for s in day_steps if s.temperature is not None]
        winds = [s.wind_speed for s in day_steps if s.wind_speed is not None]
        noon = min(day_steps, key=lambda s: abs(s.time.astimezone(LOCAL_TZ).hour - 12))
        result.append(DayForecast(
            date=day,
            min_temp=min(temps) if temps else None,
            max_temp=max(temps) if temps else None,
            precipitation=round(sum(s.precipitation or 0 for s in day_steps), 1),
            symbol=noon.symbol, symbol_text=noon.symbol_text,
            max_wind=max(winds) if winds else None,
        ))
    return result


class WeatherService:
    def __init__(self, weather: WeatherData | None = None, ttl_seconds: float = 600, now=None):
        self.weather = weather or SmhiWeatherData()
        self.ttl = ttl_seconds
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._cache = _TTLCache()

    def overview(self, section: Section) -> WeatherOverview:
        from src.sections import WeatherSection

        items = [w for w in section.watch_items if w.enabled]
        places = [self._place(w.name, w.symbol, w.id) for w in items if w.type == "place"]

        areas = {int(w.symbol): w for w in items if w.type == "warning_area" and (w.symbol or "").isdigit()}
        views, error = [], None
        if areas:
            try:
                warnings = self._cache.get("warnings", min(self.ttl, 300), self.weather.warnings)
            except Exception as exc:
                log.warning("Fetching SMHI warnings failed", exc_info=True)
                warnings, error = [], f"SMHI-varningar kunde inte hämtas: {exc}"
            now = self.now()
            for w in warnings:
                touched = [areas[a] for a in w.area_ids if a in areas]
                if not touched or (w.end and w.end < now):
                    continue
                views.append(WarningView(
                    warning=w,
                    areas=[WARNING_AREAS_BY_ID[int(i.symbol)].name for i in touched],
                    alerting=any(w.level in WeatherSection.alert_levels(section, i) for i in touched),
                ))
            views.sort(key=lambda v: (-v.warning.rank, v.warning.start or now))
        return WeatherOverview(places=places, warnings=views, updated_at=datetime.now().astimezone(), error=error)

    def _place(self, name: str, symbol: str | None, item_id: int | None) -> PlaceForecast:
        position = parse_position(symbol)
        if position is None:
            return PlaceForecast(name=name, lat=0, lon=0, watch_item_id=item_id,
                                 error='Ange position som "lat,lon", t.ex. 59.3293,18.0686')
        lat, lon = position
        place = PlaceForecast(name=name, lat=lat, lon=lon, watch_item_id=item_id)
        try:
            steps = self._cache.get(("forecast", round(lat, 3), round(lon, 3)), self.ttl,
                                    lambda: self.weather.forecast(lat, lon))
        except Exception as exc:
            log.warning("Forecast for %s failed", name, exc_info=True)
            place.error = f"Prognosen kunde inte hämtas: {exc}"
            return place
        now = self.now()
        upcoming = [s for s in steps if s.time >= now.replace(minute=0, second=0, microsecond=0)]
        place.now = upcoming[0] if upcoming else None
        place.hours = [s for s in upcoming if s.hours <= 1][:HOURS]
        place.days = summarize_days(steps)
        return place

    def brief_snapshot(self, sections: list[Section]) -> dict | None:
        """Today's weather for the morning brief (JSON-friendly), or None without weather sections."""
        sections = [s for s in sections if s.kind == "weather" and s.enabled]
        if not sections:
            return None
        places, warnings, seen = [], [], set()
        for section in sections:
            overview = self.overview(section)
            for p in overview.places:
                today = p.days[0] if p.days else None
                places.append({
                    "name": p.name, "error": p.error,
                    "now_temp": p.now.temperature if p.now else None,
                    "symbol_text": today.symbol_text if today else "",
                    "min_temp": today.min_temp if today else None,
                    "max_temp": today.max_temp if today else None,
                    "precipitation": today.precipitation if today else None,
                    "max_wind": today.max_wind if today else None,
                })
            for v in overview.warnings:
                if v.warning.id in seen:
                    continue
                seen.add(v.warning.id)
                warnings.append({
                    "level": v.warning.level, "level_name": v.warning.level_name, "event": v.warning.event,
                    "area_name": v.warning.area_name, "areas": v.areas,
                    "start": v.warning.start.isoformat() if v.warning.start else None,
                    "end": v.warning.end.isoformat() if v.warning.end else None,
                })
        return {"places": places, "warnings": warnings}
