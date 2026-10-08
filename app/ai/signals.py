"""signals = Jev when it answers, the rules engine when it does not. The source is always reported."""
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from . import jev, rules_signals
from .questions import QUESTION_SET_VERSION, question_names
from .state import Snapshot

log = logging.getLogger("sereel.signals")


@dataclass
class Signals:
    probabilities: dict[str, Decimal]
    source: str  # "jev" | "rules"
    question_set_version: str = QUESTION_SET_VERSION
    jev_result: jev.JevResult | None = None
    jev_error: str | None = None
    names: list[str] = field(default_factory=list)


def get_signals(snap: Snapshot, names: list[str] | None = None) -> Signals:
    names = names or question_names(snap.has_strategy)
    try:
        res = jev.ask(snap.to_state_text(), names)
        return Signals(res.probabilities, "jev", jev_result=res, names=names)
    except jev.JevError as e:
        log.warning("Jev unavailable (%s): using the rules engine", e)
        return Signals(rules_signals.answer(snap, names), "rules", jev_error=str(e), names=names)
