"""Entrypoint: wire the pure core to the two network edges.

Target details (domain, endpoint, tab ids, venue labels) come from the runtime site
profile - see site.py and site.example.yaml. Nothing identifying is committed.

Two modes matter:

  --once   one poll, then exit. Cheap, good for private-repo Actions and debugging.
  --loop   poll on `interval` until the window closes or --max-runtime expires.

--loop exists because GitHub Actions cron cannot deliver 3-minute resolution: schedules
are throttled, drift 5-15 minutes routinely, and get dropped under load. Letting cron
merely *start* a job that then paces itself converts unreliable scheduling into a
reliable cadence.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import random
import sys
import time
from pathlib import Path

from .config import (
    Config,
    describe_schedule,
    interval_at,
    load_config,
    local_now,
    next_open,
)
from .diff import diff, suppress_recent
from .fetch import FetchError, build_session, fetch_tab
from .format import build_payload, build_test_ping, classify_mention
from .models import Event, EventType, ParseResult
from .notify import NotifyError, send
from .parse import parse_table
from .rules import filter_events
from .site import Site, SiteConfigError, load_site
from .state import State, load, mark_notified, record, record_failure, save

log = logging.getLogger("slotwatch")

DEFAULT_CONFIG = Path("rules.yaml")
DEFAULT_STATE = Path("state/seen.json")


def collect(
    config: Config,
    site: Site,
    *,
    today: dt.date,
    session=None,
    fixture: str | None = None,
) -> ParseResult:
    """Read every configured tab and fold them into one observation.

    Aggregating before diffing matters: state.slots is keyed by game_id across all
    tabs, so recording one tab at a time would erase the others.
    """
    def parse(html: str, tab: str = "") -> ParseResult:
        return parse_table(
            html,
            today=today,
            tab=tab,
            field_name=site.field_name,
            empty_marker=site.empty_marker,
        )

    if fixture is not None:
        # A fixture stands in for the first configured tab.
        return parse(fixture, tab=config.poll.tabs[0] if config.poll.tabs else "")

    slots: tuple = ()
    anomalies: tuple = ()
    empties = 0

    for key in config.poll.tabs:
        html = fetch_tab(site.tab(key), site, session=session)
        result = parse(html, tab=key)
        slots += result.slots
        anomalies += tuple(f"[{key}] {a}" for a in result.anomalies)
        empties += int(result.is_empty_state)

    return ParseResult(
        slots=slots,
        # Only "everything is empty" counts as the empty state.
        is_empty_state=empties == len(config.poll.tabs) and not slots,
        anomalies=anomalies,
    )


def poll_once(
    config: Config,
    state: State,
    site: Site,
    *,
    now: dt.datetime,
    session=None,
    fixture: str | None = None,
    dry_run: bool = False,
    webhook_url: str | None = None,
    mention: str | None = None,
) -> tuple[State, list[Event]]:
    today = local_now(config, now).date()

    try:
        result = collect(config, site, today=today, session=session, fixture=fixture)
    except FetchError as exc:
        state = record_failure(state)
        log.warning("fetch failed (streak %d): %s", state.failure_streak, exc)
        if state.failure_streak < config.poll.failure_threshold:
            return state, []
        events = [
            Event(
                type=EventType.HEALTH,
                message=f"{state.failure_streak} consecutive fetch failures: {exc}",
            )
        ]
        state, sent, _ = _deliver(config, state, events, site, now=now, dry_run=dry_run,
                                  webhook_url=webhook_url, mention=mention)
        return state, sent

    log.info(
        "parsed %d slots (empty_state=%s, anomalies=%d)",
        len(result.slots), result.is_empty_state, len(result.anomalies),
    )

    events = diff(state, result, now=now, failure_threshold=config.poll.failure_threshold)
    matched = filter_events(events, config.rules)

    state, sent, delivery_failed = _deliver(
        config, state, matched, site, now=now, dry_run=dry_run,
        webhook_url=webhook_url, mention=mention,
    )

    if delivery_failed:
        # Deliberately skip record(). Advancing state here would forget the very
        # transition we failed to announce, losing the alert permanently instead of
        # retrying it on the next poll.
        log.warning("not recording this observation so the alert is retried next poll")
        return state, sent

    # Record last, so the diff above compared against the previous world.
    return record(state, result, now=now), sent


def _deliver(
    config: Config,
    state: State,
    events: list[Event],
    site: Site,
    *,
    now: dt.datetime,
    dry_run: bool,
    webhook_url: str | None,
    mention: str | None,
) -> tuple[State, list[Event], bool]:
    """Returns (state, actually-sent events, delivery_failed)."""
    to_send = suppress_recent(
        events,
        state,
        now=now,
        cooldown=config.poll.cooldown,
        health_cooldown=config.poll.health_cooldown,
    )
    if not to_send:
        return state, [], False

    label = (
        site.tab(config.poll.tabs[0]).label
        if len(config.poll.tabs) == 1
        else "watched sessions"
    )
    payload = build_payload(
        to_send, mention=mention, tab_label=label, book_url=site.book_url,
        tab_display=site.tab_display(),
        username=config.notify.username, avatar_url=config.notify.avatar_url,
    )

    for event in to_send:
        target = event.slot.label if event.slot else (event.message or "")
        log.info("EVENT %s: %s", event.type.value, target)

    if dry_run:
        log.info("dry-run: would post %s", payload)
        # Not marked as notified, so repeated dry runs stay informative.
        return state, to_send, False

    try:
        send(payload, webhook_url or "")
    except NotifyError as exc:
        log.error("could not notify: %s", exc)
        return state, [], True

    return mark_notified(state, to_send, now=now), to_send, False


def run_test_ping(
    config: Config,
    site: Site,
    *,
    webhook_url: str | None,
    mention: str | None,
    dry_run: bool,
    fixture: str | None = None,
    revision: str | None = None,
) -> int:
    """Fetch once and report what the bot can see. Never touches state.

    Runs on every code update so a broken secret, an invalid site profile or drifted
    markup surfaces immediately - rather than as weeks of silence that looks exactly
    like "nothing has opened up".
    """
    now = dt.datetime.now(dt.UTC)
    today = local_now(config, now).date()
    label = (
        site.tab(config.poll.tabs[0]).label
        if len(config.poll.tabs) == 1
        else "watched sessions"
    )

    session = None if fixture else build_session(site)
    error: str | None = None
    result = ParseResult()
    try:
        result = collect(config, site, today=today, session=session, fixture=fixture)
    except FetchError as exc:
        error = str(exc)
    finally:
        if session:
            session.close()

    bookable = [s for s in result.slots if s.availability.is_bookable]
    # Synthesise events for currently-bookable slots to show what the rules would catch.
    watched = filter_events(
        [Event(type=EventType.NEW_SLOT, slot=s) for s in bookable], config.rules
    )

    payload = build_test_ping(
        tab_label=label,
        slots=len(result.slots),
        bookable=len(bookable),
        watched=len(watched),
        anomalies=result.anomalies,
        rules=[r.name for r in config.rules if r.enabled],
        schedule=describe_schedule(config),
        revision=revision,
        error=error,
        mention=mention,
        username=config.notify.username,
        avatar_url=config.notify.avatar_url,
    )

    log.info("mention form: %s", classify_mention(mention))
    log.info(
        "deploy check: %d slots, %d bookable, %d watched, %d anomalies%s",
        len(result.slots), len(bookable), len(watched), len(result.anomalies),
        f", error={error}" if error else "",
    )

    if dry_run:
        log.info("dry-run: would post %s", payload)
    else:
        try:
            send(payload, webhook_url or "")
        except NotifyError as exc:
            log.error("deploy check could not notify: %s", exc)
            return 1

    # Red build on a real problem, so a failed check is impossible to miss.
    return 0 if error is None and not result.anomalies else 1


def _sleep_for(config: Config, interval: dt.timedelta) -> float:
    return interval.total_seconds() + random.uniform(0, config.poll.jitter_seconds)


def run(args: argparse.Namespace) -> int:
    try:
        site = load_site(args.site)
    except SiteConfigError as exc:
        log.error("%s", exc)
        return 3

    config = load_config(args.config, known_tabs=set(site.tabs))
    state = load(args.state)
    fixture = Path(args.fixture).read_text(encoding="utf-8", errors="replace") if args.fixture else None

    webhook_url = os.environ.get(config.notify.webhook_url_env)
    mention = config.notify.mention
    if not mention and config.notify.mention_env:
        mention = os.environ.get(config.notify.mention_env)

    if not args.dry_run and not webhook_url:
        log.error(
            "%s is not set; refusing to run. Use --dry-run to test without Discord.",
            config.notify.webhook_url_env,
        )
        return 2

    if args.test_ping:
        return run_test_ping(
            config, site, webhook_url=webhook_url, mention=mention,
            dry_run=args.dry_run, fixture=fixture,
            revision=os.environ.get("GITHUB_SHA"),
        )

    if state.is_cold:
        log.info("cold start: this poll seeds state and will not notify")

    session = None if fixture else build_session(site)
    deadline = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=args.max_runtime)
    total = 0

    try:
        while True:
            now = dt.datetime.now(dt.UTC)
            interval = interval_at(config, now)

            if interval is None and not args.ignore_window:
                if args.once:
                    log.info("outside the polling window; nothing to do")
                    return 0
                if now >= deadline:
                    break
                upcoming = next_open(config, now)
                if upcoming is None or upcoming >= deadline:
                    # Nothing opens before this job's budget runs out (a weekend, or
                    # after hours). Exiting beats burning runner time on no-op waits.
                    log.info("no polling window opens before this job ends; exiting")
                    break
                wait = (upcoming - now).total_seconds()
                log.info(
                    "outside the polling window; sleeping %.0f min until %s",
                    wait / 60, local_now(config, upcoming).strftime("%a %H:%M %Z"),
                )
                time.sleep(min(wait, (deadline - now).total_seconds()))
                continue

            state, sent = poll_once(
                config, state, site, now=now, session=session, fixture=fixture,
                dry_run=args.dry_run, webhook_url=webhook_url, mention=mention,
            )
            total += len(sent)
            save(args.state, state)

            if args.once:
                return 0

            now = dt.datetime.now(dt.UTC)
            if now >= deadline:
                break
            nap = min(_sleep_for(config, interval or dt.timedelta(minutes=10)),
                      (deadline - now).total_seconds())
            time.sleep(max(nap, 0))
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        if session:
            session.close()

    log.info("finished; %d notification(s) sent", total)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slotwatch", description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, type=Path)
    parser.add_argument("--state", default=DEFAULT_STATE, type=Path)
    parser.add_argument(
        "--site", default=None, type=Path,
        help="site profile YAML (default: $SITE_PROFILE, else ./site.yaml)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="single poll, then exit")
    mode.add_argument("--loop", action="store_true", help="poll until the window closes")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="never post to Discord; state is still saved so seeding works",
    )
    parser.add_argument("--fixture", help="parse a local HTML file instead of fetching")
    parser.add_argument(
        "--max-runtime", type=float, default=120,
        help="minutes before --loop exits (Actions jobs cap at 6h)",
    )
    parser.add_argument(
        "--test-ping", action="store_true",
        help="fetch once and post a deploy-check summary to Discord; ignores state",
    )
    parser.add_argument(
        "--ignore-window", action="store_true",
        help="poll regardless of the configured time windows",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.once and not args.loop:
        args.once = True
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
