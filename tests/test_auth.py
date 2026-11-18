"""Login with Keycloak tokens: read-only for visitors, personal watches for users."""

from tests.conftest import ISSUER


def _section(client, slug):
    return next(s for s in client.get("/api/sections").json() if s["slug"] == slug)


def test_auth_config_is_public(auth_env):
    client, _, _ = auth_env
    cfg = client.get("/api/auth").json()
    assert cfg == {"enabled": True, "issuer": ISSUER, "client_id": "morning-times", "required_group": "newspaper",
                   "version": "0.1.0"}


def test_visitors_can_read_but_not_change(auth_env):
    client, _, _ = auth_env
    eko = _section(client, "ekonomi")
    assert [w["name"] for w in client.get(f"/api/sections/{eko['id']}/watch-items").json()] == ["Volvo", "Ericsson"]
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/config").status_code == 401  # shows email recipients
    denied = [
        client.post(f"/api/sections/{eko['id']}/watch-items", json={"type": "stock", "name": "SAAB", "symbol": "SAAB-B.ST"}),
        client.patch(f"/api/sections/{eko['id']}", json={"name": "Pengar"}),
        client.post(f"/api/sections/{eko['id']}/sources", json={"name": "X", "url": "https://x.test/rss"}),
        client.delete(f"/api/sections/{eko['id']}"),
        client.post("/api/briefs/generate"),
        client.post("/api/jobs/ingest"),
    ]
    assert [r.status_code for r in denied] == [401] * len(denied)
    assert denied[0].json()["detail"] == "Logga in för att ändra"
    assert client.get("/api/alerts").json() == []


def test_rejected_tokens(auth_env):
    client, _, tokens = auth_env
    cases = {
        "not in group": (tokens.headers(groups=["family"]), 403),
        "same-named subgroup": (tokens.headers(groups=["/family/newspaper"]), 403),
        "other client": (tokens.headers(azp="grafana"), 401),
        "expired": (tokens.headers(expires_in=-120), 401),
        "forged": (tokens.headers(wrong_key=True), 401),
        "other realm": (tokens.headers(issuer="https://keycloak.test/realms/master"), 401),
        "garbage": ({"Authorization": "Bearer nope"}, 401),
    }
    for name, (headers, status) in cases.items():
        assert client.get("/api/me", headers=headers).status_code == status, name
    assert "newspaper" in client.get("/api/me", headers=tokens.headers(groups=["family"])).json()["detail"]
    # Keycloak's "full group path" form is accepted too.
    assert client.get("/api/me", headers=tokens.headers(groups=["/newspaper"])).status_code == 200


def test_watches_are_personal(auth_env):
    client, services, tokens = auth_env
    anna, bo = tokens.headers(sub="u-anna", username="anna"), tokens.headers(sub="u-bo", username="bo")
    eko = _section(client, "ekonomi")
    url = f"/api/sections/{eko['id']}/watch-items"

    assert client.get("/api/me", headers=anna).json() == {
        "username": "anna", "name": "Anna", "email": "anna@example.com", "login_enabled": True}
    # First login: a copy of the defaults (new ids), so changes never touch what visitors see.
    defaults = client.get(url).json()
    mine = client.get(url, headers=anna).json()
    assert [w["name"] for w in mine] == ["Volvo", "Ericsson"]
    assert not {w["id"] for w in mine} & {w["id"] for w in defaults}

    saab = client.post(url, headers=anna, json={"type": "stock", "name": "SAAB", "symbol": "SAAB-B.ST"})
    assert saab.status_code == 201
    volvo = next(w for w in mine if w["name"] == "Volvo")
    assert client.delete(f"/api/watch-items/{volvo['id']}", headers=anna).status_code == 204

    assert [w["name"] for w in client.get(url, headers=anna).json()] == ["Ericsson", "SAAB"]
    assert [w["name"] for w in client.get(url).json()] == ["Volvo", "Ericsson"]  # visitors: unchanged
    assert [w["name"] for w in client.get(url, headers=bo).json()] == ["Volvo", "Ericsson"]  # bo: own copy

    # Nobody can change someone else's watches or the defaults.
    assert client.patch(f"/api/watch-items/{saab.json()['id']}", headers=bo, json={"enabled": False}).status_code == 404
    assert client.delete(f"/api/watch-items/{defaults[0]['id']}", headers=anna).status_code == 404

    # Shared settings: any member can change them.
    assert client.patch(f"/api/sections/{eko['id']}", headers=bo, json={"name": "Pengar"}).status_code == 200


def test_alerts_go_to_the_owner(auth_env):
    client, services, tokens = auth_env
    anna = tokens.headers(sub="u-anna", username="anna", email="anna@example.com")
    client.get("/api/me", headers=anna)  # first login copies Volvo/Ericsson
    services.ingest.run()

    # Watched-only articles and "watched" names follow the viewer.
    mine = client.get("/api/articles?watched_only=true", headers=anna).json()
    assert {a["title"]: a["watched"] for a in mine} == {
        "Volvo lämnar vinstvarning": ["Volvo"], "Regn i Göteborg hela veckan": ["Göteborg"]}
    lokalt = _section(client, "lokalt")["id"]
    gbg = client.get(f"/api/sections/{lokalt}/watch-items", headers=anna).json()[0]
    assert client.delete(f"/api/watch-items/{gbg['id']}", headers=anna).status_code == 204
    assert len(client.get("/api/articles?watched_only=true", headers=anna).json()) == 1
    assert len(client.get("/api/articles?watched_only=true").json()) == 2  # visitors: default watches

    result = services.alerts.run()
    # Only anna's copy alerts: default watches are silent when login is on.
    assert result.created == 1 and result.sent == 1
    assert services.notifier.recipients == [["anna@example.com"]]
    alerts = client.get("/api/alerts", headers=anna).json()
    assert [a["subject"] for a in alerts] == ["Volvo: Volvo lämnar vinstvarning"]
    assert client.get("/api/alerts", headers=tokens.headers(sub="u-bo", username="bo")).json() == []


def test_login_needs_an_issuer():
    import pytest
    from src.auth import TokenVerifier
    from src.settings import AuthSettings
    with pytest.raises(ValueError, match="issuer"):
        TokenVerifier(AuthSettings(enabled=True))
