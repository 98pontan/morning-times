"""Open data for the electricity section. All free and without API keys:

- Spot prices per bidding zone (SE1-SE4), 15-minute resolution, from elprisetjustnu.se (Nord Pool day-ahead):
  https://www.elprisetjustnu.se/api/v1/prices/2026/11-13_SE3.json   (tomorrow's prices appear ~13:00)
- Production per type, load and cross-border flows for Sweden from energy-charts.info (Fraunhofer ISE,
  based on ENTSO-E; about 1-2 hours behind real time; "private and internal use only"):
  https://api.energy-charts.info/public_power?country=se  and  /cbpf?country=se
- Nuclear outages from Nord Pool's UMM (urgent market messages):
  https://ummapi.nordpoolgroup.com/messages?...&fuelTypes=14

Per-zone production and the flows between SE1-SE4 are not public without an ENTSO-E token, so the live
map shows zone prices plus national production and flows to the neighbouring countries.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

ZONES = ["SE1", "SE2", "SE3", "SE4"]
ZONE_NAMES = {"SE1": "Luleå", "SE2": "Sundsvall", "SE3": "Stockholm", "SE4": "Malmö"}
PRICES_URL = "https://www.elprisetjustnu.se/api/v1/prices/{d:%Y}/{d:%m-%d}_{zone}.json"
POWER_URL = "https://api.energy-charts.info/public_power?country=se"
FLOWS_URL = "https://api.energy-charts.info/cbpf?country=se"
UMM_URL = "https://ummapi.nordpoolgroup.com/messages"
SE3_EIC = "10Y1001A1001A46L"   # all Swedish reactors are in SE3
NUCLEAR_FUEL_TYPE = 14

# The six reactors in operation, installed capacity in MW (as reported in Nord Pool UMM).
REACTORS: dict[str, int] = {
    "Forsmark 1": 1104, "Forsmark 2": 1121, "Forsmark 3": 1172,
    "Ringhals 3": 1081, "Ringhals 4": 1134, "Oskarshamn 3": 1400,
}
NUCLEAR_CAPACITY = sum(REACTORS.values())

# energy-charts production type -> our category. Anything not listed counts as "other".
PRODUCTION_TYPES = {
    "Nuclear": "nuclear",
    "Hydro water reservoir": "hydro", "Hydro Run-of-River": "hydro", "Hydro pumped storage": "hydro",
    "Wind onshore": "wind", "Wind offshore": "wind",
    "Solar": "solar",
}
SKIP_TYPES = {"Load", "Residual load", "Cross border electricity trading", "Renewable share of load",
              "Renewable share of generation", "Hydro pumped storage consumption"}
COUNTRY_NAMES = {"Denmark": "Danmark", "Finland": "Finland", "Germany": "Tyskland", "Lithuania": "Litauen",
                 "Norway": "Norge", "Poland": "Polen"}


@dataclass
class PricePoint:
    start: datetime
    price: float   # öre/kWh, spot price excl. VAT and fees


@dataclass
class LiveSnapshot:
    time: datetime
    production: dict[str, float]     # MW per category: nuclear, hydro, wind, solar, other
    load: float | None               # MW
    net_export: float | None         # MW, positive = Sweden exports
    flows: dict[str, float]          # MW per neighbouring country, positive = export from Sweden
    flows_time: datetime | None = None


@dataclass
class Outage:
    reactor: str                     # e.g. "Ringhals 4"
    unavailable: float               # MW
    start: datetime
    end: datetime
    reason: str = ""
    planned: bool = True


class EnergyData(ABC):
    @abstractmethod
    def prices(self, day: date, zone: str) -> list[PricePoint] | None:
        """Prices for a day, or None when they are not published yet."""

    @abstractmethod
    def live(self) -> LiveSnapshot | None: ...

    @abstractmethod
    def outages(self, start: datetime, end: datetime) -> list[Outage]: ...


# ---- parsing ----------------------------------------------------------------------------

def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_prices(payload: list[dict]) -> list[PricePoint]:
    """Quarter-hour prices -> hourly averages in öre/kWh."""
    hours: dict[datetime, list[float]] = {}
    for row in payload:
        start = _time(row["time_start"])
        hours.setdefault(start.replace(minute=0), []).append(row["SEK_per_kWh"] * 100)
    return [PricePoint(start=h, price=round(sum(v) / len(v), 2)) for h, v in sorted(hours.items())]


def _last_index(*series: list) -> int | None:
    """Latest index where every series has a value."""
    for i in range(min(len(s) for s in series) - 1, -1, -1):
        if all(s[i] is not None for s in series):
            return i
    return None


def parse_live(power: dict, flows: dict | None) -> LiveSnapshot | None:
    types = {p["name"]: p["data"] for p in power.get("production_types", [])}
    if "Load" not in types:
        return None
    producing = {name: data for name, data in types.items() if name not in SKIP_TYPES}
    i = _last_index(types["Load"], *[d for n, d in producing.items() if n in ("Nuclear", "Wind onshore")])
    if i is None:
        return None
    production = {"nuclear": 0.0, "hydro": 0.0, "wind": 0.0, "solar": 0.0, "other": 0.0}
    for name, data in producing.items():
        value = data[i] if i < len(data) else None
        if value is not None and value > 0:
            production[PRODUCTION_TYPES.get(name, "other")] += value
    trading = types.get("Cross border electricity trading")
    net = trading[i] if trading and i < len(trading) else None
    snapshot = LiveSnapshot(
        time=datetime.fromtimestamp(power["unix_seconds"][i], timezone.utc),
        production={k: round(v) for k, v in production.items()},
        load=types["Load"][i],
        # energy-charts: negative trading = export; we use positive = export.
        net_export=-net if net is not None else None,
        flows={},
    )
    if flows:
        countries = {c["name"]: c["data"] for c in flows.get("countries", []) if c["name"] in COUNTRY_NAMES}
        j = _last_index(*countries.values()) if countries else None
        if j is not None:
            snapshot.flows = {COUNTRY_NAMES[n]: round(-d[j] * 1000) for n, d in countries.items()}  # GW -> MW
            snapshot.flows_time = datetime.fromtimestamp(flows["unix_seconds"][j], timezone.utc)
    return snapshot


_REACTOR = re.compile(r"(Forsmark|Ringhals|Oskarshamn)\D*(\d)", re.IGNORECASE)


def reactor_name(*names: str | None) -> str | None:
    """'Ringhals Block4' / 'Forsmark Block1 G11' / 'Oskarshamn 3' -> 'Ringhals 4' etc."""
    for name in names:
        match = _REACTOR.search(name or "")
        if match:
            candidate = f"{match.group(1).capitalize()} {match.group(2)}"
            if candidate in REACTORS:
                return candidate
    return None


def parse_outages(payload: dict) -> list[Outage]:
    outages = []
    for message in payload.get("items", []):
        if message.get("isOutdated"):
            continue
        units = (message.get("generationUnits") or []) + (message.get("productionUnits") or [])
        for unit in units:
            if unit.get("fuelType", NUCLEAR_FUEL_TYPE) != NUCLEAR_FUEL_TYPE:
                continue
            reactor = reactor_name(unit.get("productionUnitName"), unit.get("name"))
            if reactor is None:
                continue
            for period in unit.get("timePeriods", []):
                outages.append(Outage(
                    reactor=reactor,
                    unavailable=float(period.get("unavailableCapacity") or 0),
                    start=_time(period["eventStart"]), end=_time(period["eventStop"]),
                    reason=(message.get("unavailabilityReason") or "").strip(),
                    planned=message.get("unavailabilityType") == 2,
                ))
    return outages


def unavailable_at(outages: list[Outage], reactor: str, at: datetime) -> float:
    """MW out of service at a moment (capped at the reactor's capacity; generator-level messages can overlap)."""
    total = sum(o.unavailable for o in outages if o.reactor == reactor and o.start <= at < o.end)
    return min(total, REACTORS[reactor])


def back_in_service(outages: list[Outage], reactor: str, now: datetime, horizon: timedelta = timedelta(days=60)):
    """When the reactor is fully available again: the end of the chain of outages running from `now`."""
    t = now
    while unavailable_at(outages, reactor, t) > 0:
        ends = [o.end for o in outages if o.reactor == reactor and o.start <= t < o.end]
        t = max(ends)
        if t - now > horizon:
            return None
    return t if t > now else None


# ---- fetching -----------------------------------------------------------------------------

class OpenEnergyData(EnergyData):
    def __init__(self, timeout_seconds: float = 20.0):
        self.timeout_seconds = timeout_seconds

    def _get(self, url: str):
        from src.sources import USER_AGENT

        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            if response.status == 204:
                return None
            return json.load(response)

    def prices(self, day: date, zone: str) -> list[PricePoint] | None:
        try:
            payload = self._get(PRICES_URL.format(d=day, zone=zone))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # not published yet
            raise
        return parse_prices(payload) if payload else None

    def live(self) -> LiveSnapshot | None:
        power = self._get(POWER_URL)
        try:
            flows = self._get(FLOWS_URL)
        except Exception:
            log.warning("Fetching cross-border flows failed", exc_info=True)
            flows = None
        return parse_live(power, flows)

    def outages(self, start: datetime, end: datetime) -> list[Outage]:
        query = urllib.parse.urlencode({
            "limit": 200, "messageTypes": "ProductionUnavailability", "areas": SE3_EIC,
            "fuelTypes": NUCLEAR_FUEL_TYPE,
            "eventStartDate": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "eventStopDate": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return parse_outages(self._get(f"{UMM_URL}?{query}"))


@dataclass
class OutlookDay:
    """Inputs and the rule-based price pressure for one day of the weekly outlook."""
    date: date
    temperature: float | None = None       # mean of the population-weighted towns, °C
    normal_temperature: float | None = None
    wind: float | None = None              # mean 10 m wind at wind-farm areas, m/s
    nuclear_available: float = 0.0         # MW at noon
    nuclear_out: list[str] = field(default_factory=list)
    price: float | None = None             # day-ahead average for the home zone when published, öre/kWh
    level: str = "normal"                  # low | normal | high
    reasons: list[str] = field(default_factory=list)
