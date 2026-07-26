"""Diff-engine tests - the behaviour that cannot be tested against the live site.

Reproducing a real Sold Out -> available transition would mean waiting days for an
actual cancellation, so these fixture pairs are the only way to exercise the feature
the bot exists for.
"""

from __future__ import annotations

import datetime as dt

import pytest

from slotwatch.diff import diff, suppress_recent
from slotwatch.models import Availability, EventType, ParseResult
from slotwatch.parse import parse_table
from slotwatch.state import State, mark_notified, record

from .conftest import CAPTURE_DAY, TARGET_GAME_ID

NOW = dt.datetime(2026, 7, 26, 13, 0, tzinfo=dt.UTC)


@pytest.fixture
def live(load):
    return parse_table(load("primary_live.html"), today=CAPTURE_DAY)


@pytest.fixture
def reopened(load):
    return parse_table(load("primary_reopened.html"), today=CAPTURE_DAY)


@pytest.fixture
def new_date(load):
    return parse_table(load("primary_new_date.html"), today=CAPTURE_DAY)


@pytest.fixture
def warm(live):
    """State as it would be after one successful poll of the live page."""
    return record(State(), live, now=NOW)


# --------------------------------------------------------------------------
# Cold start - the run that must stay silent
# --------------------------------------------------------------------------


def test_cold_start_emits_nothing(live):
    """Otherwise run #1 pings all 24 rows, including 7 already-bookable ones."""
    events = diff(State(), live, now=NOW)

    assert events == []


def test_cold_start_still_records_everything(live):
    state = record(State(), live, now=NOW)

    assert state.seeded is True
    assert len(state.slots) == 24
    assert state.slots[TARGET_GAME_ID] == Availability.SOLD_OUT


def test_second_poll_after_seeding_is_silent_when_nothing_changed(warm, live):
    assert diff(warm, live, now=NOW) == []


# --------------------------------------------------------------------------
# REOPENED - the primary signal
# --------------------------------------------------------------------------


def test_sold_out_to_limited_emits_reopened(warm, reopened):
    events = diff(warm, reopened, now=NOW)

    assert len(events) == 1
    event = events[0]
    assert event.type is EventType.REOPENED
    assert event.slot.game_id == TARGET_GAME_ID
    assert event.previous is Availability.SOLD_OUT
    assert event.slot.availability is Availability.LIMITED
    assert event.slot.spaces_left == 2


def test_reopened_carries_enough_context_to_render_a_message(warm, reopened):
    event = diff(warm, reopened, now=NOW)[0]

    assert event.slot.level == "Intermediate - Court 1"
    assert event.slot.time_raw == "4:00 pm - 7:30 pm"
    assert event.slot.date_raw == "Sun 08/02"


# --------------------------------------------------------------------------
# NEW_SLOT - a fresh Sunday enters the rolling window
# --------------------------------------------------------------------------


def test_new_bookable_slots_emit_new_slot(warm, new_date):
    events = diff(warm, new_date, now=NOW)

    assert len(events) == 6
    assert {e.type for e in events} == {EventType.NEW_SLOT}
    assert all(e.slot.date_raw == "Sun 08/23" for e in events)
    assert all(e.previous is None for e in events)


def test_new_but_sold_out_slot_is_recorded_without_pinging(warm, live):
    """An unseen game_id that is already full is not news."""
    extra = live.slots[0].__class__(
        game_id="99999",
        date_raw="Sun 08/30",
        date=dt.date(2026, 8, 30),
        gym="Central Gym",
        level="Intermediate - Court 1",
        time_raw="4:00 pm - 7:30 pm",
        fee=None,
        availability=Availability.SOLD_OUT,
        spaces_left=0,
        radio_disabled=True,
    )
    result = ParseResult(slots=live.slots + (extra,))

    assert diff(warm, result, now=NOW) == []
    assert "99999" in record(warm, result, now=NOW).slots


# --------------------------------------------------------------------------
# Non-events: recorded, never pinged
# --------------------------------------------------------------------------


def test_slot_ageing_off_the_window_is_not_an_event(warm, live):
    """The page shows a rolling window; departures are routine, not errors."""
    trimmed = ParseResult(slots=tuple(s for s in live.slots if s.date_raw != "Sun 07/26"))

    assert diff(warm, trimmed, now=NOW) == []


def test_becoming_sold_out_is_not_an_event(warm, live):
    downgraded = ParseResult(
        slots=tuple(
            s.__class__(**{**{f: getattr(s, f) for f in s.__slots__},
                           "availability": Availability.SOLD_OUT,
                           "spaces_left": 0,
                           "radio_disabled": True})
            if s.availability.is_bookable else s
            for s in live.slots
        )
    )

    assert diff(warm, downgraded, now=NOW) == []


