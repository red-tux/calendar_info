"""Fetch/parse errors shared by the calendar sources.

Lives in its own module so google_source.py can raise them without importing ics_source.py
(and with it icalendar). `SourceError` is re-exported from ics_source for compatibility.
"""
from __future__ import annotations


class SourceError(Exception):
    """A calendar source could not be fetched or parsed. Message is user-facing."""


class AuthError(SourceError):
    """The stored authorization is gone or rejected: the user has to reconnect the account.

    Separate from SourceError so the poll loop can flag the calendar as needing attention
    instead of quietly serving the cache forever.
    """
