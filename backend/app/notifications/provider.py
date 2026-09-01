"""The transport a run summary leaves by, behind one small interface.

``build_email_provider()`` is the only place in this codebase that decides which
transport is in use — the same rule ``app.llm.provider`` follows, for the same
reason: the service that composes a summary must not know or care whether it
leaves by SMTP, by a hosted API, or nowhere at all. Swapping the transport is a
change in this file and in the environment; it is not a change anywhere a
business rule lives.

``NullProvider`` is a first-class outcome, not a test double. A deployment with no
mail host is the default deployment, and a run that completes there must still
complete: the provider reports why nothing was sent and the caller records that
as the state of the summary.
"""

from __future__ import annotations

import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage as _MimeMessage

from app.notifications.config import EmailConfig, load_email_config


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One composed summary, ready to hand to a transport."""

    subject: str
    body: str
    recipients: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SendResult:
    """What happened, in the shape the caller stores and audits.

    ``sent`` is the only thing a caller branches on. ``reason`` is filled whenever
    it is false, so "nothing was sent" is never silent.
    """

    sent: bool
    provider: str
    recipient_count: int = 0
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "sent": self.sent,
            "provider": self.provider,
            "recipient_count": self.recipient_count,
            "reason": self.reason,
        }


class EmailProvider(ABC):
    """Send a composed message, or say why it could not be sent."""

    name: str = "unknown"

    @abstractmethod
    def send(self, message: EmailMessage) -> SendResult: ...

    def describe(self) -> dict[str, object]:
        return {"provider": self.name}


class NullProvider(EmailProvider):
    """No transport configured. Reports the reason and sends nothing."""

    name = "none"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def send(self, message: EmailMessage) -> SendResult:
        return SendResult(sent=False, provider=self.name, reason=self._reason)

    def describe(self) -> dict[str, object]:
        return {"provider": self.name, "unavailable_reason": self._reason}


class SmtpProvider(EmailProvider):
    """Plain SMTP over the standard library.

    Deliberately the whole implementation: a run summary is a short text mail to a
    configured list, and a dependency would buy nothing here. STARTTLS is used
    when configured, and a failure returns a :class:`SendResult` rather than
    raising — the summary is a notification about work that already completed and
    is already stored, so a mail server being down must not turn a successful run
    into a failed request.
    """

    name = "smtp"

    def __init__(self, config: EmailConfig) -> None:
        self._config = config

    def send(self, message: EmailMessage) -> SendResult:
        config = self._config
        mime = _MimeMessage()
        mime["Subject"] = message.subject
        mime["From"] = config.sender
        mime["To"] = ", ".join(message.recipients)
        mime.set_content(message.body)

        try:
            with smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds) as client:
                if config.use_tls:
                    client.starttls(context=ssl.create_default_context())
                if config.username:
                    client.login(config.username, config.password)
                client.send_message(mime)
        except Exception as exc:  # noqa: BLE001 - reported, never raised onward
            # The class name and message only. A traceback from a mail library can
            # carry the credentials it was handed.
            return SendResult(
                sent=False,
                provider=self.name,
                reason=f"The mail server refused the summary ({type(exc).__name__}).",
            )
        return SendResult(
            sent=True, provider=self.name, recipient_count=len(message.recipients)
        )

    def describe(self) -> dict[str, object]:
        return self._config.describe()


def build_email_provider(config: EmailConfig | None = None) -> EmailProvider:
    """The one dispatch point. Everything else in the platform takes what it returns."""

    resolved = config or load_email_config()
    reason = resolved.unavailable_reason
    if reason is not None:
        return NullProvider(reason)
    if resolved.provider == "smtp":
        return SmtpProvider(resolved)
    # Unreachable while ``unavailable_reason`` validates the provider name; kept so
    # adding a name to SUPPORTED_PROVIDERS without a transport fails loudly here
    # rather than silently sending nothing.
    return NullProvider(f"No transport is implemented for '{resolved.provider}'.")
