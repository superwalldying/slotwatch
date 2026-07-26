"""Watch-rule tests. Rules are data, which is what lets Phase 2 add slash commands
without touching the poller."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from slotwatch.models import Availability, Event, EventType, Slot, WatchRule
from slotwatch.rules import filter_events, matches, rules_from_config


def slot(**overrides) -> Slot:
    base = dict(
        game_id="16212",
        date_raw="Sun 08/02",
        date=dt.date(2026, 8, 2),
        gym="Central Gym",
        level="Intermediate - Court 1",
        time_raw="4:00 pm - 7:30 pm",
        fee=Decimal("16.00"),
        availability=Availability.LIMITED,
        spaces_left=2,
        radio_disabled=False,
        tab="primary",
    )
    return Slot(**{**base, **overrides})


TARGET_RULE = WatchRule(
    name="Sunday Intermediate Court 1 late block",
    gym="Central Gym",
    level="Intermediate - Court 1",
    time="4:00 pm - 7:30 pm",
)


# --------------------------------------------------------------------------
# Field matching
# --------------------------------------------------------------------------


def test_exact_rule_matches_the_target_slot():
    assert matches(TARGET_RULE, slot()) is True


@pytest.mark.parametrize(
    "slot_field,value",
    [
        ("level", "Intermediate - Court 2"),
        ("time_raw", "12:00 pm - 3:30 pm"),
        ("gym", "East Gym"),
    ],
)
def test_exact_rule_rejects_a_differing_field(slot_field, value):
    assert matches(TARGET_RULE, slot(**{slot_field: value})) is False


def test_matching_is_case_and_whitespace_insensitive():
    rule = WatchRule(name="r", level="  intermediate - COURT 1 ")

    assert matches(rule, slot()) is True


def test_unset_fields_are_wildcards():
    assert matches(WatchRule(name="everything"), slot()) is True
    assert matches(WatchRule(name="everything"), slot(level="Beginner - Court 2")) is True


def test_any_is_an_explicit_wildcard():
    assert matches(WatchRule(name="r", date="any", level="any"), slot()) is True


def test_disabled_rule_never_matches():
    assert matches(WatchRule(name="off", enabled=False), slot()) is False


# --------------------------------------------------------------------------
# Regex matching - the broader companion rule
# --------------------------------------------------------------------------


def test_regex_rule_spans_both_courts():
    rule = WatchRule(name="either court", level=r"Intermediate - Court [12]", match="regex")

    assert matches(rule, slot(level="Intermediate - Court 1")) is True
    assert matches(rule, slot(level="Intermediate - Court 2")) is True
    assert matches(rule, slot(level="Beginner - Court 1")) is False


def test_regex_is_a_search_not_a_full_match():
    rule = WatchRule(name="any intermediate", level="Intermediate", match="regex")

    assert matches(rule, slot()) is True


def test_invalid_regex_does_not_explode():
    rule = WatchRule(name="bad", level="Court [", match="regex")

    assert matches(rule, slot()) is False


# --------------------------------------------------------------------------
# Date and weekday
# --------------------------------------------------------------------------


def test_iso_date_matches_exactly():
    assert matches(WatchRule(name="r", date="2026-08-02"), slot()) is True
    assert matches(WatchRule(name="r", date="2026-08-09"), slot()) is False


def test_weekday_rule():
    assert matches(WatchRule(name="r", weekday="sunday"), slot()) is True
    assert matches(WatchRule(name="r", weekday="friday"), slot()) is False


def test_date_rule_against_unparseable_slot_date_fails_closed():
    """A slot whose date we couldn't read must not satisfy a date-specific rule."""
    assert matches(WatchRule(name="r", date="2026-08-02"), slot(date=None)) is False
    assert matches(WatchRule(name="r", weekday="sunday"), slot(date=None)) is False


# --------------------------------------------------------------------------
# Event filtering
# --------------------------------------------------------------------------


def test_matching_event_is_kept_and_tagged_with_the_rule_name():
    event = Event(type=EventType.REOPENED, slot=slot())

    kept = filter_events([event], [TARGET_RULE])

    assert len(kept) == 1
    assert kept[0].rule_name == TARGET_RULE.name


def test_non_matching_event_is_dropped():
    event = Event(type=EventType.NEW_SLOT, slot=slot(level="Beginner - Court 1"))

    assert filter_events([event], [TARGET_RULE]) == []


