"""Outbound notifications. One transport interface, resolved from settings.

Nothing outside this package speaks SMTP, and nothing inside it knows what a KPI
is: composition of a summary lives in ``app.services.run_email``, which reads
stored rows and hands this package finished text.
"""

from app.notifications.config import EmailConfig, load_email_config
from app.notifications.provider import (
    EmailMessage,
    EmailProvider,
    NullProvider,
    SendResult,
    SmtpProvider,
    build_email_provider,
)

__all__ = [
    "EmailConfig",
    "EmailMessage",
    "EmailProvider",
    "NullProvider",
    "SendResult",
    "SmtpProvider",
    "build_email_provider",
    "load_email_config",
]
