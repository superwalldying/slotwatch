"""The daily heartbeat, and the merge that stops two runs colliding over state.

Both live here because they answer the same question from opposite ends: was the bot
actually watching today? The heartbeat reports it; the merge is what stopped a second
concurrent run from failing the job and losing a day's state along the way.

Both edges of the day are detected from the clock on an ordinary loop iteration, so
these tests drive day_tick at the interesting instants rather than sleeping through a day.
"""

from __future__ import annotations

import datetime as dt

import pytest
import responses

from slotwatch.config import load_config
from slotwatch.fetch import FetchError
from slotwatch.main import day_tick, main, poll_once
from slotwatch.state import DayState, State, count_poll, load, open_day, save

from .conftest import SITE

YAML = """
poll:
  tabs: [primary]
  timezone: America/New_York
  windows:
    - days: [mon, tue, wed, thu, fri]
      start: "09:00"
      end: "17:00"
      interval_minutes: 3
notify:
  webhook_url_env: TEST_WEBHOOK
  daily_summary: true
  coverage_floor_percent: 75
rules: []
"""

WEBHOOK = "https://discord.com/api/webhooks/x/y"

# 2026-07-27 is a Monday. The real day this was built to explain.
MON_DAWN = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.UTC)  # 08:00 EDT, not open yet
MON_MORNING = dt.datetime(2026, 7, 27, 14, 0, tzinfo=dt.UTC)  # 10:00 EDT, inside
MON_EVENING = dt.datetime(2026, 7, 27, 21, 1, tzinfo=dt.UTC)  # 17:01 EDT, shut
TUE_MORNING = dt.datetime(2026, 7, 28, 14, 0, tzinfo=dt.UTC)  # 10:00 EDT, next day

# 09:00-17:00 at 3-minute spacing.
FULL_DAY = 160


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "daily.yaml"
    path.write_text(YAML, encoding="utf-8")
    return load_config(path)


def tick(config, state, *, now, **kwargs):
    kwargs.setdefault("webhook_url", WEBHOOK)
    return day_tick(config, state, now=now, **kwargs)


def polled(times: int, *, date: str = "2026-07-27", failed: int = 0) -> State:
    """A day already part-way through, as the closing report would find it."""
    state = open_day(State(), date)
    for index in range(times):
        state = count_poll(state, failed=index < failed)
    return state


# --------------------------------------------------------------------------
# Opening edge
# --------------------------------------------------------------------------


