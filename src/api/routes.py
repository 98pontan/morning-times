"""REST API used by the frontend. Everything configurable is editable here."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from src.api import schemas as s
from src.db.models import Alert, Article, Brief, Section, Source, WatchItem, utcnow
from src.sections import PLUGINS, get_plugin
from src.sections.regions import REGIONS, REGIONS_BY_SLUG, Region
from src.services.ingest import rematch_section
from src.services.market import RANGES
from src.sources import FETCHERS

router = APIRouter()
api = APIRouter(prefix="/api")


def services(request: Request):
    return request.app.state.services


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.services.db.session_factory()
    try:
        yield session
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, f"Conflict: {exc.orig}") from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _get(session: Session, model, obj_id: int):
    obj = session.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {obj_id} not found")
    return obj


def _apply(obj, patch) -> None:
    for key, value in patch.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)


# ---- health & config ------------------------------------------------------------------

@router.get("/healthz")
def health(svc=Depends(services)):
    try:
        svc.db.ping()
    except Exception as exc:
        raise HTTPException(503, f"database unavailable: {exc}") from exc
    if svc.scheduler is not None and not svc.scheduler.running:
        raise HTTPException(503, "scheduler not running")
    return {"status": "ok"}


@api.get("/config")
def show_config(svc=Depends(services)):
    return {
        "settings": svc.settings.public_dict(),
        "summarizer": svc.summarizer.name,
        "jobs": svc.scheduler.jobs() if svc.scheduler else [],
        "section_kinds": [k.model_dump() for k in section_kinds()],
        "source_kinds": sorted(FETCHERS),
    }


@api.get("/section-kinds", response_model=list[s.SectionKindOut])
def section_kinds():
    return [s.SectionKindOut(kind=p.kind, description=p.description, item_types=sorted(p.item_types))
            for p in PLUGINS.values()]


# ---- sections ---------------------------------------------------------------------------

@api.get("/sections", response_model=list[s.SectionOut])
def list_sections(session: Session = Depends(get_session)):
    return session.scalars(select(Section).order_by(Section.position, Section.id)).all()


@api.post("/sections", response_model=s.SectionOut, status_code=201)
def create_section(body: s.SectionIn, session: Session = Depends(get_session)):
    _check_kind(body.kind)
    section = Section(**body.model_dump())
    session.add(section)
    session.flush()
    return section


@api.get("/sections/{section_id}", response_model=s.SectionOut)
def get_section(section_id: int, session: Session = Depends(get_session)):
    return _get(session, Section, section_id)


@api.patch("/sections/{section_id}", response_model=s.SectionOut)
def update_section(section_id: int, body: s.SectionPatch, session: Session = Depends(get_session)):
    section = _get(session, Section, section_id)
    _apply(section, body)
    return section


@api.delete("/sections/{section_id}", status_code=204)
def delete_section(section_id: int, session: Session = Depends(get_session)):
    session.delete(_get(session, Section, section_id))
    return Response(status_code=204)


def _check_kind(kind: str) -> None:
    if kind not in PLUGINS:
        raise HTTPException(422, f"Unknown section kind '{kind}'. Available: {sorted(PLUGINS)}")


# ---- sources ----------------------------------------------------------------------------

@api.get("/sections/{section_id}/sources", response_model=list[s.SourceOut])
def list_sources(section_id: int, session: Session = Depends(get_session)):
    return _get(session, Section, section_id).sources


@api.post("/sections/{section_id}/sources", response_model=s.SourceOut, status_code=201)
def create_source(section_id: int, body: s.SourceIn, session: Session = Depends(get_session)):
    _get(session, Section, section_id)
    if body.kind not in FETCHERS:
        raise HTTPException(422, f"Unknown source kind '{body.kind}'. Available: {sorted(FETCHERS)}")
    source = Source(section_id=section_id, **body.model_dump())
    session.add(source)
    session.flush()
    return source


@api.patch("/sources/{source_id}", response_model=s.SourceOut)
def update_source(source_id: int, body: s.SourcePatch, session: Session = Depends(get_session)):
    source = _get(session, Source, source_id)
    _apply(source, body)
    return source


@api.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: int, session: Session = Depends(get_session)):
    session.delete(_get(session, Source, source_id))
    return Response(status_code=204)


# ---- regions (local editions a regional section can pick) -------------------------------

def _regional_section(session: Session, section_id: int) -> Section:
    section = _get(session, Section, section_id)
    if section.kind != "regional":
        raise HTTPException(422, f"Only regional sections have regions, '{section.slug}' is {section.kind}")
    return section


def _region_out(region: Region, source: Source | None) -> s.RegionOut:
    return s.RegionOut(
        slug=region.slug, name=region.name, covers=region.covers, url=region.url,
        source_id=source.id if source else None, enabled=bool(source and source.enabled),
    )


@api.get("/sections/{section_id}/regions", response_model=list[s.RegionOut])
def list_regions(section_id: int, session: Session = Depends(get_session)):
    by_url = {src.url: src for src in _regional_section(session, section_id).sources}
    return [_region_out(r, by_url.get(r.url)) for r in REGIONS]


@api.put("/sections/{section_id}/regions/{slug}", response_model=s.RegionOut)
def set_region(
    section_id: int, slug: str, body: s.RegionPatch,
    session: Session = Depends(get_session), svc=Depends(services),
):
    """Pick or un-pick a region: adds/enables its feed, or disables it (kept, so it can be re-picked)."""
    section = _regional_section(session, section_id)
    region = REGIONS_BY_SLUG.get(slug)
    if region is None:
        raise HTTPException(404, f"Unknown region '{slug}'")
    source = next((src for src in section.sources if src.url == region.url), None)
    if source is None and body.enabled:
        source = Source(section_id=section.id, name=region.source_name, url=region.url, kind="rss")
        session.add(source)
    if source is not None:
        source.enabled = body.enabled
    session.commit()
    if body.enabled and source.last_fetched_at is None:
        # Fetch now so the region's news shows up at once instead of after the next ingest run.
        svc.ingest.run_source(source.id)
        session.refresh(source)
    return _region_out(region, source)


# ---- stock screen (economy sections) ----------------------------------------------------

@api.get("/sections/{section_id}/market", response_model=s.MarketOverviewOut)
def market_overview(section_id: int, session: Session = Depends(get_session), svc=Depends(services)):
    """Prices and development for the section's watched stocks and its reference index (cached ~2 min)."""
    section = session.scalar(
        select(Section).where(Section.id == section_id).options(selectinload(Section.watch_items))
    )
    if section is None:
        raise HTTPException(404, f"Section {section_id} not found")
    if section.kind != "economy":
        raise HTTPException(422, f"Only economy sections have a stock screen, '{section.slug}' is {section.kind}")
    return svc.market.overview(section)


