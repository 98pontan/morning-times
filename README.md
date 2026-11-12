# The Morning Times

A configurable news service: it collects Swedish and international news into sections
(world, Sweden, politics, economy, sports, local), follows the stocks, teams and regions you
care about, emails alerts on important events, and sends a Morning Brew-style brief every morning.

This repository is the **backend** (FastAPI). A frontend will use the REST API under `/api`.

## How it works

| Job | Default schedule | What it does |
|---|---|---|
| Ingest | every 15 min | Fetches all enabled RSS sources, stores new articles, tags them with watched items (keyword match) |
| Alerts | every 30 min | Stock moves above a threshold and upcoming quarterly reports (Yahoo Finance); the AI rates new articles about watched items, and those rated ≥ 7/10 are emailed |
| Brief | 06:30 Europe/Stockholm | The AI writes the brief from the last 24 h per section; stored in the database and emailed as HTML |

The AI provider is set with `ai.provider`:

| Provider | Key | Notes |
|---|---|---|
| `gemini` | `GEMINI_API_KEY` | Google Gemini free tier, fine for personal use. Google's terms require paid services if the app is offered to other users in the EEA. |
| `claude` | `ANTHROPIC_API_KEY` | Claude API. `claude_model: claude-haiku-5-5` costs about $0.20/month for the brief. |
| `rules` | none | No AI: the brief is a list of headlines and only market alerts are sent. |
| `auto` | | Claude if its key is set, else Gemini, else rules. |

If the chosen provider has no key, or a request fails (quota, network), the app falls back to
`rules` for that brief, so the morning email still arrives.

## Configuration

- `config.yaml`: runtime settings (schedule, email, model). Override any value with
  `APP_<SECTION>__<KEY>`, e.g. `APP_SCHEDULER__BRIEF_TIME=07:00`.
- `seed.yaml`: initial sections, sources, stocks, teams and regions. Loaded **only into an empty
  database**; after that, edit everything through the API (and later the frontend).
- Secrets, from environment variables only:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | e.g. `postgresql+psycopg://user:pass@host:5432/morning_times` (default: local SQLite) |
| `GEMINI_API_KEY` | Gemini API key (Google AI Studio) when `ai.provider: gemini` |
| `ANTHROPIC_API_KEY` | Claude API key when `ai.provider: claude` |
| `EMAIL_PASSWORD` | Gmail app password for `email.sender` |

## Run locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python main.py          # http://localhost:8080/docs for the interactive API
.venv/bin/pytest                  # tests (no network, no API calls)
```

Useful calls:

```bash
curl -X POST localhost:8080/api/jobs/ingest                 # fetch news now
curl -X POST "localhost:8080/api/briefs/generate?send=false" # write today's brief without emailing
open http://localhost:8080/api/briefs/$(date +%F)/html        # view it
curl localhost:8080/api/sections/6/regions                  # SVT local editions, which are picked
curl -X PUT localhost:8080/api/sections/6/regions/skane \
  -H 'content-type: application/json' -d '{"enabled": true}'  # pick Skåne (fetched at once)
curl localhost:8080/api/sections/4/market                    # stock screen: watched stocks + OMXS30
curl "localhost:8080/api/market/chart?symbol=VOLV-B.ST&range=3m"  # 1d 1w 1m 3m ytd 1y 5y
curl -X POST localhost:8080/api/sections/4/watch-items \
  -H 'content-type: application/json' \
  -d '{"type": "stock", "name": "Atlas Copco", "symbol": "ATCO-A.ST", "settings": {"move_threshold_pct": 4}}'
```

## Structure

```
main.py                 entry point
src/app.py              wiring: Services + FastAPI app + scheduler
src/settings.py         config.yaml + env overrides
src/db/                 SQLAlchemy models and database/migrations helper
src/sources/            fetchers (RssFetcher); add new source types here
src/sections/           section kinds: general, economy (stocks), sports (teams), regional
src/ai/                 Summarizer interface: GeminiSummarizer, ClaudeSummarizer, RuleSummarizer
src/services/           ingest, alerts, brief, HTML rendering
src/api/                REST API (sections, sources, watch items, articles, briefs, alerts, jobs)
src/templates/          email templates
migrations/             Alembic (DATABASE_URL=... alembic revision --autogenerate -m "...")
```

Extending: a new section kind is a `SectionPlugin` subclass in `src/sections`, a new source type
is a `Fetcher` subclass in `src/sources`, a new notification channel is a `Notifier` subclass.

## Notes

- Run **one replica**: the scheduler runs inside the app, so more replicas would send duplicate emails.
- Yahoo Finance data (via `yfinance`) is unofficial and delayed about 15 minutes.
- Article texts from feeds are passed to the AI as data inside tags, with instructions to never
  follow instructions found in them.
