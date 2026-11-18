"""Users from Keycloak: created at first login with a copy of the default watches."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.auth import Identity
from src.db.models import Section, User, WatchItem, utcnow
from src.services.ingest import rematch_section

log = logging.getLogger(__name__)


def user_for(session: Session, identity: Identity) -> User:
    """The user for a verified token; created (with the default watches) on first login."""
    user = session.scalar(select(User).where(User.subject == identity.subject))
    if user is None:
        try:
            with session.begin_nested():
                user = User(subject=identity.subject, username=identity.username, name=identity.name,
                            email=identity.email)
                session.add(user)
                session.flush()
        except IntegrityError:
            # Two first requests at once: the other one created the user.
            user = session.scalar(select(User).where(User.subject == identity.subject))
        else:
            copy_default_watches(session, user)
            log.info("New user %s (%s)", user.username, user.email or "no email")
            return user
    user.username, user.name, user.email = identity.username, identity.name, identity.email
    user.last_seen_at = utcnow()
    return user


def copy_default_watches(session: Session, user: User) -> int:
    defaults = session.scalars(select(WatchItem).where(WatchItem.owner_id.is_(None))).all()
    for w in defaults:
        session.add(WatchItem(
            section_id=w.section_id, type=w.type, name=w.name, symbol=w.symbol,
            keywords=list(w.keywords or []), settings=dict(w.settings or {}), enabled=w.enabled, owner_id=user.id,
        ))
    session.flush()
    # Tag recent articles with the new copies so the user's lists are complete at once.
    for section_id in {w.section_id for w in defaults}:
        rematch_section(session, session.get(Section, section_id))
    return len(defaults)
