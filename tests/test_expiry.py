"""Sessions that have already finished.

The site keeps same-day rows listed, so a cancellation can free a spot in a block that
is already over - the live capture carries six rows dated its own capture day. Nothing
about such a spot is actionable, and a ping you cannot act on is how you learn to ignore
the ones you can.

The suppression is deliberately one-directional. Every path where the bot cannot be sure
a session is over must still alert, because staying quiet is the failure this project
exists to prevent; the fail-open tests below are the load-bearing ones.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from slotwatch.diff import diff, has_ended
from slotwatch.models import Availability, ParseResult
from slotwatch.parse import parse_table, parse_time_range
from slotwatch.state import State, record

from .conftest import CAPTURE_DAY

NOW = dt.datetime(2026, 7, 26, 13, 0, tzinfo=dt.UTC)

# The capture day is itself a Sunday, and the page lists that day's own sessions.
SAME_DAY_MORNING = "9:00 am - 12:00 pm"


@pytest.fixture
def live(load):
    return parse_table(load("primary_live.html"), today=CAPTURE_DAY)


@pytest.fixture
def warm(live):
    return record(State(), live, now=NOW)


@pytest.fixture
def victim(live):
    """A sold-out row dated the capture day - the one that can expire mid-watch."""
    return next(
        s for s in live.slots
        if s.date == CAPTURE_DAY and s.availability is Availability.SOLD_OUT
    )


def freed(live, target) -> ParseResult:
    """The page with `target` reopened, exactly as a cancellation would leave it."""
    return ParseResult(slots=tuple(
        dataclasses.replace(
            s, availability=Availability.OPEN, spaces_left=None, radio_disabled=False
        )
        if s.game_id == target.game_id else s
        for s in live.slots
    ))


def local(hour: int, minute: int = 0) -> dt.datetime:
    """Naive wall-clock time at the venue on the capture day."""
    return dt.datetime.combine(CAPTURE_DAY, dt.time(hour, minute))


# --------------------------------------------------------------------------
# Reading the time column
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("4:00 pm - 7:30 pm", (dt.time(16, 0), dt.time(19, 30))),
    ("9:00 am - 12:00 pm", (dt.time(9, 0), dt.time(12, 0))),
    ("12:00 pm - 3:30 pm", (dt.time(12, 0), dt.time(15, 30))),
    ("9:30 am - 11:30 am", (dt.time(9, 30), dt.time(11, 30))),
    # Near-misses worth tolerating rather than flagging.
    ("1:30 p.m. – 3:30 p.m.", (dt.time(13, 30), dt.time(15, 30))),
    ("10:00 PM to 1:00 AM", (dt.time(22, 0), dt.time(1, 0))),
    ("12:00 am - 12:00 pm", (dt.time(0, 0), dt.time(12, 0))),
])
def test_time_ranges_the_page_actually_uses(text, expected):
    assert parse_time_range(text) == expected


def test_an_unqualified_opening_half_does_not_invert_the_range():
    """Borrowing "pm" blindly turns 9am-12pm into 9pm-12pm, which ends_at would then
    mistake for an overnight block and never treat as finished."""
    assert parse_time_range("9:00 - 12:00 pm") == (dt.time(9, 0), dt.time(12, 0))
    assert parse_time_range("11:30 - 1:30 pm") == (dt.time(11, 30), dt.time(13, 30))


@pytest.mark.parametrize("text", ["", "noon til dusk", "16:00 - 19:30", "TBC"])
def test_unreadable_times_yield_nothing_rather_than_a_guess(text):
    assert parse_time_range(text) == (None, None)


def test_every_row_in_the_live_capture_has_a_readable_time(live):
    """If this fails the real page has drifted, and expiry silently stops working."""
    assert live.slots
    assert [s.time_raw for s in live.slots if s.end_time is None] == []
    assert [a for a in live.anomalies if "unparseable time" in a] == []


def test_an_unreadable_time_is_flagged_like_an_unreadable_date(load):
    html = load("primary_live.html").replace("9:00 am - 12:00 pm", "whenever")

    result = parse_table(html, today=CAPTURE_DAY)

    assert any("unparseable time" in a for a in result.anomalies)


# --------------------------------------------------------------------------
# ends_at
# --------------------------------------------------------------------------


def test_ends_at_combines_the_date_with_the_closing_time(victim):
    assert victim.time_raw == SAME_DAY_MORNING
    assert victim.ends_at == dt.datetime.combine(CAPTURE_DAY, dt.time(12, 0))


def test_an_overnight_block_ends_the_following_day(victim):
    overnight = dataclasses.replace(
        victim, start_time=dt.time(22, 0), end_time=dt.time(1, 0)
    )

    assert overnight.ends_at == dt.datetime.combine(
        CAPTURE_DAY + dt.timedelta(days=1), dt.time(1, 0)
    )


@pytest.mark.parametrize("field", ["date", "end_time"])
def test_ends_at_is_unknown_when_either_half_is_unreadable(victim, field):
    assert dataclasses.replace(victim, **{field: None}).ends_at is None


# --------------------------------------------------------------------------
# The guard, measured against the session's end
# --------------------------------------------------------------------------


@pytest.mark.parametrize("when,expected", [
    (local(8, 0), False),    # before it starts
    (local(10, 30), False),  # half way through - a freed spot is still worth taking
    (local(11, 59), False),  # one minute left
    (local(12, 0), True),    # the closing minute itself
    (local(17, 0), True),    # long over
])
def test_a_session_counts_as_ended_only_once_it_is_over(victim, when, expected):
    assert has_ended(victim, when) is expected


def test_a_reopening_after_the_session_ended_is_not_announced(warm, live, victim):
    events = diff(warm, freed(live, victim), now=NOW, now_local=local(17, 0))

    assert [e for e in events if e.slot and e.slot.game_id == victim.game_id] == []


def test_the_same_reopening_earlier_in_the_day_is_announced(warm, live, victim):
    events = diff(warm, freed(live, victim), now=NOW, now_local=local(10, 30))

    assert [e for e in events if e.slot and e.slot.game_id == victim.game_id]


def test_a_finished_session_is_still_recorded_in_state(warm, live, victim):
    """Dropping it from the observation would look like ageing off the rolling window,
    and its return would then read as a brand-new slot."""
    after = record(warm, freed(live, victim), now=NOW)

    assert victim.game_id in after.slots


def test_expiry_never_suppresses_a_future_date(warm, live):
    """The whole point: only today's rows can expire, next Sunday's cannot."""
    future = next(s for s in live.slots
                  if s.date and s.date > CAPTURE_DAY
                  and s.availability is Availability.SOLD_OUT)

    events = diff(warm, freed(live, future), now=NOW, now_local=local(23, 59))

    assert [e for e in events if e.slot and e.slot.game_id == future.game_id]


# --------------------------------------------------------------------------
# Failing open - the load-bearing half
# --------------------------------------------------------------------------


def test_without_a_local_clock_nothing_is_suppressed(warm, live, victim):
    """Callers that never opt in keep the old behaviour rather than going quiet."""
    events = diff(warm, freed(live, victim), now=NOW)

    assert [e for e in events if e.slot and e.slot.game_id == victim.game_id]


@pytest.mark.parametrize("field", ["date", "end_time"])
def test_an_unreadable_slot_still_alerts_long_after_it_would_have_ended(victim, field):
    blind = dataclasses.replace(
        victim, availability=Availability.OPEN, spaces_left=None,
        radio_disabled=False, **{field: None},
    )

    assert has_ended(blind, local(23, 59)) is False
    assert diff(State(seeded=True), ParseResult(slots=(blind,)),
                now=NOW, now_local=local(23, 59))


def test_expiry_does_not_swallow_a_health_warning(warm, live, victim):
    """Drifted markup must stay loud even when every row on the page has expired."""
    page = ParseResult(slots=freed(live, victim).slots,
                       anomalies=("unrecognised column header 'Skill'",))

    events = diff(warm, page, now=NOW, now_local=local(23, 59))

    assert any(e.message and "Skill" in e.message for e in events)
