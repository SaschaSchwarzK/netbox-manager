from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return timezone-naive UTC for compatibility with existing SQLite rows."""
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc_aware(value: datetime) -> datetime:
    """Return an aware UTC datetime; existing naive database values are UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
