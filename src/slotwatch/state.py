"""Durable memory across runs, plus the pure transforms over it.

State is what makes the bot correct rather than merely functional: without it every
restart would either re-ping everything or miss the one opening you cared about.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from .models import Event, ParseResult

STATE_VERSION = 1

# Keep the file bounded without losing anything that still matters.
NOTIFIED_RETENTION = dt.timedelta(days=30)


@dataclass(frozen=True)
class State:
    version: int = STATE_VERSION
    seeded: bool = False  # False only before the very first successful poll
    slots: dict[str, str] = field(default_factory=dict)  # game_id -> availability
    notified: dict[str, str] = field(default_factory=dict)  # dedup_key -> ISO ts
    failure_streak: int = 0

    @property
    def is_cold(self) -> bool:
        return not self.seeded


def record(state: State, result: ParseResult, *, now: dt.datetime) -> State:
    """Fold an observation into state. Marks the bot seeded, so run #1 stays silent.

    A degraded or empty read never erases memory - only a clean, populated page is
    trusted to define the full current set. That asymmetry is deliberate: forgetting
    a slot would turn its reappearance into a false "new slot" ping.
    """
    if not result.slots:
        return replace(state, seeded=True, failure_streak=0)

    observed = {slot.game_id: str(slot.availability) for slot in result.slots}
    # With anomalies present, some rows were skipped - merge so they aren't forgotten.
    slots = {**state.slots, **observed} if result.anomalies else observed
    return replace(state, seeded=True, slots=slots, failure_streak=0)


def record_failure(state: State) -> State:
    return replace(state, failure_streak=state.failure_streak + 1)


def mark_notified(state: State, events: list[Event], *, now: dt.datetime) -> State:
    notified = dict(state.notified)
    for event in events:
        notified[event.dedup_key] = now.isoformat()

    cutoff = now - NOTIFIED_RETENTION
    pruned = {
        key: ts
        for key, ts in notified.items()
        if _parse_ts(ts) is None or _parse_ts(ts) >= cutoff
    }
    return replace(state, notified=pruned)


def _parse_ts(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def load(path: str | Path) -> State:
    """Missing or unreadable file yields cold state, which seeds silently."""
    path = Path(path)
    if not path.exists():
        return State()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return State()
    if not isinstance(raw, dict):
        return State()
    return State(
        version=int(raw.get("version", STATE_VERSION)),
        seeded=bool(raw.get("seeded", False)),
        slots=dict(raw.get("slots") or {}),
        notified=dict(raw.get("notified") or {}),
        failure_streak=int(raw.get("failure_streak", 0)),
    )


def save(path: str | Path, state: State) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(state)
    # Sorted keys keep the committed diff readable and churn-free.
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