@api.get("/market/chart", response_model=s.ChartOut)
def market_chart(
    symbol: str = Query(pattern=r"^[A-Za-z0-9^=.\-]{1,32}$", description="Yahoo symbol, e.g. VOLV-B.ST or ^OMX"),
    range: str = Query("1m", description=f"One of {', '.join(RANGES)}"),
    svc=Depends(services),
):
    if range not in RANGES:
        raise HTTPException(422, f"Unknown range '{range}'. Available: {list(RANGES)}")
    return s.ChartOut(symbol=symbol, range=range, points=svc.market.chart(symbol, range))


# ---- watch items (stocks, teams, regions, keywords) -------------------------------------

@api.get("/sections/{section_id}/watch-items", response_model=list[s.WatchItemOut])
def list_watch_items(section_id: int, session: Session = Depends(get_session)):
    return _get(session, Section, section_id).watch_items


@api.post("/sections/{section_id}/watch-items", response_model=s.WatchItemOut, status_code=201)
def create_watch_item(section_id: int, body: s.WatchItemIn, session: Session = Depends(get_session)):
    section = _get(session, Section, section_id)
    plugin = get_plugin(section.kind)
    if body.type not in plugin.item_types:
        raise HTTPException(422, f"A {section.kind} section can watch {sorted(plugin.item_types)}, not '{body.type}'")
    item = WatchItem(section_id=section_id, **body.model_dump())
    session.add(item)
    session.flush()
    rematch_section(session, section)
    return item


