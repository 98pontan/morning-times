"""SMHI open data for the weather section: impact-based warnings and point forecasts.

Both APIs are free and need no key (https://opendata.smhi.se):
- Warnings: https://opendata-download-warnings.smhi.se/ibww/api/version/1/warning.json
- Forecast: the snow1g point forecast (replaced pmp3g in 2025), hourly for ~2 days, then 3-12 hourly to 10 days.

The warning area catalog below is SMHI's metadata/area.json (checked 2026-11-12): the 21 counties and
the sea districts. A warning lists the counties/sea districts it affects, so watching "Stockholms län"
catches every warning that touches the county, also when it only covers a part of it.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

WARNINGS_URL = "https://opendata-download-warnings.smhi.se/ibww/api/version/1/warning.json"
FORECAST_URL = ("https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1"
                "/geotype/point/lon/{lon:.4f}/lat/{lat:.4f}/data.json")
LOCAL_TZ = ZoneInfo("Europe/Stockholm")
WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]
MISSING = 9999  # SMHI's "no value", e.g. cloud base when there are no clouds

# Lowest to highest. MESSAGE ("Meddelande") is used for slow events such as water shortage or fire risk.
LEVELS = ["MESSAGE", "YELLOW", "ORANGE", "RED"]
LEVEL_NAMES = {"MESSAGE": "Meddelande", "YELLOW": "Gul varning", "ORANGE": "Orange varning", "RED": "Röd varning"}
DEFAULT_ALERT_LEVELS = ["YELLOW", "ORANGE", "RED"]


@dataclass(frozen=True)
class WarningArea:
    id: int
    name: str
    kind: str  # county | sea


WARNING_AREAS: list[WarningArea] = [
    WarningArea(10, "Blekinge län", "county"), WarningArea(20, "Dalarnas län", "county"),
    WarningArea(9, "Gotlands län", "county"), WarningArea(21, "Gävleborgs län", "county"),
    WarningArea(13, "Hallands län", "county"), WarningArea(23, "Jämtlands län", "county"),
    WarningArea(6, "Jönköpings län", "county"), WarningArea(8, "Kalmar län", "county"),
    WarningArea(7, "Kronobergs län", "county"), WarningArea(25, "Norrbottens län", "county"),
    WarningArea(12, "Skåne län", "county"), WarningArea(1, "Stockholms län", "county"),
    WarningArea(4, "Södermanlands län", "county"), WarningArea(3, "Uppsala län", "county"),
    WarningArea(17, "Värmlands län", "county"), WarningArea(24, "Västerbottens län", "county"),
    WarningArea(22, "Västernorrlands län", "county"), WarningArea(19, "Västmanlands län", "county"),
    WarningArea(14, "Västra Götalands län", "county"), WarningArea(18, "Örebro län", "county"),
    WarningArea(5, "Östergötlands län", "county"),
    WarningArea(41, "Bottenviken", "sea"), WarningArea(42, "Norra Kvarken", "sea"),
    WarningArea(43, "Norra Bottenhavet", "sea"), WarningArea(44, "Södra Bottenhavet", "sea"),
    WarningArea(45, "Ålands hav", "sea"), WarningArea(46, "Skärgårdshavet", "sea"),
    WarningArea(47, "Finska viken", "sea"), WarningArea(48, "Norra Östersjön", "sea"),
    WarningArea(49, "Mellersta Östersjön", "sea"), WarningArea(50, "Rigabukten", "sea"),
    WarningArea(51, "Sydöstra Östersjön", "sea"), WarningArea(52, "Södra Östersjön", "sea"),
    WarningArea(53, "Sydvästra Östersjön", "sea"), WarningArea(54, "Bälten", "sea"),
    WarningArea(55, "Öresund", "sea"), WarningArea(56, "Kattegatt", "sea"),
    WarningArea(57, "Skagerrak", "sea"), WarningArea(58, "Vänern", "sea"),
]
WARNING_AREAS_BY_ID: dict[int, WarningArea] = {a.id: a for a in WARNING_AREAS}

# One forecast point per county for the weather map: the county seat (or its largest town). id = county id.
COUNTY_POINTS: dict[int, tuple[str, float, float]] = {
    1: ("Stockholm", 59.3293, 18.0686), 3: ("Uppsala", 59.8586, 17.6389), 4: ("Nyköping", 58.7530, 17.0079),
    5: ("Linköping", 58.4108, 15.6214), 6: ("Jönköping", 57.7826, 14.1618), 7: ("Växjö", 56.8777, 14.8091),
    8: ("Kalmar", 56.6634, 16.3568), 9: ("Visby", 57.6348, 18.2948), 10: ("Karlskrona", 56.1612, 15.5869),
    12: ("Malmö", 55.6050, 13.0038), 13: ("Halmstad", 56.6745, 12.8578), 14: ("Göteborg", 57.7089, 11.9746),
    17: ("Karlstad", 59.3793, 13.5036), 18: ("Örebro", 59.2753, 15.2134), 19: ("Västerås", 59.6099, 16.5448),
    20: ("Falun", 60.6065, 15.6355), 21: ("Gävle", 60.6749, 17.1413), 22: ("Sundsvall", 62.3908, 17.3069),
    23: ("Östersund", 63.1792, 14.6357), 24: ("Umeå", 63.8258, 20.2630), 25: ("Luleå", 65.5848, 22.1567),
}

# SMHI weather symbols (Wsymb2, 1-27), used by snow1g's symbol_code.
SYMBOLS = {
    1: "Klart", 2: "Nästan klart", 3: "Växlande molnighet", 4: "Halvklart", 5: "Molnigt", 6: "Mulet",
    7: "Dimma", 8: "Lätta regnskurar", 9: "Regnskurar", 10: "Kraftiga regnskurar", 11: "Åskskurar",
    12: "Lätta byar av snöblandat regn", 13: "Byar av snöblandat regn", 14: "Kraftiga byar av snöblandat regn",
    15: "Lätta snöbyar", 16: "Snöbyar", 17: "Kraftiga snöbyar", 18: "Lätt regn", 19: "Regn",
    20: "Kraftigt regn", 21: "Åska", 22: "Lätt snöblandat regn", 23: "Snöblandat regn",
    24: "Kraftigt snöblandat regn", 25: "Lätt snöfall", 26: "Snöfall", 27: "Kraftigt snöfall",
}


@dataclass
class Warning:
    """One warning area of an SMHI warning (one event can have several areas with different levels)."""
    id: int                      # the warning area id; stable while the warning lives
    event: str                   # e.g. "Snöfall"
    event_code: str              # e.g. SNOW
    level: str                   # MESSAGE | YELLOW | ORANGE | RED
    area_name: str               # SMHI's own description, e.g. "Högre terräng mellan Timrå och Örnsköldsvik"
    area_ids: list[int]          # affected counties / sea districts
    start: datetime | None = None
    end: datetime | None = None
    published: datetime | None = None
    descriptions: dict[str, str] = field(default_factory=dict)  # title (sv) -> text (sv)

    @property
    def level_name(self) -> str:
        return LEVEL_NAMES.get(self.level, self.level)

    @property
    def rank(self) -> int:
        return LEVELS.index(self.level) if self.level in LEVELS else -1


@dataclass
class ForecastStep:
    time: datetime               # end of the step (UTC)
    hours: float                 # length of the step the precipitation covers
    temperature: float | None
    symbol: int | None
    precipitation: float | None  # mm during the step
    wind_speed: float | None     # m/s
    wind_gust: float | None      # m/s
    wind_direction: float | None

    @property
    def symbol_text(self) -> str:
        return SYMBOLS.get(self.symbol or 0, "")


class WeatherData(ABC):
    @abstractmethod
    def warnings(self) -> list[Warning]: ...

    @abstractmethod
    def forecast(self, lat: float, lon: float) -> list[ForecastStep]: ...


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _value(data: dict, key: str) -> float | None:
    value = data.get(key)
    return None if value is None or value == MISSING else value


def parse_warnings(payload: list[dict]) -> list[Warning]:
    warnings = []
    for event in payload:
        name = event.get("event", {})
        for area in event.get("warningAreas", []):
            level = area.get("warningLevel", {}).get("code", "")
            warnings.append(Warning(
                id=area["id"],
                event=(area.get("eventDescription") or name).get("sv", "") or name.get("sv", ""),
                event_code=name.get("code", ""),
                level=level,
                area_name=(area.get("areaName") or {}).get("sv", ""),
                area_ids=[a["id"] for a in area.get("affectedAreas", [])],
                start=_time(area.get("approximateStart")),
                end=_time(area.get("approximateEnd")),
                published=_time(area.get("published")),
                descriptions={
                    d["title"]["sv"]: d["text"]["sv"].strip()
                    for d in area.get("descriptions", []) if d.get("title") and d.get("text")
                },
            ))
    return warnings


def parse_forecast(payload: dict) -> list[ForecastStep]:
    steps = []
    for entry in payload.get("timeSeries", []):
        data = entry.get("data", {})
        end = _time(entry["time"])
        start = _time(entry.get("intervalParametersStartTime")) or end
        hours = max((end - start).total_seconds() / 3600, 1.0)
        # Unit kg/m2 = mm fallen during the step (1 h at first, up to 12 h later on; see parameter.json).
        amount = _value(data, "precipitation_amount_mean")
        symbol = _value(data, "symbol_code")
        steps.append(ForecastStep(
            time=end, hours=hours,
            temperature=_value(data, "air_temperature"),
            symbol=int(symbol) if symbol is not None else None,
            precipitation=amount,
            wind_speed=_value(data, "wind_speed"),
            wind_gust=_value(data, "wind_speed_of_gust"),
            wind_direction=_value(data, "wind_from_direction"),
        ))
    return steps


class SmhiWeatherData(WeatherData):
    def __init__(self, timeout_seconds: float = 20.0):
        self.timeout_seconds = timeout_seconds

    def _get(self, url: str):
        from src.sources import USER_AGENT

        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return json.load(response)

    def warnings(self) -> list[Warning]:
        return parse_warnings(self._get(WARNINGS_URL))

    def forecast(self, lat: float, lon: float) -> list[ForecastStep]:
        return parse_forecast(self._get(FORECAST_URL.format(lat=lat, lon=lon)))


def parse_position(symbol: str | None) -> tuple[float, float] | None:
    """A place's symbol is "lat,lon", e.g. "59.3293,18.0686"."""
    try:
        lat, lon = (float(x) for x in (symbol or "").split(","))
    except ValueError:
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def local_text(t: datetime | None) -> str:
    """Swedish local time, e.g. "tor 13/11 08:00"."""
    if t is None:
        return "?"
    t = t.astimezone(LOCAL_TZ)
    return f"{WEEKDAYS[t.weekday()]} {t.day}/{t.month} {t:%H:%M}"
