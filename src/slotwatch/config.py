"""Configuration and polling-window logic.

The window gate lives here, in pure Python with a real timezone, rather than in the
cron expression. GitHub Actions cron is UTC-only, so encoding "9am-5pm Eastern" as a
UTC schedule would quietly drift by an hour every time DST flips.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .models import WatchRule
from .rules import rules_from_config

DEFAULT_INTERVAL = dt.timedelta(minutes=10)
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_COOLDOWN = dt.timedelta(hours=6)
DEFAULT_HEALTH_COOLDOWN = dt.timedelta(hours=12)

_DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


@dataclass(frozen=True)
class Window:
    days: frozenset[int]
    start: dt.time
    end: dt.time
    interval: dt.timedelta
    dense_start: dt.time | None = None
    dense_end: dt.time | None = None
    dense_interval: dt.timedelta | None = None

    def contains(self, moment: dt.datetime) -> bool:
        return moment.weekday() in self.days and self.start <= moment.time() < self.end

    def interval_for(self, moment: dt.datetime) -> dt.timedelta:
        if (
            self.dense_interval is not None
            and self.dense_start is not None
            and self.dense_end is not None
            and self.dense_start <= moment.time() < self.dense_end
        ):
            return self.dense_interval
        return self.interval


@dataclass(frozen=True)
class PollConfig:
    tabs: tuple[str, ...] = ("primary",)
    timezone: str = DEFAULT_TIMEZONE
    cooldown: dt.timedelta = DEFAULT_COOLDOWN
    health_cooldown: dt.timedelta = DEFAULT_HEALTH_COOLDOWN
    windows: tuple[Window, ...] = ()
    jitter_seconds: int = 20
    failure_threshold: int = 3


@dataclass(frozen=True)
class NotifyConfig:
    webhook_url_env: str = "DISCORD_WEBHOOK_URL"
    mention: str | None = None
    mention_env: str | None = None


@dataclass(frozen=True)
class Config:
    poll: PollConfig = field(default_factory=PollConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    rules: list[WatchRule] = field(default_factory=list)


def _time(value: Any, label: str) -> dt.time:
    text = str(value).strip()
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        return dt.time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must look like HH:MM, got {value!r}") from exc


def _window(entry: dict[str, Any]) -> Window:
    days = set()
    for name in entry.get("days") or list(_DAY_NAMES):
        index = _DAY_NAMES.get(str(name).strip().casefold())
        if index is None:
            raise ValueError(f"unknown day name {name!r} in polling window")
        days.add(index)

    dense = entry.get("dense") or {}
    return Window(
        days=frozenset(days),
        start=_time(entry.get("start", "00:00"), "window start"),
        end=_time(entry.get("end", "23:59"), "window end"),
        interval=dt.timedelta(minutes=float(entry.get("interval_minutes", 10))),
        dense_start=_time(dense["start"], "dense start") if dense.get("start") else None,
        dense_end=_time(dense["end"], "dense end") if dense.get("end") else None,
        dense_interval=(
            dt.timedelta(minutes=float(dense["interval_minutes"]))
            if dense.get("interval_minutes")
            else None
        ),
    )


def load_config(path: str | Path, *, known_tabs: set[str] | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    poll_raw = raw.get("poll") or {}
    notify_raw = raw.get("notify") or {}

    tabs = tuple(str(t).strip() for t in (poll_raw.get("tabs") or ["primary"]))
    if known_tabs is not None:
        for tab in tabs:
            if tab not in known_tabs:
                raise ValueError(
                    f"unknown tab {tab!r}; site profile defines {sorted(known_tabs)}"
                )

    poll = PollConfig(
        tabs=tabs,
        timezone=str(poll_raw.get("timezone", DEFAULT_TIMEZONE)),
        cooldown=dt.timedelta(hours=float(poll_raw.get("cooldown_hours", 6))),
        health_cooldown=dt.timedelta(hours=float(poll_raw.get("health_cooldown_hours", 12))),
        windows=tuple(_window(w) for w in (poll_raw.get("windows") or [])),
        jitter_seconds=int(poll_raw.get("jitter_seconds", 20)),
        failure_threshold=int(poll_raw.get("failure_threshold", 3)),
    )

    notify = NotifyConfig(
        webhook_url_env=str(notify_raw.get("webhook_url_env", "DISCORD_WEBHOOK_URL")),
        mention=(str(notify_raw["mention"]) if notify_raw.get("mention") else None),
        mention_env=(str(notify_raw["mention_env"]) if notify_raw.get("mention_env") else None),
    )

    return Config(poll=poll, notify=notify, rules=rules_from_config(raw.get("rules")))


def local_now(config: Config, now: dt.datetime) -> dt.datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    return now.astimezone(ZoneInfo(config.poll.timezone))


def interval_at(config: Config, now: dt.datetime) -> dt.timedelta | None:
    """Polling interval for this instant, or None when outside every window."""
    if not config.poll.windows:
        return DEFAULT_INTERVAL

    moment = local_now(config, now)
    for window in config.poll.windows:
        if window.contains(moment):
            return window.interval_for(moment)
    return None


def next_open(
    config: Config, now: dt.datetime, *, horizon: dt.timedelta = dt.timedelta(hours=24)
) -> dt.datetime | None:
    """When the next polling window opens, or None if none does within `horizon`.

    Lets a looping job sleep exactly until work resumes - or exit immediately when the
    answer is "not today" - instead of waking every minute to re-check. Scanning in
    5-minute steps keeps it trivial and correct across DST boundaries, since each probe
    is converted through the real timezone.
    """
    if not config.poll.windows:
        return now

    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)

    step = dt.timedelta(minutes=5)
    probe = now
    limit = now + horizon
    while probe <= limit:
        if interval_at(config, probe) is not None:
            return probe
        probe += step
    return None


_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _format_days(days: frozenset[int]) -> str:
    ordered = sorted(days)
    if not ordered:
        return "never"
    # Collapse a contiguous run into a range: {0,1,2,3} -> "Mon-Thu".
    if len(ordered) > 2 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{_DAY_ABBR[ordered[0]]}-{_DAY_ABBR[ordered[-1]]}"
    return ", ".join(_DAY_ABBR[d] for d in ordered)


def describe_schedule(config: Config) -> str:
    """Human-readable schedule, for the deploy-check message."""
    if not config.poll.windows:
        mins = int(DEFAULT_INTERVAL.total_seconds() // 60)
        return f"always, every {mins} min"

    parts = []
    for window in config.poll.windows:
        mins = int(window.interval.total_seconds() // 60)
        text = (
            f"{_format_days(window.days)} "
            f"{window.start.strftime('%H:%M')}-{window.end.strftime('%H:%M')} "
            f"every {mins} min"
        )
        if window.dense_interval and window.dense_start and window.dense_end:
            dense_mins = int(window.dense_interval.total_seconds() // 60)
            text += (
                f" ({dense_mins} min {window.dense_start.strftime('%H:%M')}"
                f"-{window.dense_end.strftime('%H:%M')})"
            )
        parts.append(text)
    return " · ".join(parts) + f" — {config.poll.timezone}"
