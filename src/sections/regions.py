"""Catalog of local news editions a regional section can pick from (SVT's regional newsrooms).

Picking a region adds (or re-enables) its RSS feed as a source of the section; un-picking disables it.
Feed URLs checked 2026-11-12. SVT has no separate Gotland or Kalmar edition (Kalmar is in Småland).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    slug: str
    name: str
    covers: str

    @property
    def url(self) -> str:
        return f"https://www.svt.se/nyheter/lokalt/{self.slug}/rss.xml"

    @property
    def source_name(self) -> str:
        return f"SVT {self.name}"


REGIONS: list[Region] = [
    Region("blekinge", "Blekinge", "Blekinge län"),
    Region("dalarna", "Dalarna", "Dalarnas län"),
    Region("gavleborg", "Gävleborg", "Gävleborgs län"),
    Region("halland", "Halland", "Hallands län"),
    Region("helsingborg", "Helsingborg", "Nordvästra Skåne"),
    Region("jamtland", "Jämtland", "Jämtlands län"),
    Region("jonkoping", "Jönköping", "Jönköpings län"),
    Region("norrbotten", "Norrbotten", "Norrbottens län"),
    Region("skane", "Skåne", "Malmö, Lund och övriga Skåne"),
    Region("smaland", "Småland", "Kalmar och Kronobergs län"),
    Region("sodertalje", "Södertälje", "Södertälje med omnejd"),
    Region("sormland", "Sörmland", "Södermanlands län"),
    Region("stockholm", "Stockholm", "Stockholms län"),
    Region("uppsala", "Uppsala", "Uppsala län"),
    Region("varmland", "Värmland", "Värmlands län"),
    Region("vast", "Väst", "Göteborg och Västra Götaland"),
    Region("vasterbotten", "Västerbotten", "Västerbottens län"),
    Region("vasternorrland", "Västernorrland", "Västernorrlands län"),
    Region("vastmanland", "Västmanland", "Västmanlands län"),
    Region("orebro", "Örebro", "Örebro län"),
    Region("ost", "Öst", "Östergötlands län"),
]

REGIONS_BY_SLUG: dict[str, Region] = {r.slug: r for r in REGIONS}
