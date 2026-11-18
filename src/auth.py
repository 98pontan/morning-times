"""Keycloak login: verify the UI's access tokens and map them to users.

The UI logs in with Authorization Code + PKCE (public client) and sends the access token as
`Authorization: Bearer ...`. Here we check the signature against the realm's keys, the issuer,
the expiry and that the token was issued to our client (`azp`), then require the group.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import jwt

from src.settings import AuthSettings

log = logging.getLogger(__name__)


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Identity:
    subject: str
    username: str
    name: str = ""
    email: str | None = None
    groups: list[str] = field(default_factory=list)


def in_group(groups: list[str], group: str) -> bool:
    """Keycloak sends group names, or full paths like "/newspaper" when "Full group path" is on.

    Only the top-level group counts: "/other/newspaper" is a different group that happens to share the name.
    """
    return group in groups or f"/{group}" in groups


class TokenVerifier:
    def __init__(self, settings: AuthSettings, signing_key: Callable[[str], object] | None = None):
        if not settings.issuer:
            raise ValueError("auth.enabled is true but auth.issuer is empty (e.g. https://keycloak.example.com/realms/homelab)")
        self.settings = settings
        self.issuer = settings.issuer.rstrip("/")
        if signing_key is None:
            # Keys are cached and refetched when Keycloak rotates them (unknown `kid`).
            client = jwt.PyJWKClient(f"{self.issuer}/protocol/openid-connect/certs", cache_keys=True, lifespan=3600)
            signing_key = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._signing_key = signing_key

    def verify(self, token: str) -> Identity:
        try:
            key = self._signing_key(token)
            claims = jwt.decode(
                token, key, algorithms=["RS256", "ES256", "PS256"], issuer=self.issuer,
                # Keycloak access tokens have aud "account" unless an audience mapper is added; azp is checked below.
                options={"verify_aud": False, "require": ["exp", "iat", "sub"]}, leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError(401, "Inloggningen har gått ut") from exc
        except (jwt.PyJWTError, ValueError) as exc:
            log.info("Rejected token: %s", exc)
            raise AuthError(401, "Ogiltig inloggning") from exc

        if claims.get("azp") != self.settings.client_id:
            raise AuthError(401, "Token är inte utfärdad för morning-times")
        groups = claims.get("groups") or []
        if not in_group(groups, self.settings.required_group):
            raise AuthError(403, f"Du måste vara med i gruppen '{self.settings.required_group}' för att logga in")
        return Identity(
            subject=claims["sub"],
            username=claims.get("preferred_username") or claims["sub"],
            name=claims.get("name") or "",
            email=claims.get("email") if claims.get("email_verified", True) else None,
            groups=groups,
        )
