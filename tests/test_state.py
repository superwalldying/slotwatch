"""State persistence, the day tally, and the concurrent-write merge.

`merge` exists because git cannot reconcile state/seen.json: two runs that each create
it conflict add/add, and there is no line-wise merge of a JSON object that means
anything. The tests below pin the two properties that make taking-one-side unsafe -
a dropped notified-marker re-pings a slot, and a dropped sighting invents a new one.
"""

from __future__ import annotations

import datetime as dt

from slotwatch.state import (
    DayState,
    State,
    clear_day,
    close_day,
    count_poll,
    load,
    merge,
    open_day,
    save,
)

NOW = dt.datetime(2026, 7, 27, 17, 0, tzinfo=dt.UTC)


def ts(hour: int) -> str:
    return NOW.replace(hour=hour).isoformat()


# --------------------------------------------------------------------------
# Round-tripping
# --------------------------------------------------------------------------


def test_state_round_trips_through_the_file(tmp_path):
    path = tmp_path / "seen.json"
    original = State(
        seeded=True,
        slots={"1": "sold_out"},
        notified={"1:reopened": ts(15)},
        failure_streak=2,
        day=DayState(date="2026-07-27", polls=41, failures=3, alerts=1, started=True),
    )

    save(path, original)

    assert load(path) == original


def test_a_state_file_written_before_day_tracking_still_loads(tmp_path):
    """Adding the day block must not strand the state file already on the branch."""
    path = tmp_path / "seen.json"
    path.write_text(
        '{"version": 1, "seeded": true, "slots": {"1": "open"}, '
        '"notified": {}, "failure_streak": 0}',
        encoding="utf-8",
    )

    state = load(path)

    assert state.seeded is True
    assert state.slots == {"1": "open"}
    assert state.day == DayState()


# --------------------------------------------------------------------------
# The day tally
# --------------------------------------------------------------------------


def test_polls_are_counted_against_the_open_day():
    state = open_day(State(), "2026-07-27")

    state = count_poll(state, alerts=1)
    state = count_poll(state, failed=True)

    assert state.day.polls == 2
    assert state.day.failures == 1
    assert state.day.alerts == 1


def test_polls_outside_an_open_day_are_not_counted():
    """--ignore-window runs and local pokes must not inflate a real day's total."""
    state = count_poll(State(), alerts=5)

    assert state.day == DayState()


def test_a_closed_day_stops_accruing_polls():
    state = close_day(open_day(State(), "2026-07-27"))

    assert count_poll(state).day.polls == 0


def test_closing_a_day_leaves_it_recorded_so_it_cannot_report_twice():
    state = close_day(open_day(State(), "2026-07-27"))

    assert state.day.date == "2026-07-27"
    assert state.day.ended is True
    assert state.day.is_open is False


def test_clearing_a_day_forgets_it_entirely():
    assert clear_day(open_day(State(), "2026-07-27")).day == DayState()


# --------------------------------------------------------------------------
# merge - the fix for the add/add conflict
# --------------------------------------------------------------------------


def test_merge_unions_sightings_from_both_runs():
    mine = State(seeded=True, slots={"a": "open"})
    theirs = State(seeded=True, slots={"b": "sold_out"})

    assert merge(mine, theirs).slots == {"a": "open", "b": "sold_out"}


def test_merge_prefers_my_view_of_a_slot_we_both_saw():
    """`mine` is the run that just polled, so its reading is the newer one."""
    mine = State(seeded=True, slots={"a": "open"})
    theirs = State(seeded=True, slots={"a": "sold_out"})

    assert merge(mine, theirs).slots == {"a": "open"}


def test_merge_never_drops_a_notified_marker():
    """A lost marker re-pings a slot you were already told about."""
    mine = State(notified={"a:reopened": ts(15)})
    theirs = State(notified={"b:new_slot": ts(16)})

    merged = merge(mine, theirs)

    assert set(merged.notified) == {"a:reopened", "b:new_slot"}


def test_merge_keeps_the_later_notification_timestamp():
    """Cooldown must err long: suppressing a duplicate beats a repeat buzz."""
    mine = State(notified={"a:reopened": ts(13)})
    theirs = State(notified={"a:reopened": ts(16)})

    assert merge(mine, theirs).notified["a:reopened"] == ts(16)
    assert merge(theirs, mine).notified["a:reopened"] == ts(16)


def test_merge_keeps_the_higher_failure_streak():
    """Err toward raising a health warning rather than swallowing one."""
    assert merge(State(failure_streak=1), State(failure_streak=4)).failure_streak == 4


def test_merge_stays_seeded_if_either_side_was():
    """Un-seeding would make the next poll ping every currently-listed row."""
    assert merge(State(seeded=False), State(seeded=True)).seeded is True


def test_merge_is_symmetric_for_everything_that_matters():
    """Only `slots` is deliberately asymmetric; nothing else may depend on order."""
    mine = State(seeded=True, notified={"a": ts(13)}, failure_streak=1)
    theirs = State(seeded=False, notified={"b": ts(16)}, failure_streak=4)

    a, b = merge(mine, theirs), merge(theirs, mine)

    assert a.notified == b.notified
    assert a.failure_streak == b.failure_streak
    assert a.seeded == b.seeded


def test_merging_the_same_day_keeps_the_higher_tally():
    """The two runs overlapped, so their counts share a prefix - max, not sum."""
    mine = State(day=DayState(date="2026-07-27", polls=41, alerts=1, started=True))
    theirs = State(day=DayState(date="2026-07-27", polls=12, alerts=0, started=True))

    day = merge(mine, theirs).day

    assert day.polls == 41
    assert day.alerts == 1


def test_merging_different_days_keeps_the_later_one():
    mine = State(day=DayState(date="2026-07-28", polls=3, started=True))
    theirs = State(day=DayState(date="2026-07-27", polls=160, started=True, ended=True))

    assert merge(mine, theirs).day.date == "2026-07-28"
    assert merge(theirs, mine).day.date == "2026-07-28"


def test_merging_a_day_keeps_a_ping_either_side_already_sent():
    """Re-sending a delivered report is worse than the report being slightly stale."""
    mine = State(day=DayState(date="2026-07-27", started=True, ended=False))
    theirs = State(day=DayState(date="2026-07-27", started=True, ended=True))

    assert merge(mine, theirs).day.ended is True


def test_merging_against_an_absent_remote_state_is_a_no_op():
    """The add/add case itself: the remote had no seen.json at all."""
    mine = State(seeded=True, slots={"a": "open"}, notified={"a:reopened": ts(15)})

    assert merge(mine, State()) == mine
