from datetime import date, timedelta

from src.db.models import Section, WatchItem
from src.sections import EconomySection
from src.sections.market import MarketData, Quote
from src.sources import RssFetcher

from tests.conftest import FakeSummarizer, feed_xml


def test_rss_parse_cleans_html():
    articles = RssFetcher.parse(feed_xml())
    assert len(articles) == 3
    assert articles[0].summary == "Volvos aktie rasar efter beskedet."


def test_ingest_matches_watch_items(env):
    client, services = env
    result = client.post("/api/jobs/ingest").json()
    assert result["new_articles"] == 6  # 3 articles x 2 sections
    assert client.post("/api/jobs/ingest").json()["new_articles"] == 0  # deduplicated

    eko = {a["title"]: a["watched"] for a in client.get("/api/articles?section=ekonomi").json()}
    assert eko["Volvo lämnar vinstvarning"] == ["Volvo"]   # "Volvos" in summary also counts
    assert eko["Ericssonfonden delar ut stipendier"] == []   # whole words only
    lokalt = {a["title"]: a["watched"] for a in client.get("/api/articles?section=lokalt").json()}
    assert lokalt["Regn i Göteborg hela veckan"] == ["Göteborg"]


def test_article_alerts_rated_once_and_emailed(env):
    client, services = env
    client.post("/api/jobs/ingest")
    services.alerts._market_alerts = lambda: 0  # no network in tests
    first = client.post("/api/jobs/alerts").json()
    assert first == {"created": 1, "rated": 2, "sent": 1}
    subject, text, html = services.notifier.sent[-1]
    assert "Volvo" in subject and "https://example.com/volvo" in html
    assert client.post("/api/jobs/alerts").json() == {"created": 0, "rated": 0, "sent": 0}


def test_brief_generated_stored_and_sent(env):
    client, services = env
    client.post("/api/jobs/ingest")
    brief = services.brief.generate(day=date(2026, 11, 10), send=True)
    assert brief.sent_at is not None
    stored = client.get("/api/briefs/2026-11-10").json()
    assert stored["data"]["content"]["headline"] == "Godmorgon"
    assert [s["slug"] for s in stored["data"]["content"]["sections"]] == ["ekonomi", "lokalt"]
    html = client.get("/api/briefs/2026-11-10/html").text
    assert "Volvo lämnar vinstvarning" in html and "https://example.com/volvo" in html
    assert services.notifier.sent[-1][0] == "Godmorgon"


def test_crud_and_validation(env):
    client, _ = env
    sport = client.post("/api/sections", json={"slug": "sport", "name": "Sport", "kind": "sports"}).json()
    assert client.post("/api/sections", json={"slug": "sport", "name": "X", "kind": "sports"}).status_code == 409
    assert client.post("/api/sections", json={"slug": "x", "name": "X", "kind": "nope"}).status_code == 422

    url = f"/api/sections/{sport['id']}/watch-items"
    assert client.post(url, json={"type": "stock", "name": "Volvo"}).status_code == 422
    team = client.post(url, json={"type": "team", "name": "AIK", "symbol": "fotboll/Allsvenskan"}).json()
    client.patch(f"/api/watch-items/{team['id']}", json={"keywords": ["Gnaget"]})
    assert client.get(url).json()[0]["keywords"] == ["Gnaget"]

    src = client.post(f"/api/sections/{sport['id']}/sources", json={"name": "SVT", "url": "https://svt.se/rss"}).json()
    assert client.patch(f"/api/sources/{src['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/sections/{sport['id']}").status_code == 204
    assert client.get(f"/api/sections/{sport['id']}").status_code == 404


def test_adding_watch_item_retags_recent_articles(env):
    client, _ = env
    client.post("/api/jobs/ingest")
    eko_id = next(s["id"] for s in client.get("/api/sections").json() if s["slug"] == "ekonomi")
    client.post(f"/api/sections/{eko_id}/watch-items", json={"type": "keyword", "name": "SMHI"})
    watched = {a["title"]: a["watched"] for a in client.get("/api/articles?section=ekonomi").json()}
    assert watched["Regn i Göteborg hela veckan"] == ["SMHI"]


def test_config_hides_secrets(env, monkeypatch):
    client, _ = env
    monkeypatch.setenv("EMAIL_PASSWORD", "hunter2")
    body = client.get("/api/config").text
    assert "hunter2" not in body and '"password_available":true' in body
    assert client.get("/healthz").json() == {"status": "ok"}


