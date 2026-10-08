"""An OpenAI-compatible chat client (DeepSeek by default), with tool calls. No provider-specific code: LLM_BASE_URL, LLM_MODEL and
LLM_API_KEY are all it knows. It returns TEXT and tool-call requests; whatever it says is data. Nothing it writes is ever used as
a parameter without passing the server's validators, and the key is never logged."""
import json
import logging
import time

import httpx

from ..config import settings
from ..errors import ServiceError

log = logging.getLogger("sereel.llm")
MAX_TOOL_ROUNDS = 6


class LLMUnavailable(ServiceError):
    def __init__(self, why: str):
        super().__init__("LLM_UNAVAILABLE", f"the assistant is unavailable right now ({why}); use the manual strategy form", 503)


def chat_completion(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False) -> dict:
    """One request; returns the assistant message ({role, content, tool_calls?}). Raises LLMUnavailable."""
    if not settings.llm_api_key:
        raise LLMUnavailable("LLM_API_KEY is not set")
    body = {"model": settings.llm_model, "messages": messages, "max_tokens": settings.llm_max_tokens, "temperature": 0.2}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    t0 = time.monotonic()
    try:
        r = httpx.post(settings.llm_base_url.rstrip("/") + "/chat/completions", json=body,
                       headers={"Authorization": f"Bearer {settings.llm_api_key}"}, timeout=settings.llm_timeout_s)
    except httpx.HTTPError as e:
        raise LLMUnavailable(type(e).__name__) from e
    if r.status_code != 200:
        raise LLMUnavailable(f"HTTP {r.status_code}")
    try:
        data = r.json()
        msg = data["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMUnavailable("unexpected response shape") from e
    usage = data.get("usage") or {}
    log.info("llm ok: model=%s latency=%dms in=%s out=%s", data.get("model", settings.llm_model), int((time.monotonic() - t0) * 1000),
             usage.get("prompt_tokens"), usage.get("completion_tokens"))
    return msg


def run(messages: list[dict], tools: list[dict], handler) -> str:
    """Let the model call tools until it answers in text. `handler(name, args: dict) -> dict` runs each tool (the server's code, with
    validation); its result goes back to the model. Bounded rounds, so a model that keeps calling tools cannot loop forever."""
    msgs = list(messages)
    for _ in range(MAX_TOOL_ROUNDS):
        msg = chat_completion(msgs, tools)
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be an object")
                result = handler(fn.get("name", ""), args)
            except (ValueError, TypeError) as e:
                result = {"ok": False, "error": f"bad tool call: {e}"}
            msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": json.dumps(result, default=str)})
    raise LLMUnavailable("the model kept calling tools without answering")
