"""End-to-end wiring tests, exercised through fixtures with no network.

This is the closest thing to "the bot actually works": seed from the live capture, then
replay the reopened capture and assert exactly one notification for the target slot.
"""

from __future__ import annotations

import datetime as dt

import pytest
import responses

from slotwatch.config import load_config
from slotwatch.fetch import FetchError
from slotwatch.main import build_parser, main, poll_once, run_test_ping
from slotwatch.models import EventType
from slotwatch.state import State, load, save

from .conftest import FIXTURES, SITE, TARGET_GAME_ID

NOW = dt.datetime(2026, 7, 26, 17, 0, tzinfo=dt.UTC)  # 13:00 EDT, inside the window

CONFIG_YAML = """
poll:
  tabs: [primary]
  timezone: America/New_York
  cooldown_hours: 6
  health_cooldown_hours: 12
windows: []
notify:
  webhook_url_env: TEST_WEBHOOK
rules:
  - name: "Sunday Intermediate Court 1, late block"
    gym: "Central Gym"
    level: "Intermediate - Court 1"
    time: "4:00 pm - 7:30 pm"
    date: any
    triggers: [new_slot, reopened]
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    return load_config(path)


SITE_YAML = """
base_url: "https://booking.invalid/"
action: "test_tab_content_action"
tabs:
  primary:
    label: "Sunday sessions"
    buttonid: 5
    filterid: 18
