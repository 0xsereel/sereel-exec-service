"""The agent's cycle log: what Jev (or the rules) answered, what was decided, and why, for every strategy on every cycle.

Two outputs, both safe to read: a one-line summary at INFO in the server log, and (unless AGENT_LOG_FILE is empty) one JSON line appended to
a rotating file. The database keeps only the latest quiet cycle per strategy (a `none` heartbeat is updated in place, so the feed is not
flooded); this file is the full history. API keys are never written: only the model id, latency, token counts, probabilities and the state hash."""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

log = logging.getLogger("sereel.agent")
MAX_BYTES, KEEP = 5_000_000, 5
_lock = threading.Lock()


def _path() -> Path | None:
    return settings.resolve(settings.agent_log_file) if settings.agent_log_file else None


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < MAX_BYTES:
        return
    for i in range(KEEP - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if src.exists():
            src.replace(path.with_name(f"{path.name}.{i + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def _append(entry: dict) -> None:
    path = _path()
    if path is None:
        return
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":"), sort_keys=True, default=str) + "\n")
    except OSError as e:  # a full disk must never stop the agent
        log.warning("could not write the agent log: %s", e)


def record(strategy_id: str, snap, sig, decision) -> None:
    probs = {k: f"{v:.2f}" for k, v in sorted(sig.probabilities.items())}
    jr = sig.jev_result
    entry = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "strategy_id": strategy_id, "source": sig.source,
             "question_set_version": sig.question_set_version, "state_hash": snap.state_hash, "signals_network": snap.signals_network,
             "degraded": snap.degraded, "probabilities": probs, "decision": decision.kind, "action": decision.action, "reason": decision.reason,
             "downgraded_from": decision.downgraded_from}
    if jr is not None:
        entry["jev"] = {"model": jr.model, "latency_ms": jr.latency_ms, "usage": jr.usage, "auth_header": jr.auth_header}
    if sig.jev_error:
        entry["jev_error"] = sig.jev_error  # why the rules answered instead
    if snap.absent:
        entry["unavailable"] = dict(sorted(snap.absent.items()))
    if settings.agent_log_state:
        entry["state"] = snap.to_state_text()
    _append(entry)
    log.info("agent %s signals (%s%s): %s -> %s%s (%s)", strategy_id[:8], sig.source, f", {jr.latency_ms}ms" if jr else f"; Jev: {sig.jev_error}",
             " ".join(f"{k}={v}" for k, v in probs.items()), decision.kind, f" {decision.action['type']}" if decision.action else "", decision.reason)


def record_failure(strategy_id: str, code: str, message: str) -> None:
    _append({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "strategy_id": strategy_id, "error": code, "message": message})


def tail(n: int = 20, strategy_id: str | None = None) -> list[dict]:
    """The last n entries (optionally for one strategy), oldest first."""
    path = _path()
    if path is None or not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if strategy_id is None or e.get("strategy_id", "").startswith(strategy_id):
            out.append(e)
    return out[-n:]
