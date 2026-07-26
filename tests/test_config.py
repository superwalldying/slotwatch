"""Config + polling-window tests.

The DST cases are the reason this logic lives in Python rather than in a cron
expression: GitHub Actions cron is UTC-only, so a fixed UTC window silently slides an
hour every November.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from slotwatch.config import (
    DEFAULT_INTERVAL,
    describe_schedule,
    interval_at,
    load_config,
    local_now,
    next_open,
)
from slotwatch.models import EventType

YAML = """
poll:
  tabs: [primary]
  timezone: America/New_York
  cooldown_hours: 6
  health_cooldown_hours: 12
  windows:
    - days: [mon, tue, wed, thu, sat, sun]
      start: "09:00"
      end: "17:00"
      interval_minutes: 3
    - days: [fri]
      start: "09:00"
      end: "17:00"
      interval_minutes: 3
      dense:
        start: "12:00"
        end: "15:00"
        interval_minutes: 2
notify:
  webhook_url_env: DISCORD_WEBHOOK_URL
  mention: "<@42>"
rules:
  - name: "Sunday Intermediate Court 1 late block"
    gym: "Central Gym"
    level: "Intermediate - Court 1"
    time: "4:00 pm - 7:30 pm"
    date: any
    triggers: [new_slot, reopened]
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(YAML, encoding="utf-8")
    return load_config(path)


def utc(year, month, day, hour, minute=0):
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.UTC)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_config_loads_poll_settings(config):
    assert config.poll.tabs == ("primary",)
    assert config.poll.timezone == "America/New_York"
    assert config.poll.cooldown == dt.timedelta(hours=6)
    assert config.poll.health_cooldown == dt.timedelta(hours=12)
    assert len(config.poll.windows) == 2


def test_config_loads_notify_settings(config):
    assert config.notify.webhook_url_env == "DISCORD_WEBHOOK_URL"
    assert config.notify.mention == "<@42>"


def test_config_loads_the_seeded_rule(config):
    assert len(config.rules) == 1
    rule = config.rules[0]
    assert rule.level == "Intermediate - Court 1"
    assert rule.triggers == frozenset({EventType.NEW_SLOT, EventType.REOPENED})


def test_missing_config_file_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


# --------------------------------------------------------------------------
# Window gating
# --------------------------------------------------------------------------


def test_inside_the_weekday_window(config):
    # Tue 2026-07-28, 14:00 UTC = 10:00 EDT
    assert interval_at(config, utc(2026, 7, 28, 14)) == dt.timedelta(minutes=3)


def test_before_the_window_opens(config):
    # 12:00 UTC = 08:00 EDT
    assert interval_at(config, utc(2026, 7, 28, 12)) is None


def test_after_the_window_closes(config):
    # 22:00 UTC = 18:00 EDT
    assert interval_at(config, utc(2026, 7, 28, 22)) is None


def test_window_end_is_exclusive(config):
    # 21:00 UTC = 17:00 EDT exactly
    assert interval_at(config, utc(2026, 7, 28, 21)) is None
    # one minute earlier is still inside
    assert interval_at(config, utc(2026, 7, 28, 20, 59)) == dt.timedelta(minutes=3)


def test_friday_dense_window_polls_faster(config):
    """Sunday-session refunds close at 3pm Friday, so cancellations cluster before it."""
    # Fri 2026-07-31, 17:00 UTC = 13:00 EDT
    assert interval_at(config, utc(2026, 7, 31, 17)) == dt.timedelta(minutes=2)


def test_friday_outside_the_dense_hours_uses_the_normal_interval(config):
    # Fri 2026-07-31, 14:00 UTC = 10:00 EDT
    assert interval_at(config, utc(2026, 7, 31, 14)) == dt.timedelta(minutes=3)


def test_friday_after_the_deadline_is_still_watched(config):
    # Fri 2026-07-31, 20:00 UTC = 16:00 EDT - past 3pm, inside 9-5
    assert interval_at(config, utc(2026, 7, 31, 20)) == dt.timedelta(minutes=3)


def test_sunday_is_watched(config):
    # Sun 2026-08-02, 15:00 UTC = 11:00 EDT
    assert interval_at(config, utc(2026, 8, 2, 15)) == dt.timedelta(minutes=3)


# --------------------------------------------------------------------------
# DST - the same UTC hour is inside the window in July and outside in January
# --------------------------------------------------------------------------


def test_same_utc_hour_differs_across_dst(config):
    summer = utc(2026, 7, 28, 13)  # 09:00 EDT (UTC-4) -> inside
    winter = utc(2026, 1, 27, 13)  # 08:00 EST (UTC-5) -> outside

    assert interval_at(config, summer) == dt.timedelta(minutes=3)
    assert interval_at(config, winter) is None


def test_winter_window_shifts_an_hour_later_in_utc(config):
    # 14:00 UTC = 09:00 EST, back inside the window
    assert interval_at(config, utc(2026, 1, 27, 14)) == dt.timedelta(minutes=3)


def test_local_now_converts_to_eastern(config):
    assert local_now(config, utc(2026, 7, 28, 14)).hour == 10
    assert local_now(config, utc(2026, 1, 27, 14)).hour == 9


