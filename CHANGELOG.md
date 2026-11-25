# Changelog

All notable changes to the backend. Versions follow [semantic versioning](https://semver.org).

## [0.1.0] - unreleased

First versioned release.

### Added
- Login with Keycloak (OIDC). Visitors can read; changes need a member of the `newspaper` group.
- Personal watches: each user follows their own stocks, teams, regions, places and warning areas,
  starting from a copy of the defaults. Alerts are emailed to the owner (migration 0004).
- `GET /api/auth` (login settings for the UI), `GET /api/me`, version in `/healthz`.
- Weather map of Sweden per county with a time slider (`GET /api/weather/map`).
- Energy section "El": spot prices per zone, live production and flows, nuclear status and a
  weekly price outlook written by AI once per day (migration 0003).
- AI fallback chain: Gemini, then OpenRouter (free models), Mistral or a local Ollama when configured.
  Gemini retries short network and "high demand" errors.

### Changed
- CI tags images with the branch name, the short commit and, for `v*` tags, the version.
  `latest` only follows main.
- Rule-based outlook headline names the days that stand out; reactor returns are sorted by date.
