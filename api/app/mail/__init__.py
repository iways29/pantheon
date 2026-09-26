"""Email: mailing lists, the outbox and Resend (ADR 028)."""

from app.mail.lists import MailingListChange, get_lists, set_list
from app.mail.outbox import Composed, compose, send
from app.mail.resend import Mailer, MailError, Outgoing, ResendMailer, mailer_from

__all__ = [
    "Composed",
    "MailError",
    "Mailer",
    "MailingListChange",
    "Outgoing",
    "ResendMailer",
    "compose",
    "get_lists",
    "mailer_from",
    "send",
    "set_list",
]
