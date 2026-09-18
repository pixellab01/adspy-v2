"""Time helpers. Ported from meta_main14/services/time_utils.py.

One rule the whole app obeys: every timestamp stored in SQLite is a
second-precision UTC ISO-8601 string, every date is ``YYYY-MM-DD`` UTC.
Never store local time, never store floats.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> str:
    """'2026-08-03T09:15:42+00:00' — the canonical timestamp format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def utc_today() -> str:
    """'2026-08-03' — the canonical metric_date format."""
    return datetime.now(UTC).date().isoformat()


def utc_shift(seconds: int) -> str:
    """Timestamp ``seconds`` away from now (negative = in the past).

    Used for lease expiry (``utc_shift(LEASE_TTL_SECONDS)``) and for the
    reconciliation grace window (``utc_shift(-RECONCILE_GRACE_SECONDS)``).
    """
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    return moment.replace(microsecond=0).isoformat()


def parse_utc(value: str | None) -> datetime | None:
    """Parse a stored timestamp back to an aware datetime; None when unusable."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def age_seconds(value: str | None) -> float | None:
    """How many seconds ago ``value`` was; None when it cannot be parsed."""
    moment = parse_utc(value)
    if moment is None:
        return None
    return (datetime.now(UTC) - moment).total_seconds()


def age_days(value: str | None) -> int | None:
    """Whole days since ``value`` — the staleness colouring on the pages list."""
    seconds = age_seconds(value)
    return None if seconds is None else int(seconds // 86400)