"""


def poll(config, state, **kwargs):
    """poll_once with the throwaway site profile injected."""
    return poll_once(config, state, SITE, **kwargs)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# The headline scenario
# --------------------------------------------------------------------------


def test_cold_start_seeds_silently(config):
    state, sent = poll(
        config, State(), now=NOW, fixture=fixture_text("primary_live.html"),
        dry_run=True,
    )

    assert sent == []
    assert state.seeded is True
    assert len(state.slots) == 24


def test_reopening_after_seeding_notifies_exactly_once(config):
    seeded, _ = poll(
        config, State(), now=NOW, fixture=fixture_text("primary_live.html"),
        dry_run=True,
    )

    _, sent = poll(
        config, seeded, now=NOW, fixture=fixture_text("primary_reopened.html"),
        dry_run=True,
    )

    assert len(sent) == 1
    assert sent[0].type is EventType.REOPENED
    assert sent[0].slot.game_id == TARGET_GAME_ID
    assert sent[0].rule_name == "Sunday Intermediate Court 1, late block"


def test_unchanged_page_is_silent(config):
    live = fixture_text("primary_live.html")
    seeded, _ = poll(config, State(), now=NOW, fixture=live, dry_run=True)

    _, sent = poll(config, seeded, now=NOW, fixture=live, dry_run=True)

    assert sent == []


def test_new_sunday_only_pings_slots_the_rules_want(config):
    """The new date brings 6 rows, but only Intermediate - Court 1 / 4pm is watched."""
    seeded, _ = poll(
        config, State(), now=NOW, fixture=fixture_text("primary_live.html"),
        dry_run=True,
    )

    _, sent = poll(
        config, seeded, now=NOW, fixture=fixture_text("primary_new_date.html"),
        dry_run=True,
    )

    assert len(sent) == 1
    assert sent[0].type is EventType.NEW_SLOT
    assert sent[0].slot.date_raw == "Sun 08/23"
    assert sent[0].slot.level == "Intermediate - Court 1"
    assert sent[0].slot.time_raw == "4:00 pm - 7:30 pm"


def test_empty_state_after_seeding_raises_health(config):
    seeded, _ = poll(
        config, State(), now=NOW, fixture=fixture_text("primary_live.html"),
        dry_run=True,
    )

    _, sent = poll(
        config, seeded, now=NOW, fixture=fixture_text("primary_empty.html"), dry_run=True
    )

    assert [e.type for e in sent] == [EventType.HEALTH]


def test_layout_change_reaches_you_even_though_no_rule_matches(config):
    seeded, _ = poll(
        config, State(), now=NOW, fixture=fixture_text("primary_live.html"),
        dry_run=True,
    )

    _, sent = poll(
        config, seeded, now=NOW, fixture=fixture_text("layout_changed.html"), dry_run=True
    )

    assert any(e.type is EventType.HEALTH for e in sent)


def test_cooldown_prevents_a_repeat_ping_on_the_next_poll(config):
    """A slot sitting at '2 Spaces' must not re-ping every 3 minutes."""
    live = fixture_text("primary_live.html")
    reopened = fixture_text("primary_reopened.html")

    state, _ = poll(config, State(), now=NOW, fixture=live, dry_run=True)
    # dry_run deliberately does not mark notified, so drive the real path instead.
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, "https://discord.com/api/webhooks/x/y", status=204)
        state, first = poll(
            config, state, now=NOW, fixture=reopened,
            webhook_url="https://discord.com/api/webhooks/x/y",
        )
        assert len(first) == 1

    _, second = poll(
        config, state, now=NOW + dt.timedelta(minutes=3), fixture=reopened, dry_run=True
    )

    assert second == []


def test_continuously_open_slot_does_not_re_ping(config):
    """Once announced, a slot that simply stays open is not news again."""
    live = fixture_text("primary_live.html")
    reopened = fixture_text("primary_reopened.html")

    state, _ = poll(config, State(), now=NOW, fixture=live, dry_run=True)
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, "https://discord.com/api/webhooks/x/y", status=204)
        state, _ = poll(
            config, state, now=NOW, fixture=reopened,
            webhook_url="https://discord.com/api/webhooks/x/y",
        )

    _, later = poll(
        config, state, now=NOW + dt.timedelta(hours=7), fixture=reopened, dry_run=True
    )

    assert later == []


def test_flapping_slot_re_pings_only_after_the_cooldown(config):
    """Sold Out -> open -> Sold Out -> open is two genuine transitions, but the
    cooldown decides whether the second one is worth another buzz."""
    live = fixture_text("primary_live.html")
    reopened = fixture_text("primary_reopened.html")
    webhook = "https://discord.com/api/webhooks/x/y"

    state, _ = poll(config, State(), now=NOW, fixture=live, dry_run=True)
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, webhook, status=204)
        state, first = poll(
            config, state, now=NOW, fixture=reopened, webhook_url=webhook
        )
    assert len(first) == 1

    # Sells out again, then frees up an hour later - inside the 6h cooldown.
    state, _ = poll(
        config, state, now=NOW + dt.timedelta(minutes=30), fixture=live, dry_run=True
    )
    state, soon = poll(
        config, state, now=NOW + dt.timedelta(hours=1), fixture=reopened, dry_run=True
    )
    assert soon == []

    # Sells out once more, frees up beyond the cooldown - worth telling you about.
    state, _ = poll(
        config, state, now=NOW + dt.timedelta(hours=6), fixture=live, dry_run=True
    )
    _, later = poll(
        config, state, now=NOW + dt.timedelta(hours=7), fixture=reopened, dry_run=True
    )

    assert len(later) == 1


# --------------------------------------------------------------------------
# Delivery failures must not lose the event
# --------------------------------------------------------------------------


def test_discord_failure_does_not_mark_the_event_as_notified(config):
    live = fixture_text("primary_live.html")
    reopened = fixture_text("primary_reopened.html")
    state, _ = poll(config, State(), now=NOW, fixture=live, dry_run=True)

    with responses.RequestsMock() as mock:
        mock.add(responses.POST, "https://discord.com/api/webhooks/x/y", status=500)
        state, sent = poll(
            config, state, now=NOW, fixture=reopened,
            webhook_url="https://discord.com/api/webhooks/x/y",
        )

    assert sent == []
    assert state.notified == {}

    # The very next poll should try again rather than swallow it.
    _, retry = poll(
        config, state, now=NOW + dt.timedelta(minutes=3), fixture=reopened, dry_run=True
    )
    assert len(retry) == 1


# --------------------------------------------------------------------------
# Fetch failures - the "scraper broken" trigger
# --------------------------------------------------------------------------


def _always_fail(*_args, **_kwargs):
    raise FetchError("primary: giving up after 3 attempts (HTTP 503)")


def test_single_fetch_failure_is_not_worth_waking_you(config, monkeypatch):
    monkeypatch.setattr("slotwatch.main.fetch_tab", _always_fail)

    state, sent = poll(config, State(seeded=True), now=NOW, dry_run=True)

    assert sent == []
    assert state.failure_streak == 1


def test_failure_streak_reaching_the_threshold_raises_health(config, monkeypatch):
    monkeypatch.setattr("slotwatch.main.fetch_tab", _always_fail)
    state = State(seeded=True)

    for _ in range(3):
        state, sent = poll(config, state, now=NOW, dry_run=True)

    assert state.failure_streak == 3
    assert [e.type for e in sent] == [EventType.HEALTH]
    assert "3 consecutive fetch failures" in sent[0].message


def test_a_successful_poll_clears_the_failure_streak(config, monkeypatch):
    monkeypatch.setattr("slotwatch.main.fetch_tab", _always_fail)
    state, _ = poll(config, State(seeded=True), now=NOW, dry_run=True)
    assert state.failure_streak == 1

    state, _ = poll(
        config, state, now=NOW, fixture=fixture_text("primary_live.html"),
        dry_run=True,
    )

    assert state.failure_streak == 0


# --------------------------------------------------------------------------
# Multiple tabs
# --------------------------------------------------------------------------


def test_multiple_tabs_are_aggregated_into_one_observation(tmp_path, monkeypatch):
    """State is keyed by game_id across all tabs, so recording them one at a time
    would erase whichever tab was read first."""
    path = tmp_path / "rules.yaml"
    path.write_text(
        CONFIG_YAML.replace(
            "tabs: [primary]", "tabs: [primary, secondary]"
        ),
        encoding="utf-8",
    )
    cfg = load_config(path)

    served = {
        "primary": fixture_text("primary_live.html"),
        "secondary": fixture_text("primary_empty.html"),
    }
    monkeypatch.setattr(
        "slotwatch.main.fetch_tab", lambda tab, site=None, **kw: served[tab.key]
    )

    state, sent = poll(cfg, State(), now=NOW, dry_run=True)

    assert len(state.slots) == 24  # 24 from Sunday, 0 from an empty Friday
    assert sent == []


def test_anomalies_are_labelled_with_their_tab(tmp_path, monkeypatch):
    path = tmp_path / "rules.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    cfg = load_config(path)
    monkeypatch.setattr(
        "slotwatch.main.fetch_tab",
        lambda tab, site=None, **kw: fixture_text("layout_changed.html"),
    )

    _, sent = poll(cfg, State(seeded=True, slots={"1": "sold_out"}), now=NOW,
                        dry_run=True)

    health = [e for e in sent if e.type is EventType.HEALTH]
    assert health and "[primary]" in health[0].message


# --------------------------------------------------------------------------
# State round-trip
# --------------------------------------------------------------------------


def test_state_survives_a_save_load_cycle(config, tmp_path):
    path = tmp_path / "seen.json"
    state, _ = poll(
        config, State(), now=NOW, fixture=fixture_text("primary_live.html"),
        dry_run=True,
    )
    save(path, state)

    restored = load(path)

    assert restored.seeded is True
    assert restored.slots == state.slots


def test_missing_state_file_loads_cold(tmp_path):
    assert load(tmp_path / "absent.json").is_cold is True


def test_corrupt_state_file_loads_cold_rather_than_crashing(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json", encoding="utf-8")

    assert load(path).is_cold is True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_defaults_to_a_single_poll():
    args = build_parser().parse_args([])

    assert args.once is False and args.loop is False  # main() resolves this to --once


def test_cli_refuses_to_run_without_a_webhook(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.delenv("TEST_WEBHOOK", raising=False)
    monkeypatch.setenv("SITE_PROFILE", SITE_YAML)

    code = main(["--once", "--config", str(cfg), "--state", str(tmp_path / "s.json")])

    assert code == 2


def test_cli_refuses_to_run_without_a_site_profile(tmp_path, monkeypatch):
    """A missing profile must be a clear, distinct exit code - not a confusing crash."""
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.delenv("SITE_PROFILE", raising=False)

    code = main([
        "--once", "--dry-run", "--config", str(cfg),
        "--state", str(tmp_path / "s.json"),
        "--site", str(tmp_path / "absent-site.yaml"),
    ])

    assert code == 3


def test_cli_dry_run_works_without_a_webhook(tmp_path, monkeypatch):
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.delenv("TEST_WEBHOOK", raising=False)
    monkeypatch.setenv("SITE_PROFILE", SITE_YAML)

    code = main([
        "--once", "--dry-run", "--ignore-window",
        "--config", str(cfg),
        "--state", str(tmp_path / "s.json"),
        "--fixture", str(FIXTURES / "primary_live.html"),
    ])

    assert code == 0
    assert load(tmp_path / "s.json").seeded is True


# --------------------------------------------------------------------------
# Deploy check (--test-ping)
# --------------------------------------------------------------------------


def test_test_ping_reports_the_live_picture_without_touching_state(config, tmp_path):
    state_path = tmp_path / "seen.json"
    webhook = "https://discord.com/api/webhooks/x/y"

    with responses.RequestsMock() as mock:
        mock.add(responses.POST, webhook, status=204)
        code = run_test_ping(
            config, SITE, webhook_url=webhook, mention=None, dry_run=False,
            fixture=fixture_text("primary_live.html"),
        )
        body = mock.calls[0].request.body.decode()

    assert code == 0
    assert "24" in body
    assert "Deploy check passed" in body
    # A deploy check must never seed or advance state.
    assert not state_path.exists()


def test_test_ping_counts_slots_its_rules_would_catch(config):
    """All Intermediate slots are full in the live capture, so nothing is watched yet."""
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, "https://discord.com/api/webhooks/x/y", status=204)
        run_test_ping(
            config, SITE, webhook_url="https://discord.com/api/webhooks/x/y",
            mention=None, dry_run=False,
            fixture=fixture_text("primary_live.html"),
        )
        body = mock.calls[0].request.body.decode()

    assert "7 bookable" in body


def test_test_ping_fails_on_drifted_markup(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, "https://discord.com/api/webhooks/x/y", status=204)
        code = run_test_ping(
            config, SITE, webhook_url="https://discord.com/api/webhooks/x/y",
            mention=None, dry_run=False,
            fixture=fixture_text("layout_changed.html"),
        )
        body = mock.calls[0].request.body.decode()

    assert code == 1, "drifted markup must produce a red build"
    assert "FAILED" in body


def test_test_ping_fails_when_the_site_is_unreachable(config, monkeypatch):
    monkeypatch.setattr("slotwatch.main.fetch_tab", _always_fail)

    with responses.RequestsMock() as mock:
        mock.add(responses.POST, "https://discord.com/api/webhooks/x/y", status=204)
        code = run_test_ping(
            config, SITE, webhook_url="https://discord.com/api/webhooks/x/y",
            mention=None, dry_run=False,
        )
        body = mock.calls[0].request.body.decode()

    assert code == 1
    assert "FAILED" in body


def test_test_ping_dry_run_posts_nothing(config):
    code = run_test_ping(
        config, SITE, webhook_url=None, mention=None, dry_run=True,
        fixture=fixture_text("primary_live.html"),
    )

    assert code == 0


def test_test_ping_returns_nonzero_if_discord_rejects_it(config):
    with responses.RequestsMock() as mock:
        mock.add(responses.POST, "https://discord.com/api/webhooks/x/y", status=500)
        code = run_test_ping(
            config, SITE, webhook_url="https://discord.com/api/webhooks/x/y",
            mention=None, dry_run=False,
            fixture=fixture_text("primary_live.html"),
        )

    assert code == 1


def test_cli_exposes_test_ping(tmp_path, monkeypatch):
    cfg = tmp_path / "rules.yaml"
    cfg.write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("SITE_PROFILE", SITE_YAML)

    code = main([
        "--test-ping", "--dry-run",
        "--config", str(cfg),
        "--state", str(tmp_path / "s.json"),
        "--fixture", str(FIXTURES / "primary_live.html"),
    ])

    assert code == 0
    assert not (tmp_path / "s.json").exists(), "deploy check must not write state"