class FakeMarket(MarketData):
    def __init__(self, quotes, earnings):
        self.quotes, self.earnings = quotes, earnings

    def quote(self, symbol):
        return self.quotes.get(symbol)

    def next_earnings_date(self, symbol):
        return self.earnings.get(symbol)


def test_economy_alerts_on_big_moves_and_report_days():
    today = date(2026, 11, 10)
    market = FakeMarket(
        {"VOLV-B.ST": Quote("VOLV-B.ST", 90, 100, "SEK"), "ERIC-B.ST": Quote("ERIC-B.ST", 101, 100, "SEK")},
        {"ERIC-B.ST": date(2026, 11, 11)},
    )
    plugin = EconomySection(market, today=lambda: today)
    section = Section(slug="e", name="E", kind="economy", settings={"move_threshold_pct": 5})
    items = [WatchItem(id=1, type="stock", name="Volvo", symbol="VOLV-B.ST", settings={}, enabled=True),
             WatchItem(id=2, type="stock", name="Ericsson", symbol="ERIC-B.ST", settings={}, enabled=True)]
    alerts = {a.dedup_key: a for a in plugin.check_alerts(section, items)}
    assert set(alerts) == {"move:VOLV-B.ST:2026-11-10:down", "earnings:ERIC-B.ST:2026-11-11"}
    assert alerts["move:VOLV-B.ST:2026-11-10:down"].subject == "Volvo -10.0% today"


def test_rules_summarizer_never_triggers_article_alerts():
    from src.ai import ArticleInput, RuleSummarizer
    ratings = RuleSummarizer().rate([
        ArticleInput(1, "Volvo lämnar vinstvarning", "", "DN", "2026-11-09", ["Volvo"]),
        ArticleInput(2, "Se det rekordkorta skidspåret", "", "SVT", "2026-11-09", ["Norrland"]),
    ])
    assert [r.importance for r in ratings] == [6, 4]


