"""Render events into a single Discord webhook payload.

Batching is the point: a new date lands ~6 rows simultaneously, and six separate pings
would train you to ignore the bot.

The booking URL and tab label are injected from the runtime site profile, so no target
details live in this file.
"""

from __future__ import annotations

import json

from .models import Availability, Event, EventType

# Placeholders only. Real values arrive from the site profile at call time. The page
# cannot deep-link a tab (tab switching is JS-driven), so every message names the tab to
# click rather than pretending the link lands there.
BOOK_URL = ""
TAB_LABEL = "sessions"

# Discord caps the webhook display-name override at 80 characters.
USERNAME_LIMIT = 80

COLOR_OPENING = 0x2ECC71  # green
COLOR_HEALTH = 0xE67E22  # orange
COLOR_TEST = 0x3498DB  # blue - visually distinct so a deploy check is never mistaken
COLOR_TEST_FAIL = 0xE74C3C  # red

# Discord's documented hard limits.
CONTENT_LIMIT = 2000
DESCRIPTION_LIMIT = 4096
TITLE_LIMIT = 256
FOOTER_LIMIT = 2048
TOTAL_LIMIT = 6000
MAX_EMBEDS = 10

# Leave room for the truncation notice and the tab hint.
_DESCRIPTION_BUDGET = 3500


def _apply_identity(
    payload: dict, username: str | None, avatar_url: str | None
) -> dict:
    """Override the webhook's own display name/avatar for this message.

    Without this, Discord shows whatever the webhook was created as - which is often a
    leftover name from some unrelated integration, and is confusing if the webhook is
    shared. Setting it per-message means the alert is always self-identifying.
    """
    if username:
        payload["username"] = username[:USERNAME_LIMIT]
    if avatar_url:
        payload["avatar_url"] = avatar_url
    return payload


def payload_size(payload: dict) -> int:
    """Approximate Discord's combined-character accounting across embeds."""
    total = len(payload.get("content", "") or "")
    for embed in payload.get("embeds", []):
        total += len(embed.get("title", "") or "")
        total += len(embed.get("description", "") or "")
        total += len(((embed.get("footer") or {}).get("text", "")) or "")
    return total


def describe_availability(slot) -> str:
    if slot.availability is Availability.LIMITED and slot.spaces_left is not None:
        unit = "Space" if slot.spaces_left == 1 else "Spaces"
        return f"{slot.spaces_left} {unit}"
    if slot.availability is Availability.OPEN:
        return "open"
    if slot.availability is Availability.SOLD_OUT:
        return "Sold Out"
    return "unknown"


def _fee(slot) -> str:
    return f"${slot.fee}" if slot.fee is not None else "$?"


def _slot_line(event: Event) -> str:
    slot = event.slot
    head = f"**{slot.date_raw}** · {slot.level} · {slot.time_raw} · {_fee(slot)}"
    now = describe_availability(slot)
    if event.type is EventType.REOPENED and event.previous is not None:
        tail = f"↳ was {_previous_text(event.previous)} → now **{now}**"
    else:
        tail = f"↳ newly listed → **{now}**"
    return f"{head}\n{tail}"


def _previous_text(previous: Availability) -> str:
    return "Sold Out" if previous is Availability.SOLD_OUT else str(previous)


def _join_within_budget(lines: list[str]) -> str:
    """Fit as many whole entries as possible, then say what was left out."""
    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        addition = len(line) + 2
        remaining = len(lines) - index
        if used + addition > _DESCRIPTION_BUDGET:
            kept.append(f"_…and {remaining} more_")
            break
        kept.append(line)
        used += addition
    return "\n\n".join(kept)


def _slot_embed(
    events: list[Event], tab_label: str = TAB_LABEL, book_url: str = BOOK_URL
) -> dict:
    reopened = sum(1 for e in events if e.type is EventType.REOPENED)
    title = ("🔔 Slot opened — " if reopened else "🔔 New slots — ") + tab_label

    description = _join_within_budget([_slot_line(e) for e in events])
    if book_url:
        description += f"\n\n[Book now]({book_url}) — then click the **{tab_label}** tab."
    else:
        description += f"\n\nOpen the booking page, then click the **{tab_label}** tab."

    rules = sorted({e.rule_name for e in events if e.rule_name})
    footer = "matched: " + ", ".join(rules) if rules else "no rule recorded"

    embed = {
        "title": title[:TITLE_LIMIT],
        "color": COLOR_OPENING,
        "description": description[:DESCRIPTION_LIMIT],
        "footer": {"text": footer[:FOOTER_LIMIT]},
    }
    if book_url:
        embed["url"] = book_url
    return embed


