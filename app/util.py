from datetime import datetime, timezone
from decimal import Decimal


def num(d: Decimal | None) -> float | None:
    """v4: money and quantities are plain JSON numbers."""
    if d is None:
        return None
    return float(Decimal(d).quantize(Decimal("1e-8")))  # 8 decimals: no Decimal-to-float noise like -0.78560000000004


def iso(dt: datetime | None) -> str | None:
    """v4: ISO 8601 strings, always UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
