"""Match slots against watch rules, and filter events down to what you asked for.

Rules are plain data so that Phase 2 (slash commands) can rewrite them via the GitHub
API without the poller changing at all.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Event, EventType, Slot, WatchRule

WILDCARDS = {None, "", "any", "*"}

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

DEFAULT_TRIGGERS = frozenset({EventType.NEW_SLOT, EventType.REOPENED})

# HEALTH is deliberately not rule-filterable: a broken scraper has to reach you even
# when nothing you watch is on the page.
_ALWAYS_DELIVER = {EventType.HEALTH}


def _is_wildcard(value: str | None) -> bool:
    return value is None or value.strip().casefold() in {"", "any", "*"}


def _text_matches(pattern: str, value: str, *, regex: bool) -> bool:
    if regex:
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            # A malformed pattern must fail closed, not crash the poll.
            return False
    return pattern.strip().casefold() == value.strip().casefold()


def matches(rule: WatchRule, slot: Slot) -> bool:
    if not rule.enabled:
        return False

    # Tab is an exact key, never a pattern - it is our own identifier, not site text.
    if not _is_wildcard(rule.tab) and rule.tab.strip() != slot.tab:
        return False

    regex = rule.match == "regex"
    for pattern, value in (
        (rule.gym, slot.gym),
        (rule.level, slot.level),
        (rule.time, slot.time_raw),
    ):
        if _is_wildcard(pattern):
            continue
        if not _text_matches(pattern, value, regex=regex):
            return False

    if not _is_wildcard(rule.date):
        # Fail closed: a slot whose date we couldn't parse can't satisfy a date rule.
        if slot.date is None or slot.date.isoformat() != rule.date.strip():
            return False

    if not _is_wildcard(rule.weekday):
        wanted = _WEEKDAYS.get(rule.weekday.strip().casefold())
        if wanted is None or slot.date is None or slot.date.weekday() != wanted:
            return False

    return True


def filter_events(events: list[Event], rules: list[WatchRule]) -> list[Event]:
    """Keep events some rule asked for, tagged with the rule that claimed them."""
    kept: list[Event] = []

    for event in events:
        if event.type in _ALWAYS_DELIVER:
            kept.append(event)
            continue
        if event.slot is None:
            continue

        for rule in rules:
            if event.type not in rule.triggers:
                continue
            if matches(rule, event.slot):
                # First match wins, so one event never pings twice.
                kept.append(
                    Event(
                        type=event.type,
                        slot=event.slot,
                        previous=event.previous,
                        message=event.message,
                        rule_name=rule.name,
                    )
                )
                break

    return kept


def rules_from_config(entries: list[dict[str, Any]] | None) -> list[WatchRule]:
    rules: list[WatchRule] = []

    for index, entry in enumerate(entries or []):
        name = str(entry.get("name") or "").strip()
        if not name:
            raise ValueError(f"watch rule #{index + 1} is missing a name")

        raw_triggers = entry.get("triggers")
        if raw_triggers is None:
            triggers = DEFAULT_TRIGGERS
        else:
            parsed = set()
            for trigger in raw_triggers:
                try:
                    event_type = EventType(str(trigger).strip().casefold())
                except ValueError as exc:
                    raise ValueError(
                        f"unknown trigger {trigger!r} in rule {name!r}; "
                        f"expected one of {[t.value for t in EventType]}"
                    ) from exc
                parsed.add(event_type)
            triggers = frozenset(parsed)

        match = str(entry.get("match", "exact")).strip().casefold()
        if match not in {"exact", "regex"}:
            raise ValueError(f"rule {name!r} has unknown match mode {match!r}")

        rules.append(
            WatchRule(
                name=name,
                enabled=bool(entry.get("enabled", True)),
                tab=_opt(entry.get("tab")),
                gym=_opt(entry.get("gym")),
                level=_opt(entry.get("level")),
                time=_opt(entry.get("time")),
                date=_opt(entry.get("date")),
                weekday=_opt(entry.get("weekday")),
                match=match,
                triggers=triggers,
            )
        )

    return rules


def _opt(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
