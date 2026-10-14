"""Reusable transactional email service for the License Management Portal.

This service is intentionally generic. It reads all connection settings from
environment variables so no credentials are ever hardcoded or committed.

Supported configuration (environment variables):

    EMAIL_PROVIDER            "smtp" (default). Other values are reserved for
                              future providers (e.g. Microsoft Graph).
    SMTP_HOST                 SMTP server hostname.
    SMTP_PORT                 SMTP server port (default 587).
    SMTP_USERNAME             SMTP username (optional for some relays).
    SMTP_PASSWORD             SMTP password / app password.
    SMTP_USE_TLS              "true"/"false" - use STARTTLS (default true).
    SMTP_USE_SSL              "true"/"false" - use implicit SSL (default false).
    EMAIL_FROM_ADDRESS        Envelope/From address.
    EMAIL_FROM_NAME           Display name for the From header.
    EMAIL_ENABLED             "true"/"false" - when false, sending is skipped
                              and ``send_email`` returns a "skipped" result
                              instead of raising.

Delivery results are returned as ``EmailResult`` objects so callers can
distinguish a successful send from a skipped/failed one and never falsely
report success.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from typing import Callable


logger = logging.getLogger("licensehub.email")


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EmailConfig:
    """SMTP configuration resolved from environment variables."""

    host: str
    port: int
    username: str | None
    password: str | None
    use_tls: bool
    use_ssl: bool
    from_address: str
    from_name: str
    enabled: bool
    provider: str = "smtp"

    @classmethod
    def from_env(cls) -> "EmailConfig":
        raw_port = (os.getenv("SMTP_PORT") or "").strip() or "587"
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError(
                "SMTP_PORT must be an integer; check the backend environment."
            ) from exc

        return cls(
            host=(os.getenv("SMTP_HOST") or "").strip(),
            port=port,
            username=(os.getenv("SMTP_USERNAME") or "").strip() or None,
            password=os.getenv("SMTP_PASSWORD") or None,
            use_tls=_as_bool(os.getenv("SMTP_USE_TLS"), default=True),
            use_ssl=_as_bool(os.getenv("SMTP_USE_SSL"), default=False),
            from_address=(os.getenv("EMAIL_FROM_ADDRESS") or "").strip(),
            from_name=(os.getenv("EMAIL_FROM_NAME") or "LicenseHub CMDB").strip(),
            enabled=_as_bool(os.getenv("EMAIL_ENABLED"), default=True),
            provider=(os.getenv("EMAIL_PROVIDER") or "smtp").strip().lower(),
        )

    def missing_settings(self) -> list[str]:
        """Return the list of required settings that are not configured."""
        missing: list[str] = []
        if not self.host:
            missing.append("SMTP_HOST")
        if not self.from_address:
            missing.append("EMAIL_FROM_ADDRESS")
        return missing


@dataclass(frozen=True)
class EmailResult:
    """Outcome of an email delivery attempt."""

    delivered: bool
    skipped: bool = False
    error: str | None = None
    recipients: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "delivered": self.delivered,
            "skipped": self.skipped,
            "error": self.error,
            "recipients": list(self.recipients),
        }


# A pluggable low-level sender. Production uses SMTP; tests replace this with a
# fake so no real network delivery happens.
EmailSender = Callable[[EmailConfig, EmailMessage, list[str]], None]


class EmailService:
    """Send transactional emails over SMTP.

    The service never raises on delivery failure. Callers receive an
    ``EmailResult`` describing success, a configured-off skip, or the failure
    reason. Failures are logged without leaking credentials.
    """

    def __init__(
        self,
        config: EmailConfig | None = None,
        sender: EmailSender | None = None,
    ) -> None:
        self.config = config or EmailConfig.from_env()
        self._sender = sender or self._send_via_smtp

    def is_configured(self) -> tuple[bool, str | None]:
        """Return ``(configured, reason)``.

        ``configured`` is False when email is disabled or required settings are
        missing. ``reason`` explains why so callers can surface a clear message.
        """
        if not self.config.enabled:
            return False, "Email delivery is disabled (EMAIL_ENABLED=false)."
        missing = self.config.missing_settings()
        if missing:
            return False, (
                "Email is not configured. Set the following environment "
                f"variables: {', '.join(missing)}."
            )
        if self.config.provider != "smtp":
            return False, (
                f"Unsupported EMAIL_PROVIDER '{self.config.provider}'. "
                "Only 'smtp' is currently implemented."
            )
        return True, None

    def send_email(
        self,
        *,
        to_addresses: list[str],
        subject: str,
        html_body: str,
        text_body: str | None = None,
    ) -> EmailResult:
        """Send a single email. Returns an ``EmailResult`` and never raises."""
        recipients = [addr.strip() for addr in to_addresses if addr and addr.strip()]
        if not recipients:
            return EmailResult(
                delivered=False,
                error="No recipient address was provided.",
            )

        configured, reason = self.is_configured()
        if not configured:
            logger.warning("Email not sent (%s): %s", subject, reason)
            return EmailResult(
                delivered=False,
                skipped=True,
                error=reason,
                recipients=tuple(recipients),
            )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((self.config.from_name, self.config.from_address))
        message["To"] = ", ".join(recipients)
        if text_body:
            message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        try:
            self._sender(self.config, message, recipients)
        except Exception as exc:  # noqa: BLE001 - deliberately broad for delivery
            logger.error(
                "Email delivery failed for subject %r to %s: %s",
                subject,
                recipients,
                exc,
            )
            return EmailResult(
                delivered=False,
                error=f"Email delivery failed: {exc}",
                recipients=tuple(recipients),
            )

        logger.info("Email delivered for subject %r to %s", subject, recipients)
        return EmailResult(delivered=True, recipients=tuple(recipients))

    @staticmethod
    def _send_via_smtp(
        config: EmailConfig,
        message: EmailMessage,
        recipients: list[str],
    ) -> None:
        if config.use_ssl:
            smtp: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
                config.host, config.port, timeout=30
            )
        else:
            smtp = smtplib.SMTP(config.host, config.port, timeout=30)
        try:
            smtp.ehlo()
            if config.use_tls and not config.use_ssl:
                smtp.starttls()
                smtp.ehlo()
            if config.username and config.password:
                smtp.login(config.username, config.password)
            smtp.send_message(message, from_addr=config.from_address, to_addrs=recipients)
        finally:
            try:
                smtp.quit()
            except Exception:  # pragma: no cover - best-effort close
                smtp.close()


_default_service: EmailService | None = None


def get_email_service() -> EmailService:
    """Return a process-wide default ``EmailService``."""
    global _default_service
    if _default_service is None:
        _default_service = EmailService()
    return _default_service


def reset_email_service_for_tests() -> None:
    """Reset the cached default service (used by the test-suite)."""
    global _default_service
    _default_service = None

