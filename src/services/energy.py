"""Electricity page: zone prices, near-live production and flows, nuclear status and a weekly price outlook.

The outlook is not a price forecast. It combines what drives Nordic prices in the short term (temperature
-> demand, wind -> cheap supply, nuclear availability) into a low/normal/high indicator per day, and an AI
(or, without AI, a template) turns that into a short Swedish text.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from src.db.models import Section
from src.sections.energy import (
    NUCLEAR_CAPACITY, REACTORS, ZONE_NAMES, ZONES, EnergyData, LiveSnapshot, OpenEnergyData, Outage, OutlookDay,
    PricePoint, back_in_service, unavailable_at,
)
from src.sections.weather import LOCAL_TZ, ForecastStep
from src.services.market import _TTLCache

log = logging.getLogger(__name__)

DEFAULT_ZONE = "SE3"
OUTLOOK_DAYS = 7

# Where most people live (demand) and where most wind power is (supply).
TEMPERATURE_POINTS = [("Stockholm", 59.33, 18.07), ("Göteborg", 57.71, 11.97), ("Malmö", 55.61, 13.0),
                      ("Sundsvall", 62.39, 17.31)]
WIND_POINTS = [("Markbygden", 65.45, 20.6), ("Västerbottens inland", 64.6, 18.7), ("Ånge", 62.5, 15.7),
               ("Jämtland", 63.0, 14.2), ("Hälsingland", 61.5, 16.5), ("Västra Götaland", 58.4, 12.8),
               ("Kalmar", 56.9, 16.4), ("Skåne", 55.7, 13.3)]
# Rough 1991-2020 monthly mean of the towns above, °C.
NORMAL_TEMPERATURE = [-1.5, -1.5, 1.0, 6.0, 11.0, 15.0, 18.0, 17.0, 12.5, 7.5, 3.5, 0.0]

WEEKDAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
LEVEL_TEXT = {"high": "elpriset väntas bli högre än vanligt", "normal": "elpriset väntas ligga på normala nivåer",
              "low": "elpriset väntas bli lägre än vanligt"}


@dataclass
class ZonePrices:
    zone: str
    name: str
    now: float | None = None             # öre/kWh this hour
    today: list[PricePoint] = field(default_factory=list)
    tomorrow: list[PricePoint] = field(default_factory=list)
    avg_today: float | None = None
    avg_tomorrow: float | None = None


@dataclass
class ReactorStatus:
    name: str
    capacity: int
    available: float                     # MW now
    reason: str = ""
    back_at: datetime | None = None      # fully available again
    planned: bool = True


@dataclass
class EnergyOverview:
    zone: str
    zones: list[ZonePrices]
    live: LiveSnapshot | None
    reactors: list[ReactorStatus]
    nuclear_available: float
    nuclear_capacity: int
    updated_at: datetime
    errors: list[str] = field(default_factory=list)


@dataclass
class EnergyOutlook:
    zone: str
    days: list[OutlookDay]
    headline: str
    text: str
    generator: str                       # gemini | claude | rules
    recent_price: float | None           # home zone average of the last 7 days, öre/kWh
    generated_at: datetime
    errors: list[str] = field(default_factory=list)


def _avg(points: list[PricePoint]) -> float | None:
    return round(sum(p.price for p in points) / len(points), 1) if points else None


def daily_means(steps: list[ForecastStep], attr: str) -> dict[date, float]:
    """Mean per Swedish calendar day, weighted by step length (steps get longer further ahead)."""
    sums: dict[date, list[float]] = {}
    for s in steps:
        value = getattr(s, attr)
        if value is None:
            continue
        acc = sums.setdefault(s.time.astimezone(LOCAL_TZ).date(), [0.0, 0.0])
        acc[0] += value * s.hours
        acc[1] += s.hours
    return {d: total / hours for d, (total, hours) in sums.items() if hours}


def rate_day(day: OutlookDay, recent: float | None = None) -> OutlookDay:
    """Price pressure from cold (demand), wind (cheap supply) and nuclear availability.

    `recent` is the home zone's average price of the last week; with it, a published day-ahead price sets the level.
    """
    score, reasons = 0, []
    if day.temperature is not None and day.normal_temperature is not None:
        diff = day.temperature - day.normal_temperature
        if diff <= -8:
            score, reasons = score + 2, reasons + ["mycket kallt"]
        elif diff <= -4:
            score, reasons = score + 1, reasons + ["kallt"]
        elif diff >= 4:
            score, reasons = score - 1, reasons + ["milt"]
    if day.wind is not None:
        # 10 m wind is well below hub height: ~4.5 m/s here already means a lot of wind power.
        if day.wind < 3.0:
            score, reasons = score + 1, reasons + ["svag vind"]
        elif day.wind >= 6.0:
            score, reasons = score - 2, reasons + ["mycket blåsigt"]
        elif day.wind >= 4.5:
            score, reasons = score - 1, reasons + ["blåsigt"]
    running = len(REACTORS) - len(day.nuclear_out)
    if day.nuclear_available < 3500:
        score, reasons = score + 2, reasons + [f"bara {running} av {len(REACTORS)} reaktorer i drift"]
    elif day.nuclear_available < 5000:
        score, reasons = score + 1, reasons + [f"{len(day.nuclear_out)} reaktorer ur drift"]
    day.level = "high" if score >= 2 else "low" if score <= -1 else "normal"
    if day.price is not None and recent:
        # The published day-ahead price beats the indicator.
        ratio = day.price / recent
        day.level = "high" if ratio > 1.25 else "low" if ratio < 0.75 else "normal"
        reasons.append(f"spotpris {day.price:.0f} öre/kWh")
    day.reasons = reasons
    return day


def day_name(d: date, today: date) -> str:
    if d == today:
        return "idag"
    if d == today + timedelta(days=1):
        return "imorgon"
    return WEEKDAYS[d.weekday()]


def rules_outlook(zone: str, days: list[OutlookDay], reactors: list[ReactorStatus], recent: float | None,
                  today: date) -> tuple[str, str]:
    """Headline and text without AI: consecutive days with the same level become one sentence."""
    groups: list[list[OutlookDay]] = []
    for d in days:
        if groups and groups[-1][-1].level == d.level:
            groups[-1].append(d)
        else:
            groups.append([d])
    sentences = []
    for group in groups:
        first, last = day_name(group[0].date, today), day_name(group[-1].date, today)
        span = first if len(group) == 1 else f"{first}–{last}"
        reasons: list[str] = []
        for r in (r for d in group for r in d.reasons):
            # One nuclear reason per group (the first day's) and no prices; those get their own sentences.
            if r in reasons or r.startswith("spotpris") or ("reaktor" in r and any("reaktor" in x for x in reasons)):
                continue
            reasons.append(r)
        why = f"{', '.join(reasons)} – " if reasons else ""
        sentences.append(f"{span[0].upper()}{span[1:]}: {why}{LEVEL_TEXT[group[0].level]}.")
    end = datetime.combine(days[-1].date + timedelta(days=7), datetime.max.time(), LOCAL_TZ) if days else None
    for r in reactors:
        if r.back_at and end and r.back_at <= end:
            when = r.back_at.astimezone(LOCAL_TZ)
            sentences.append(f"{r.name} väntas vara tillbaka i full drift {WEEKDAYS[when.weekday()]} {when.day}/{when.month}"
                             f" (+{r.capacity - round(r.available)} MW).")
    known = [d for d in days if d.price is not None]
    if known and recent:
        d = known[-1]
        sentences.append(f"Spotpriset i {zone} {day_name(d.date, today)} är i snitt {d.price:.0f} öre/kWh "
                         f"(senaste veckan {recent:.0f} öre/kWh).")

    levels = [g[0].level for g in groups]
    word = {"high": "höga", "normal": "normala", "low": "låga"}
    if len(set(levels)) == 1:
        headline = f"{word[levels[0]].capitalize()} elpriser väntas hela veckan"
    else:
        headline = f"{word[levels[0]].capitalize()} elpriser först, sedan {word[levels[-1]]}"
    return headline, " ".join(sentences)


class EnergyService:
    def __init__(self, data: EnergyData | None = None, weather=None, summarizer=None, now=None,
                 outlook_ttl: float = 3 * 3600):
        from src.services.weather import WeatherService

        self.data = data or OpenEnergyData()
        self.weather = weather or WeatherService()
        self.summarizer = summarizer
        self.now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))
        self.outlook_ttl = outlook_ttl
        self._cache = _TTLCache()
        self._outlook: dict[tuple, tuple[float, EnergyOutlook]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def zone_of(section: Section) -> str:
        zone = str(section.settings.get("zone", DEFAULT_ZONE)).upper()
        return zone if zone in ZONES else DEFAULT_ZONE

    # ---- cached fetches ----

    def _prices(self, day: date, zone: str) -> list[PricePoint] | None:
        today = self.now().astimezone(LOCAL_TZ).date()
        ttl = 24 * 3600 if day < today else 3600
        key = ("prices", day, zone)
        if self._cache.peek(key, None) is None and key in self._cache:
            ttl = 900  # not published yet (tomorrow before ~13:00): ask again in 15 minutes
        return self._cache.get(key, ttl, lambda: self.data.prices(day, zone))

    def _outages(self) -> list[Outage]:
        now = self.now()
        return self._cache.get("outages", 1800, lambda: self.data.outages(now, now + timedelta(days=OUTLOOK_DAYS + 1)))

    def _reactors(self, outages: list[Outage]) -> list[ReactorStatus]:
        now = self.now()
        result = []
        for name, capacity in REACTORS.items():
            out = unavailable_at(outages, name, now)
            current = [o for o in outages if o.reactor == name and o.start <= now < o.end]
            result.append(ReactorStatus(
                name=name, capacity=capacity, available=capacity - out,
                reason=current[0].reason if current else "",
                back_at=back_in_service(outages, name, now) if out else None,
                planned=current[0].planned if current else True,
            ))
        return result

    # ---- page data ----

    def overview(self, section: Section) -> EnergyOverview:
        zone = self.zone_of(section)
        now = self.now()
        today = now.astimezone(LOCAL_TZ).date()
        errors: list[str] = []

        def zone_prices(z: str) -> ZonePrices:
            result = ZonePrices(zone=z, name=ZONE_NAMES[z])
            try:
                result.today = self._prices(today, z) or []
                result.tomorrow = self._prices(today + timedelta(days=1), z) or []
            except Exception as exc:
                log.warning("Prices for %s failed", z, exc_info=True)
                errors.append(f"Elpriser för {z} kunde inte hämtas: {exc}")
            hour = now.replace(minute=0, second=0, microsecond=0)
            result.now = next((p.price for p in result.today if p.start == hour), None)
            result.avg_today, result.avg_tomorrow = _avg(result.today), _avg(result.tomorrow)
            return result

        with ThreadPoolExecutor(max_workers=4) as pool:
            zones = list(pool.map(zone_prices, ZONES))
        live = None
        try:
            live = self._cache.get("live", 300, self.data.live)
        except Exception as exc:
            log.warning("Live production failed", exc_info=True)
            errors.append(f"Produktionsdata kunde inte hämtas: {exc}")
        reactors = []
        try:
            reactors = self._reactors(self._outages())
        except Exception as exc:
            log.warning("Nuclear outages failed", exc_info=True)
            errors.append(f"Kärnkraftsstatus (Nord Pool UMM) kunde inte hämtas: {exc}")
        return EnergyOverview(
            zone=zone, zones=zones, live=live, reactors=reactors,
            nuclear_available=sum(r.available for r in reactors) if reactors else 0,
            nuclear_capacity=NUCLEAR_CAPACITY, updated_at=datetime.now().astimezone(), errors=errors,
        )

    def outlook(self, section: Section) -> EnergyOutlook:
        zone = self.zone_of(section)
        now = self.now()
        key = (zone, now.astimezone(LOCAL_TZ).date())
        with self._lock:
            hit = self._outlook.get(key)
            # A template text (AI failed or is off) is retried sooner than an AI text.
            ttl = self.outlook_ttl if hit and hit[1].generator != "rules" else min(self.outlook_ttl, 1800)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        outlook = self._build_outlook(zone)
        with self._lock:
            self._outlook[key] = (time.monotonic(), outlook)
        return outlook

    def _build_outlook(self, zone: str) -> EnergyOutlook:
        now = self.now()
        today = now.astimezone(LOCAL_TZ).date()
        dates = [today + timedelta(days=i) for i in range(OUTLOOK_DAYS)]
        errors: list[str] = []

        def means(points, attr) -> dict[date, float]:
            per_point = []
            for name, lat, lon in points:
                try:
                    per_point.append(daily_means(self.weather.forecast(lat, lon), attr))
                except Exception:
                    log.warning("Outlook forecast for %s failed", name, exc_info=True)
            if not per_point:
                errors.append("SMHI-prognoser kunde inte hämtas")
            return {d: sum(m[d] for m in per_point if d in m) / n
                    for d in dates if (n := sum(1 for m in per_point if d in m))}

        temps, winds = means(TEMPERATURE_POINTS, "temperature"), means(WIND_POINTS, "wind_speed")
        try:
            outages = self._outages()
        except Exception as exc:
            log.warning("Nuclear outages failed", exc_info=True)
            errors.append(f"Kärnkraftsstatus kunde inte hämtas: {exc}")
            outages = []
        reactors = self._reactors(outages)

        recent_prices = []
        for i in range(1, 8):
            try:
                recent_prices += self._prices(today - timedelta(days=i), zone) or []
            except Exception:
                log.warning("Price history for %s failed", zone, exc_info=True)
        recent = _avg(recent_prices)

        days = []
        for d in dates:
            noon = datetime.combine(d, datetime.min.time().replace(hour=12), LOCAL_TZ)
            out = {name: unavailable_at(outages, name, noon) for name in REACTORS}
            try:
                prices = self._prices(d, zone) if d <= today + timedelta(days=1) else None
            except Exception:
                prices = None
            days.append(rate_day(OutlookDay(
                date=d,
                temperature=round(temps[d], 1) if d in temps else None,
                normal_temperature=NORMAL_TEMPERATURE[d.month - 1],
                wind=round(winds[d], 1) if d in winds else None,
                nuclear_available=NUCLEAR_CAPACITY - sum(out.values()),
                nuclear_out=[n for n, mw in out.items() if mw >= REACTORS[n] / 2],
                price=_avg(prices or []),
            ), recent))

        headline, text = rules_outlook(zone, days, reactors, recent, today)
        generator = "rules"
        if self.summarizer is not None:
            payload = self._ai_payload(zone, days, reactors, recent, today)
            try:
                written = self.summarizer.energy_outlook(payload)
                headline, text, generator = written.headline, written.text, self.summarizer.name
            except NotImplementedError:
                pass
            except Exception as exc:
                log.warning("AI energy outlook failed; using the template", exc_info=True)
                errors.append(f"AI-texten kunde inte skrivas ({exc}); visar en enklare text.")
        return EnergyOutlook(zone=zone, days=days, headline=headline, text=text, generator=generator,
                             recent_price=recent, generated_at=datetime.now().astimezone(), errors=errors)

    @staticmethod
    def _ai_payload(zone, days, reactors, recent, today) -> dict:
        return {
            "today": f"{WEEKDAYS[today.weekday()]} {today.isoformat()}",
            "zone": zone,
            "recent_7_day_average_ore_per_kwh": recent,
            "days": [{
                "date": d.date.isoformat(), "weekday": WEEKDAYS[d.date.weekday()],
                "mean_temperature_c": d.temperature, "normal_temperature_c": d.normal_temperature,
                "mean_wind_at_wind_farms_m_s": d.wind,
                "nuclear_available_mw": round(d.nuclear_available), "nuclear_capacity_mw": NUCLEAR_CAPACITY,
                "reactors_out": d.nuclear_out,
                "day_ahead_average_ore_per_kwh": d.price,
                "rule_based_level": d.level, "rule_reasons": d.reasons,
            } for d in days],
            "reactors": [{
                "name": r.name, "capacity_mw": r.capacity, "available_now_mw": round(r.available),
                "fully_back": _local(r.back_at) if r.back_at else None,
                "reason": r.reason, "planned": r.planned,
            } for r in reactors if r.available < r.capacity],
        }


def _local(t: datetime) -> str:
    t = t.astimezone(LOCAL_TZ)
    return f"{WEEKDAYS[t.weekday()]} {t:%Y-%m-%d %H:%M}"
