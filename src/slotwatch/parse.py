"""Turn the site's booking table into Slots. Pure: HTML string in, ParseResult out.

Design rules earned from inspecting the real markup:

* Locate the table by its *headers*, never by position - the full page carries an
  unrelated waiver table too.
* Map column name -> index from the header row rather than trusting fixed offsets,
  so an inserted column can't silently shift every field.
* Skip rows we can't fully trust instead of emitting a Slot with shifted data.
* A recognised "no sessions" table is zero slots, NOT a failure. Conflating those two
  is the one bug that would make the bot fail silently forever.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

from .models import Availability, ParseResult, Slot
from .site import DEFAULT_EMPTY_MARKER, DEFAULT_FIELD_NAME

# Header label (normalised, casefolded) -> canonical column key.
HEADER_KEYS = {
    "select": "select",
    "date": "date",
    "gym": "gym",
    "level": "level",
    "time": "time",
    "fee": "fee",
    "fee ($)": "fee",
    "available": "available",
}
CANONICAL_ORDER = ("select", "date", "gym", "level", "time", "fee", "available")
REQUIRED_COLUMNS = ("date", "gym", "level", "time", "fee", "available")

_SPACES_RE = re.compile(r"^(\d+)\s+spaces?$")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")

# "4:00 pm - 7:30 pm", and the near-misses worth tolerating: en/em dashes, "to", a
# missing meridiem on the opening half ("9:00 - 12:00 pm"), and "a.m."-style periods.
_TIME_RANGE_RE = re.compile(
    r"(?P<sh>\d{1,2})(?::(?P<sm>\d{2}))?\s*(?:(?P<sap>[ap])\.?\s*m\.?)?"
    r"\s*(?:-|–|—|to)\s*"
    r"(?P<eh>\d{1,2})(?::(?P<em>\d{2}))?\s*(?P<eap>[ap])\.?\s*m\.?",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Collapse the page's &nbsp;-padded, wildly-indented cell text."""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def parse_availability(text: str) -> tuple[Availability, int | None]:
    """Map the Available column to (availability, spaces_left).

    Vocabulary confirmed against real responses: "Sold Out" | "Yes" | "N Space(s)".
    Anything else is UNKNOWN, which is never bookable - we refuse to ping on a
    string we don't understand.
    """
    value = normalize(text).casefold()
    if value == "sold out":
        return Availability.SOLD_OUT, 0
    if value == "yes":
        return Availability.OPEN, None
    if match := _SPACES_RE.match(value):
        return Availability.LIMITED, int(match.group(1))
    return Availability.UNKNOWN, None


def _clock(hour: str, minute: str | None, meridiem: str | None) -> dt.time | None:
    value, minutes = int(hour), int(minute or 0)
    if not 1 <= value <= 12 or minutes > 59:
        return None
    half = (meridiem or "").casefold()
    if half == "a":
        value = 0 if value == 12 else value
    elif half == "p":
        value = 12 if value == 12 else value + 12
    else:
        return None
    return dt.time(value, minutes)


def parse_time_range(text: str) -> tuple[dt.time | None, dt.time | None]:
    """Split "4:00 pm - 7:30 pm" into its two ends.

    Used only to tell whether a session is already over, so anything unreadable yields
    (None, None) and the slot is treated as still live. That direction is deliberate:
    suppressing an alert means silence, and silence hiding a real opening is the exact
    failure this bot exists to prevent. See models.Slot.ends_at.
    """
    match = _TIME_RANGE_RE.search(normalize(text))
    if not match:
        return None, None

    closing = match.group("eap")
    end = _clock(match.group("eh"), match.group("em"), closing)
    opening = match.group("sap")
    if opening:
        return _clock(match.group("sh"), match.group("sm"), opening), end

    # "9:00 - 12:00 pm": an unqualified opening half borrows the closing meridiem, but
    # only if that reads forwards. Borrowing blindly turns 9am-12pm into 9pm-12pm, an
    # inverted range that ends_at would then mistake for an overnight block.
    start = _clock(match.group("sh"), match.group("sm"), closing)
    if start is not None and end is not None and start >= end:
        flipped = _clock(match.group("sh"), match.group("sm"),
                         "a" if closing.casefold() == "p" else "p")
        if flipped is not None and flipped < end:
            start = flipped
    return start, end


def infer_year(month: int, day: int, today: dt.date) -> int:
    """The table never states a year, so pick the nearest plausible one.

    "Nearest to today" handles both rollover directions for free: a 12/28 listing
    seen on Jan 5 is last year, a 01/04 listing seen on Dec 28 is next year.
    """
    best: tuple[int, int] | None = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            continue  # e.g. Feb 29 in a non-leap year
        distance = abs((candidate - today).days)
        if best is None or distance < best[0]:
            best = (distance, year)
    if best is None:
        raise ValueError(f"no valid date for {month:02d}/{day:02d}")
    return best[1]


