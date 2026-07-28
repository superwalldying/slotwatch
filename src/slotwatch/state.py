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
class DayState:
    """Per-day polling tally behind the start/end-of-day heartbeat.

    Kept in the same file as the rest of state so one commit persists everything and
    one merge resolves everything. `polls` is what the end-of-day report is measured
    against - see config.expected_polls for the other half of that comparison.
    """

    date: str | None = None  # local ISO date being tracked; None when no day is open
    polls: int = 0  # polling attempts made, whether or not the fetch succeeded
    failures: int = 0  # of those, attempts whose fetch failed
    alerts: int = 0  # notifications actually delivered
    started: bool = False  # start-of-day heartbeat delivered
    ended: bool = False  # end-of-day heartbeat delivered

    @property
    def is_open(self) -> bool:
        return self.date is not None and self.started and not self.ended


@dataclass(frozen=True)
class State:
    version: int = STATE_VERSION
    seeded: bool = False  # False only before the very first successful poll
    slots: dict[str, str] = field(default_factory=dict)  # game_id -> availability
    notified: dict[str, str] = field(default_factory=dict)  # dedup_key -> ISO ts
    failure_streak: int = 0
    day: DayState = field(default_factory=DayState)

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


def open_day(state: State, date: str) -> State:
    """Begin tracking a local day. Called once the start-of-day ping is delivered."""
    return replace(state, day=DayState(date=date, started=True))


def count_poll(state: State, *, failed: bool = False, alerts: int = 0) -> State:
    """Tally one polling attempt against the open day.

    A no-op when no day is open, so `--ignore-window` runs and ad-hoc local polls do
    not inflate a real day's count.
    """
    if not state.day.is_open:
        return state
    day = state.day
    return replace(
        state,
        day=replace(
            day,
            polls=day.polls + 1,
            failures=day.failures + int(failed),
            alerts=day.alerts + alerts,
        ),
    )


def close_day(state: State) -> State:
    """Mark the open day reported. Kept (not cleared) so it cannot be reported twice."""
    return replace(state, day=replace(state.day, ended=True))


def clear_day(state: State) -> State:
    return replace(state, day=DayState())


def merge(mine: State, theirs: State) -> State:
    """Fold a concurrently-written state into this one, `mine` winning ties.

    seen.json is a set of observations, not prose: two runs that each create it produce
    an add/add conflict git cannot resolve, and a line-wise merge of JSON is meaningless.
    Taking one side wholesale is worse still - it would drop either a sighting or a
    notified-marker, and a lost marker re-pings a slot you were already told about. The
    union is well-defined, so compute it here and let CI commit the result as an
    ordinary fast-forward.

    `mine` is the run that has just polled, so it holds the newer view of `slots`.
    Everything else resolves toward the safer answer: `notified` is unioned and keeps
    the later timestamp (cooldown errs long rather than short), and `failure_streak`
    keeps the higher count (a health warning is raised rather than swallowed).
    """
    notified = dict(theirs.notified)
    for key, ts in mine.notified.items():
        notified[key] = _later(notified.get(key), ts)

    return State(
        version=max(mine.version, theirs.version),
        seeded=mine.seeded or theirs.seeded,
        slots={**theirs.slots, **mine.slots},
        notified=notified,
        failure_streak=max(mine.failure_streak, theirs.failure_streak),
        day=_merge_day(mine.day, theirs.day),
    )


def _merge_day(mine: DayState, theirs: DayState) -> DayState:
    if mine.date != theirs.date:
        # Different days: the later date is the one still in progress. An unreported
        # earlier day is dropped rather than resurrected - two runs disagreeing about
        # which day is open means the clock has already moved past the older one.
        return mine if (mine.date or "") >= (theirs.date or "") else theirs
    return DayState(
        date=mine.date,
        # Counts are cumulative per day, and neither side saw the other's polls. Max,
        # not sum: the two runs overlapped, so their tallies share a prefix.
        polls=max(mine.polls, theirs.polls),
        failures=max(mine.failures, theirs.failures),
        alerts=max(mine.alerts, theirs.alerts),
        # A ping either side already delivered must not be sent again.
        started=mine.started or theirs.started,
        ended=mine.ended or theirs.ended,
    )


def _later(existing: str | None, candidate: str) -> str:
    if existing is None:
        return candidate
    a, b = _parse_ts(existing), _parse_ts(candidate)
    if a is None:
        return candidate
    if b is None:
        return existing
    return existing if a >= b else candidate


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
        day=_day(raw.get("day")),
    )


def _day(raw: object) -> DayState:
    """A state file written before day tracking existed simply has no open day."""
    if not isinstance(raw, dict):
        return DayState()
    date = raw.get("date")
    return DayState(
        date=str(date) if date else None,
        polls=int(raw.get("polls", 0)),
        failures=int(raw.get("failures", 0)),
        alerts=int(raw.get("alerts", 0)),
        started=bool(raw.get("started", False)),
        ended=bool(raw.get("ended", False)),
    )


def save(path: str | Path, state: State) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dataclasses.asdict(state)
    # Sorted keys keep the committed diff readable and churn-free.
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
