"""Parser tests - the encoded contract for the site's booking table.

Every expectation here was verified against real responses (see the plan's
"Verified site contract"), not inferred from documentation.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from slotwatch.models import Availability
from slotwatch.parse import infer_year, parse_availability, parse_table

from .conftest import CAPTURE_DAY, TARGET_GAME_ID


# --------------------------------------------------------------------------
# The live capture
# --------------------------------------------------------------------------


def test_live_fixture_parses_all_24_rows(load):
    result = parse_table(load("primary_live.html"), today=CAPTURE_DAY)

    assert len(result.slots) == 24
    assert result.is_empty_state is False
    assert result.anomalies == ()


def test_target_slot_parsed_exactly(load):
    result = parse_table(load("primary_live.html"), today=CAPTURE_DAY)
    slot = next(s for s in result.slots if s.game_id == TARGET_GAME_ID)

    assert slot.date_raw == "Sun 08/02"
    assert slot.date == dt.date(2026, 8, 2)
    assert slot.gym == "Central Gym"
    assert slot.level == "Intermediate - Court 1"
    assert slot.time_raw == "4:00 pm - 7:30 pm"
    assert slot.fee == Decimal("16.00")
    assert slot.availability is Availability.SOLD_OUT
    assert slot.radio_disabled is True
    assert slot.spaces_left == 0


def test_live_fixture_availability_distribution(load):
    """17 sold out / 5 open / 2 limited - counted from the real response."""
    slots = parse_table(load("primary_live.html"), today=CAPTURE_DAY).slots
    counts = {a: sum(1 for s in slots if s.availability is a) for a in Availability}

    assert counts[Availability.SOLD_OUT] == 17
    assert counts[Availability.OPEN] == 5
    assert counts[Availability.LIMITED] == 2
    assert counts[Availability.UNKNOWN] == 0


def test_game_ids_are_unique_and_numeric(load):
    slots = parse_table(load("primary_live.html"), today=CAPTURE_DAY).slots
    ids = [s.game_id for s in slots]

    assert len(set(ids)) == len(ids)
    assert all(i.isdigit() for i in ids)


def test_spaces_left_captured_for_limited_rows(load):
    slots = parse_table(load("primary_live.html"), today=CAPTURE_DAY).slots
    limited = sorted(s.spaces_left for s in slots if s.availability is Availability.LIMITED)

    assert limited == [1, 2]


def test_bookable_matches_radio_disabled_across_live_fixture(load):
    """disabled <=> Sold Out held 17/17 and 7/7 on the real page. Lock it in."""
    slots = parse_table(load("primary_live.html"), today=CAPTURE_DAY).slots

    for slot in slots:
        assert slot.availability.is_bookable is not slot.radio_disabled


# --------------------------------------------------------------------------
# Empty state - the distinction that keeps silence honest
# --------------------------------------------------------------------------


def test_empty_state_is_zero_slots_not_an_error(load):
    result = parse_table(load("primary_empty.html"), today=CAPTURE_DAY)

    assert result.is_empty_state is True
    assert result.slots == ()
    assert result.anomalies == ()


def test_empty_state_picks_the_slot_table_not_the_waiver_table(load):
    """primary_empty.html is a full page containing a second, unrelated table."""
    result = parse_table(load("primary_empty.html"), today=CAPTURE_DAY)

    assert result.is_empty_state is True


# --------------------------------------------------------------------------
# Breakage must be loud, never silently empty
# --------------------------------------------------------------------------


def test_layout_change_reports_anomalies_and_still_returns_good_rows(load):
    result = parse_table(load("layout_changed.html"), today=CAPTURE_DAY)

    assert result.anomalies, "renamed header + malformed row must be reported"
    # The critical assertion: degraded markup does NOT masquerade as "no slots".
    assert result.slots, "must not silently return an empty slot list"
    assert result.is_empty_state is False
    assert len(result.slots) == 23  # the cell-short row is skipped, not guessed at


def test_malformed_row_is_skipped_rather_than_mis_assigned(load):
    """A row missing a <td> must not yield a Slot with shifted column data."""
    result = parse_table(load("layout_changed.html"), today=CAPTURE_DAY)

    assert "16193" not in {s.game_id for s in result.slots}
    for slot in result.slots:
        assert slot.level != slot.time_raw


def test_missing_table_is_an_anomaly_not_an_empty_state():
    result = parse_table("<html><body><p>nothing here</p></body></html>", today=CAPTURE_DAY)

    assert result.slots == ()
    assert result.is_empty_state is False
    assert result.anomalies


def test_empty_input_is_an_anomaly():
    result = parse_table("", today=CAPTURE_DAY)

    assert result.is_empty_state is False
    assert result.anomalies


# --------------------------------------------------------------------------
# Availability vocabulary (closed set, verified on real data)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected,spaces",
    [
        ("Sold Out", Availability.SOLD_OUT, 0),
        ("sold out", Availability.SOLD_OUT, 0),
        ("SOLD OUT", Availability.SOLD_OUT, 0),
        ("Yes", Availability.OPEN, None),
        ("yes", Availability.OPEN, None),
        ("1 Space", Availability.LIMITED, 1),
        ("2 Spaces", Availability.LIMITED, 2),
        ("12 Spaces", Availability.LIMITED, 12),
        ("", Availability.UNKNOWN, None),
        ("Waitlist", Availability.UNKNOWN, None),
        ("garbage", Availability.UNKNOWN, None),
    ],
)
def test_parse_availability(text, expected, spaces):
    assert parse_availability(text) == (expected, spaces)


def test_unknown_availability_is_never_treated_as_bookable():
    availability, _ = parse_availability("something new")

    assert availability is Availability.UNKNOWN
    assert availability.is_bookable is False


# --------------------------------------------------------------------------
# Year inference - the page never states a year
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "month,day,today,expected",
    [
        (8, 2, dt.date(2026, 7, 26), 2026),  # same year, just ahead
        (7, 26, dt.date(2026, 7, 26), 2026),  # today
        (12, 28, dt.date(2026, 1, 5), 2025),  # Dec listing seen in Jan -> last year
        (1, 4, dt.date(2025, 12, 28), 2026),  # Jan listing seen in Dec -> next year
        (1, 15, dt.date(2026, 1, 5), 2026),
        (12, 20, dt.date(2026, 12, 28), 2026),
    ],
)
def test_infer_year_picks_the_nearest_plausible_date(month, day, today, expected):
    assert infer_year(month, day, today) == expected


def test_infer_year_handles_leap_day():
    assert infer_year(2, 29, dt.date(2028, 2, 1)) == 2028


def test_dates_are_ordered_and_all_sundays_in_live_fixture(load):
    """the primary tab should only ever list Sundays - a cheap sanity net."""
    slots = parse_table(load("primary_live.html"), today=CAPTURE_DAY).slots

    assert {s.date.weekday() for s in slots} == {6}
    assert min(s.date for s in slots) == dt.date(2026, 7, 26)
    assert max(s.date for s in slots) == dt.date(2026, 8, 16)


# --------------------------------------------------------------------------
# The large archived page (different program, identical renderer)
# --------------------------------------------------------------------------


def test_archived_page_parses(load):
    result = parse_table(load("archived_110_rows.html"), today=dt.date(2026, 1, 16))

    assert len(result.slots) == 110
    assert result.is_empty_state is False


def test_middle_radio_field_is_ignored(load):
    """It was 24 on the live page and 0/6 in the archive with no relation to
    availability. Parsing it would be a bug; assert nothing depends on it."""
    archived = parse_table(load("archived_110_rows.html"), today=dt.date(2026, 1, 16))
    limited = [s for s in archived.slots if s.availability is Availability.LIMITED]

    assert limited, "archive has 'N Spaces' rows"
    for slot in limited:
        assert slot.spaces_left is not None
        assert slot.spaces_left > 0