def _best_table(soup: BeautifulSoup):
    """Pick the table whose header row looks most like the open-play schedule."""
    best = None
    best_score = 0
    for table in soup.find_all("table"):
        header = _header_row(table)
        if header is None:
            continue
        score = sum(
            1
            for cell in header.find_all("th")
            if normalize(cell.get_text()).casefold() in HEADER_KEYS
        )
        if score > best_score:
            best, best_score = table, score
    # Four recognised headers is comfortably more than any incidental table shows.
    return (best, best_score) if best_score >= 4 else (None, best_score)


def _header_row(table):
    for row in table.find_all("tr"):
        if len(row.find_all("th")) >= 2:
            return row
    return None


def _column_map(header_row) -> tuple[dict[str, int], list[str]]:
    """Build {column key: index}, falling back to canonical position when a header
    is unrecognised (renamed), and reporting that as an anomaly."""
    anomalies: list[str] = []
    columns: dict[str, int] = {}
    cells = header_row.find_all("th")

    for index, cell in enumerate(cells):
        label = normalize(cell.get_text()).casefold()
        key = HEADER_KEYS.get(label)
        if key is None:
            anomalies.append(f"unrecognised column header {label!r} at index {index}")
        elif key not in columns:
            columns[key] = index

    for key in REQUIRED_COLUMNS:
        if key in columns:
            continue
        fallback = CANONICAL_ORDER.index(key)
        if fallback < len(cells):
            columns[key] = fallback
            anomalies.append(f"column {key!r} missing; falling back to index {fallback}")
        else:
            anomalies.append(f"column {key!r} missing and no positional fallback")
    return columns, anomalies


def _parse_fee(text: str) -> Decimal | None:
    cleaned = normalize(text).lstrip("$").replace(",", "")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_table(
    html: str,
    *,
    today: dt.date,
    tab: str = "",
    field_name: str = DEFAULT_FIELD_NAME,
    empty_marker: str = DEFAULT_EMPTY_MARKER,
) -> ParseResult:
    if not html or not html.strip():
        return ParseResult(anomalies=("empty response body",))

    soup = BeautifulSoup(html, "lxml")
    table, score = _best_table(soup)
    if table is None:
        return ParseResult(
            anomalies=(
                f"no table with recognisable open-play headers (best match had {score})",
            )
        )

    header_row = _header_row(table)
    columns, anomalies = _column_map(header_row)

    rows = [
        tr for tr in table.find_all("tr")
        if tr.find("input", attrs={"name": field_name})
    ]

    if not rows:
        if empty_marker in normalize(table.get_text()).casefold():
            return ParseResult(is_empty_state=True, anomalies=tuple(anomalies))
        anomalies.append("table found but it has neither slot rows nor the empty-state notice")
        return ParseResult(anomalies=tuple(anomalies))

    needed = max(columns[key] for key in REQUIRED_COLUMNS if key in columns)
    slots: list[Slot] = []

    for row in rows:
        radio = row.find("input", attrs={"name": field_name})
        game_id = str(radio.get("value", "")).split("#", 1)[0].strip()
        if not game_id.isdigit():
            anomalies.append(f"row with unusable radio value {radio.get('value')!r}")
            continue

        cells = row.find_all("td")
        if len(cells) <= needed:
            # Trusting positions here would mis-assign every field after the gap.
            anomalies.append(
                f"row {game_id} has {len(cells)} cells, need > {needed}; skipped"
            )
            continue

        def cell(key: str) -> str:
            return normalize(cells[columns[key]].get_text())

        date_raw = cell("date")
        date: dt.date | None = None
        if match := _MONTH_DAY_RE.search(date_raw):
            month, day = int(match.group(1)), int(match.group(2))
            try:
                date = dt.date(infer_year(month, day, today), month, day)
            except ValueError:
                anomalies.append(f"row {game_id} has invalid date {date_raw!r}")
        else:
            anomalies.append(f"row {game_id} has unparseable date {date_raw!r}")

        time_raw = cell("time")
        start_time, end_time = parse_time_range(time_raw)
        if end_time is None:
            # Flagged like an unparseable date or fee: the bot can still alert on this
            # row, it just can no longer tell when the session is over.
            anomalies.append(f"row {game_id} has unparseable time {time_raw!r}")

        fee = _parse_fee(cell("fee"))
        if fee is None:
            anomalies.append(f"row {game_id} has unparseable fee {cell('fee')!r}")

        availability, spaces_left = parse_availability(cell("available"))
        if availability is Availability.UNKNOWN:
            anomalies.append(
                f"row {game_id} has unknown availability {cell('available')!r}"
            )

        disabled = radio.has_attr("disabled")
        if availability is not Availability.UNKNOWN and availability.is_bookable == disabled:
            anomalies.append(
                f"row {game_id}: availability {cell('available')!r} disagrees with "
                f"radio disabled={disabled}"
            )

        slots.append(
            Slot(
                game_id=game_id,
                date_raw=date_raw,
                date=date,
                gym=cell("gym"),
                level=cell("level"),
                time_raw=time_raw,
                fee=fee,
                availability=availability,
                spaces_left=spaces_left,
                radio_disabled=disabled,
                tab=tab,
                start_time=start_time,
                end_time=end_time,
            )
        )

    return ParseResult(slots=tuple(slots), anomalies=tuple(anomalies))
