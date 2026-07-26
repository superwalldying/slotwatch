"""Network-using canary. Excluded from default runs; opt in with `pytest -m live`.

Fixtures freeze the markup as captured, so a fully green suite can never prove the
selectors still match *tomorrow's* page. This is the counterpart to that gap - a cheap,
explicit check that the real page still parses. The HEALTH trigger covers the same risk
at runtime; this just lets you check on demand.

Needs a real site profile ($SITE_PROFILE or ./site.yaml) and skips cleanly without one,
so a fresh clone can still run `pytest -m live` without a confusing failure.
"""

from __future__ import annotations

import datetime as dt

import pytest

from slotwatch.fetch import fetch_tab
from slotwatch.parse import parse_table
from slotwatch.site import SiteConfigError, load_site

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def site():
    try:
        return load_site()
    except SiteConfigError as exc:
        pytest.skip(f"no site profile configured: {exc}")


@pytest.fixture(scope="module")
def live_html(site):
    tab_key = "primary" if "primary" in site.tabs else sorted(site.tabs)[0]
    return fetch_tab(site.tab(tab_key), site)


def test_live_response_parses_without_anomalies(live_html, site):
    result = parse_table(
        live_html,
        today=dt.date.today(),
        field_name=site.field_name,
        empty_marker=site.empty_marker,
    )

    assert result.anomalies == (), f"page markup may have drifted: {result.anomalies}"
    assert result.slots or result.is_empty_state


def test_live_response_has_the_expected_columns(live_html):
    for header in ("Select", "Date", "Gym", "Level", "Time", "Fee ($)", "Available"):
        assert header in live_html, f"missing column header: {header}"


def test_live_slots_are_all_one_weekday_at_one_venue(live_html, site):
    result = parse_table(
        live_html,
        today=dt.date.today(),
        field_name=site.field_name,
        empty_marker=site.empty_marker,
    )
    if not result.slots:
        pytest.skip("no sessions currently listed")

    assert len({s.gym for s in result.slots}) == 1
    assert len({s.date.weekday() for s in result.slots if s.date}) == 1


def test_live_availability_vocabulary_is_still_closed(live_html, site):
    """If the site introduces a new Available string, learn it here rather than from a
    missed alert."""
    result = parse_table(
        live_html,
        today=dt.date.today(),
        field_name=site.field_name,
        empty_marker=site.empty_marker,
    )
    if not result.slots:
        pytest.skip("no sessions currently listed")

    unknown = [s for s in result.slots if s.availability.value == "unknown"]
    assert not unknown, f"unrecognised availability text on {len(unknown)} row(s)"
