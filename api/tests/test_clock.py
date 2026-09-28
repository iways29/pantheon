"""Agents are told the owner's date, not UTC's (2026-09-27: Sunday evening in
New York read as Monday)."""

from datetime import UTC, datetime

from app.clock import today_line


def test_sunday_evening_in_new_york_is_still_sunday() -> None:
    utc = datetime(2026, 9, 28, 1, 13, tzinfo=UTC)  # 21:13 on Sunday in New York
    assert today_line(utc) == "Today: Sunday 27 September 2026, 21:13 in New York"
