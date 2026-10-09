"""Shared UTC helpers. Stored times retain compatibility with the original database."""
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Choose a valid schedule time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Choose a valid schedule time") from None
    if parsed.tzinfo is None:
        raise ValueError("Schedule time must include a timezone")
    return parsed.astimezone(timezone.utc)
