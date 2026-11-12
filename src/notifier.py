"""Notification channels. Add one by subclassing Notifier."""

from __future__ import annotations

import logging
import os
import smtplib
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from src.settings import EmailSettings

log = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, subject: str, text: str, html: str | None = None) -> None: ...


class LogNotifier(Notifier):
    def send(self, subject: str, text: str, html: str | None = None) -> None:
        log.info("NOTIFICATION: %s\n%s", subject, text)


class EmailNotifier(Notifier):
    def __init__(self, settings: EmailSettings):
        self.settings = settings

    def send(self, subject: str, text: str, html: str | None = None) -> None:
        password = os.environ.get(self.settings.password_env)
        if not password:
            raise RuntimeError(f"Environment variable {self.settings.password_env} is not set")
        if not self.settings.recipients:
            raise RuntimeError("email.recipients is empty")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.settings.sender
        msg["To"] = ", ".join(self.settings.recipients)
        msg.attach(MIMEText(text, "plain", "utf-8"))
        if html:
            msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as conn:
            conn.starttls()
            conn.login(self.settings.sender, password)
            conn.send_message(msg)
        log.info("Email '%s' sent to %d recipient(s)", subject, len(self.settings.recipients))


def build_notifier(settings: EmailSettings) -> Notifier:
    return EmailNotifier(settings) if settings.enabled else LogNotifier()
