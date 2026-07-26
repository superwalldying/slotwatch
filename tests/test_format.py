"""Formatting tests. The load-bearing requirement is batching: a fresh Sunday drops
~6 rows at once and that must be one message, not six phone buzzes."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from slotwatch.format import (
    COLOR_TEST,
    COLOR_TEST_FAIL,
    CONTENT_LIMIT,
    DESCRIPTION_LIMIT,
    MAX_EMBEDS,
    TOTAL_LIMIT,
    USERNAME_LIMIT,
    build_payload,
    build_test_ping,
    classify_mention,
    normalize_mention,
    payload_size,
)
from slotwatch.models import Availability, Event, EventType, Slot


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
    )
    return Slot(**{**base, **overrides})


REOPENED = Event(
    type=EventType.REOPENED,
    slot=slot(),
    previous=Availability.SOLD_OUT,
    rule_name="Sunday Intermediate Court 1 late block",
)


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------


def test_six_events_produce_a_single_message():
    events = [
        Event(type=EventType.NEW_SLOT, slot=slot(game_id=str(16270 + i)), rule_name="r")
        for i in range(6)
    ]

    payload = build_payload(events)

    assert len(payload["embeds"]) == 1
    # One entry marker per event, all inside the single embed.
    assert payload["embeds"][0]["description"].count("↳") == 6


def test_no_events_produces_nothing_to_send():
    assert build_payload([]) is None


# --------------------------------------------------------------------------
# Content of a reopened alert
# --------------------------------------------------------------------------


def test_reopened_alert_shows_the_transition():
    description = build_payload([REOPENED])["embeds"][0]["description"]

    assert "Sun 08/02" in description
    assert "Intermediate - Court 1" in description
    assert "4:00 pm - 7:30 pm" in description
    assert "16.00" in description
    assert "Sold Out" in description
    assert "2 Spaces" in description


def test_new_slot_alert_does_not_claim_a_previous_state():
    event = Event(type=EventType.NEW_SLOT, slot=slot(), rule_name="r")

    description = build_payload([event])["embeds"][0]["description"]

    assert "Sold Out" not in description
    assert "2 Spaces" in description


def test_open_availability_reads_naturally():
    event = Event(
        type=EventType.NEW_SLOT,
        slot=slot(availability=Availability.OPEN, spaces_left=None),
        rule_name="r",
    )

    assert "open" in build_payload([event])["embeds"][0]["description"].casefold()


def test_booking_link_and_tab_hint_are_present():
    """The tab is JS-driven, so a deep link can't preselect it - say which to click."""
    payload = build_payload(
        [REOPENED], tab_label="Sunday sessions", book_url="https://booking.invalid/x"
    )
    blob = str(payload)

    assert "https://booking.invalid/x" in blob
    assert "Sunday sessions" in blob


def test_without_a_book_url_the_message_still_names_the_tab():
    """No profile URL configured must not produce a broken empty markdown link."""
    description = build_payload([REOPENED], tab_label="Sunday sessions")["embeds"][0][
        "description"
    ]

    assert "Sunday sessions" in description
    assert "[Book now]()" not in description


def test_matched_rule_is_recorded_for_debuggability():
    payload = build_payload([REOPENED])

    assert "Sunday Intermediate Court 1 late block" in str(payload["embeds"][0]["footer"])


# --------------------------------------------------------------------------
# Mentions
# --------------------------------------------------------------------------


def test_mention_is_included_so_the_alert_actually_buzzes():
    payload = build_payload([REOPENED], mention="<@12345>")

    assert payload["content"].startswith("<@12345>")
    assert payload["allowed_mentions"] == {"parse": ["users"]}


def test_no_mention_configured_means_no_ping_prefix():
    payload = build_payload([REOPENED])

    assert "<@" not in payload.get("content", "")


# --------------------------------------------------------------------------
# Health alerts
# --------------------------------------------------------------------------


def test_health_event_gets_its_own_embed_and_colour():
    health = Event(type=EventType.HEALTH, message="unrecognised column header 'Skill'")

    payload = build_payload([health])
    embed = payload["embeds"][0]

    assert "Skill" in embed["description"]
    assert embed["color"] != build_payload([REOPENED])["embeds"][0]["color"]


def test_slot_and_health_events_are_separate_embeds():
    health = Event(type=EventType.HEALTH, message="layout changed")

    payload = build_payload([REOPENED, health])

    assert len(payload["embeds"]) == 2


# --------------------------------------------------------------------------
# Discord's hard limits
# --------------------------------------------------------------------------


def test_large_batch_stays_within_discord_limits():
    events = [
        Event(
            type=EventType.NEW_SLOT,
            slot=slot(game_id=str(20000 + i), level=f"Intermediate - Court {i}"),
            rule_name="a rather long watch rule name for padding purposes",
        )
        for i in range(110)
    ]

    payload = build_payload(events)

    assert len(payload["embeds"]) <= MAX_EMBEDS
    assert len(payload.get("content", "")) <= CONTENT_LIMIT
    for embed in payload["embeds"]:
        assert len(embed["description"]) <= DESCRIPTION_LIMIT
    assert payload_size(payload) <= TOTAL_LIMIT