def test_spaces_count_dropping_is_not_an_event(warm, live):
    """You declined the low-spaces nudge, so LIMITED -> LIMITED stays silent."""
    tightened = ParseResult(
        slots=tuple(
            s.__class__(**{**{f: getattr(s, f) for f in s.__slots__}, "spaces_left": 1})
            if s.availability is Availability.LIMITED else s
            for s in live.slots
        )
    )

    assert diff(warm, tightened, now=NOW) == []


def test_unknown_availability_never_produces_a_slot_ping(warm, live):
    """An unrecognised string must raise HEALTH, never masquerade as an opening."""
    mutated = ParseResult(
        slots=tuple(
            s.__class__(**{**{f: getattr(s, f) for f in s.__slots__},
                           "availability": Availability.UNKNOWN,
                           "spaces_left": None})
            if s.game_id == TARGET_GAME_ID else s
            for s in live.slots
        ),
        anomalies=("row 16212 has unknown availability 'Waitlist'",),
    )
    events = diff(warm, mutated, now=NOW)

    assert {e.type for e in events} == {EventType.HEALTH}


# --------------------------------------------------------------------------
# HEALTH - so silence is never mistaken for "no slots yet"
# --------------------------------------------------------------------------


def test_parse_anomalies_emit_one_health_event(warm, live):
    result = ParseResult(slots=live.slots, anomalies=("header renamed", "row 5 short"))
    events = diff(warm, result, now=NOW)

    health = [e for e in events if e.type is EventType.HEALTH]
    assert len(health) == 1
    assert "header renamed" in health[0].message


def test_going_empty_after_having_slots_is_a_health_event(warm):
    events = diff(warm, ParseResult(is_empty_state=True), now=NOW)

    assert [e.type for e in events] == [EventType.HEALTH]


def test_empty_state_from_cold_is_not_a_health_event():
    assert diff(State(), ParseResult(is_empty_state=True), now=NOW) == []


def test_empty_state_while_already_empty_is_quiet():
    state = record(State(), ParseResult(is_empty_state=True), now=NOW)

    assert diff(state, ParseResult(is_empty_state=True), now=NOW) == []


# --------------------------------------------------------------------------
# Cooldown suppression - a flapping slot must not ping every poll
# --------------------------------------------------------------------------


def test_repeat_event_inside_cooldown_is_suppressed(warm, reopened):
    events = diff(warm, reopened, now=NOW)
    state = mark_notified(warm, events, now=NOW)

    later = NOW + dt.timedelta(hours=1)
    again = suppress_recent(events, state, now=later, cooldown=dt.timedelta(hours=6))

    assert again == []


def test_repeat_event_after_cooldown_is_allowed(warm, reopened):
    events = diff(warm, reopened, now=NOW)
    state = mark_notified(warm, events, now=NOW)

    later = NOW + dt.timedelta(hours=7)
    again = suppress_recent(events, state, now=later, cooldown=dt.timedelta(hours=6))

    assert len(again) == 1


def test_first_occurrence_is_never_suppressed(warm, reopened):
    events = diff(warm, reopened, now=NOW)

    assert suppress_recent(events, warm, now=NOW, cooldown=dt.timedelta(hours=6)) == events


def test_health_uses_its_own_longer_cooldown(warm):
    events = diff(warm, ParseResult(is_empty_state=True), now=NOW)
    state = mark_notified(warm, events, now=NOW)

    # Past the 6h slot cooldown but inside the 12h health cooldown.
    later = NOW + dt.timedelta(hours=8)
    again = suppress_recent(
        events,
        state,
        now=later,
        cooldown=dt.timedelta(hours=6),
        health_cooldown=dt.timedelta(hours=12),
    )

    assert again == []


def test_distinct_slots_are_suppressed_independently(warm, new_date):
    events = diff(warm, new_date, now=NOW)
    state = mark_notified(warm, events[:1], now=NOW)

    remaining = suppress_recent(events, state, now=NOW, cooldown=dt.timedelta(hours=6))

    assert len(remaining) == 5


def test_reopen_and_new_slot_for_same_game_id_are_separate_keys(warm, reopened):
    """dedup_key includes the event type, so one doesn't mask the other."""
    reopen_event = diff(warm, reopened, now=NOW)[0]
    state = mark_notified(warm, [reopen_event], now=NOW)
    forged = reopen_event.__class__(type=EventType.NEW_SLOT, slot=reopen_event.slot)

    assert suppress_recent([forged], state, now=NOW, cooldown=dt.timedelta(hours=6))


# --------------------------------------------------------------------------
# Failure streaks
# --------------------------------------------------------------------------


def test_failure_streak_reaching_threshold_emits_health(warm):
    state = warm.__class__(**{**warm.__dict__, "failure_streak": 3})
    events = diff(state, ParseResult(slots=(), anomalies=()), now=NOW, failure_threshold=3)

    assert any(e.type is EventType.HEALTH for e in events)
