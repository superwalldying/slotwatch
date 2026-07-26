from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from slotwatch.site import Site, Tab

FIXTURES = Path(__file__).parent / "fixtures"

# The day the base fixture was captured. Pinning it keeps year-inference assertions
# stable forever instead of rotting as the real clock moves on.
CAPTURE_DAY = dt.date(2026, 7, 26)

# Sun 08/02, Intermediate - Court 1, 4:00 pm - 7:30 pm
TARGET_GAME_ID = "16212"

# A throwaway site profile. Uses the reserved .invalid TLD so a bug that escapes the
# HTTP mocks fails to resolve instead of reaching a real host.
SITE = Site(
    ajax_url="https://booking.invalid/wp-admin/admin-ajax.php",
    action="test_tab_content_action",
    book_url="https://booking.invalid/?page_id=0",
    referer="https://booking.invalid/?page_id=0",
    tabs={
        "primary": Tab("primary", "Sunday sessions", buttonid=5, filterid=18),
        "secondary": Tab("secondary", "Friday sessions", buttonid=4, filterid=6),
    },
    user_agent="slotwatch-test/0.1",
)


@pytest.fixture
def site() -> Site:
    return SITE


@pytest.fixture
def load():
    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")

    return _load
