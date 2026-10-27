"""Tracks builder tools benched after a spend-cap or usage-limit failure."""

from datetime import datetime, timedelta
import json
from pathlib import Path


DEFAULT_COOLDOWN = timedelta(hours=5)
CAP_PHRASES = ("spend cap", "usage limit")


def is_cap_failure(output):
    text = (output or "").lower()
    return any(phrase in text for phrase in CAP_PHRASES)


def load_cooldowns(path, now):
    """Return {tool: until} for tools still in cooldown, dropping any that have passed."""
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    active = {tool: datetime.fromisoformat(until) for tool, until in raw.items()}
    expired = [tool for tool, until in active.items() if until <= now]
    for tool in expired:
        del active[tool]
    if expired:
        _write(path, active)
    return active


def record_cooldown(path, tool, now, cooldown=DEFAULT_COOLDOWN):
    """Bench `tool` until `now + cooldown`, keeping other still-active cooldowns."""
    cooldowns = load_cooldowns(path, now)
    cooldowns[tool] = now + cooldown
    _write(path, cooldowns)
    return cooldowns


def _write(path, cooldowns):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({tool: until.isoformat() for tool, until in cooldowns.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
