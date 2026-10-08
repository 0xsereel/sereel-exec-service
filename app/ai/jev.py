"""Jev (TypeSafe's System One model) through the ngrok AI Gateway: yes/no probabilities for a fixed question set.

POST {JEV_BASE_URL}/systemone  {model, state, questions}  ->  {model, answers: {name: {type: "noul", noul: 0..1}}, usage}
Auth: the ngrok gateway documents `Authorization: Bearer <key>` (OpenAI style) and `x-api-key` (Anthropic style). With
JEV_AUTH_HEADER=auto the first is tried and, on a 401, the second; whichever worked is returned so it can be pinned.
The key is never logged; only model, latency and token counts are."""
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from ..config import settings
from .questions import question_defs

log = logging.getLogger("sereel.jev")


class JevError(Exception):
    pass


@dataclass
class JevResult:
    probabilities: dict[str, Decimal]
    model: str
    latency_ms: int
    auth_header: str  # "Authorization: Bearer" | "x-api-key"
    usage: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _headers(kind: str) -> dict:
    key = settings.jev_api_key
    return {"Authorization": f"Bearer {key}"} if kind == "bearer" else {"x-api-key": key}


def _label(kind: str) -> str:
    return "Authorization: Bearer" if kind == "bearer" else "x-api-key"


def ask(state_text: str, names: list[str], client: httpx.Client | None = None) -> JevResult:
    """One call for all `names`. One retry on a timeout or 5xx. Raises JevError (the caller falls back to rules)."""
    if not settings.jev_api_key:
        raise JevError("JEV_API_KEY is not set")
    body = {"model": settings.jev_model, "state": state_text, "questions": question_defs(names)}
    url = settings.jev_base_url.rstrip("/") + "/systemone"
    order = {"auto": ["bearer", "x-api-key"], "bearer": ["bearer"], "x-api-key": ["x-api-key"]}.get(settings.jev_auth_header)
    if order is None:
        raise JevError(f"JEV_AUTH_HEADER must be auto, bearer or x-api-key, got '{settings.jev_auth_header}'")
    own = client is None
    client = client or httpx.Client(timeout=settings.jev_timeout_s)
    try:
        last = "no attempt"
        for kind in order:
            for attempt in (1, 2):
                t0 = time.monotonic()
                try:
                    r = client.post(url, json=body, headers=_headers(kind))
                except httpx.HTTPError as e:
                    last = f"{type(e).__name__}"
                    if attempt == 1:
                        continue
                    break
                ms = int((time.monotonic() - t0) * 1000)
                if r.status_code == 401:
                    last = "401 (the key was rejected with this header)"
                    break  # a different header may work; the same one will not
                if r.status_code >= 500 and attempt == 1:
                    last = f"HTTP {r.status_code}"
                    continue
                if r.status_code != 200:
                    raise JevError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                try:
                    probs = {n: Decimal(str(data["answers"][n]["noul"])) for n in names}
                except (KeyError, TypeError, ValueError) as e:
                    raise JevError(f"unexpected response shape: {type(e).__name__} {e}") from e
                if any(not (0 <= p <= 1) for p in probs.values()):
                    raise JevError("a probability was outside 0..1")
                usage = data.get("usage") or {}
                log.info("jev ok: model=%s latency=%dms in=%s out=%s header=%s", data.get("model"), ms,
                         usage.get("input_tokens"), usage.get("output_tokens"), _label(kind))
                return JevResult(probs, str(data.get("model", settings.jev_model)), ms, _label(kind), usage, data)
        raise JevError(f"Jev unreachable or rejected the key: {last}")
    finally:
        if own:
            client.close()
