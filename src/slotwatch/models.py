"""Domain types. Deliberately free of I/O so everything here stays trivially testable."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class Availability(StrEnum):
    """The Available column is a closed vocabulary (verified on real data)."""

    SOLD_OUT = "sold_out"  # "Sold Out"
    OPEN = "open"  # "Yes" - open, remaining count not disclosed
    LIMITED = "limited"  # "N Space" / "N Spaces"
    UNKNOWN = "unknown"  # anything else - flagged, never acted on

    @property
    def is_bookable(self) -> bool:
        """UNKNOWN is deliberately not bookable: never ping on a string we don't grasp."""
        return self in (Availability.OPEN, Availability.LIMITED)


@dataclass(frozen=True, slots=True)
class Slot:
    game_id: str  # first '#'-field of the radio value - the stable dedup key
    date_raw: str  # "Sun 08/02" exactly as shown
    date: dt.date | None  # year inferred; None if unparseable
    gym: str  # venue name as shown
    level: str  # "Intermediate - Court 1"
    time_raw: str  # "4:00 pm - 7:30 pm"
    fee: Decimal | None
    availability: Availability
    spaces_left: int | None  # set for LIMITED and SOLD_OUT(0); None for OPEN
    radio_disabled: bool  # cross-checked against availability
    # Which configured tab this row came from. Lets alerts be grouped and attributed
    # per venue, and lets rules target a tab instead of matching venue text - the venue
    # strings are formatted inconsistently between tabs, so matching them is a trap.
    tab: str = ""
    # time_raw split into its two ends, when it could be read. Only `ends_at` consumes
    # these; time_raw stays the single source of truth for anything shown to a human.
    start_time: dt.time | None = None
    end_time: dt.time | None = None

    @property
    def label(self) -> str:
        return f"{self.date_raw} · {self.level} · {self.time_raw}"

    @property
    def ends_at(self) -> dt.datetime | None:
        """Naive local wall-clock moment this session finishes, if it can be known.

        None whenever the date or the time could not be parsed, which callers must read
        as "assume it is still live" rather than "assume it is over" - see diff.has_ended.
        """
        if self.date is None or self.end_time is None:
            return None
        day = self.date
        if self.start_time is not None and self.end_time <= self.start_time:
            # An overnight block - "10:00 pm - 1:00 am" finishes the following day.
            day += dt.timedelta(days=1)
        return dt.datetime.combine(day, self.end_time)


@dataclass(frozen=True, slots=True)
class ParseResult:
    slots: tuple[Slot, ...] = ()
    is_empty_state: bool = False
    anomalies: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return not self.anomalies


class EventType(StrEnum):
    NEW_SLOT = "new_slot"
    REOPENED = "reopened"
    HEALTH = "health"


@dataclass(frozen=True, slots=True)
class Event:
    type: EventType
    slot: Slot | None = None
    previous: Availability | None = None
    message: str | None = None  # populated for HEALTH
    rule_name: str | None = None  # set once a WatchRule claims it

    @property
    def dedup_key(self) -> str:
        """Identity for cooldown suppression."""
        if self.type is EventType.HEALTH:
            return "health"
        assert self.slot is not None
        return f"{self.slot.game_id}:{self.type.value}"


@dataclass(frozen=True, slots=True)
class WatchRule:
    """A slot filter. Data, not code - which is what lets Phase 2 add slash commands
    without touching the poller."""

    name: str
    enabled: bool = True
    tab: str | None = None  # restrict to one configured tab; None matches any
    gym: str | None = None
    level: str | None = None
    time: str | None = None
    date: str | None = None  # None/"any" wildcard, or an ISO date
    weekday: str | None = None  # e.g. "sunday"
    match: str = "exact"  # "exact" | "regex"
    triggers: frozenset[EventType] = field(
        default_factory=lambda: frozenset({EventType.NEW_SLOT, EventType.REOPENED})
    )
