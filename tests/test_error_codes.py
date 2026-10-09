"""The error-code registry, the source, and docs/REFERENCE.md must agree (Cantina matches some codes by exact spelling)."""
import re
from pathlib import Path

from app.errors import CODES

ROOT = Path(__file__).resolve().parent.parent
SPECIAL = ["AUTHORIZATION_REQUIRED", "STALE_PRICE", "PRICE_DEVIATION", "INSUFFICIENT_MARGIN"]  # handled specially by Cantina


def codes_in_source() -> set[str]:
    found = set()
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "errors.py":
            continue
        found |= set(re.findall(r'(?:ServiceError|PriceError|_bad|err|_err)\(\s*(?:\d+,\s*)?"([A-Z][A-Z_]+)"', path.read_text()))
    return found


def test_every_code_used_in_the_source_is_registered():
    unregistered = codes_in_source() - set(CODES)
    assert not unregistered, f"add to app/errors.py CODES and docs/REFERENCE.md: {sorted(unregistered)}"


def test_every_registered_code_is_in_the_readme():
    readme = (ROOT / "docs" / "REFERENCE.md").read_text()
    missing = [c for c in CODES if f"`{c}`" not in readme]
    assert not missing, f"docs/REFERENCE.md 'Error codes' table is missing: {missing}"


def test_cantina_special_codes_are_spelled_exactly_and_registered():
    assert all(c in CODES for c in SPECIAL)
    assert "MARKET_CLOSED" not in CODES  # a closed market is a flag on the response, not an error (see README)


def test_the_special_codes_that_exist_today_are_actually_emitted():
    emitted = codes_in_source()
    for c in ("STALE_PRICE", "PRICE_DEVIATION", "INSUFFICIENT_MARGIN", "AUTHORIZATION_REQUIRED"):
        assert c in emitted, f"{c} is no longer emitted anywhere"
