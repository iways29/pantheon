"""Sending email through Resend (ADR 028).

One POST per email, with an `Idempotency-Key` so a retried send within 24
hours is not delivered twice (https://resend.com/docs/api-reference/emails/send-email).
The key is the email's own row key, so the database and Resend agree on what
"the same email" means.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings


class MailError(RuntimeError):
    """Resend refused or could not be reached. The message is safe to store."""


@dataclass(frozen=True)
class Outgoing:
    from_address: str
    recipients: list[str]
    subject: str
    text: str
    html: str
    idempotency_key: str
    reply_to: str | None = None


class Mailer(Protocol):
    def send(self, email: Outgoing) -> str:
        """Send one email; return the provider's id. Raises MailError."""
        ...


@dataclass(frozen=True)
class ResendMailer:
    api_key: str
    base_url: str = "https://api.resend.com"
    timeout_seconds: float = 20.0

    def send(self, email: Outgoing) -> str:
        body: dict[str, object] = {
            "from": email.from_address,
            "to": email.recipients,
            "subject": email.subject,
            "text": email.text,
            "html": email.html,
        }
        if email.reply_to:
            body["reply_to"] = email.reply_to
        try:
            response = httpx.post(
                f"{self.base_url.rstrip('/')}/emails",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Idempotency-Key": email.idempotency_key[:256],
                },
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise MailError(f"Resend could not be reached: {type(error).__name__}") from error
        if response.status_code >= 400:
            try:
                detail = response.json().get("message") or response.text
            except ValueError:
                detail = response.text
            raise MailError(f"Resend refused the email ({response.status_code}): {detail[:300]}")
        provider_id = response.json().get("id")
        if not provider_id:
            raise MailError("Resend answered without an email id")
        return str(provider_id)


def mailer_from(settings: Settings) -> ResendMailer | None:
    if not settings.resend_api_key:
        return None
    return ResendMailer(api_key=settings.resend_api_key, base_url=settings.resend_base_url)
