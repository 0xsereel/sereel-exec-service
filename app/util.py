from datetime import datetime, timezone
from decimal import Decimal


def num(d: Decimal | None) -> float | None:
    """v4: money and quantities are plain JSON numbers."""
    return None if d is None else float(d)


def iso(dt: datetime | None) -> str | None:
    """v4: ISO 8601 strings, always UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