def test_naive_datetime_is_treated_as_utc(config):
    naive = dt.datetime(2026, 7, 28, 14)

    assert interval_at(config, naive) == dt.timedelta(minutes=3)


# --------------------------------------------------------------------------
# Degenerate configs
# --------------------------------------------------------------------------


def test_no_windows_means_always_poll(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text(
        "poll:\n  tabs: [primary]\n  timezone: America/New_York\n"
        "notify:\n  webhook_url_env: X\nrules: []\n",
        encoding="utf-8",
    )
    config = load_config(path)

    assert interval_at(config, utc(2026, 7, 28, 3)) == DEFAULT_INTERVAL


def test_unknown_day_name_is_rejected(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text(
        "poll:\n  timezone: America/New_York\n  windows:\n"
        '    - days: [funday]\n      start: "09:00"\n      end: "17:00"\n'
        "      interval_minutes: 3\nnotify:\n  webhook_url_env: X\nrules: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="funday"):
        load_config(path)


def test_unknown_tab_is_rejected_when_the_site_profile_is_known(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text(
        "poll:\n  tabs: [atlantis_tuesday]\n  timezone: America/New_York\n"
        "notify:\n  webhook_url_env: X\nrules: []\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="atlantis_tuesday"):
        load_config(path, known_tabs={"primary"})


# --------------------------------------------------------------------------
# next_open - lets a looping job sleep until work resumes, or exit
# --------------------------------------------------------------------------


def test_next_open_returns_now_when_already_inside(config):
    inside = utc(2026, 7, 28, 14)

    assert interval_at(config, inside) is not None
    assert next_open(config, inside) == inside


def test_next_open_finds_this_morning_from_before_the_window(config):
    # Tue 08:00 EDT -> should be 09:00 EDT the same day
    upcoming = next_open(config, utc(2026, 7, 28, 12))

    assert local_now(config, upcoming).hour == 9
    assert local_now(config, upcoming).date() == dt.date(2026, 7, 28)


def test_next_open_skips_to_the_next_day_after_hours(config):
    # Tue 18:00 EDT -> Wed 09:00 EDT
    upcoming = next_open(config, utc(2026, 7, 28, 22))

    assert local_now(config, upcoming).date() == dt.date(2026, 7, 29)
    assert local_now(config, upcoming).hour == 9


def test_next_open_returns_none_when_nothing_opens_in_the_horizon(tmp_path):
    """A weekday-only schedule must report 'nothing today' on a Saturday, so a looping
    job exits instead of burning runner time on no-op waits."""
    path = tmp_path / "r.yaml"
    path.write_text(
        "poll:\n  timezone: America/New_York\n  windows:\n"
        '    - days: [mon, tue, wed, thu, fri]\n      start: "09:00"\n      end: "17:00"\n'
        "      interval_minutes: 3\nnotify:\n  webhook_url_env: X\nrules: []\n",
        encoding="utf-8",
    )
    cfg = load_config(path)

    # Sat 2026-08-01 12:00 EDT; Monday is more than 24h away.
    assert next_open(cfg, utc(2026, 8, 1, 16), horizon=dt.timedelta(hours=24)) is None
    # With a longer horizon it finds Monday morning.
    found = next_open(cfg, utc(2026, 8, 1, 16), horizon=dt.timedelta(days=3))
    assert local_now(cfg, found).strftime("%a") == "Mon"


# --------------------------------------------------------------------------
# describe_schedule - shown in the deploy-check ping
# --------------------------------------------------------------------------


def test_describe_schedule_collapses_contiguous_days():
    """The shipped config is Mon-Thu + Fri, so the run should collapse to a range."""
    shipped = load_config(Path(__file__).resolve().parents[1] / "rules.yaml")

    assert "Mon-Thu" in describe_schedule(shipped)


def test_describe_schedule_lists_non_contiguous_days_individually(config):
    text = describe_schedule(config)

    assert "Mon, Tue, Wed, Thu, Sat, Sun" in text
    assert "09:00-17:00" in text
    assert "every 3 min" in text
    assert "America/New_York" in text


def test_describe_schedule_mentions_the_dense_window(config):
    assert "2 min 12:00-15:00" in describe_schedule(config)


# --------------------------------------------------------------------------
# The shipped config, not a test fixture
# --------------------------------------------------------------------------


def test_shipped_rules_yaml_polls_weekdays_only():
    """Guards the real rules.yaml against a weekend day creeping back in."""
    shipped = load_config(Path(__file__).resolve().parents[1] / "rules.yaml")
    covered = set()
    for window in shipped.poll.windows:
        covered |= window.days

    assert covered == {0, 1, 2, 3, 4}, "expected Mon-Fri only"


def test_shipped_rules_yaml_is_quiet_at_the_weekend():
    shipped = load_config(Path(__file__).resolve().parents[1] / "rules.yaml")

    assert interval_at(shipped, utc(2026, 8, 1, 16)) is None  # Sat 12:00 EDT
    assert interval_at(shipped, utc(2026, 8, 2, 16)) is None  # Sun 12:00 EDT
    assert interval_at(shipped, utc(2026, 7, 31, 17)) == dt.timedelta(minutes=2)  # Fri 13:00