def test_claude_summarizer_request_shape():
    from types import SimpleNamespace
    from src.ai import ArticleInput, ClaudeSummarizer, RatingResult, ArticleRating
    from src.settings import AISettings

    calls = []

    class FakeMessages:
        def parse(self, **kwargs):
            calls.append(kwargs)
            out = RatingResult(ratings=[ArticleRating(article_id=1, importance=12, reason="big"),
                                        ArticleRating(article_id=99, importance=5, reason="unknown id")])
            usage = SimpleNamespace(input_tokens=10, cache_read_input_tokens=0, output_tokens=5)
            return SimpleNamespace(parsed_output=out, stop_reason="end_turn", stop_details=None,
                                   usage=usage, _request_id="req_test")

    client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    summarizer = ClaudeSummarizer(AISettings(), client=client)
    ratings = summarizer.rate([ArticleInput(1, "Volvo vinstvarning", "", "DN", "2026-11-09", ["Volvo"])])

    assert [(r.article_id, r.importance) for r in ratings] == [(1, 10)]  # clamped, unknown id dropped
    req = calls[0]
    assert req["model"] == "claude-opus-5-5"  # AISettings().claude_model
    assert req["fallbacks"] == "default" and req["betas"] == ["server-side-fallback-2026-07-01"]
    assert req["output_format"] is RatingResult and req["output_config"] == {"effort": "low"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<articles>" in req["messages"][0]["content"]


def _fake_gemini(output_text, status="completed", fail=None):
    from types import SimpleNamespace
    calls = []

    class Interactions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if fail:
                raise fail
            return SimpleNamespace(status=status, output_text=output_text,
                                   usage=SimpleNamespace(total_input_tokens=10, total_output_tokens=5))

    return SimpleNamespace(interactions=Interactions()), calls


def test_gemini_summarizer_request_and_parsing():
    import json
    from datetime import date
    from src.ai import ArticleInput, BriefContent, GeminiSummarizer, SectionInput
    from src.settings import AISettings

    brief = {"headline": "God morgon", "intro": "Hej.", "sections": [
        {"slug": "ekonomi", "title": "Pengar", "summary": "Text.",
         "highlights": [{"article_id": 1, "text": "Volvo"}, {"article_id": 42, "text": "made up"}]}]}
    client, calls = _fake_gemini(json.dumps(brief))
    s = GeminiSummarizer(AISettings(), client=client)
    out = s.write_brief(date(2026, 11, 10), [SectionInput("ekonomi", "Ekonomi", ["Volvo"],
                        [ArticleInput(1, "Volvo", "", "DN", "2026-11-10", ["Volvo"])])])

    assert [h.article_id for h in out.sections[0].highlights] == [1]  # invented id dropped
    req = calls[0]
    assert req["model"] == "gemini-3.8-flash" and req["store"] is False
    assert req["response_format"]["schema"] == BriefContent.model_json_schema()
    assert req["generation_config"] == {"thinking_level": "medium"}
    assert "<news>" in req["input"] and "Morning Brew" in req["system_instruction"]


def test_gemini_failures_become_summarizer_errors():
    import pytest
    from src.ai import ArticleInput, GeminiSummarizer, SummarizerError
    from src.settings import AISettings
    article = [ArticleInput(1, "t", "", "s", "2026-11-10", ["x"])]
    for client, _ in [_fake_gemini(None, fail=RuntimeError("429 quota")),
                      _fake_gemini("{}", status="failed"),
                      _fake_gemini("not json")]:
        with pytest.raises(SummarizerError):
            GeminiSummarizer(AISettings(), client=client).rate(article)


def test_provider_selection(monkeypatch):
    from src.ai import ClaudeSummarizer, GeminiSummarizer, RuleSummarizer, build_summarizer
    from src.settings import AISettings
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert isinstance(build_summarizer(AISettings(provider="auto")), RuleSummarizer)
    assert isinstance(build_summarizer(AISettings(provider="gemini")), RuleSummarizer)  # no key -> fallback
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert isinstance(build_summarizer(AISettings(provider="auto")), GeminiSummarizer)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert isinstance(build_summarizer(AISettings(provider="auto")), ClaudeSummarizer)
    assert isinstance(build_summarizer(AISettings(provider="gemini")), GeminiSummarizer)


def test_regions_pick_fetch_filter_and_unpick(env):
    client, services = env
    lokalt = next(s for s in client.get("/api/sections").json() if s["slug"] == "lokalt")
    regions = client.get(f"/api/sections/{lokalt['id']}/regions").json()
    skane = next(r for r in regions if r["slug"] == "skane")
    assert skane == {**skane, "enabled": False, "source_id": None}
    assert len(regions) == 21

    # Picking a region adds its SVT feed and fetches it right away.
    picked = client.put(f"/api/sections/{lokalt['id']}/regions/skane", json={"enabled": True}).json()
    assert picked["enabled"] and picked["source_id"]
    source = next(s for s in client.get(f"/api/sections/{lokalt['id']}/sources").json() if s["id"] == picked["source_id"])
    assert source["name"] == "SVT Skåne" and source["last_fetched_at"]
    by_region = client.get(f"/api/articles?section=lokalt&source_id={picked['source_id']}").json()
    assert len(by_region) == 3 and {a["source_id"] for a in by_region} == {picked["source_id"]}
    assert client.get("/api/articles?section=ekonomi").json() == []  # only that one source was fetched

    # Un-picking disables the source and hides its articles; picking again re-enables the same source.
    client.put(f"/api/sections/{lokalt['id']}/regions/skane", json={"enabled": False})
    assert client.get(f"/api/articles?section=lokalt&source_id={picked['source_id']}").json() == []
    again = client.put(f"/api/sections/{lokalt['id']}/regions/skane", json={"enabled": True}).json()
    assert again["source_id"] == picked["source_id"]

    assert client.put(f"/api/sections/{lokalt['id']}/regions/atlantis", json={"enabled": True}).status_code == 404
    ekonomi = next(s for s in client.get("/api/sections").json() if s["slug"] == "ekonomi")
    assert client.get(f"/api/sections/{ekonomi['id']}/regions").status_code == 422


def _daily_bars(start: date, closes: list[float]):
    from datetime import datetime, time as dtime, timedelta, timezone
    from src.sections.market import Bar
    return [Bar(datetime.combine(start + timedelta(days=i), dtime(17, 30), timezone.utc), c, 1000.0)
            for i, c in enumerate(closes)]


def test_stock_row_returns_and_turnover():
    from src.services.market import build_row
    # 400 days, price 100 until 2026-01-01 then rising 1 per day; last day 2026-11-12.
    start = date(2025, 10, 9)
    closes = [100.0 if start + timedelta(days=i) <= date(2025, 12, 31) else 100.0 + (start + timedelta(days=i) - date(2025, 12, 31)).days
              for i in range(400)]
    bars = _daily_bars(start, closes)
    assert bars[-1].t.date() == date(2026, 11, 12)
    row = build_row("Volvo", "VOLV-B.ST", bars)
    assert row.last == 416 and row.change == 1 and round(row.change_pct, 3) == round(1 / 415 * 100, 3)
    assert round(row.returns["ytd"], 6) == 316.0          # 100 at 2025-12-31 -> 416
    assert round(row.returns["1w"], 4) == round((416 - 409) / 409 * 100, 4)
    assert row.returns["1y"] == 316.0                     # 2025-11-12 was still 100
    assert row.high_52w == 416 and row.low_52w == 100
    assert row.turnover == 416_000 and len(row.spark) == 91
    assert build_row("X", "X", []).error


def test_market_endpoints(env):
    from src.services.market import MarketService
    client, services = env

    class Market(FakeMarket):
        def __init__(self):
            super().__init__({}, {"VOLV-B.ST": date(2026, 11, 20)})
            self.calls = 0

        def history(self, symbols, period="2y", interval="1d"):
            self.calls += 1
            data = {"VOLV-B.ST": _daily_bars(date(2026, 11, 1), [100, 110]), "^OMX": _daily_bars(date(2026, 11, 1), [2000, 1980])}
            return {s: data[s] for s in symbols if s in data}

        def currency(self, symbol):
            return "SEK"

    market = Market()
    services.market = MarketService(market)
    ekonomi = next(s for s in client.get("/api/sections").json() if s["slug"] == "ekonomi")
    overview = client.get(f"/api/sections/{ekonomi['id']}/market").json()
    assert overview["index"]["name"] == "OMX Stockholm 30" and round(overview["index"]["change_pct"], 2) == -1.0
    volvo, ericsson = overview["stocks"]  # both watched stocks, in watch-list order
    assert (volvo["last"], volvo["change_pct"], volvo["currency"], volvo["earnings_date"]) == (110, 10, "SEK", "2026-11-20")
    assert ericsson["error"] == "Ingen kursdata"
    client.get(f"/api/sections/{ekonomi['id']}/market")
    assert market.calls == 1  # cached

    chart = client.get("/api/market/chart?symbol=^OMX&range=1m").json()
    assert [p["close"] for p in chart["points"]] == [2000, 1980]
    assert client.get("/api/market/chart?symbol=^OMX&range=7y").status_code == 422
    assert client.get("/api/market/chart?symbol=bad%20symbol").status_code == 422
    lokalt = next(s for s in client.get("/api/sections").json() if s["slug"] == "lokalt")
    assert client.get(f"/api/sections/{lokalt['id']}/market").status_code == 422


# ---- weather -------------------------------------------------------------------------------

def _smhi_warnings(level="YELLOW", area_ids=(1,), warning_id=10766, end="2099-11-13T08:00:00.000Z"):
    return [{"id": 3392, "event": {"sv": "Snö", "en": "Snow", "code": "SNOW"}, "warningAreas": [{
        "id": warning_id, "approximateStart": "2026-11-12T17:00:00.000Z", "approximateEnd": end,
        "published": "2026-11-12T12:30:00.000Z", "areaName": {"sv": "Norra Stockholms län"},
        "warningLevel": {"sv": "Gul", "code": level}, "eventDescription": {"sv": "Snöfall", "code": "SNOW"},
        "affectedAreas": [{"id": a, "sv": "x"} for a in area_ids],
        "descriptions": [{"title": {"sv": "Vad händer?"}, "text": {"sv": "Mycket snö. "}}],
    }]}]


def _smhi_forecast():
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 11, 12, 23, tzinfo=timezone.utc)  # 00:00 Swedish time on 13/11
    series = []
    for i in range(30):
        t = start + timedelta(hours=i)
        series.append({"time": t.isoformat().replace("+00:00", "Z"),
                       "intervalParametersStartTime": (t - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                       "data": {"air_temperature": float(i % 24) - 5, "symbol_code": 26 if i == 12 else 6,
                                "precipitation_amount_mean": 0.5, "wind_speed": 4.0, "wind_speed_of_gust": 9.0,
                                "cloud_base_altitude": 9999}})
    return {"timeSeries": series}


class FakeWeather:
    def __init__(self, warnings_payload=None):
        from src.sections.weather import parse_forecast, parse_warnings
        self._warnings = parse_warnings(warnings_payload or [])
        self._forecast = parse_forecast(_smhi_forecast())
        self.calls = 0

    def warnings(self):
        self.calls += 1
        return self._warnings

    def forecast(self, lat, lon):
        return self._forecast


def test_smhi_parsing():
    from src.sections.weather import parse_forecast, parse_warnings, parse_position
    (w,) = parse_warnings(_smhi_warnings(level="ORANGE"))
    assert (w.id, w.event, w.level, w.level_name, w.area_ids) == (10766, "Snöfall", "ORANGE", "Orange varning", [1])
    assert w.descriptions == {"Vad händer?": "Mycket snö."}
    steps = parse_forecast(_smhi_forecast())
    assert steps[12].symbol_text == "Snöfall" and steps[0].precipitation == 0.5 and steps[0].hours == 1
    assert parse_position("59.3293,18.0686") == (59.3293, 18.0686) and parse_position("Stockholm") is None


def test_weather_alerts_only_for_chosen_areas_and_levels():
    from datetime import datetime, timezone
    from src.sections import WeatherSection
    section = Section(slug="v", name="Väder", kind="weather", settings={})
    items = [WatchItem(id=1, type="warning_area", name="Stockholms län", symbol="1",
                       settings={"levels": ["YELLOW", "ORANGE"]}, enabled=True),
             WatchItem(id=2, type="warning_area", name="Skåne län", symbol="12", settings={}, enabled=False)]
    now = lambda: datetime(2026, 11, 12, 18, tzinfo=timezone.utc)

    def alerts(payload):
        return WeatherSection(FakeWeather(payload), now=now).check_alerts(section, items)

    (yellow,) = alerts(_smhi_warnings("YELLOW", area_ids=(1, 3)))
    assert yellow.dedup_key == "smhi:10766:YELLOW" and yellow.kind == "weather_warning"
    assert yellow.subject == "Gul varning från SMHI: Snöfall, Norra Stockholms län"
    assert "Gäller tor 12/11 18:00" in yellow.body  # Swedish local time
    assert "Stockholms län, Uppsala län" in yellow.body and "Vad händer?: Mycket snö." in yellow.body
    assert alerts(_smhi_warnings("ORANGE"))[0].dedup_key == "smhi:10766:ORANGE"  # upgrade = new alert
    assert alerts(_smhi_warnings("RED")) == []            # not chosen for Stockholm
    assert alerts(_smhi_warnings("MESSAGE")) == []
    assert alerts(_smhi_warnings("ORANGE", area_ids=(12,))) == []   # Skåne is not followed
    assert alerts(_smhi_warnings("ORANGE", end="2026-11-12T10:00:00.000Z")) == []  # already over
    section.settings = {"warning_levels": ["RED"]}
    items[0].settings = {}
    assert len(alerts(_smhi_warnings("RED"))) == 1        # section default applies without own levels


def _weather_env(env, payload):
    from datetime import datetime, timezone
    from src.db.models import Section as S, WatchItem as W
    from src.sections import WeatherSection
    from src.services.weather import WeatherService
    client, services = env
    fake = FakeWeather(payload)
    now = lambda: datetime(2026, 11, 12, 22, 30, tzinfo=timezone.utc)
    services.weather = services.brief.weather = WeatherService(fake, now=now)
    plugin = WeatherSection(fake, now=now)
    with services.db.session() as session:
        section = S(slug="vader", name="Väder", kind="weather", position=9, settings={})
        section.watch_items = [
            W(type="warning_area", name="Stockholms län", symbol="1", keywords=[], settings={"levels": ["YELLOW", "ORANGE"]}),
            W(type="place", name="Stockholm", symbol="59.3293,18.0686", keywords=[], settings={}),
        ]
        session.add(section)
        session.flush()
        section_id = section.id
    return client, services, section_id, plugin


def test_weather_endpoints_alerts_and_brief(env, monkeypatch):
    from src.sections import PLUGINS
    client, services, vader, plugin = _weather_env(env, _smhi_warnings("ORANGE", area_ids=(1,)))
    monkeypatch.setitem(PLUGINS, "weather", plugin)

    overview = client.get(f"/api/sections/{vader}/weather").json()
    (place,) = overview["places"]
    assert place["name"] == "Stockholm" and place["now"]["temperature"] == -5
    assert len(place["hours"]) == 24 and place["days"][0]["date"] == "2026-11-13"
    assert place["days"][0]["symbol_text"] == "Snöfall" and place["days"][0]["precipitation"] == 12.0
    (warning,) = overview["warnings"]
    assert warning["level"] == "ORANGE" and warning["areas"] == ["Stockholms län"] and warning["alerting"]

    # Warning areas: listed, followed at the chosen levels; another area can be added and un-followed.
    areas = {a["id"]: a for a in client.get(f"/api/sections/{vader}/warning-areas").json()}
    assert len(areas) == 39 and areas[1]["enabled"] and areas[1]["levels"] == ["YELLOW", "ORANGE"]
    assert areas[12] == {**areas[12], "enabled": False, "watch_item_id": None}
    skane = client.put(f"/api/sections/{vader}/warning-areas/12", json={"enabled": True, "levels": ["RED"]}).json()
    assert skane["enabled"] and skane["levels"] == ["RED"] and skane["name"] == "Skåne län"
    off = client.put(f"/api/sections/{vader}/warning-areas/12", json={"enabled": False}).json()
    assert not off["enabled"] and off["watch_item_id"] == skane["watch_item_id"]
    assert client.put(f"/api/sections/{vader}/warning-areas/999", json={"enabled": True}).status_code == 404
    assert client.put(f"/api/sections/{vader}/warning-areas/1", json={"enabled": True, "levels": ["PINK"]}).status_code == 422
    ekonomi = next(s for s in client.get("/api/sections").json() if s["slug"] == "ekonomi")
    assert client.get(f"/api/sections/{ekonomi['id']}/weather").status_code == 422

    # The alert job emails the orange warning once.
    services.alerts._article_alerts = lambda: (0, 0)
    from src.sections import EconomySection
    monkeypatch.setitem(PLUGINS, "economy", EconomySection(FakeMarket({}, {})))
    assert client.post("/api/jobs/alerts").json() == {"created": 1, "rated": 0, "sent": 1}
    subject, text, html = services.notifier.sent[-1]
    assert subject == "Orange varning från SMHI: Snöfall, Norra Stockholms län" and "smhi.se" in html
    assert client.post("/api/jobs/alerts").json()["created"] == 0

    # The brief gets a weather block.
    services.brief.generate(day=date(2026, 11, 13), send=False)
    brief = client.get("/api/briefs/2026-11-13").json()
    assert brief["data"]["weather"]["places"][0]["max_temp"] == 18
    html = client.get("/api/briefs/2026-11-13/html").text
    assert "Väder" in html and "Orange varning" in html and "Snöfall" in html


def test_weather_migration_adds_section_to_seeded_database(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    url = f"sqlite:///{tmp_path}/m.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0001")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sections (slug, name, kind, enabled, position, settings) "
                          "VALUES ('sverige', 'Sverige', 'general', 1, 3, '{}')"))
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        section = conn.execute(text("SELECT id, slug, position FROM sections WHERE kind = 'weather'")).one()
        items = conn.execute(text("SELECT type, symbol, settings FROM watch_items WHERE section_id = :id"),
                             {"id": section.id}).all()
    assert (section.slug, section.position) == ("vader", 4)
    assert [(i.type, i.symbol) for i in items] == [("warning_area", "1"), ("place", "59.3293,18.0686")]


