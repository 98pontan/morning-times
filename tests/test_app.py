from datetime import date

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