def test_the_first_poll_of_the_day_announces_the_day(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        state = tick(config, State(), now=MON_MORNING)
        body = mock.calls[0].request.body.decode()

    assert state.day.date == "2026-07-27"
    assert state.day.started is True
    assert str(FULL_DAY) in body, "the opening ping should state the day's poll target"


def test_the_day_is_announced_only_once(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        state = tick(config, State(), now=MON_MORNING)
        tick(config, state, now=MON_MORNING + dt.timedelta(hours=1))

        assert len(mock.calls) == 1


def test_nothing_is_announced_before_the_window_opens(config):
    """08:00 is outside every window, but the day has not happened yet."""
    assert tick(config, State(), now=MON_DAWN).day == DayState()


def test_a_dry_run_announces_nothing(config):
    state = tick(config, State(), now=MON_MORNING, dry_run=True, webhook_url=None)

    assert state.day == DayState(), "a dry run must not record a ping it never sent"


# --------------------------------------------------------------------------
# Closing edge - polls made against polls scheduled
# --------------------------------------------------------------------------


def test_the_closing_report_compares_polls_against_the_schedule(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        state = tick(config, polled(41), now=MON_EVENING)
        body = mock.calls[0].request.body.decode()

    assert state.day.ended is True
    assert "41" in body and str(FULL_DAY) in body
    assert "short" in body


def test_the_closing_report_counts_fetch_failures_separately(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        tick(config, polled(FULL_DAY, failed=7), now=MON_EVENING)
        body = mock.calls[0].request.body.decode()

    assert "Fetch failures" in body


def test_a_full_day_closes_without_buzzing_you(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        tick(config, polled(FULL_DAY), now=MON_EVENING, mention="<@42>")
        body = mock.calls[0].request.body.decode()

    assert "allowed_mentions" not in body


def test_a_mostly_missed_day_does_buzz_you(config):
    """The exact failure this project exists to prevent: silence that proves nothing."""
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        tick(config, polled(41), now=MON_EVENING, mention="<@42>")
        body = mock.calls[0].request.body.decode()

    assert "allowed_mentions" in body
    assert "42" in body


def test_the_day_is_closed_only_once(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        state = tick(config, polled(FULL_DAY), now=MON_EVENING)
        tick(config, state, now=MON_EVENING + dt.timedelta(minutes=30))

        assert len(mock.calls) == 1


def test_the_day_does_not_close_while_the_window_is_still_open(config):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        state = tick(config, polled(20), now=MON_MORNING)

        assert len(mock.calls) == 0

    assert state.day.ended is False


# --------------------------------------------------------------------------
# Reports that would otherwise be lost
# --------------------------------------------------------------------------


def test_a_day_whose_job_died_early_is_reported_late_not_lost(config):
    """--max-runtime can expire before the window shuts, and Actions drops runs. A
    missing report looks exactly like a dead bot, so the next day's first tick sends it."""
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        state = tick(config, polled(41), now=TUE_MORNING)
        bodies = [call.request.body.decode() for call in mock.calls]

    assert len(bodies) == 2, "yesterday's closing report, then today's opening one"
    assert "27 Jul" in bodies[0] and "41" in bodies[0]
    assert "28 Jul" in bodies[1]
    assert state.day.date == "2026-07-28"
    assert state.day.polls == 0, "the new day starts from zero"


def test_a_rejected_report_is_retried_rather_than_dropped(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=500)
        state = tick(config, polled(41), now=MON_EVENING)

    assert state.day.ended is False, "an undelivered report must stay pending"
    assert state.day.date == "2026-07-27"


def test_a_rejected_close_does_not_let_the_next_day_overwrite_it(config):
    """Opening today on top of an unreported yesterday would lose it permanently."""
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=500)
        state = tick(config, polled(41), now=TUE_MORNING)

    assert state.day.date == "2026-07-27"
    assert state.day.ended is False


def test_daily_summary_can_be_switched_off(tmp_path):
    path = tmp_path / "quiet.yaml"
    path.write_text(YAML.replace("daily_summary: true", "daily_summary: false"),
                    encoding="utf-8")

    assert tick(load_config(path), State(), now=MON_MORNING).day == DayState()


# --------------------------------------------------------------------------
# Counting an attempt, not a success
# --------------------------------------------------------------------------


def _always_fail(*args, **kwargs):
    raise FetchError("connection refused")


def test_a_failed_fetch_still_counts_as_an_attempt(config, monkeypatch):
    """Coverage is about attempts: a poll that could not read the page used a slot."""
    monkeypatch.setattr("slotwatch.main.fetch_tab", _always_fail)
    state = open_day(State(), "2026-07-27")

    before = state.failure_streak
    state, _ = poll_once(config, state, SITE, now=MON_MORNING, dry_run=True)
    state = count_poll(state, failed=state.failure_streak > before)

    assert state.day.polls == 1
    assert state.day.failures == 1


# --------------------------------------------------------------------------
# --merge-state: reconciling two runs that both wrote state
# --------------------------------------------------------------------------


def test_merge_state_unions_two_state_files(tmp_path):
    mine, theirs = tmp_path / "mine.json", tmp_path / "theirs.json"
    save(mine, State(seeded=True, slots={"a": "open"},
                     notified={"a:reopened": "2026-07-27T15:00:00+00:00"}))
    save(theirs, State(seeded=True, slots={"b": "sold_out"},
                       notified={"b:new_slot": "2026-07-27T16:00:00+00:00"}))

    code = main(["--state", str(mine), "--merge-state", str(theirs)])

    merged = load(mine)
    assert code == 0
    assert merged.slots == {"a": "open", "b": "sold_out"}
    assert set(merged.notified) == {"a:reopened", "b:new_slot"}


def test_merge_state_needs_no_site_profile_or_webhook(tmp_path, monkeypatch):
    """It runs on the CI persist path, which fires even after the poll step failed."""
    monkeypatch.delenv("SITE_PROFILE", raising=False)
    monkeypatch.delenv("TEST_WEBHOOK", raising=False)
    mine, theirs = tmp_path / "mine.json", tmp_path / "theirs.json"
    save(mine, State(seeded=True, slots={"a": "open"}))
    save(theirs, State(seeded=True, slots={"b": "open"}))

    assert main(["--state", str(mine), "--merge-state", str(theirs)]) == 0


def test_merge_state_survives_a_remote_that_had_no_state_file(tmp_path):
    """The add/add case that failed run 30301348989: the file existed on neither side."""
    mine = tmp_path / "mine.json"
    save(mine, State(seeded=True, slots={"a": "open"}))

    code = main(["--state", str(mine), "--merge-state", str(tmp_path / "absent.json")])

    assert code == 0
    assert load(mine).slots == {"a": "open"}


def test_a_corrupt_day_in_the_state_file_does_not_take_the_poller_down(config):
    """A hand-edited state file must not crash the run that was about to poll."""
    state = State(day=DayState(date="not-a-date", polls=9, started=True))

    with responses.RequestsMock() as mock:
        mock.add(responses.POST, WEBHOOK, status=204)
        state = tick(config, state, now=MON_EVENING)
        body = mock.calls[0].request.body.decode()

    # Reported as best it can be described, then cleared so it blocks nothing further.
    assert "not-a-date" in body
    assert "9" in body
    assert state.day == DayState()