def test_rule_can_opt_out_of_a_trigger():
    reopen_only = WatchRule(
        name="reopen only",
        level="Intermediate - Court 1",
        triggers=frozenset({EventType.REOPENED}),
    )
    new_slot = Event(type=EventType.NEW_SLOT, slot=slot())
    reopened = Event(type=EventType.REOPENED, slot=slot())

    kept = filter_events([new_slot, reopened], [reopen_only])

    assert [e.type for e in kept] == [EventType.REOPENED]


def test_health_events_bypass_rules_entirely():
    """A broken scraper must reach you even if no watch rule matches anything."""
    health = Event(type=EventType.HEALTH, message="layout changed")

    assert filter_events([health], []) == [health]
    assert filter_events([health], [TARGET_RULE]) == [health]


def test_event_matched_by_two_rules_is_emitted_once():
    broad = WatchRule(name="broad", level="Intermediate", match="regex")
    event = Event(type=EventType.REOPENED, slot=slot())

    kept = filter_events([event], [TARGET_RULE, broad])

    assert len(kept) == 1
    assert kept[0].rule_name == TARGET_RULE.name  # first match wins


def test_no_rules_means_no_slot_pings():
    event = Event(type=EventType.REOPENED, slot=slot())

    assert filter_events([event], []) == []


# --------------------------------------------------------------------------
# Building rules from config
# --------------------------------------------------------------------------


def test_rules_from_config_reads_the_seeded_rule():
    rules = rules_from_config(
        [
            {
                "name": "Sunday Intermediate Court 1 late block",
                "gym": "Central Gym",
                "level": "Intermediate - Court 1",
                "time": "4:00 pm - 7:30 pm",
                "date": "any",
                "triggers": ["new_slot", "reopened"],
            }
        ]
    )

    assert len(rules) == 1
    rule = rules[0]
    assert rule.name == "Sunday Intermediate Court 1 late block"
    assert rule.level == "Intermediate - Court 1"
    assert rule.triggers == frozenset({EventType.NEW_SLOT, EventType.REOPENED})
    assert rule.enabled is True


def test_rules_from_config_defaults_triggers():
    rules = rules_from_config([{"name": "bare"}])

    assert rules[0].triggers == frozenset({EventType.NEW_SLOT, EventType.REOPENED})


def test_rules_from_config_rejects_unknown_trigger():
    with pytest.raises(ValueError, match="unknown trigger"):
        rules_from_config([{"name": "bad", "triggers": ["sold_out"]}])


def test_rules_from_config_requires_a_name():
    with pytest.raises(ValueError, match="name"):
        rules_from_config([{"level": "Beginner"}])


# --------------------------------------------------------------------------
# Tab scoping - more robust than matching venue text
# --------------------------------------------------------------------------


def test_rule_restricted_to_a_tab_ignores_other_tabs():
    rule = WatchRule(name="beacon only", tab="beacon")

    assert matches(rule, slot(tab="beacon")) is True
    assert matches(rule, slot(tab="primary")) is False


def test_rule_without_a_tab_matches_any_tab():
    rule = WatchRule(name="anywhere")

    assert matches(rule, slot(tab="beacon")) is True
    assert matches(rule, slot(tab="primary")) is True


def test_tab_is_matched_exactly_even_in_regex_mode():
    """tab is our own key, not site text, so it must never be treated as a pattern."""
    rule = WatchRule(name="r", tab="prim", level="Intermediate", match="regex")

    assert matches(rule, slot(tab="primary")) is False


def test_tab_scoping_survives_inconsistent_venue_formatting():
    """The real venues are 'Brandeis H.S.' and 'Beacon HS' - matching those exactly is a
    trap, which is why rules target the tab instead."""
    rule = WatchRule(name="beacon", tab="beacon", triggers=frozenset({EventType.NEW_SLOT}))
    event = Event(type=EventType.NEW_SLOT, slot=slot(tab="beacon", gym="Beacon HS"))

    assert filter_events([event], [rule])[0].rule_name == "beacon"


def test_rules_from_config_reads_the_tab_key():
    rules = rules_from_config([{"name": "b", "tab": "beacon", "triggers": ["new_slot"]}])

    assert rules[0].tab == "beacon"
    assert rules[0].triggers == frozenset({EventType.NEW_SLOT})
