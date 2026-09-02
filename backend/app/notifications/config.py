"""Email configuration, resolved from settings at call time.

Same two guarantees as ``app.llm.config``, for the same reasons:

* **The platform runs with no mail server.** ``EmailConfig.unavailable_reason``
  is the single source of truth for "can a run summary be sent right now", and
  callers check it before composing anything. A deployment that configures no
  host is not a broken deployment — it is the default one — so an unsent summary
  is reported as a state, never raised as an error that would fail the run that
  produced it.
* **Credentials never travel.** ``describe()`` returns what the API and the audit
  trail are allowed to see: provider, host, port, sender, and how many addresses
  the fallback list holds. The password is not in it, and neither are any
  addresses — a mailing list is personal data, and a count answers "was this
  delivered anywhere" without putting addresses into every audit row.

Who a summary is addressed to is *not* decided here. This module knows nothing
about companies, so it cannot ask which of a company's registered users may see a
result; ``app.services.run_email`` resolves that against the membership table and
treats ``EMAIL_RECIPIENTS`` as the fallback for a company with no entitled member.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings

#: Transports this build knows how to speak. A bad value here is a clear
#: configuration error rather than an import failure at the end of a run.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("smtp",)


@dataclass(frozen=True, slots=True)
class EmailConfig:
    enabled: bool
    provider: str
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    timeout_seconds: int
    sender: str
    recipients: tuple[str, ...]
    subject_prefix: str

    @property
    def unavailable_reason(self) -> str | None:
        """Why a summary cannot be sent, or ``None`` when it can.

        Deliberately silent about ``recipients``. Who receives a run summary is a
        per-company question answered against that company's registered users, and
        only a company with no entitled member falls back to this list — so an empty
        ``EMAIL_RECIPIENTS`` is a normal deployment, not a broken transport. The
        "nobody to send to" case belongs to the caller that knows the company, and
        ``app.services.run_email`` reports it there.
        """

        if not self.enabled:
            return "Post-run email is disabled for this deployment (EMAIL_ENABLED)."
        if self.provider not in SUPPORTED_PROVIDERS:
            return (
                f"EMAIL_PROVIDER '{self.provider}' is not one this build supports "
                f"({', '.join(SUPPORTED_PROVIDERS)})."
            )
        if not self.host:
            return "No mail host is configured (SMTP_HOST)."
        if not self.sender:
            return "No sender address is configured (EMAIL_FROM)."
        return None

    def describe(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "host": self.host,
            "port": self.port,
            "sender": self.sender,
            # The configured fallback list only. The number actually addressed is
            # per company and is reported by the send, not by the configuration.
            "fallback_recipient_count": len(self.recipients),
            "tls": self.use_tls,
            "enabled": self.enabled,
            "unavailable_reason": self.unavailable_reason,
        }


def _recipients(raw: str) -> tuple[str, ...]:
    """The configured list, split and de-duplicated in order.

    Order is preserved rather than sorted so the operator's own first address
    stays first, and duplicates are dropped so a copy-paste in the environment
    does not send the same person the same summary twice.
    """

    seen: dict[str, None] = {}
    for part in raw.replace(";", ",").split(","):
        address = part.strip()
        if address:
            seen.setdefault(address, None)
    return tuple(seen)


def load_email_config(settings: Settings | None = None) -> EmailConfig:
    resolved = settings or get_settings()
    return EmailConfig(
        enabled=bool(resolved.email_enabled),
        provider=(resolved.email_provider or "smtp").strip().lower(),
        host=(resolved.smtp_host or "").strip(),
        port=int(resolved.smtp_port or 0),
        username=resolved.smtp_username or "",
        password=resolved.smtp_password or "",
        use_tls=bool(resolved.smtp_use_tls),
        timeout_seconds=max(1, int(resolved.smtp_timeout_seconds or 20)),
        sender=(resolved.email_from or "").strip(),
        recipients=_recipients(resolved.email_recipients or ""),
        subject_prefix=(resolved.email_subject_prefix or "").strip(),
    )
