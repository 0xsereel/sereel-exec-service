import hmac

from fastapi import Depends, Header

from .config import settings
from .errors import ServiceError


def require_key(x_sereel_key: str | None = Header(default=None)) -> None:
    """Every route except /health. Fails closed: with no API_KEY configured nothing is authorised."""
    if not settings.api_key or not x_sereel_key or not hmac.compare_digest(x_sereel_key, settings.api_key):
        raise ServiceError("UNAUTHORIZED", "missing or invalid X-Sereel-Key", 401)


auth = [Depends(require_key)]
