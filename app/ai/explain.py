"""Plain-language explanation of a decision that was ALREADY made. The model receives only structured facts produced by this service
(no user free text), returns JSON that is validated against a strict schema, and anything off falls back to a deterministic template.
The text is stored with the decision and is never read back for a parameter."""
import json
import logging
from decimal import Decimal

from ..errors import ServiceError
from . import llm
from .decide import Decision
from .state import Snapshot

log = logging.getLogger("sereel.explain")
LIMITS = {"headline": 80, "explanation": 600, "risk_note": 200}
SYSTEM = ("You explain a decision that a rules engine has ALREADY made about a gold hedge, for a fund manager. Use only the facts "
          "provided. Do not change, add or recommend any number or action. Reply with a JSON object with exactly the keys headline "
          "(max 80 characters), explanation (max 600 characters) and risk_note (max 200 characters, may be empty).")


def validate(obj) -> dict | None:
    """The exact schema or None. Nothing is truncated: an over-long answer is rejected, not trimmed into something unintended."""
    if not isinstance(obj, dict) or set(obj) != set(LIMITS):
        return None
    for k, cap in LIMITS.items():
        v = obj[k]
        if not isinstance(v, str) or len(v) > cap or (k != "risk_note" and not v.strip()):
            return None
    return {k: obj[k].strip() for k in LIMITS}


def template(d: Decision, view: dict, snap: Snapshot) -> dict:
    a = d.action or {}
    t, params = a.get("type"), a.get("params") or {}
    ratio = snap.sections.get("strategy", {}).get("maintenance_ratio", "n/a")
    gap = snap.sections.get("strategy", {}).get("gap_oz", "n/a")
    note = {"hold": "No trading until the price sources agree again.", "top_up": "Margin is the only protection against liquidation.",
            "rebalance": "A rebalance trades on the thin testnet book and may not fully fill.", "return_excess": ""}.get(t, "")
    if d.kind == "none":
        return {"headline": "No action needed", "explanation": f"The hedge is within its limits (maintenance ratio {ratio}, gap {gap} oz).", "risk_note": ""}
    verb = "Suggested" if d.kind == "suggest" else "Proposed"
    if t == "hold":
        head, text = "Hold: do not trade now", f"The price sources disagree or the move is abnormal ({d.reason})."
    elif t == "top_up":
        head, text = f"{verb}: top up ${params['amount_usd']}", f"Maintenance ratio is {ratio}; ${params['amount_usd']} brings it back to 2.0."
    elif t == "rebalance":
        head, text = f"{verb}: rebalance to {params['target_size']} oz", f"The hedge is {gap} oz from its target of {params['target_size']} oz."
    else:
        head, text = f"{verb}: return ${params['amount_usd']}", f"Margin is well above the requirement; ${params['amount_usd']} can be returned safely."
    if d.downgraded_from:
        note = (note + " " + d.reason.split("; ", 1)[-1]).strip()
    return {"headline": head[:80], "explanation": text[:600], "risk_note": note[:200]}


def explain(d: Decision, view: dict, snap: Snapshot, probs: dict[str, Decimal]) -> dict:
    base = template(d, view, snap)
    if d.kind == "none":
        return base  # heartbeats never spend a model call
    facts = {"decision": d.kind, "action": d.action, "rule": d.reason, "downgraded_from": d.downgraded_from,
             "probabilities": {k: f"{v:.2f}" for k, v in sorted(probs.items())},
             "strategy": snap.sections.get("strategy", {}), "market": {k: snap.sections.get("venue", {}).get(k) for k in ("hl_mark",)},
             "template_text": base}
    try:
        msg = llm.chat_completion([{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(facts, default=str)}],
                                  json_mode=True)
        ok = validate(json.loads(msg.get("content") or ""))
    except (ServiceError, ValueError, TypeError):
        ok = None
    if ok is None:
        log.info("explanation fell back to the template")
        return base
    return ok
