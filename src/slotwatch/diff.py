"""The heart of the bot: what changed, and is any of it worth a ping?

Keyed on the site's own stable game_id, so reformatting the page can't fake a change.
Pure functions over (state, observation) - which is what lets us test a Sold Out ->
available transition without waiting days for a real cancellation.
"""

from __future__ import annotations

import datetime as dt

from .models import Availability, Event, EventType, ParseResult
from .state import State, _parse_ts

DEFAULT_COOLDOWN = dt.timedelta(hours=6)
DEFAULT_HEALTH_COOLDOWN = dt.timedelta(hours=12)
DEFAULT_FAILURE_THRESHOLD = 3

# Keep a health message readable rather than dumping 24 row-level complaints.
_MAX_REPORTED_ANOMALIES = 5


def diff(
    state: State,
    result: ParseResult,
    *,
    now: dt.datetime,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> list[Event]:
    events: list[Event] = []

    reasons: list[str] = []
    if result.anomalies:
        shown = list(result.anomalies[:_MAX_REPORTED_ANOMALIES])
        if len(result.anomalies) > _MAX_REPORTED_ANOMALIES:
            shown.append(f"(+{len(result.anomalies) - _MAX_REPORTED_ANOMALIES} more)")
        reasons.append("; ".join(shown))
    if result.is_empty_state and state.slots:
        reasons.append(
            f"tab dropped from {len(state.slots)} known slots to the empty-state notice"
        )
    if state.failure_streak >= failure_threshold:
        reasons.append(f"{state.failure_streak} consecutive fetch failures")
    if reasons:
        events.append(Event(type=EventType.HEALTH, message=" | ".join(reasons)))

    # The first successful poll only seeds memory. Pinging here would fire on all 24
    # currently-listed rows, most of which are old news.
    if state.is_cold:
        return events

    for slot in result.slots:
        previous = state.slots.get(slot.game_id)

        if previous is None:
            # Unseen and already full is not news - record it, stay quiet.
            if slot.availability.is_bookable:
                events.append(Event(type=EventType.NEW_SLOT, slot=slot))
            continue

        # The signal you actually want: a cancellation freed a spot.
        if Availability(previous) is Availability.SOLD_OUT and slot.availability.is_bookable:
            events.append(
                Event(
                    type=EventType.REOPENED,
                    slot=slot,
                    previous=Availability.SOLD_OUT,
                )
            )

    return events


def suppress_recent(
    events: list[Event],
    state: State,
    *,
    now: dt.datetime,
    cooldown: dt.timedelta = DEFAULT_COOLDOWN,
    health_cooldown: dt.timedelta | None = None,
) -> list[Event]:
    """Drop events already announced inside their cooldown.

    Without this, a slot flapping Sold Out <-> Yes would ping on every single poll,
    and a persistent scraper break would ping all day.
    """
    health_window = health_cooldown if health_cooldown is not None else cooldown
    kept: list[Event] = []

    for event in events:
        window = health_window if event.type is EventType.HEALTH else cooldown
        last = state.notified.get(event.dedup_key)
        if last is None:
            kept.append(event)
            continue
        previous = _parse_ts(last)
        if previous is None or now - previous >= window:
            kept.append(event)

    return kept