def test_truncation_says_how_many_were_omitted():
    events = [
        Event(type=EventType.NEW_SLOT, slot=slot(game_id=str(20000 + i)), rule_name="r")
        for i in range(110)
    ]

    blob = str(build_payload(events))

    assert "more" in blob.casefold()


# --------------------------------------------------------------------------
# Deploy check ping
# --------------------------------------------------------------------------


def _ping(**overrides):
    base = dict(
        tab_label="Sunday sessions",
        slots=24,
        bookable=7,
        watched=0,
        anomalies=(),
        rules=["Court 1, late block", "Any Intermediate opening"],
        schedule="Mon-Thu 09:00-17:00 every 3 min — America/New_York",
    )
    return build_test_ping(**{**base, **overrides})


def test_test_ping_reports_what_the_bot_can_see():
    description = _ping()["embeds"][0]["description"]

    assert "24" in description
    assert "7 bookable" in description
    assert "17 full" in description
    assert "Sunday sessions" in description


def test_test_ping_lists_active_rules_and_schedule():
    description = _ping()["embeds"][0]["description"]

    assert "Court 1, late block" in description
    assert "Any Intermediate opening" in description
    assert "Mon-Thu" in description


def test_healthy_ping_says_passed():
    payload = _ping()

    assert "passed" in payload["embeds"][0]["title"]
    assert payload["embeds"][0]["color"] == COLOR_TEST
    assert "passed" in payload["content"]


def test_anomalies_make_the_ping_fail_loudly():
    payload = _ping(anomalies=("unrecognised column header 'Skill'",))
    embed = payload["embeds"][0]

    assert "FAILED" in embed["title"]
    assert embed["color"] == COLOR_TEST_FAIL
    assert "Skill" in embed["description"]


def test_fetch_error_makes_the_ping_fail_loudly():
    payload = _ping(error="HTTP 503", slots=0, bookable=0)
    embed = payload["embeds"][0]

    assert "FAILED" in embed["title"]
    assert "503" in embed["description"]


def test_ping_warns_when_no_rules_are_active():
    description = _ping(rules=[])["embeds"][0]["description"]

    assert "No active rules" in description


def test_zero_watched_slots_is_explained_rather_than_alarming():
    """All-full is the normal state here; the ping shouldn't read like a failure."""
    description = _ping(watched=0)["embeds"][0]["description"]

    assert "expected" in description.casefold()


def test_ping_is_visually_distinct_from_a_real_alert():
    """A deploy check mistaken for an opening would be worse than no check."""
    ping = _ping()["embeds"][0]
    alert = build_payload([REOPENED])["embeds"][0]

    assert ping["color"] != alert["color"]
    assert ping["title"] != alert["title"]
    assert "Deploy check" in ping["title"]


def test_ping_includes_the_revision_when_known():
    payload = _ping(revision="abc123def456789")

    assert "abc123def456" in payload["embeds"][0]["footer"]["text"]


def test_ping_mention_pings_you():
    payload = _ping(mention="<@42>")

    assert payload["content"].startswith("<@42>")
    assert payload["allowed_mentions"] == {"parse": ["users"]}


# --------------------------------------------------------------------------
# Webhook display identity
# --------------------------------------------------------------------------


def test_username_override_is_sent_so_messages_self_identify():
    """Otherwise Discord shows whatever name the webhook was created with."""
    payload = build_payload([REOPENED], username="slotwatch")

    assert payload["username"] == "slotwatch"


def test_username_override_applies_to_the_deploy_check_too():
    assert _ping(username="slotwatch")["username"] == "slotwatch"


def test_no_username_configured_leaves_the_field_out():
    """Absent, not empty - an empty username would be rejected by Discord."""
    assert "username" not in build_payload([REOPENED])
    assert "username" not in _ping()


def test_avatar_url_is_optional_and_independent():
    payload = build_payload([REOPENED], avatar_url="https://example.invalid/i.png")

    assert payload["avatar_url"] == "https://example.invalid/i.png"
    assert "username" not in payload


def test_overlong_username_is_truncated_to_discords_limit():
    payload = build_payload([REOPENED], username="x" * 200)

    assert len(payload["username"]) == USERNAME_LIMIT


# --------------------------------------------------------------------------
# Multi-tab grouping
# --------------------------------------------------------------------------

TAB_DISPLAY = {
    "primary": ("Sunday sessions", "https://booking.invalid/sun"),
    "beacon": ("Friday sessions", "https://booking.invalid/fri"),
}


def test_two_tabs_produce_one_embed_each_with_the_right_venue_and_link():
    events = [
        Event(type=EventType.REOPENED, slot=slot(tab="primary"),
              previous=Availability.SOLD_OUT, rule_name="a"),
        Event(type=EventType.NEW_SLOT, slot=slot(game_id="99", tab="beacon"), rule_name="b"),
    ]

    payload = build_payload(events, tab_display=TAB_DISPLAY)

    assert len(payload["embeds"]) == 2
    titles = [e["title"] for e in payload["embeds"]]
    assert any("Sunday sessions" in t for t in titles)
    assert any("Friday sessions" in t for t in titles)
    urls = {e["url"] for e in payload["embeds"]}
    assert urls == {"https://booking.invalid/sun", "https://booking.invalid/fri"}