@api.patch("/watch-items/{item_id}", response_model=s.WatchItemOut)
def update_watch_item(item_id: int, body: s.WatchItemPatch, session: Session = Depends(get_session)):
    item = _get(session, WatchItem, item_id)
    _apply(item, body)
    session.flush()
    rematch_section(session, item.section)
    return item


@api.delete("/watch-items/{item_id}", status_code=204)
def delete_watch_item(item_id: int, session: Session = Depends(get_session)):
    session.delete(_get(session, WatchItem, item_id))
    return Response(status_code=204)


# ---- articles ---------------------------------------------------------------------------

@api.get("/articles", response_model=list[s.ArticleOut])
def list_articles(
    section: str | None = Query(None, description="Section slug"),
    source_id: int | None = Query(None, description="Only articles from this source (e.g. one region)"),
    watched_only: bool = False,
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
):
    query = (
        select(Article).join(Section).outerjoin(Source, Article.source_id == Source.id)
        # Articles from a disabled source (e.g. an un-picked region) are hidden.
        .where(Section.enabled, or_(Source.id.is_(None), Source.enabled))
        .where(Article.published_at >= utcnow() - timedelta(hours=hours))
        .options(selectinload(Article.watch_items), selectinload(Article.source))
        .order_by(Article.published_at.desc()).limit(limit)
    )
    if section:
        query = query.where(Section.slug == section)
    if source_id is not None:
        query = query.where(Article.source_id == source_id)
    if watched_only:
        query = query.where(Article.watch_items.any())
    return [
        s.ArticleOut(
            id=a.id, section_id=a.section_id, source_id=a.source_id, url=a.url, title=a.title, summary=a.summary,
            source=a.source.name if a.source else None, published_at=a.published_at,
            importance=a.importance, importance_reason=a.importance_reason,
            watched=[w.name for w in a.watch_items],
        )
        for a in session.scalars(query).all()
    ]


# ---- briefs & alerts ---------------------------------------------------------------------

@api.get("/briefs", response_model=list[s.BriefOut])
def list_briefs(limit: int = Query(14, ge=1, le=365), session: Session = Depends(get_session)):
    return session.scalars(select(Brief).order_by(Brief.date.desc()).limit(limit)).all()


@api.get("/briefs/latest", response_model=s.BriefOut)
def latest_brief(session: Session = Depends(get_session)):
    brief = session.scalar(select(Brief).order_by(Brief.date.desc()).limit(1))
    if brief is None:
        raise HTTPException(404, "No brief yet")
    return brief


@api.get("/briefs/{day}", response_model=s.BriefOut)
def get_brief(day: date, session: Session = Depends(get_session)):
    brief = session.scalar(select(Brief).where(Brief.date == day))
    if brief is None:
        raise HTTPException(404, f"No brief for {day}")
    return brief


@api.get("/briefs/{day}/html", response_class=HTMLResponse)
def get_brief_html(day: date, session: Session = Depends(get_session)):
    return get_brief(day, session).html


@api.post("/briefs/generate", response_model=s.BriefOut)
def generate_brief(send: bool = False, svc=Depends(services)):
    return svc.brief.generate(send=send)


@api.get("/alerts", response_model=list[s.AlertOut])
def list_alerts(limit: int = Query(50, ge=1, le=500), session: Session = Depends(get_session)):
    return session.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(limit)).all()


# ---- manual job triggers -----------------------------------------------------------------

@api.post("/jobs/ingest")
def run_ingest(svc=Depends(services)):
    return svc.ingest.run().__dict__


@api.post("/jobs/alerts")
def run_alerts(svc=Depends(services)):
    return svc.alerts.run().__dict__


router.include_router(api)
