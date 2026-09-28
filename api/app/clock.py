"""The owner's clock: the date and time agents are told.

Agents were told the UTC date, so on a Sunday evening in New York they wrote
"Monday" (2026-09-27). Everything an agent reads about today is in the
owner's time zone.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

OWNER_TZ = ZoneInfo("America/New_York")


def owner_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(OWNER_TZ)


def today_line(now: datetime | None = None) -> str:
    """ "Today: Sunday 27 September 2026, 21:13 in New York"."""
    return f"Today: {owner_now(now):%A %-d %B %Y, %H:%M} in New York"