def test_single_tab_still_produces_one_embed():
    events = [Event(type=EventType.NEW_SLOT, slot=slot(tab="beacon"), rule_name="b")]

    payload = build_payload(events, tab_display=TAB_DISPLAY)

    assert len(payload["embeds"]) == 1
    assert "Friday sessions" in payload["embeds"][0]["title"]


def test_multi_tab_summary_names_the_venues():
    """Otherwise the notification preview can't tell you which venue opened up."""
    events = [
        Event(type=EventType.NEW_SLOT, slot=slot(tab="primary"), rule_name="a"),
        Event(type=EventType.NEW_SLOT, slot=slot(game_id="99", tab="beacon"), rule_name="b"),
    ]

    content = build_payload(events, tab_display=TAB_DISPLAY)["content"]

    assert "Sunday sessions" in content and "Friday sessions" in content


def test_unknown_tab_falls_back_to_the_generic_label():
    events = [Event(type=EventType.NEW_SLOT, slot=slot(tab="mystery"), rule_name="a")]

    payload = build_payload(events, tab_label="sessions", tab_display=TAB_DISPLAY)

    assert "sessions" in payload["embeds"][0]["title"]


def test_health_embed_is_still_appended_alongside_tab_groups():
    events = [
        Event(type=EventType.NEW_SLOT, slot=slot(tab="primary"), rule_name="a"),
        Event(type=EventType.NEW_SLOT, slot=slot(game_id="99", tab="beacon"), rule_name="b"),
        Event(type=EventType.HEALTH, message="drifted"),
    ]

    assert len(build_payload(events, tab_display=TAB_DISPLAY)["embeds"]) == 3


# --------------------------------------------------------------------------
# Mention normalisation
# --------------------------------------------------------------------------


def test_bare_user_id_is_wrapped_so_it_actually_pings():
    """A bare ID renders as literal text - the alert looks fine but never buzzes."""
    payload = build_payload([REOPENED], mention="119290074629275652")

    assert payload["content"].startswith("<@119290074629275652>")


def test_already_formatted_mention_is_left_alone():
    payload = build_payload([REOPENED], mention="<@42>")

    assert payload["content"].startswith("<@42>")
    assert "<@<@" not in payload["content"]


def test_role_mention_is_preserved():
    payload = build_payload([REOPENED], mention="<@&999>")

    assert payload["content"].startswith("<@&999>")


def test_blank_mention_is_treated_as_absent():
    assert "<@" not in build_payload([REOPENED], mention="   ")["content"]
    assert "allowed_mentions" not in build_payload([REOPENED], mention="   ")


def test_deploy_check_normalises_the_mention_too():
    assert _ping(mention="119290074629275652")["content"].startswith("<@1192900746292756")


@pytest.mark.parametrize("raw,expected", [
    ("424242424242", "<@424242424242>"),
    ("<@424242424242>", "<@424242424242>"),
    ("<@!424242424242>", "<@!424242424242>"),
    ("<@&424242424242>", "<@&424242424242>"),
    ("  424242424242  ", "<@424242424242>"),
    ("", None), (None, None),
])
def test_normalize_mention_cases(raw, expected):
    assert normalize_mention(raw) == expected


def test_short_numbers_are_not_mistaken_for_user_ids():
    """Discord snowflakes are ~18 digits; treating '42' as one would be a false positive."""
    assert normalize_mention("42") == "42"


@pytest.mark.parametrize("raw,expected", [
    ("119290074629275652", "<@119290074629275652>"),
    ("@119290074629275652", "<@119290074629275652>"),   # copied from the Discord UI
    ("<@119290074629275652>", "<@119290074629275652>"),
    ("<119290074629275652>", "<@119290074629275652>"),
    ("<@119290074629275652", "<@119290074629275652>"),  # truncated paste
    ("  @119290074629275652  ", "<@119290074629275652>"),
    ("<@!42424242424>", "<@!42424242424>"),
    ("<@&42424242424>", "<@&42424242424>"),
])
def test_every_plausible_mention_form_becomes_a_real_ping(raw, expected):
    assert normalize_mention(raw) == expected


def test_unmentionable_value_is_passed_through_not_silently_dropped():
    """A username#discriminator cannot be pinged; keep it visible so the cause is obvious."""
    assert normalize_mention("someone#1234") == "someone#1234"


@pytest.mark.parametrize("raw,shape", [
    (None, "none"),
    ("", "none"),
    ("<@119290074629275652>", "already-formatted"),
    ("119290074629275652", "bare-id (wrapped)"),
    ("@119290074629275652", "bare-id (wrapped)"),
    ("someone#1234", "unrecognised (sent as-is; will NOT ping)"),
])
def test_classify_mention_describes_the_shape_only(raw, shape):
    assert classify_mention(raw) == shape


def test_classify_never_leaks_the_id():
    """Actions logs are public on a public repo."""
    assert "119290074629275652" not in classify_mention("119290074629275652")
