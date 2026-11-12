from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.ai import ArticleRating, BriefContent, BriefHighlight, BriefSection, Summarizer
from src.app import Services, create_app
from src.notifier import Notifier
from src.settings import Settings

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Volvo lämnar vinstvarning</title><link>https://example.com/volvo</link>
  <description>&lt;p&gt;Volvos aktie rasar efter beskedet.&lt;/p&gt;</description>
  <pubDate>{now}</pubDate></item>
<item><title>Regn i Göteborg hela veckan</title><link>https://example.com/gbg</link>
  <description>SMHI varnar.</description><pubDate>{now}</pubDate></item>
<item><title>Ericssonfonden delar ut stipendier</title><link>https://example.com/fonden</link>
  <description></description><pubDate>{now}</pubDate></item>
</channel></rss>"""


def feed_xml() -> str:
    now = datetime.now(timezone.utc) - timedelta(minutes=5)
    return FEED.format(now=now.strftime("%a, %d %b %Y %H:%M:%S +0000"))


class FakeSummarizer(Summarizer):
    name = "fake"

    def __init__(self):
        self.rated = []

    def rate(self, articles):
        self.rated += articles
        return [ArticleRating(article_id=a.id, importance=9 if "vinstvarning" in a.title else 3, reason="test")
                for a in articles]

    def write_brief(self, day: date, sections):
        return BriefContent(headline="Godmorgon", intro="Intro.", sections=[
            BriefSection(slug=s.slug, title=s.name, summary="Sammanfattning.",
                         highlights=[BriefHighlight(article_id=a.id, text=a.title) for a in s.articles[:3]])
            for s in sections if s.articles
        ])


class RecordingNotifier(Notifier):
    def __init__(self):
        self.sent = []

    def send(self, subject, text, html=None):
        self.sent.append((subject, text, html))


SEED = """
sections:
  - slug: ekonomi
    name: Ekonomi
    kind: economy
    sources: [{name: Test, url: "https://example.com/feed.xml"}]
    watch:
      - {type: stock, name: Volvo, symbol: VOLV-B.ST}
      - {type: stock, name: Ericsson, symbol: ERIC-B.ST}
  - slug: lokalt
    name: Lokalt
    kind: regional
    sources: [{name: Lokal, url: "https://example.com/local.xml"}]
    watch:
      - {type: region, name: Göteborg}
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    seed = tmp_path / "seed.yaml"
    seed.write_text(SEED)
    settings = Settings.model_validate({
        "database": {"url": f"sqlite:///{tmp_path}/test.db"},
        "scheduler": {"enabled": False},
        "seed_file": str(seed),
    })
    # Serve the test feed instead of hitting the network.
    from src.sources import RssFetcher
    monkeypatch.setattr(RssFetcher, "fetch", lambda self, url: RssFetcher.parse(feed_xml()))
    services = Services.build(settings, summarizer=FakeSummarizer(), notifier=RecordingNotifier())
    app = create_app(settings, services)
    with TestClient(app) as client:
        yield client, services
