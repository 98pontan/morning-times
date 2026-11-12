from datetime import date, timedelta

from src.db.models import Section, WatchItem
from src.sections import EconomySection
from src.sections.market import MarketData, Quote
from src.sources import RssFetcher

from tests.conftest import feed_xml


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