def _health_embed(events: list[Event]) -> dict:
    lines = [e.message or "unspecified problem" for e in events]
    description = (
        _join_within_budget(lines)
        + "\n\nThe scraper may need attention — silence might not mean "
        "\"no slots\" until this is resolved."
    )
    return {
        "title": "⚠️ Slot watcher health",
        "color": COLOR_HEALTH,
        "description": description[:DESCRIPTION_LIMIT],
    }


def build_payload(
    events: list[Event],
    *,
    mention: str | None = None,
    tab_label: str = TAB_LABEL,
    book_url: str = BOOK_URL,
    username: str | None = None,
    avatar_url: str | None = None,
) -> dict | None:
    """One payload for the whole batch, or None when there is nothing to say."""
    if not events:
        return None

    slot_events = [e for e in events if e.type is not EventType.HEALTH and e.slot]
    health_events = [e for e in events if e.type is EventType.HEALTH]

    embeds: list[dict] = []
    if slot_events:
        embeds.append(_slot_embed(slot_events, tab_label, book_url))
    if health_events:
        embeds.append(_health_embed(health_events))

    payload: dict = {"embeds": embeds[:MAX_EMBEDS]}

    summary = _summary(slot_events, health_events)
    content = f"{mention} {summary}".strip() if mention else summary
    payload["content"] = content[:CONTENT_LIMIT]
    if mention:
        payload["allowed_mentions"] = {"parse": ["users"]}

    return _apply_identity(payload, username, avatar_url)


def _summary(slot_events: list[Event], health_events: list[Event]) -> str:
    parts = []
    reopened = sum(1 for e in slot_events if e.type is EventType.REOPENED)
    new = sum(1 for e in slot_events if e.type is EventType.NEW_SLOT)
    if reopened:
        parts.append(f"{reopened} slot{'s' if reopened != 1 else ''} reopened")
    if new:
        parts.append(f"{new} new slot{'s' if new != 1 else ''}")
    if health_events:
        parts.append("scraper health warning")
    return " · ".join(parts)


def build_test_ping(
    *,
    tab_label: str,
    slots: int,
    bookable: int,
    watched: int,
    anomalies: tuple[str, ...] = (),
    rules: list[str] | None = None,
    schedule: str = "",
    revision: str | None = None,
    error: str | None = None,
    mention: str | None = None,
    username: str | None = None,
    avatar_url: str | None = None,
) -> dict:
    """Deploy-check message: proves secrets, site profile, parsing and Discord all work.

    Sent on every code update. Styled unmistakably differently from a real slot alert -
    a deploy check that looked like an opening would be worse than no check at all.
    """
    healthy = error is None and not anomalies

    lines = []
    if error:
        lines.append(f"**Could not read the page:** {error}")
    else:
        lines.append(
            f"Read **{slots}** slot(s) from **{tab_label}** — "
            f"{bookable} bookable, {slots - bookable} full."
        )
        lines.append(
            f"Matching your rules right now: **{watched}** bookable slot(s)."
            + ("" if watched else " (expected — they're usually all full.)")
        )

    if anomalies:
        shown = "; ".join(anomalies[:3])
        extra = f" (+{len(anomalies) - 3} more)" if len(anomalies) > 3 else ""
        lines.append(f"**Parser anomalies:** {shown}{extra}")
    elif not error:
        lines.append("Parser anomalies: none.")

    if rules:
        lines.append("Active rules: " + " · ".join(rules))
    else:
        lines.append("**No active rules** — nothing would ever ping you.")

    if schedule:
        lines.append(f"Polling: {schedule}")

    status = "passed" if healthy else "FAILED"
    embed = {
        "title": f"🧪 Deploy check {status}",
        "color": COLOR_TEST if healthy else COLOR_TEST_FAIL,
        "description": "\n".join(lines)[:DESCRIPTION_LIMIT],
    }
    if revision:
        embed["footer"] = {"text": f"revision {revision[:12]}"[:FOOTER_LIMIT]}

    payload: dict = {"embeds": [embed]}
    summary = (
        "slotwatch deploy check passed — alerts are working"
        if healthy
        else "slotwatch deploy check FAILED — alerts may not reach you"
    )
    content = f"{mention} {summary}".strip() if mention else summary
    payload["content"] = content[:CONTENT_LIMIT]
    if mention:
        payload["allowed_mentions"] = {"parse": ["users"]}
    return _apply_identity(payload, username, avatar_url)


def to_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)
