"""
Per-coin profit-trail settings.

profit_targets.json maps each symbol to when the profit trail arms and how far
it may pull back before selling:

    {
      "AAVE/USD": {"arm_pct": 10, "trail_pct": 3.5},
      "BTC/USD":  {"arm_pct": 4,  "trail_pct": 1.5, "locked": true}
    }

calibrate_profit_targets.py writes it from each coin's price history; edit it
by hand to override (set "locked": true so recalibration keeps your values).
arm_pct 0 turns the trail off for that coin. Symbols not listed use
PROFIT_TRAIL_ARM_PCT / PROFIT_TRAIL_PCT from .env. The file is re-read on
every lookup, so edits apply on the next check without a restart.
"""
from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

import config


def _path() -> Path:
    return Path(config.PROFIT_TARGETS_FILE)


def load_targets() -> dict[str, dict]:
    """Return the per-symbol settings, keyed by compact symbol (AAVEUSD)."""
    path = _path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Could not read {path}: {exc}")
        return {}
    return {symbol.replace("/", "").upper(): entry for symbol, entry in raw.items()}


def save_targets(targets: dict[str, dict]) -> None:
    path = _path()
    path.write_text(json.dumps(targets, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def trail_settings(symbol: str, targets: dict[str, dict] | None = None) -> tuple[float, float]:
    """Return (arm %, trail %) for *symbol*, falling back to the .env defaults."""
    if targets is None:
        targets = load_targets()
    entry = targets.get(symbol.replace("/", "").upper(), {})
    try:
        arm = float(entry.get("arm_pct", config.PROFIT_TRAIL_ARM_PCT))
        trail = float(entry.get("trail_pct", config.PROFIT_TRAIL_PCT))
    except (TypeError, ValueError):
        logger.warning(f"{symbol}: Bad profit target entry {entry!r}; using defaults")
        return config.PROFIT_TRAIL_ARM_PCT, config.PROFIT_TRAIL_PCT
    return arm, trail
