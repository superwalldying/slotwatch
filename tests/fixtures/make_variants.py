"""Derive test-fixture variants from the captured base response.

The three base fixtures are real captures with proper nouns and contact details
anonymised; their structure is byte-faithful to the live page. Do not hand-edit them:

  primary_live.html      captured AJAX fragment, 24 rows, mixed availability
  primary_empty.html     captured full page showing the "no sessions" notice
  archived_110_rows.html larger capture, 110 rows

The state *transitions* we need to test cannot be captured from the live site - you would
wait days for a real cancellation - so they are derived here instead. Regenerate with:

    python tests/fixtures/make_variants.py

`html.parser` is used deliberately: lxml wraps a fragment in <html><body>, which would
make the derived fixtures structurally unlike a real AJAX response.
"""

from __future__ import annotations

import copy
from pathlib import Path

from bs4 import BeautifulSoup

HERE = Path(__file__).parent
BASE = HERE / "primary_live.html"

# The originally requested slot: 08/02, Intermediate - Court 1, 4:00 pm - 7:30 pm.
TARGET_GAME_ID = "16212"


def _soup(path: Path) -> BeautifulSoup:
    return BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")


def _slot_rows(soup: BeautifulSoup) -> list:
    return [tr for tr in soup.find_all("tr") if tr.find("input", attrs={"name": "f_GameID"})]


def _game_id(tr) -> str:
    return tr.find("input", attrs={"name": "f_GameID"})["value"].split("#", 1)[0]


def _set_availability(tr, text: str, *, bookable: bool) -> None:
    """Set the Available cell and keep the radio's disabled state consistent with it."""
    tr.find_all("td")[6].string = f"\xa0{text}\xa0"
    radio = tr.find("input", attrs={"name": "f_GameID"})
    if bookable:
        del radio["disabled"]
    else:
        radio["disabled"] = "disabled"


def make_reopened() -> str:
    """The core signal: a sold-out slot frees up from a cancellation."""
    soup = _soup(BASE)
    for tr in _slot_rows(soup):
        if _game_id(tr) == TARGET_GAME_ID:
            _set_availability(tr, "2 Spaces", bookable=True)
            break
    else:  # pragma: no cover - guards against a re-capture changing ids
        raise SystemExit(f"game_id {TARGET_GAME_ID} not found in {BASE.name}")
    return str(soup)


def make_new_date() -> str:
    """A new date enters the rolling window, bookable, with fresh game_ids."""
    soup = _soup(BASE)
    rows = _slot_rows(soup)
    last_date = rows[-1].find_all("td")[1].get_text().strip()
    block = [tr for tr in rows if tr.find_all("td")[1].get_text().strip() == last_date]

    anchor = rows[-1]
    for offset, tr in enumerate(block):
        new = copy.copy(tr)
        new_id = str(16270 + offset)
        radio = new.find("input", attrs={"name": "f_GameID"})
        _, mid, fee = radio["value"].split("#")
        radio["value"] = f"{new_id}#{mid}#{fee}"
        radio["id"] = f"radio-{new_id}0"
        new.find_all("td")[1].string = "\xa0 Sun 08/23\xa0"
        _set_availability(new, "Yes", bookable=True)
        anchor.insert_after(new)
        anchor = new
    return str(soup)


def make_layout_changed() -> str:
    """Site markup drifts: a renamed header and a row missing a cell.

    Must surface as a HEALTH anomaly, never as a silently-empty slot list.
    """
    soup = _soup(BASE)
    for th in soup.find_all("th"):
        if th.get_text().strip() == "Level":
            th.string = "Skill"
            break
    _slot_rows(soup)[0].find_all("td")[3].decompose()
    return str(soup)


VARIANTS = {
    "primary_reopened.html": make_reopened,
    "primary_new_date.html": make_new_date,
    "layout_changed.html": make_layout_changed,
}


def main() -> None:
    for name, build in VARIANTS.items():
        (HERE / name).write_text(build(), encoding="utf-8")
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