def test_weather_map_lists_every_county_with_warnings(env):
    client, services, _, _ = _weather_env(env, _smhi_warnings("ORANGE", area_ids=(1, 3)))
    result = client.get("/api/weather/map").json()
    regions = {r["id"]: r for r in result["regions"]}
    assert len(regions) == 21 and regions[1]["place"] == "Stockholm" and regions[25]["name"] == "Norrbottens län"
    assert regions[1]["warning_level"] == "ORANGE" and regions[3]["warning_level"] == "ORANGE"
    assert regions[12]["warning_level"] is None
    # From the current hour (22:00 UTC is before the first step at 23:00 UTC), with day summaries.
    assert regions[12]["steps"][0]["time"].startswith("2026-11-12T23:00") and regions[12]["days"]


# ---- electricity ---------------------------------------------------------------------------

def _price_payload(day: str, sek: float):
    return [{"SEK_per_kWh": sek + q * 0.01, "EUR_per_kWh": 0, "EXR": 11,
             "time_start": f"{day}T{h:02d}:{q * 15:02d}:00+01:00", "time_end": "x"}
            for h in range(24) for q in range(4)]


class FakeEnergy:
    def __init__(self):
        from src.sections.energy import parse_live, parse_outages
        self.live_snapshot = parse_live(
            {"unix_seconds": [1794547800, 1794548700],
             "production_types": [
                 {"name": "Nuclear", "data": [3667.0, None]}, {"name": "Hydro water reservoir", "data": [7523.4, None]},
                 {"name": "Wind onshore", "data": [11021.0, None]}, {"name": "Solar", "data": [1.7, None]},
                 {"name": "Others", "data": [1251.6, None]}, {"name": "Load", "data": [23877.0, None]},
                 {"name": "Cross border electricity trading", "data": [-5139.8, None]},
                 {"name": "Renewable share of load", "data": [77.7, None]}]},
            {"unix_seconds": [1794545100], "countries": [
                {"name": "Norway", "data": [-2.869]}, {"name": "Lithuania", "data": [0.461]}, {"name": "sum", "data": [-5.8]}]},
        )
        unit = lambda name, mw, start, stop, field="generationUnits": {
            "isOutdated": False, "unavailabilityType": 2, "unavailabilityReason": "Delayed startup.",
            field: [{"productionUnitName": None, "name": name, "fuelType": 14, "installedCapacity": mw,
                     "timePeriods": [{"unavailableCapacity": mw, "eventStart": start, "eventStop": stop}]}]}
        self.outage_list = parse_outages({"items": [
            unit("Ringhals Block4", 1134, "2026-09-01T22:00:00Z", "2026-11-16T22:59:00Z"),
            unit("Forsmark Block1 G11", 1098, "2026-09-06T10:00:00Z", "2026-11-17T11:00:00Z", "productionUnits"),
            unit("Forsmark Block1 G11", 549, "2026-11-17T11:00:00Z", "2026-12-03T22:59:00Z"),
            {**unit("Ringhals 3", 1081, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"), "isOutdated": True},
        ]})

    def prices(self, day, zone):
        if day > date(2026, 11, 13):
            return None  # tomorrow not published yet
        from src.sections.energy import parse_prices
        sek = 1.0 if day == date(2026, 11, 13) else 2.0
        return parse_prices(_price_payload(day.isoformat(), sek * (2 if zone == "SE4" else 1)))

    def live(self):
        return self.live_snapshot

    def outages(self, start, end):
        return self.outage_list


class OutlookSummarizer(FakeSummarizer):
    def __init__(self, fail=False):
        super().__init__()
        self.fail, self.payloads = fail, []

    def energy_outlook(self, data):
        from src.ai.base import OutlookText
        self.payloads.append(data)
        if self.fail:
            raise RuntimeError("quota")
        return OutlookText(headline="Billigt idag", text="Det blåser.")


def _energy_env(env, summarizer):
    from datetime import datetime, timezone
    from src.db.models import Section as S
    from src.services.energy import EnergyService
    from src.services.weather import WeatherService
    client, services = env
    now = lambda: datetime(2026, 11, 13, 8, 30, tzinfo=timezone.utc)
    weather = WeatherService(FakeWeather(), now=now)
    services.energy = EnergyService(FakeEnergy(), weather=weather, summarizer=summarizer, now=now)
    with services.db.session() as session:
        section = S(slug="el", name="El", kind="energy", position=10, settings={"zone": "se4"})
        session.add(section)
        session.flush()
        return client, services, section.id


def test_energy_overview(env):
    client, services, el = _energy_env(env, OutlookSummarizer())
    result = client.get(f"/api/sections/{el}/energy").json()
    assert result["zone"] == "SE4" and result["errors"] == []
    zones = {z["zone"]: z for z in result["zones"]}
    # Quarter-hours become hourly averages in öre/kWh; 08:30 UTC is the 09:00 hour in Sweden.
    assert len(zones["SE3"]["today"]) == 24 and zones["SE3"]["now"] == 101.5 and zones["SE3"]["avg_today"] == 101.5
    assert zones["SE4"]["avg_today"] == 201.5 and zones["SE3"]["tomorrow"] == [] and zones["SE3"]["avg_tomorrow"] is None
    live = result["live"]
    assert live["production"] == {"nuclear": 3667, "hydro": 7523, "wind": 11021, "solar": 2, "other": 1252}
    assert live["net_export"] == 5139.8 and live["flows"] == {"Norge": 2869, "Litauen": -461}
    reactors = {r["name"]: r for r in result["reactors"]}
    assert reactors["Ringhals 4"]["available"] == 0 and reactors["Ringhals 4"]["back_at"].startswith("2026-11-16T22:59")
    # Forsmark 1 comes back in two steps; "fully back" is the end of the second.
    assert reactors["Forsmark 1"]["available"] == 6 and reactors["Forsmark 1"]["back_at"].startswith("2026-12-03")
    assert reactors["Ringhals 3"]["available"] == 1081 and reactors["Ringhals 3"]["back_at"] is None
    assert result["nuclear_available"] == 7012 - 1134 - 1098
    ekonomi = next(s for s in client.get("/api/sections").json() if s["slug"] == "ekonomi")
    assert client.get(f"/api/sections/{ekonomi['id']}/energy").status_code == 422


def test_energy_outlook_ai_text_and_rules_fallback(env):
    summarizer = OutlookSummarizer()
    client, services, el = _energy_env(env, summarizer)
    result = client.get(f"/api/sections/{el}/energy/outlook").json()
    assert (result["headline"], result["text"], result["generator"]) == ("Billigt idag", "Det blåser.", "fake")
    days = result["days"]
    assert len(days) == 7 and days[0]["date"] == "2026-11-13"
    # Today's published price (201.5) is far below last week's (401.5): low, whatever the weather says.
    assert result["recent_price"] == 401.5 and days[0]["price"] == 201.5 and days[0]["level"] == "low"
    assert days[0]["nuclear_out"] == ["Forsmark 1", "Ringhals 4"] and days[0]["nuclear_available"] == 7012 - 1134 - 1098
    # Tuesday 17/11: Ringhals 4 is back and Forsmark 1 half back (549 MW out, so not counted as "out").
    assert days[4]["nuclear_out"] == [] and days[4]["nuclear_available"] == 7012 - 549
    payload = summarizer.payloads[0]
    assert payload["zone"] == "SE4" and {r["name"] for r in payload["reactors"]} == {"Forsmark 1", "Ringhals 4"}
    # Cached: a second request does not call the AI again.
    client.get(f"/api/sections/{el}/energy/outlook")
    assert len(summarizer.payloads) == 1

    # AI failure: template text, and the error is shown.
    services.energy.summarizer = OutlookSummarizer(fail=True)
    services.energy._outlook.clear()
    result = client.get(f"/api/sections/{el}/energy/outlook").json()
    assert result["generator"] == "rules" and "quota" in result["errors"][0]
    assert result["text"].startswith("Idag:") and "Ringhals 4 väntas vara tillbaka i full drift måndag 16/11" in result["text"]


def test_energy_rate_day():
    from datetime import date as d
    from src.sections.energy import OutlookDay
    from src.services.energy import rate_day
    cold_calm = rate_day(OutlookDay(date=d(2026, 11, 16), temperature=-6, normal_temperature=3.5, wind=2.0,
                                    nuclear_available=3700, nuclear_out=["Ringhals 3", "Ringhals 4", "Forsmark 1"]))
    assert cold_calm.level == "high" and cold_calm.reasons == ["mycket kallt", "svag vind", "3 reaktorer ur drift"]
    windy = rate_day(OutlookDay(date=d(2026, 11, 16), temperature=4, normal_temperature=3.5, wind=6.5,
                                nuclear_available=7012))
    assert windy.level == "low" and windy.reasons == ["mycket blåsigt"]


def test_energy_migration_adds_section_to_seeded_database(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text
    url = f"sqlite:///{tmp_path}/m.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "0001")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sections (slug, name, kind, enabled, position, settings) "
                          "VALUES ('sverige', 'Sverige', 'general', 1, 3, '{}')"))
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        section = conn.execute(text("SELECT slug, name, position, settings FROM sections WHERE kind = 'energy'")).one()
    assert (section.slug, section.name, section.position) == ("el", "El", 5) and "SE3" in section.settings
